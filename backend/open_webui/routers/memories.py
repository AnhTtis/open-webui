from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from open_webui.config import RAG_EMBEDDING_QUERY_PREFIX
from open_webui.constants import ERROR_MESSAGES
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_session
from open_webui.models.config import Config
from open_webui.models.memories import (
    AgentProfileModel,
    Memories,
    MemoryConflictError,
    MemoryModel,
    MemoryProposalModel,
    MemoryQuotaExceeded,
    MemoryRequestTooLarge,
    MemoryRevisionModel,
    memory_collection_name,
    parse_memory_vector_doc_id,
)
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.retrieval.vector.main import SearchResult
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.memory import (
    clean_memory_content,
    clean_memory_path,
    memory_vector_text,
    read_memory_path_rows,
    user_can_use_memories,
    validate_memory_operations,
)
from open_webui.utils.memory_limits import (
    MEMORY_JSON_IMPORT_MAX_BYTES,
    MEMORY_MAX_QUERY_BYTES,
    MEMORY_NDJSON_IMPORT_MAX_BYTES,
    MEMORY_NDJSON_LINE_MAX_BYTES,
    MEMORY_NDJSON_MAX_RECORDS,
    MEMORY_VECTOR_OVERSAMPLE,
    MEMORY_VECTOR_TOP_K,
    clamp_page_size,
    utf8_bytes,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()


async def check_memories_permission(user):
    if await user_can_use_memories(user):
        return
    config = await Config.get_many('memories.enable')
    if not config.get('memories.enable'):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
    )


def _raise_memory_http(exc: Exception) -> None:
    if isinstance(exc, MemoryQuotaExceeded):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.as_dict()) from exc
    if isinstance(exc, MemoryRequestTooLarge):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={'code': exc.code, 'message': str(exc)},
        ) from exc
    if isinstance(exc, MemoryConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'code': getattr(exc, 'code', 'memory_conflict'), 'message': str(exc)},
        ) from exc
    raise exc


############################
# GetMemories
# Let what is remembered here spare someone the cost
# of learning it twice.
############################


@router.get('/', response_model=list[MemoryModel])
async def get_memories(
    request: Request,
    skip: int = 0,
    limit: int | None = None,
    cursor: str | None = None,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    page, _total, _next = await Memories.list_memories_page(
        user.id,
        skip=skip,
        limit=limit,
        cursor=cursor,
    )
    return page


@router.get('/page')
async def get_memories_page(
    skip: int = 0,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = 'active',
    memory_type: str | None = 'all',
    query: str | None = None,
    path: str | None = None,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    page_size = clamp_page_size(limit)
    items, total, next_cursor = await Memories.list_memories_page(
        user.id,
        skip=skip,
        limit=page_size,
        cursor=cursor,
        status=status,
        memory_type=memory_type,
        query=query,
        path=path,
        include_archived=status in {'archived', 'all'},
        include_deleted=status in {'deleted', 'all'},
    )
    return {
        'items': items,
        'total': total,
        'next_cursor': next_cursor,
        'skip': skip,
        'limit': page_size,
    }


@router.get('/summary')
async def get_memory_summary(user=Depends(get_verified_user)):
    await check_memories_permission(user)
    return await Memories.get_user_summary(user.id)


@router.get('/admin/health')
async def get_memory_admin_health(user=Depends(get_admin_user)):
    return await Memories.get_admin_health()


############################
# AddMemory
############################


class AddMemoryForm(BaseModel):
    content: str
    type: Literal['user', 'context'] = 'context'
    path: str | None = None


class MemoryUpdateModel(BaseModel):
    content: str | None = None
    type: Literal['user', 'context'] | None = None
    path: str | None = None
    expected_version: int | None = None


class MemoryOperationModel(BaseModel):
    action: Literal['add', 'replace', 'remove', 'move']
    id: str | None = None
    content: str | None = None
    type: Literal['user', 'context'] | None = None
    path: str | None = None
    expected_version: int | None = None


class UpdateMemoriesForm(BaseModel):
    operations: list[MemoryOperationModel]
    source: Literal['tool', 'background_review'] | None = None


class SearchMemoriesForm(BaseModel):
    query: str | None = None
    type: Literal['user', 'context', 'all'] = 'all'
    status: Literal['active', 'candidate', 'archived', 'deleted', 'all'] = 'active'
    path: str | None = None
    memory_id: str | None = None
    skip: int = 0
    limit: int = 20


class ReviewProposalForm(BaseModel):
    approve: bool


class LearningStateForm(BaseModel):
    paused: bool


class ListMemoryPathsForm(BaseModel):
    query: str | None = None
    type: Literal['user', 'context', 'all'] = 'all'
    limit: int = 100


class ReadMemoryPathForm(BaseModel):
    path: str
    type: Literal['user', 'context', 'all'] = 'all'
    include_children: bool = True
    limit: int = 50


async def reindex_memory_vectors_for_user(
    request: Request,
    user_id: str,
    memories: list[MemoryModel] | None = None,
    user=None,
) -> int:
    """Queue a generation-fenced rebuild without loading the whole user set into RAM."""
    from open_webui.utils.memory_jobs import embedding_fingerprint

    result = await Memories.start_reindex_generation(user_id, fingerprint=embedding_fingerprint(request.app))
    return int(result.get('queued') or 0)


@router.post('/add', response_model=MemoryModel | None)
async def add_memory(
    request: Request,
    form_data: AddMemoryForm,
    user=Depends(get_verified_user),
):
    """Persist a new memory and embed it into the user's vector collection.

    Does NOT use ``Depends(get_async_session)`` — database operations manage their
    own short-lived sessions so a connection is not held during the external
    embedding API call (``EMBEDDING_FUNCTION``), which can take 1-5+ seconds.
    """
    await check_memories_permission(user)

    content = clean_memory_content(form_data.content)
    path = clean_memory_path(form_data.path)
    try:
        memory = await Memories.insert_new_memory(
            user.id,
            content,
            memory_type=form_data.type,
            path=path,
            meta={'created_by': 'manual'},
        )
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as exc:
        _raise_memory_http(exc)

    await publish_event(
        request,
        EVENTS.MEMORY_CREATED,
        actor=user,
        subject_id=memory.id,
        data={'type': memory.type, 'has_path': bool(memory.path)},
    )
    return memory


@router.post('/update', response_model=list[dict])
async def update_memories(
    request: Request,
    form_data: UpdateMemoriesForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)

    operations = validate_memory_operations(form_data)
    metadata = getattr(request.state, 'metadata', {}) or {}
    source = form_data.source or 'tool'
    for operation in operations:
        if operation.get('action') in {'add', 'replace', 'move'}:
            operation['meta'] = {
                'created_by': source,
                'chat_id': metadata.get('chat_id'),
                'message_id': metadata.get('message_id'),
                'model': metadata.get('model'),
            }

    try:
        results = await Memories.apply_memory_operations(user.id, operations)
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as e:
        _raise_memory_http(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    response = []
    for result in results:
        memory = result.get('memory')
        if isinstance(memory, MemoryModel):
            result = {**result, 'memory': memory.model_dump()}
        response.append(result)

    for result in response:
        status_value = result.get('status')
        memory = result.get('memory') or {}
        memory_id = memory.get('id') or result.get('id')

        if status_value == 'created':
            event = EVENTS.MEMORY_CREATED
        elif status_value == 'updated':
            event = EVENTS.MEMORY_UPDATED
        elif status_value == 'deleted':
            event = EVENTS.MEMORY_DELETED
        else:
            continue

        await publish_event(
            request,
            event,
            actor=user,
            subject_id=memory_id,
            data={
                'type': memory.get('type'),
                'has_path': bool(memory.get('path')),
                'operation': result.get('action'),
            },
        )

    return response


############################
# QueryMemory
############################


class QueryMemoryForm(BaseModel):
    content: str
    k: int = 8


@router.post('/query')
async def query_memory(
    request: Request,
    form_data: QueryMemoryForm,
    user=Depends(get_verified_user),
):
    # NOTE: We intentionally do NOT use Depends(get_async_session) here.
    # This prevents holding a connection during EMBEDDING_FUNCTION()
    # which makes external embedding API calls (1-5+ seconds).
    await check_memories_permission(user)

    if utf8_bytes(form_data.content) > MEMORY_MAX_QUERY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={'code': 'memory_request_too_large', 'message': 'Query is too large'},
        )

    if not await Memories.user_has_memories(user.id):
        raise HTTPException(status_code=404, detail='No memories found for user')

    generation = await Memories.get_active_generation(user.id)
    collection_name = generation.collection_name if generation else memory_collection_name(user.id)
    top_k = max(1, min(int(form_data.k or MEMORY_VECTOR_TOP_K), MEMORY_VECTOR_TOP_K))
    oversample = max(top_k, min(MEMORY_VECTOR_OVERSAMPLE, MEMORY_VECTOR_OVERSAMPLE))

    vector = await request.app.state.EMBEDDING_FUNCTION(form_data.content, prefix=RAG_EMBEDDING_QUERY_PREFIX, user=user)
    results = await ASYNC_VECTOR_DB_CLIENT.search(
        collection_name=collection_name,
        vectors=[vector],
        limit=oversample,
    )

    relevance_threshold = max(0.0, min(float(await Config.get('memories.relevance_threshold', 0.2)), 1.0))
    raw_ids = (results.ids[0] if results and results.ids else []) or []
    raw_dists = (results.distances[0] if results and results.distances else []) or []

    ranked: list[tuple[str, float | None]] = []
    for idx, raw_id in enumerate(raw_ids):
        score = raw_dists[idx] if idx < len(raw_dists) else None
        if relevance_threshold > 0.0 and score is not None and score < relevance_threshold:
            continue
        ranked.append((str(raw_id), score))

    parsed_ids = []
    parsed_hits = []
    for raw_id, score in ranked:
        generation_id, memory_id, revision = parse_memory_vector_doc_id(raw_id)
        parsed_hits.append((raw_id, generation_id, memory_id, revision, score))
        if memory_id:
            parsed_ids.append(memory_id)

    canonical = await Memories.get_memories_by_ids(user.id, parsed_ids, eligible_only=True)
    by_id = {memory.id: memory for memory in canonical}

    filtered_ids: list[str] = []
    filtered_docs: list[str] = []
    filtered_metas: list[dict] = []
    filtered_dists: list[float] = []
    rejected: list[tuple[str, str | None, int | None]] = []

    for raw_id, generation_id, memory_id, revision, score in parsed_hits:
        memory = by_id.get(memory_id) if memory_id else None
        if not memory or memory.status != 'active':
            rejected.append((raw_id, memory_id, revision))
            continue
        if revision is not None and revision != memory.current_revision:
            rejected.append((raw_id, memory_id, revision))
            continue
        filtered_ids.append(raw_id)
        filtered_docs.append(memory_vector_text(memory.content, memory.path))
        filtered_metas.append(
            {
                'created_at': memory.created_at,
                'updated_at': memory.updated_at,
                'type': memory.type,
                'path': memory.path,
                'status': memory.status,
                'version': memory.version,
                'revision': memory.current_revision,
                'generation': generation_id,
                'user_id': user.id,
            }
        )
        filtered_dists.append(float(score) if score is not None else 0.0)
        if len(filtered_ids) >= top_k:
            break

    for raw_id, memory_id, revision in rejected[:8]:
        try:
            await Memories.enqueue_job(
                user_id=user.id,
                memory_id=memory_id,
                job_type='delete_embedding' if not memory_id or memory_id not in by_id else 'upsert_embedding',
                idempotency_key=f'repair:{user.id}:{raw_id}'[:190],
                payload={
                    'vector_doc_id': raw_id,
                    'collection_name': collection_name,
                    'revision': revision,
                    'repair': True,
                },
            )
        except Exception:
            log.debug('Failed to enqueue memory repair for %s', raw_id)

    return SearchResult(
        ids=[filtered_ids],
        documents=[filtered_docs],
        metadatas=[filtered_metas],
        distances=[filtered_dists],
    )


@router.post('/search', response_model=list[MemoryModel])
async def search_memories(
    form_data: SearchMemoriesForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    memories, _total = await Memories.search_memories(
        user.id,
        query=form_data.query,
        memory_type=form_data.type,
        status=form_data.status,
        skip=form_data.skip,
        limit=form_data.limit,
        path=form_data.path,
        memory_id=form_data.memory_id,
    )
    return memories


@router.post('/paths')
async def list_memory_paths(
    form_data: ListMemoryPathsForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    return await Memories.list_path_groups(
        user.id,
        query=form_data.query or '',
        memory_type=form_data.type,
        limit=form_data.limit,
    )


@router.post('/path')
async def read_memory_path(
    form_data: ReadMemoryPathForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    lookup_path = clean_memory_path(form_data.path)
    if not lookup_path:
        raise HTTPException(status_code=400, detail='Memory path is required')
    page, _total, _next = await Memories.list_memories_page(
        user.id,
        path=lookup_path,
        memory_type=form_data.type,
        limit=form_data.limit,
        status='active',
    )
    result = read_memory_path_rows(
        page,
        path=lookup_path,
        memory_type=form_data.type,
        include_children=form_data.include_children,
        limit=form_data.limit,
    )
    parts = lookup_path.split('/')
    result['parents'] = ['/'.join(parts[:idx]) for idx in range(1, len(parts))]
    return {
        **result,
        'memories': [memory.model_dump() if hasattr(memory, 'model_dump') else memory for memory in result['memories']],
    }


############################
# ReindexMemoryVectorDB
############################
@router.post('/reindex')
async def reindex_memories_from_vector_db(
    request: Request,
    user=Depends(get_admin_user),
):
    after_user_id = None
    total_users = 0
    total_memories = 0
    while True:
        user_ids = await Memories.iter_users_with_memories(after_user_id=after_user_id, limit=100)
        if not user_ids:
            break
        for user_id in user_ids:
            total_memories += await reindex_memory_vectors_for_user(request, user_id)
            total_users += 1
        after_user_id = user_ids[-1]

    await publish_event(
        request,
        EVENTS.MEMORY_RESET,
        actor=user,
        subject_id='all',
        subject_type='user',
        data={'count': total_memories, 'user_count': total_users, 'reindex': True},
    )
    return {
        'status': True,
        'total_users': total_users,
        'total_memories': total_memories,
        'operation': 'queued',
    }


@router.post('/reset', response_model=bool)
async def reset_memory_from_vector_db(
    request: Request,
    user=Depends(get_verified_user),
):
    """Queue a bounded, restart-safe rebuild of the user's memory vectors."""
    await check_memories_permission(user)

    count = await reindex_memory_vectors_for_user(request, user.id, user=user)

    await publish_event(
        request,
        EVENTS.MEMORY_RESET,
        actor=user,
        subject_id=user.id,
        subject_type='user',
        data={'count': count, 'reindex': True},
    )
    return True


############################
# DeleteMemoriesByUserId
############################


@router.delete('/delete/user', response_model=bool)
async def delete_memory_by_user_id(
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await check_memories_permission(user)

    result = await Memories.delete_memories_by_user_id(user.id, db=db)

    if result:
        await publish_event(
            request,
            EVENTS.MEMORY_DELETED,
            actor=user,
            subject_id=user.id,
            subject_type='user',
        )
        return True

    return False


@router.get('/proposals', response_model=list[MemoryProposalModel])
async def get_memory_proposals(
    proposal_status: Literal['pending', 'approved', 'rejected', 'conflicted', 'all'] = 'pending',
    skip: int = 0,
    limit: int | None = None,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    return await Memories.get_proposals(user.id, status=proposal_status, skip=skip, limit=limit)


@router.get('/proposals/page')
async def get_memory_proposals_page(
    proposal_status: Literal['pending', 'approved', 'rejected', 'conflicted', 'all'] = 'pending',
    skip: int = 0,
    limit: int | None = None,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    page_size = clamp_page_size(limit)
    items, total = await Memories.get_proposals_page(user.id, status=proposal_status, skip=skip, limit=page_size)
    return {'items': items, 'total': total, 'skip': skip, 'limit': page_size}


@router.post('/proposals/{proposal_id}/review')
async def review_memory_proposal(
    proposal_id: str,
    form_data: ReviewProposalForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    try:
        proposal, results = await Memories.review_proposal(proposal_id, user.id, form_data.approve)
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as exc:
        _raise_memory_http(exc)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not proposal:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    return {
        'proposal': proposal,
        'results': [
            (
                {**result, 'memory': result['memory'].model_dump()}
                if isinstance(result.get('memory'), MemoryModel)
                else result
            )
            for result in results
        ],
    }


@router.get('/export')
async def export_memories(user=Depends(get_verified_user)):
    await check_memories_permission(user)
    bundle = await Memories.export_memory_bundle(user.id)
    payload = json.dumps(bundle, ensure_ascii=False, separators=(',', ':'))
    return Response(
        content=payload,
        media_type='application/json',
        headers={
            'Content-Disposition': 'attachment; filename="open-webui-memory-export.json"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@router.get('/export/ndjson')
async def export_memories_ndjson(user=Depends(get_verified_user)):
    await check_memories_permission(user)
    user_id = user.id

    async def generate():
        hasher = hashlib.sha256()
        count = 0

        def emit(obj: dict) -> str:
            line = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
            hasher.update(line.encode('utf-8'))
            hasher.update(b'\n')
            return line + '\n'

        yield emit(
            {
                'type': 'manifest',
                'schema_version': 2,
                'format': 'ndjson',
                'exported_at': int(time.time()),
            }
        )
        async for batch in Memories.iter_user_memory_batches(user_id):
            for memory in batch:
                count += 1
                yield emit(
                    {
                        'type': 'memory',
                        'id': memory.id,
                        'content': memory.content,
                        'type_name': memory.type,
                        'path': memory.path,
                        'status': memory.status,
                        'scope': memory.scope,
                        'kind': memory.kind,
                        'structured_value': memory.structured_value,
                        'current_revision': memory.current_revision,
                        'version': memory.version,
                        'updated_at': memory.updated_at,
                        'created_at': memory.created_at,
                    }
                )
        footer = {'type': 'footer', 'schema_version': 2, 'count': count, 'sha256': hasher.hexdigest()}
        yield json.dumps(footer, ensure_ascii=False, separators=(',', ':')) + '\n'

    return StreamingResponse(
        generate(),
        media_type='application/x-ndjson',
        headers={
            'Content-Disposition': 'attachment; filename="open-webui-memory-export.ndjson"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@router.post('/import')
async def import_memories(
    file: UploadFile = File(...),
    dry_run: bool = False,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    max_bytes = MEMORY_JSON_IMPORT_MAX_BYTES
    payload = await file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail='Memory export is too large')
    try:
        bundle = json.loads(payload.decode('utf-8'))
        return await Memories.import_memory_bundle(user.id, bundle, dry_run=dry_run)
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as exc:
        _raise_memory_http(exc)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post('/import/ndjson')
async def import_memories_ndjson(
    file: UploadFile = File(...),
    dry_run: bool = False,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    transfer = await Memories.create_transfer(user.id, direction='import', format='ndjson', dry_run=dry_run)
    leftover = b''
    total_bytes = 0
    index = 0
    batch: list[tuple[int, str, dict]] = []

    async def flush_batch() -> None:
        nonlocal batch
        if batch:
            await Memories.append_transfer_records(transfer.id, batch)
            batch = []

    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MEMORY_NDJSON_IMPORT_MAX_BYTES:
                raise HTTPException(status_code=413, detail='NDJSON export is too large')
            leftover += chunk
            while b'\n' in leftover:
                raw_line, leftover = leftover.split(b'\n', 1)
                if not raw_line.strip():
                    continue
                if len(raw_line) > MEMORY_NDJSON_LINE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail='NDJSON line is too large')
                try:
                    record = json.loads(raw_line.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                if not isinstance(record, dict):
                    raise HTTPException(status_code=422, detail='NDJSON record must be an object')
                record_type = str(record.get('type') or 'memory')
                if record_type in {'manifest', 'footer'}:
                    continue
                if record_type == 'memory' and 'type_name' in record and 'type' not in record:
                    record = {**record, 'type': record.get('type_name')}
                batch.append((index, record_type, record))
                index += 1
                if index > MEMORY_NDJSON_MAX_RECORDS:
                    raise HTTPException(status_code=413, detail='NDJSON record cap exceeded')
                if len(batch) >= 100:
                    await flush_batch()
        if leftover.strip():
            if len(leftover) > MEMORY_NDJSON_LINE_MAX_BYTES:
                raise HTTPException(status_code=413, detail='NDJSON line is too large')
            record = json.loads(leftover.decode('utf-8'))
            if isinstance(record, dict) and str(record.get('type') or 'memory') not in {'manifest', 'footer'}:
                if record.get('type') == 'memory' and 'type_name' in record:
                    record = {**record, 'type': record.get('type_name')}
                batch.append((index, str(record.get('type') or 'memory'), record))
        await flush_batch()
        return await Memories.publish_transfer(transfer.id, user.id)
    except HTTPException:
        raise
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as exc:
        _raise_memory_http(exc)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/operations/{operation_id}')
async def get_memory_operation(operation_id: str, user=Depends(get_verified_user)):
    await check_memories_permission(user)
    transfer = await Memories.get_transfer(operation_id, user.id)
    if not transfer:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    return {
        'id': transfer.id,
        'status': transfer.status,
        'direction': transfer.direction,
        'format': transfer.format,
        'dry_run': transfer.dry_run,
        'total_records': transfer.total_records,
        'processed_records': transfer.processed_records,
    }


@router.get('/profile', response_model=AgentProfileModel)
async def get_memory_profile(user=Depends(get_verified_user)):
    await check_memories_permission(user)
    return await Memories.get_or_create_profile(user.id)


@router.post('/profile/learning', response_model=AgentProfileModel)
async def set_memory_learning(
    form_data: LearningStateForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    return await Memories.set_learning_paused(user.id, form_data.paused)


@router.get('/{memory_id}/history', response_model=list[MemoryRevisionModel])
async def get_memory_history(
    memory_id: str,
    skip: int = 0,
    limit: int | None = None,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    return await Memories.get_memory_revisions(memory_id, user.id, skip=skip, limit=limit)


@router.get('/{memory_id}/history/page')
async def get_memory_history_page(
    memory_id: str,
    skip: int = 0,
    limit: int | None = None,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    page_size = clamp_page_size(limit)
    items, total = await Memories.get_memory_revisions_page(memory_id, user.id, skip=skip, limit=page_size)
    return {'items': items, 'total': total, 'skip': skip, 'limit': page_size}


@router.post('/{memory_id}/restore/{revision}', response_model=MemoryModel | None)
async def restore_memory_revision(
    memory_id: str,
    revision: int,
    request: Request,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    try:
        memory = await Memories.restore_memory_revision(
            memory_id,
            revision,
            user.id,
            meta={'created_by': 'manual', 'actor_id': user.id},
        )
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as exc:
        _raise_memory_http(exc)
    if not memory:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)

    await publish_event(
        request,
        EVENTS.MEMORY_UPDATED,
        actor=user,
        subject_id=memory.id,
        data={
            'type': memory.type,
            'has_path': bool(memory.path),
            'operation': 'restore',
            'revision': revision,
        },
    )
    return memory


############################
# UpdateMemoryById
############################


@router.post('/{memory_id}/update', response_model=MemoryModel | None)
async def update_memory_by_id(
    memory_id: str,
    request: Request,
    form_data: MemoryUpdateModel,
    user=Depends(get_verified_user),
):
    # NOTE: We intentionally do NOT use Depends(get_async_session) here.
    # Database operations (update_memory_by_id_and_user_id) manage their own
    # short-lived sessions. This prevents holding a connection during
    # EMBEDDING_FUNCTION() which makes external API calls (1-5+ seconds).
    await check_memories_permission(user)

    content = clean_memory_content(form_data.content) if form_data.content is not None else None
    path = clean_memory_path(form_data.path)
    changed_fields = form_data.model_fields_set - {'expected_version'}
    if not changed_fields:
        raise HTTPException(status_code=400, detail='No memory update provided')
    try:
        memory = await Memories.update_memory_by_id_and_user_id(
            memory_id,
            user.id,
            content,
            memory_type=form_data.type,
            path=path,
            update_path='path' in form_data.model_fields_set,
            meta={'created_by': 'manual'},
            expected_version=form_data.expected_version,
        )
    except (MemoryQuotaExceeded, MemoryRequestTooLarge, MemoryConflictError) as exc:
        _raise_memory_http(exc)
    if memory is None:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)

    await publish_event(
        request,
        EVENTS.MEMORY_UPDATED,
        actor=user,
        subject_id=memory.id,
        data={'type': memory.type, 'has_path': bool(memory.path)},
    )
    return memory


############################
# DeleteMemoryById
############################


@router.delete('/{memory_id}', response_model=bool)
async def delete_memory_by_id(
    memory_id: str,
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await check_memories_permission(user)

    result = await Memories.delete_memory_by_id_and_user_id(memory_id, user.id, db=db)

    if result:
        await publish_event(
            request,
            EVENTS.MEMORY_DELETED,
            actor=user,
            subject_id=memory_id,
        )
        return True

    return False
