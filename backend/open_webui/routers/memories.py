from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
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
    MemoryRevisionModel,
)
from open_webui.models.users import Users
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.memory import (
    clean_memory_content,
    clean_memory_path,
    list_memory_path_groups,
    read_memory_path_rows,
    search_memory_rows,
    validate_memory_operations,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()


async def check_memories_permission(user):
    config = await Config.get_many('memories.enable', 'user.permissions')
    if not config.get('memories.enable'):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if user.role != 'admin' and not await has_permission(user.id, 'features.memories', config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


############################
# GetMemories
# Let what is remembered here spare someone the cost
# of learning it twice.
############################


@router.get('/', response_model=list[MemoryModel])
async def get_memories(
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await check_memories_permission(user)

    return await Memories.get_memories_by_user_id(user.id, db=db)


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
    """Queue idempotent rebuild jobs without deleting the live collection."""
    memories = memories if memories is not None else await Memories.get_memories_by_user_id(user_id)
    queued = 0
    for memory in memories or []:
        if memory.status not in {'active', 'candidate'}:
            continue
        await Memories.enqueue_job(
            user_id=user_id,
            memory_id=memory.id,
            job_type='upsert_embedding',
            idempotency_key=f'memory:{memory.id}:revision:{memory.current_revision}:reindex',
            payload={'revision': memory.current_revision, 'reindex': True},
        )
        queued += 1
    return queued


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
    memory = await Memories.insert_new_memory(
        user.id,
        content,
        memory_type=form_data.type,
        path=path,
        meta={'created_by': 'manual'},
    )

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
    except MemoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

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
    # Database operations (get_memories_by_user_id) manage their own short-lived sessions.
    # This prevents holding a connection during EMBEDDING_FUNCTION()
    # which makes external embedding API calls (1-5+ seconds).
    await check_memories_permission(user)

    memories = await Memories.get_memories_by_user_id(user.id)
    if not memories:
        raise HTTPException(status_code=404, detail='No memories found for user')

    vector = await request.app.state.EMBEDDING_FUNCTION(form_data.content, prefix=RAG_EMBEDDING_QUERY_PREFIX, user=user)

    results = await ASYNC_VECTOR_DB_CLIENT.search(
        collection_name=f'user-memory-{user.id}',
        vectors=[vector],
        limit=max(1, min(form_data.k, 20)),
    )

    # Filter results by relevance threshold to avoid returning unrelated
    # memories.  Vector similarity search always returns the top-K nearest
    # neighbours even when they are completely irrelevant; applying the
    # same RELEVANCE_THRESHOLD used by RAG ensures only genuinely matching
    # memories are surfaced (distances are normalised to 0→1, higher is
    # better).
    relevance_threshold = max(0.0, min(float(await Config.get('memories.relevance_threshold', 0.2)), 1.0))
    if results and relevance_threshold > 0.0 and results.distances and results.distances[0]:
        from open_webui.retrieval.vector.main import SearchResult

        filtered_ids = []
        filtered_docs = []
        filtered_metas = []
        filtered_dists = []

        for idx, score in enumerate(results.distances[0]):
            if score >= relevance_threshold:
                if results.ids and results.ids[0]:
                    filtered_ids.append(results.ids[0][idx])
                if results.documents and results.documents[0]:
                    filtered_docs.append(results.documents[0][idx])
                if results.metadatas and results.metadatas[0]:
                    filtered_metas.append(results.metadatas[0][idx])
                filtered_dists.append(score)

        results = SearchResult(
            ids=[filtered_ids] if filtered_ids else [[]],
            documents=[filtered_docs] if filtered_docs else [[]],
            metadatas=[filtered_metas] if filtered_metas else [[]],
            distances=[filtered_dists] if filtered_dists else [[]],
        )

    return results


@router.post('/search', response_model=list[MemoryModel])
async def search_memories(
    form_data: SearchMemoriesForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)

    if form_data.path or form_data.memory_id:
        memories = await Memories.get_memories_by_user_id(
            user.id,
            include_archived=form_data.status in {'archived', 'all'},
            include_deleted=form_data.status in {'deleted', 'all'},
        )
        if form_data.status != 'all':
            memories = [memory for memory in memories if memory.status == form_data.status]
        return search_memory_rows(
            memories,
            query=form_data.query,
            path=form_data.path,
            memory_id=form_data.memory_id,
            memory_type=form_data.type,
            limit=form_data.limit,
        )

    memories, _ = await Memories.search_memories(
        user.id,
        query=form_data.query,
        memory_type=form_data.type,
        status=form_data.status,
        skip=form_data.skip,
        limit=form_data.limit,
    )
    return memories


@router.post('/paths')
async def list_memory_paths(
    form_data: ListMemoryPathsForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)

    memories = await Memories.get_memories_by_user_id(user.id)
    return list_memory_path_groups(
        memories,
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

    memories = await Memories.get_memories_by_user_id(user.id)
    result = read_memory_path_rows(
        memories,
        path=form_data.path,
        memory_type=form_data.type,
        include_children=form_data.include_children,
        limit=form_data.limit,
    )
    return {
        **result,
        'memories': [memory.model_dump() for memory in result['memories']],
    }


############################
# ReindexMemoryVectorDB
############################
@router.post('/reindex')
async def reindex_memories_from_vector_db(
    request: Request,
    user=Depends(get_admin_user),
):
    memories = await Memories.get_memories()
    memories = memories or []
    memories_by_user_id = {}
    for memory in memories:
        memories_by_user_id.setdefault(memory.user_id, []).append(memory)

    users_result = await Users.get_users()
    users = users_result.get('users', []) if users_result else []
    total_memories = 0

    for memory_user in users:
        total_memories += await reindex_memory_vectors_for_user(
            request,
            memory_user.id,
            memories=memories_by_user_id.get(memory_user.id, []),
            user=memory_user,
        )

    await publish_event(
        request,
        EVENTS.MEMORY_RESET,
        actor=user,
        subject_id='all',
        subject_type='user',
        data={'count': total_memories, 'user_count': len(users), 'reindex': True},
    )
    return {'status': True, 'total_users': len(users), 'total_memories': total_memories}


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
    proposal_status: Literal['pending', 'approved', 'rejected', 'all'] = 'pending',
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    return await Memories.get_proposals(user.id, status=proposal_status)


@router.post('/proposals/{proposal_id}/review')
async def review_memory_proposal(
    proposal_id: str,
    form_data: ReviewProposalForm,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    try:
        proposal, results = await Memories.review_proposal(proposal_id, user.id, form_data.approve)
    except MemoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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


@router.post('/import')
async def import_memories(
    file: UploadFile = File(...),
    dry_run: bool = False,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    max_bytes = 10 * 1024 * 1024
    payload = await file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail='Memory export is too large')
    try:
        bundle = json.loads(payload.decode('utf-8'))
        return await Memories.import_memory_bundle(user.id, bundle, dry_run=dry_run)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get('/{memory_id}/history', response_model=list[MemoryRevisionModel])
async def get_memory_history(memory_id: str, user=Depends(get_verified_user)):
    await check_memories_permission(user)
    return await Memories.get_memory_revisions(memory_id, user.id)


@router.post('/{memory_id}/restore/{revision}', response_model=MemoryModel | None)
async def restore_memory_revision(
    memory_id: str,
    revision: int,
    request: Request,
    user=Depends(get_verified_user),
):
    await check_memories_permission(user)
    memory = await Memories.restore_memory_revision(
        memory_id,
        revision,
        user.id,
        meta={'created_by': 'manual', 'actor_id': user.id},
    )
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
    except MemoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
