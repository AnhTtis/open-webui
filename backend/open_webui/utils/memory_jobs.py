"""Durable workers for memory extraction and rebuildable vector state."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket

from open_webui.config import RAG_EMBEDDING_CONTENT_PREFIX, RAG_EMBEDDING_ENGINE, RAG_EMBEDDING_MODEL
from open_webui.env import DATABASE_URL
from open_webui.models.memories import (
    LIVE_MEMORY_STATUSES,
    Memories,
    MemoryJobModel,
    memory_collection_name,
)
from open_webui.models.users import Users
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.utils.memory import memory_vector_text
from open_webui.utils.memory_limits import (
    MEMORY_JOB_BATCH_SIZE,
    MEMORY_JOB_CONCURRENCY,
    MEMORY_JOB_HEARTBEAT_SECONDS,
    MEMORY_JOB_LEASE_SECONDS,
    MEMORY_JOB_TIMEOUT_SECONDS,
    RECALLABLE_MEMORY_STATUSES,
)
from starlette.datastructures import Headers
from starlette.requests import Request

log = logging.getLogger(__name__)

_JOB_SEMAPHORE = asyncio.Semaphore(MEMORY_JOB_CONCURRENCY)
_TASKS: set[asyncio.Task] = set()
_CLAIM_LOCK = asyncio.Lock()
_SQLITE_WORKER_WARNED = False


def _warn_sqlite_multiprocess() -> None:
    global _SQLITE_WORKER_WARNED
    if _SQLITE_WORKER_WARNED:
        return
    dialect = (DATABASE_URL or '').split(':', 1)[0]
    if 'sqlite' not in dialect:
        return
    workers = os.getenv('WEB_CONCURRENCY') or os.getenv('UVICORN_WORKERS') or '1'
    try:
        worker_count = int(workers)
    except (TypeError, ValueError):
        worker_count = 1
    if worker_count > 1:
        log.warning(
            'SQLite durable memory jobs are single-process only; WEB_CONCURRENCY/UVICORN_WORKERS=%s. Use PostgreSQL for multi-worker deployments.',
            workers,
        )
    _SQLITE_WORKER_WARNED = True


def _worker_id(app) -> str:
    instance_id = getattr(app.state, 'instance_id', None) or socket.gethostname()
    return f'{instance_id}:{os.getpid()}'


def embedding_fingerprint(app=None) -> str:
    engine = RAG_EMBEDDING_ENGINE or 'default'
    model = RAG_EMBEDDING_MODEL or 'unknown'
    prefix = RAG_EMBEDDING_CONTENT_PREFIX or ''
    raw = f'{engine}|{model}|{prefix}|schema=mem-v2'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


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


def _job_collection(job: MemoryJobModel) -> str:
    payload = job.payload or {}
    return payload.get('collection_name') or memory_collection_name(job.user_id, payload.get('generation'))


def _job_doc_id(job: MemoryJobModel, memory) -> str:
    payload = job.payload or {}
    if payload.get('vector_doc_id'):
        return payload['vector_doc_id']
    generation = payload.get('generation') or 1
    revision = payload.get('revision')
    if revision is None and memory is not None:
        revision = memory.current_revision
    return f'mem:{generation}:{job.memory_id}:r{revision or 0}'


async def _safe_vector_delete(collection_name: str, ids: list[str] | None = None) -> None:
    try:
        await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=collection_name, ids=ids)
    except Exception as exc:
        log.debug('Vector delete skipped for %s: %s', collection_name, exc)


async def _safe_delete_collection(collection_name: str) -> None:
    try:
        if await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name):
            await ASYNC_VECTOR_DB_CLIENT.delete_collection(collection_name)
    except Exception as exc:
        log.debug('Vector collection delete skipped for %s: %s', collection_name, exc)


async def _load_job_memory(job: MemoryJobModel):
    if not job.memory_id:
        return None
    return await Memories.get_memory_by_id_and_user_id(job.memory_id, job.user_id)


async def _upsert_embedding(app, job: MemoryJobModel, user) -> None:
    if await Memories.account_cleanup_pending(job.user_id):
        return
    memory = await _load_job_memory(job)
    if not memory:
        await _safe_vector_delete(_job_collection(job), [_job_doc_id(job, None)])
        return

    payload = job.payload or {}
    requested_revision = payload.get('revision')
    if requested_revision is not None and requested_revision != memory.current_revision:
        await _safe_vector_delete(_job_collection(job), [_job_doc_id(job, memory)])
        return
    if memory.status not in LIVE_MEMORY_STATUSES or memory.status not in RECALLABLE_MEMORY_STATUSES:
        await _safe_vector_delete(_job_collection(job), [_job_doc_id(job, memory)])
        return

    text = memory_vector_text(memory.content, memory.path)
    vector = await app.state.EMBEDDING_FUNCTION(text, prefix=RAG_EMBEDDING_CONTENT_PREFIX, user=user)

    memory = await _load_job_memory(job)
    if not memory:
        await _safe_vector_delete(_job_collection(job), [_job_doc_id(job, None)])
        return
    if requested_revision is not None and requested_revision != memory.current_revision:
        return
    if memory.status not in RECALLABLE_MEMORY_STATUSES:
        await _safe_vector_delete(_job_collection(job), [_job_doc_id(job, memory)])
        return

    await ASYNC_VECTOR_DB_CLIENT.upsert(
        collection_name=_job_collection(job),
        items=[
            {
                'id': _job_doc_id(job, memory),
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
                    'generation': (job.payload or {}).get('generation') or 1,
                    'user_id': job.user_id,
                },
            }
        ],
    )


async def _delete_embedding(job: MemoryJobModel) -> None:
    memory = await _load_job_memory(job)
    requested_revision = (job.payload or {}).get('revision')
    if memory and requested_revision is not None and requested_revision != memory.current_revision:
        return
    if memory and memory.status in RECALLABLE_MEMORY_STATUSES:
        return
    await _safe_vector_delete(_job_collection(job), [_job_doc_id(job, memory)])


async def _extract_turns(app, job: MemoryJobModel, user) -> None:
    from open_webui.routers.memories import UpdateMemoriesForm
    from open_webui.utils.memory import _generate_memory_operations, validate_memory_operations

    payload = job.payload or {}
    memories = await Memories.get_extraction_snapshot(job.user_id)
    existing_lines = [
        f'- id={memory.id} type={memory.type} path={memory.path or ""} content={memory.content}'
        for memory in memories
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
            'idempotency_prefix': f'extract:{job.id}',
        },
    )


async def _cleanup_account(job_or_cleanup) -> None:
    collections = []
    if hasattr(job_or_cleanup, 'collections'):
        collections = list(job_or_cleanup.collections or [])
    else:
        collections = list((job_or_cleanup.payload or {}).get('collections') or [])
    user_id = job_or_cleanup.user_id
    names = set(collections or [memory_collection_name(user_id)])
    names.add(memory_collection_name(user_id))
    for name in names:
        await _safe_delete_collection(name)


async def execute_memory_job(app, job: MemoryJobModel, worker_id: str) -> None:
    claim_token = job.claim_token
    lease_generation = job.lease_generation

    async def heartbeat():
        while True:
            await asyncio.sleep(MEMORY_JOB_HEARTBEAT_SECONDS)
            alive = await Memories.heartbeat_job(
                job.id,
                worker_id,
                claim_token=claim_token,
                lease_generation=lease_generation,
                lease_seconds=MEMORY_JOB_LEASE_SECONDS,
            )
            if not alive:
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        user = await Users.get_user_by_id(job.user_id)
        if not user:
            if job.job_type == 'account_cleanup':
                await _cleanup_account(job)
                await Memories.complete_job(
                    job.id, worker_id, claim_token=claim_token, lease_generation=lease_generation
                )
            else:
                await Memories.fail_job(
                    job.id,
                    worker_id,
                    'user_missing',
                    claim_token=claim_token,
                    lease_generation=lease_generation,
                )
            return

        async with asyncio.timeout(MEMORY_JOB_TIMEOUT_SECONDS):
            if job.job_type == 'upsert_embedding':
                await _upsert_embedding(app, job, user)
                await Memories.complete_job(
                    job.id,
                    worker_id,
                    sync_memory=True,
                    claim_token=claim_token,
                    lease_generation=lease_generation,
                )
            elif job.job_type == 'delete_embedding':
                await _delete_embedding(job)
                await Memories.complete_job(
                    job.id,
                    worker_id,
                    sync_memory=True,
                    claim_token=claim_token,
                    lease_generation=lease_generation,
                )
            elif job.job_type == 'extract_turns':
                await _extract_turns(app, job, user)
                await Memories.complete_job(
                    job.id, worker_id, claim_token=claim_token, lease_generation=lease_generation
                )
            elif job.job_type == 'account_cleanup':
                await _cleanup_account(job)
                await Memories.complete_job(
                    job.id, worker_id, claim_token=claim_token, lease_generation=lease_generation
                )
            else:
                raise ValueError(f'Unsupported memory job type: {job.job_type}')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception('Memory job %s (%s) failed', job.id, job.job_type)
        await Memories.fail_job(
            job.id,
            worker_id,
            str(exc),
            claim_token=claim_token,
            lease_generation=lease_generation,
        )
    finally:
        heartbeat_task.cancel()


async def _run_job(app, job: MemoryJobModel, worker_id: str) -> None:
    async with _JOB_SEMAPHORE:
        await execute_memory_job(app, job, worker_id)


async def _dispatch_cleanup(app, worker_id: str) -> int:
    rows = await Memories.claim_account_cleanup(worker_id, limit=2, lease_seconds=MEMORY_JOB_LEASE_SECONDS)
    for row in rows:
        async def run(cleanup=row):
            async with _JOB_SEMAPHORE:
                try:
                    await _cleanup_account(cleanup)
                    await Memories.complete_account_cleanup(cleanup.id, worker_id)
                except Exception as exc:
                    await Memories.fail_account_cleanup(cleanup.id, worker_id, str(exc))

        task = asyncio.create_task(run())
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    return len(rows)


async def shutdown_memory_jobs() -> None:
    tasks = list(_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def claim_and_dispatch_memory_jobs(app) -> int:
    """Claim a bounded job batch; leases make spawned work restart-safe."""
    _warn_sqlite_multiprocess()
    worker_id = _worker_id(app)
    free_slots = _JOB_SEMAPHORE._value
    if free_slots <= 0:
        return 0
    limit = min(MEMORY_JOB_BATCH_SIZE, max(1, free_slots))
    async with _CLAIM_LOCK:
        jobs = await Memories.claim_jobs(
            worker_id,
            limit=limit,
            lease_seconds=MEMORY_JOB_LEASE_SECONDS,
        )
    for job in jobs:
        task = asyncio.create_task(_run_job(app, job, worker_id))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    await _dispatch_cleanup(app, worker_id)
    return len(jobs)
