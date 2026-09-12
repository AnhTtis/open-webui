"""Durable workers for memory extraction and rebuildable vector state."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from open_webui.config import RAG_EMBEDDING_CONTENT_PREFIX
from open_webui.models.memories import Memories, MemoryJobModel
from open_webui.models.users import Users
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.utils.memory import memory_vector_text
from starlette.datastructures import Headers
from starlette.requests import Request

log = logging.getLogger(__name__)

MEMORY_JOB_BATCH_SIZE = max(1, min(int(os.getenv('MEMORY_JOB_BATCH_SIZE', '4')), 20))
MEMORY_JOB_LEASE_SECONDS = max(30, int(os.getenv('MEMORY_JOB_LEASE_SECONDS', '180')))


def _worker_id(app) -> str:
    instance_id = getattr(app.state, 'instance_id', None) or socket.gethostname()
    return f'{instance_id}:{os.getpid()}'


def _build_request(app) -> Request:
    return Request(
        {
            'type': 'http',
            'asgi.version': '3.0',
            'asgi.spec_version': '2.0',
            'method': 'POST',
            'path': '/internal/memory-job',
            'query_string': b'',
            'headers': Headers({}).raw,
            'client': ('127.0.0.1', 0),
            'server': ('127.0.0.1', 0),
            'scheme': 'http',
            'app': app,
        }
    )


async def _upsert_embedding(app, job: MemoryJobModel, user) -> None:
    memory = await Memories.get_memory_by_id(job.memory_id) if job.memory_id else None
    if not memory:
        return

    requested_revision = (job.payload or {}).get('revision')
    if requested_revision is not None and requested_revision != memory.current_revision:
        return
    if memory.status not in {'active', 'candidate'}:
        await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=f'user-memory-{job.user_id}', ids=[memory.id])
        return

    text = memory_vector_text(memory.content, memory.path)
    vector = await app.state.EMBEDDING_FUNCTION(text, prefix=RAG_EMBEDDING_CONTENT_PREFIX, user=user)
    await ASYNC_VECTOR_DB_CLIENT.upsert(
        collection_name=f'user-memory-{job.user_id}',
        items=[
            {
                'id': memory.id,
                'text': text,
                'vector': vector,
                'metadata': {
                    'created_at': memory.created_at,
                    'updated_at': memory.updated_at,
                    'type': memory.type,
                    'path': memory.path,
                    'status': memory.status,
                    'version': memory.version,
                    'revision': memory.current_revision,
                },
            }
        ],
    )


async def _delete_embedding(job: MemoryJobModel) -> None:
    memory = await Memories.get_memory_by_id(job.memory_id) if job.memory_id else None
    requested_revision = (job.payload or {}).get('revision')
    if memory and requested_revision is not None and requested_revision != memory.current_revision:
        return
    if memory and memory.status != 'deleted':
        return
    if job.memory_id:
        await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=f'user-memory-{job.user_id}', ids=[job.memory_id])


async def _extract_turns(app, job: MemoryJobModel, user) -> None:
    from open_webui.routers.memories import UpdateMemoriesForm
    from open_webui.utils.memory import _generate_memory_operations, validate_memory_operations

    payload = job.payload or {}
    memories = await Memories.get_memories_by_user_id(job.user_id)
    existing_lines = [
        f'- id={memory.id} type={memory.type} path={memory.path or ""} content={memory.content}'
        for memory in memories[:250]
    ]
    transcript_lines = []
    for message in payload.get('messages') or []:
        role = message.get('role')
        content = message.get('content')
        if role not in {'user', 'assistant'} or not isinstance(content, str) or not content.strip():
            continue
        transcript_lines.append(f'{role}: {content.strip()}')

    operations = await _generate_memory_operations(
        request=_build_request(app),
        user=user,
        model_id=payload.get('model_id'),
        metadata={
            'chat_id': payload.get('chat_id'),
            'message_id': payload.get('message_id'),
            'extractor_version': payload.get('extractor_version', 'durable-v1'),
        },
        existing_text='\n'.join(existing_lines) if existing_lines else '(none)',
        transcript='\n\n'.join(transcript_lines),
    )
    if not operations:
        return

    form = UpdateMemoriesForm(operations=operations, source='background_review')
    validated = validate_memory_operations(form)
    for operation in validated:
        operation['meta'] = {
            'created_by': 'background_review',
            'chat_id': payload.get('chat_id'),
            'message_id': payload.get('message_id'),
            'model': payload.get('model_id'),
            'extractor_version': payload.get('extractor_version', 'durable-v1'),
        }
    await Memories.create_proposals(
        job.user_id,
        validated,
        metadata={
            'chat_id': payload.get('chat_id'),
            'message_id': payload.get('message_id'),
            'model': payload.get('model_id'),
        },
    )


async def execute_memory_job(app, job: MemoryJobModel, worker_id: str) -> None:
    try:
        user = await Users.get_user_by_id(job.user_id)
        if not user:
            await Memories.complete_job(job.id, worker_id)
            return

        if job.job_type == 'upsert_embedding':
            await _upsert_embedding(app, job, user)
            await Memories.complete_job(job.id, worker_id, sync_memory=True)
        elif job.job_type == 'delete_embedding':
            await _delete_embedding(job)
            await Memories.complete_job(job.id, worker_id, sync_memory=True)
        elif job.job_type == 'extract_turns':
            await _extract_turns(app, job, user)
            await Memories.complete_job(job.id, worker_id)
        else:
            raise ValueError(f'Unsupported memory job type: {job.job_type}')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception('Memory job %s (%s) failed', job.id, job.job_type)
        await Memories.fail_job(job.id, worker_id, str(exc))


async def claim_and_dispatch_memory_jobs(app) -> int:
    """Claim a bounded job batch; leases make spawned work restart-safe."""
    worker_id = _worker_id(app)
    jobs = await Memories.claim_jobs(
        worker_id,
        limit=MEMORY_JOB_BATCH_SIZE,
        lease_seconds=MEMORY_JOB_LEASE_SECONDS,
    )
    for job in jobs:
        asyncio.create_task(execute_memory_job(app, job, worker_id))
    return len(jobs)
