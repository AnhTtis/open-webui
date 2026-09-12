"""Durable, per-user memory storage and lifecycle records."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from open_webui.internal.db import Base, get_async_db_context
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ACTIVE_MEMORY_STATUSES = ('active', 'candidate')


@asynccontextmanager
async def _memory_db(db: AsyncSession | None = None):
    if db is not None:
        yield db
    else:
        async with get_async_db_context() as session:
            yield session


def normalize_memory_hash(content: str, memory_type: str, path: str | None) -> str:
    normalized = '\n'.join(
        (
            memory_type.strip().lower(),
            (path or '').strip().strip('/').lower(),
            ' '.join((content or '').split()).casefold(),
        )
    )
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


class Memory(Base):
    """Current state of a memory item; revisions and evidence are append-only."""

    __tablename__ = 'memory'
    __table_args__ = (
        Index('ix_memory_id_user_id', 'id', 'user_id'),
        Index('ix_memory_user_status_updated', 'user_id', 'status', 'updated_at'),
        Index('ix_memory_user_scope_status', 'user_id', 'scope', 'status'),
        Index('ix_memory_user_hash', 'user_id', 'normalized_hash'),
        Index('ix_memory_user_expires', 'user_id', 'expires_at'),
        CheckConstraint("scope IN ('session', 'working', 'long_term')", name='ck_memory_scope'),
        CheckConstraint(
            "status IN ('candidate', 'active', 'superseded', 'archived', 'deleted')",
            name='ck_memory_status',
        ),
        CheckConstraint('confidence >= 0 AND confidence <= 1', name='ck_memory_confidence'),
        CheckConstraint('trust >= 0 AND trust <= 1', name='ck_memory_trust'),
        CheckConstraint('importance >= 0 AND importance <= 1', name='ck_memory_importance'),
        CheckConstraint('version >= 1', name='ck_memory_version'),
    )

    id = Column(String, primary_key=True, unique=True)
    user_id = Column(String, index=True, nullable=False)
    agent_profile_id = Column(Text, nullable=True)
    type = Column(String, default='context', server_default='context', index=True)
    scope = Column(String(20), nullable=False, default='long_term', server_default='long_term')
    kind = Column(String(24), nullable=False, default='fact', server_default='fact')
    path = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    structured_value = Column(JSON, nullable=True)
    normalized_hash = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default='active', server_default='active')
    confidence = Column(Float, nullable=False, default=1.0, server_default='1')
    trust = Column(Float, nullable=False, default=1.0, server_default='1')
    importance = Column(Float, nullable=False, default=0.5, server_default='0.5')
    valid_from = Column(BigInteger, nullable=True)
    valid_to = Column(BigInteger, nullable=True)
    expires_at = Column(BigInteger, nullable=True)
    last_recalled_at = Column(BigInteger, nullable=True)
    recall_count = Column(Integer, nullable=False, default=0, server_default='0')
    helpful_count = Column(Integer, nullable=False, default=0, server_default='0')
    current_revision = Column(Integer, nullable=False, default=0, server_default='0')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    sync_status = Column(String(20), nullable=False, default='pending', server_default='pending')
    meta = Column(JSON, nullable=True)
    archived_at = Column(BigInteger, nullable=True)
    deleted_at = Column(BigInteger, nullable=True)
    updated_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class MemoryRevision(Base):
    __tablename__ = 'memory_revision'
    __table_args__ = (
        UniqueConstraint('memory_id', 'revision', name='uq_memory_revision_number'),
        Index('ix_memory_revision_memory_created', 'memory_id', 'created_at'),
    )

    id = Column(Text, primary_key=True)
    memory_id = Column(Text, ForeignKey('memory.id', ondelete='CASCADE'), nullable=False)
    revision = Column(Integer, nullable=False)
    action = Column(String(24), nullable=False)
    content = Column(Text, nullable=True)
    structured_value = Column(JSON, nullable=True)
    memory_type = Column(String(20), nullable=True)
    path = Column(Text, nullable=True)
    status = Column(String(20), nullable=False)
    reason = Column(Text, nullable=True)
    actor_type = Column(String(24), nullable=False)
    actor_id = Column(Text, nullable=True)
    source = Column(String(32), nullable=False)
    chat_id = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)
    model_id = Column(Text, nullable=True)
    extractor_version = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)


class MemoryEvidence(Base):
    __tablename__ = 'memory_evidence'
    __table_args__ = (Index('ix_memory_evidence_memory', 'memory_id', 'observed_at'),)

    id = Column(Text, primary_key=True)
    memory_id = Column(Text, ForeignKey('memory.id', ondelete='CASCADE'), nullable=False)
    revision_id = Column(Text, ForeignKey('memory_revision.id', ondelete='CASCADE'), nullable=False)
    chat_id = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)
    session_id = Column(Text, nullable=True)
    source_excerpt = Column(Text, nullable=True)
    source_hash = Column(String(64), nullable=True)
    source_role = Column(String(20), nullable=True)
    actor_type = Column(String(24), nullable=False)
    tool_name = Column(Text, nullable=True)
    model_id = Column(Text, nullable=True)
    extraction_run_id = Column(Text, nullable=True)
    observed_at = Column(BigInteger, nullable=False)


class MemoryRelation(Base):
    __tablename__ = 'memory_relation'
    __table_args__ = (
        UniqueConstraint('source_memory_id', 'target_memory_id', 'relation_type', name='uq_memory_relation'),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    source_memory_id = Column(Text, ForeignKey('memory.id', ondelete='CASCADE'), nullable=False)
    target_memory_id = Column(Text, ForeignKey('memory.id', ondelete='CASCADE'), nullable=False)
    relation_type = Column(String(24), nullable=False)
    score = Column(Float, nullable=True)
    created_at = Column(BigInteger, nullable=False)


class AgentProfile(Base):
    __tablename__ = 'agent_profile'
    __table_args__ = (
        UniqueConstraint('user_id', 'agent_id', name='uq_agent_profile_user_agent'),
        Index('ix_agent_profile_user', 'user_id'),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    agent_id = Column(Text, nullable=False, default='default', server_default='default')
    current_revision = Column(Integer, nullable=False, default=0, server_default='0')
    learning_paused = Column(Boolean, nullable=False, default=False, server_default='0')
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class AgentProfileRevision(Base):
    __tablename__ = 'agent_profile_revision'
    __table_args__ = (UniqueConstraint('profile_id', 'revision', name='uq_agent_profile_revision'),)

    id = Column(Text, primary_key=True)
    profile_id = Column(Text, ForeignKey('agent_profile.id', ondelete='CASCADE'), nullable=False)
    revision = Column(Integer, nullable=False)
    narrative = Column(Text, nullable=True)
    traits = Column(JSON, nullable=True)
    preferences = Column(JSON, nullable=True)
    boundaries = Column(JSON, nullable=True)
    active_goals = Column(JSON, nullable=True)
    source_run_id = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)


class SessionMemoryState(Base):
    __tablename__ = 'session_memory_state'
    __table_args__ = (
        UniqueConstraint('user_id', 'session_id', name='uq_session_memory_user_session'),
        Index('ix_session_memory_user_chat', 'user_id', 'chat_id'),
        Index('ix_session_memory_expires', 'expires_at'),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    agent_profile_id = Column(Text, ForeignKey('agent_profile.id', ondelete='SET NULL'), nullable=True)
    chat_id = Column(Text, nullable=False)
    session_id = Column(Text, nullable=False)
    rolling_summary = Column(Text, nullable=True)
    current_goals = Column(JSON, nullable=True)
    open_loops = Column(JSON, nullable=True)
    active_entities = Column(JSON, nullable=True)
    transcript_cursor = Column(Text, nullable=True)
    last_checkpoint_message_id = Column(Text, nullable=True)
    checkpoint_status = Column(String(20), nullable=False, default='pending', server_default='pending')
    expires_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


class MemoryProposal(Base):
    __tablename__ = 'memory_proposal'
    __table_args__ = (Index('ix_memory_proposal_user_status', 'user_id', 'status', 'created_at'),)

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    memory_id = Column(Text, ForeignKey('memory.id', ondelete='SET NULL'), nullable=True)
    action = Column(String(20), nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    confidence = Column(Float, nullable=True)
    reason = Column(Text, nullable=True)
    chat_id = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)
    model_id = Column(Text, nullable=True)
    reviewed_at = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=True)


class MemoryJob(Base):
    __tablename__ = 'memory_job'
    __table_args__ = (
        UniqueConstraint('idempotency_key', name='uq_memory_job_idempotency'),
        Index('ix_memory_job_claim', 'status', 'available_at', 'lease_expires_at'),
        Index('ix_memory_job_user', 'user_id', 'created_at'),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    memory_id = Column(Text, nullable=True)
    job_type = Column(String(32), nullable=False)
    payload = Column(JSON, nullable=True)
    payload_version = Column(Integer, nullable=False, default=1, server_default='1')
    idempotency_key = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    attempt_count = Column(Integer, nullable=False, default=0, server_default='0')
    available_at = Column(BigInteger, nullable=False)
    lease_owner = Column(Text, nullable=True)
    lease_expires_at = Column(BigInteger, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)


class MemoryAuditEvent(Base):
    __tablename__ = 'memory_audit_event'
    __table_args__ = (Index('ix_memory_audit_user_created', 'user_id', 'created_at'),)

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    memory_id = Column(Text, nullable=True)
    revision_id = Column(Text, nullable=True)
    proposal_id = Column(Text, nullable=True)
    action = Column(String(32), nullable=False)
    actor_type = Column(String(24), nullable=False)
    actor_id = Column(Text, nullable=True)
    correlation_id = Column(Text, nullable=True)
    data = Column(JSON, nullable=True)
    created_at = Column(BigInteger, nullable=False)


class MemoryModel(BaseModel):
    id: str
    user_id: str
    type: Literal['user', 'context'] = 'context'
    agent_profile_id: str | None = None
    scope: Literal['session', 'working', 'long_term'] = 'long_term'
    kind: str = 'fact'
    path: str | None = None
    content: str
    structured_value: dict | list | str | int | float | bool | None = None
    normalized_hash: str | None = None
    status: str = 'active'
    confidence: float = 1.0
    trust: float = 1.0
    importance: float = 0.5
    valid_from: int | None = None
    valid_to: int | None = None
    expires_at: int | None = None
    last_recalled_at: int | None = None
    recall_count: int = 0
    helpful_count: int = 0
    current_revision: int = 0
    version: int = 1
    sync_status: str = 'pending'
    meta: dict | None = None
    archived_at: int | None = None
    deleted_at: int | None = None
    updated_at: int
    created_at: int
    model_config = ConfigDict(from_attributes=True)


class MemoryRevisionModel(BaseModel):
    id: str
    memory_id: str
    revision: int
    action: str
    content: str | None = None
    memory_type: str | None = None
    path: str | None = None
    status: str
    reason: str | None = None
    actor_type: str
    actor_id: str | None = None
    source: str
    chat_id: str | None = None
    message_id: str | None = None
    model_id: str | None = None
    created_at: int
    model_config = ConfigDict(from_attributes=True)


class MemoryProposalModel(BaseModel):
    id: str
    user_id: str
    memory_id: str | None = None
    action: str
    payload: dict
    status: str
    confidence: float | None = None
    reason: str | None = None
    chat_id: str | None = None
    message_id: str | None = None
    model_id: str | None = None
    reviewed_at: int | None = None
    created_at: int
    expires_at: int | None = None
    model_config = ConfigDict(from_attributes=True)


class AgentProfileModel(BaseModel):
    id: str
    user_id: str
    agent_id: str
    current_revision: int
    learning_paused: bool
    created_at: int
    updated_at: int
    model_config = ConfigDict(from_attributes=True)


class MemoryJobModel(BaseModel):
    id: str
    user_id: str
    memory_id: str | None = None
    job_type: str
    payload: dict | None = None
    payload_version: int
    idempotency_key: str
    status: str
    attempt_count: int
    available_at: int
    lease_owner: str | None = None
    lease_expires_at: int | None = None
    last_error: str | None = None
    created_at: int
    updated_at: int
    completed_at: int | None = None
    model_config = ConfigDict(from_attributes=True)


class MemoryConflictError(ValueError):
    pass


class MemoriesTable:
    @staticmethod
    def normalize_memory_type(memory_type: str | None = None) -> str:
        return 'user' if memory_type == 'user' else 'context'

    @staticmethod
    def _provenance(meta: dict | None) -> dict:
        meta = meta or {}
        return {
            'actor_type': meta.get('created_by') or meta.get('updated_by') or 'manual',
            'actor_id': meta.get('actor_id'),
            'source': meta.get('source') or meta.get('created_by') or 'manual',
            'chat_id': meta.get('chat_id'),
            'message_id': meta.get('message_id'),
            'model_id': meta.get('model'),
            'extractor_version': meta.get('extractor_version'),
            'reason': meta.get('reason'),
        }

    async def _append_revision(self, db: AsyncSession, memory: Memory, action: str, meta: dict | None) -> str:
        provenance = self._provenance(meta)
        memory.current_revision = int(memory.current_revision or 0) + 1
        revision_id = str(uuid.uuid4())
        revision = MemoryRevision(
            id=revision_id,
            memory_id=memory.id,
            revision=memory.current_revision,
            action=action,
            content=memory.content,
            structured_value=memory.structured_value,
            memory_type=memory.type,
            path=memory.path,
            status=memory.status,
            reason=provenance['reason'],
            actor_type=provenance['actor_type'],
            actor_id=provenance['actor_id'],
            source=provenance['source'],
            chat_id=provenance['chat_id'],
            message_id=provenance['message_id'],
            model_id=provenance['model_id'],
            extractor_version=provenance['extractor_version'],
            created_at=int(time.time()),
        )
        db.add(revision)
        if provenance['chat_id'] or provenance['message_id']:
            db.add(
                MemoryEvidence(
                    id=str(uuid.uuid4()),
                    memory_id=memory.id,
                    revision_id=revision_id,
                    chat_id=provenance['chat_id'],
                    message_id=provenance['message_id'],
                    session_id=(meta or {}).get('session_id'),
                    source_excerpt=(meta or {}).get('source_excerpt'),
                    source_hash=(meta or {}).get('source_hash'),
                    source_role=(meta or {}).get('source_role'),
                    actor_type=provenance['actor_type'],
                    tool_name=(meta or {}).get('tool_name'),
                    model_id=provenance['model_id'],
                    extraction_run_id=(meta or {}).get('extraction_run_id'),
                    observed_at=int(time.time()),
                )
            )
        db.add(
            MemoryAuditEvent(
                id=str(uuid.uuid4()),
                user_id=memory.user_id,
                memory_id=memory.id,
                revision_id=revision_id,
                action=action,
                actor_type=provenance['actor_type'],
                actor_id=provenance['actor_id'],
                correlation_id=(meta or {}).get('correlation_id'),
                data={'revision': memory.current_revision, 'status': memory.status},
                created_at=int(time.time()),
            )
        )
        return revision_id

    async def _enqueue_sync_job(self, db: AsyncSession, memory: Memory, operation: str) -> None:
        now = int(time.time())
        memory.sync_status = 'pending'
        db.add(
            MemoryJob(
                id=str(uuid.uuid4()),
                user_id=memory.user_id,
                memory_id=memory.id,
                job_type='delete_embedding' if operation == 'delete' else 'upsert_embedding',
                payload={'revision': memory.current_revision},
                idempotency_key=f'memory:{memory.id}:revision:{memory.current_revision}:{operation}',
                status='pending',
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    async def insert_new_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str | None = None,
        path: str | None = None,
        meta: dict | None = None,
        db: AsyncSession | None = None,
    ) -> MemoryModel | None:
        async with _memory_db(db) as db:
            now = int(time.time())
            memory_type = self.normalize_memory_type(memory_type)
            content_hash = normalize_memory_hash(content, memory_type, path)
            existing = await db.execute(
                select(Memory).where(
                    Memory.user_id == user_id,
                    Memory.normalized_hash == content_hash,
                    Memory.status.in_(ACTIVE_MEMORY_STATUSES),
                )
            )
            record = existing.scalars().first()
            if record:
                return MemoryModel.model_validate(record)

            record = Memory(
                id=str(uuid.uuid4()),
                user_id=user_id,
                type=memory_type,
                scope=(meta or {}).get('scope', 'long_term'),
                kind=(meta or {}).get('kind', 'preference' if memory_type == 'user' else 'fact'),
                path=path,
                content=content,
                structured_value=(meta or {}).get('structured_value'),
                normalized_hash=content_hash,
                meta=meta,
                created_at=now,
                updated_at=now,
            )
            db.add(record)
            await db.flush()
            await self._append_revision(db, record, 'add', meta)
            await self._enqueue_sync_job(db, record, 'upsert')
            await db.commit()
            return MemoryModel.model_validate(record)

    async def update_memory_by_id_and_user_id(
        self,
        id: str,
        user_id: str,
        content: str | None,
        memory_type: str | None = None,
        path: str | None = None,
        update_path: bool = False,
        meta: dict | None = None,
        expected_version: int | None = None,
        db: AsyncSession | None = None,
    ) -> MemoryModel | None:
        async with _memory_db(db) as db:
            result = await db.execute(
                select(Memory).where(Memory.id == id, Memory.user_id == user_id).with_for_update()
            )
            memory = result.scalars().first()
            if not memory or memory.status == 'deleted':
                return None
            if expected_version is not None and memory.version != expected_version:
                raise MemoryConflictError('Memory was changed by another request')

            if content is not None:
                memory.content = content
            if memory_type is not None:
                memory.type = self.normalize_memory_type(memory_type)
            if update_path:
                memory.path = path
            if meta is not None:
                original_created_by = (memory.meta or {}).get('created_by')
                memory.meta = {**(memory.meta or {}), **meta}
                if original_created_by:
                    memory.meta['created_by'] = original_created_by
            memory.normalized_hash = normalize_memory_hash(memory.content, memory.type, memory.path)
            memory.updated_at = int(time.time())
            memory.version = int(memory.version or 0) + 1
            await self._append_revision(db, memory, 'replace', meta)
            await self._enqueue_sync_job(db, memory, 'upsert')
            await db.commit()
            return MemoryModel.model_validate(memory)

    async def get_memories(self, db: AsyncSession | None = None) -> list[MemoryModel]:
        async with _memory_db(db) as db:
            result = await db.execute(select(Memory).where(Memory.status != 'deleted'))
            return [MemoryModel.model_validate(memory) for memory in result.scalars().all()]

    async def get_memories_by_user_id(
        self,
        user_id: str,
        db: AsyncSession | None = None,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> list[MemoryModel]:
        async with _memory_db(db) as db:
            stmt = select(Memory).where(Memory.user_id == user_id)
            if not include_deleted:
                stmt = stmt.where(Memory.status != 'deleted')
            if not include_archived:
                stmt = stmt.where(Memory.status != 'archived')
            stmt = stmt.order_by(Memory.updated_at.desc(), Memory.id)
            result = await db.execute(stmt)
            return [MemoryModel.model_validate(memory) for memory in result.scalars().all()]

    async def search_memories(
        self,
        user_id: str,
        query: str | None = None,
        memory_type: str = 'all',
        status: str = 'active',
        skip: int = 0,
        limit: int = 50,
        db: AsyncSession | None = None,
    ) -> tuple[list[MemoryModel], int]:
        async with _memory_db(db) as db:
            stmt = select(Memory).where(Memory.user_id == user_id)
            if status != 'all':
                stmt = stmt.where(Memory.status == status)
            if memory_type != 'all':
                stmt = stmt.where(Memory.type == memory_type)
            if query:
                value = f'%{query.strip()}%'
                stmt = stmt.where(or_(Memory.content.ilike(value), Memory.path.ilike(value)))
            total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
            stmt = (
                stmt.order_by(Memory.updated_at.desc(), Memory.id).offset(max(0, skip)).limit(max(1, min(limit, 100)))
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [MemoryModel.model_validate(row) for row in rows], total

    async def get_memory_by_id(self, id: str, db: AsyncSession | None = None) -> MemoryModel | None:
        async with _memory_db(db) as db:
            memory = await db.get(Memory, id)
            return MemoryModel.model_validate(memory) if memory else None

    async def get_memory_revisions(
        self, memory_id: str, user_id: str, db: AsyncSession | None = None
    ) -> list[MemoryRevisionModel]:
        async with _memory_db(db) as db:
            memory = await db.get(Memory, memory_id)
            if not memory or memory.user_id != user_id:
                return []
            result = await db.execute(
                select(MemoryRevision)
                .where(MemoryRevision.memory_id == memory_id)
                .order_by(MemoryRevision.revision.desc())
            )
            return [MemoryRevisionModel.model_validate(row) for row in result.scalars().all()]

    async def restore_memory_revision(
        self, memory_id: str, revision: int, user_id: str, meta: dict | None = None, db: AsyncSession | None = None
    ) -> MemoryModel | None:
        async with _memory_db(db) as db:
            memory_result = await db.execute(
                select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update()
            )
            memory = memory_result.scalars().first()
            if not memory:
                return None
            result = await db.execute(
                select(MemoryRevision).where(MemoryRevision.memory_id == memory_id, MemoryRevision.revision == revision)
            )
            source = result.scalars().first()
            if not source:
                return None
            memory.content = source.content or ''
            memory.type = self.normalize_memory_type(source.memory_type)
            memory.path = source.path
            memory.status = 'active'
            memory.archived_at = None
            memory.deleted_at = None
            memory.normalized_hash = normalize_memory_hash(memory.content, memory.type, memory.path)
            memory.version = int(memory.version or 0) + 1
            memory.updated_at = int(time.time())
            await self._append_revision(
                db, memory, 'restore', {**(meta or {}), 'reason': f'Restored revision {revision}'}
            )
            await self._enqueue_sync_job(db, memory, 'upsert')
            await db.commit()
            return MemoryModel.model_validate(memory)

    async def delete_memory_by_id(self, id: str, db: AsyncSession | None = None) -> bool:
        async with _memory_db(db) as db:
            memory = await db.get(Memory, id)
            if not memory:
                return False
            return await self._soft_delete(db, memory, {'created_by': 'system'})

    async def delete_memories_by_user_id(self, user_id: str, db: AsyncSession | None = None) -> bool:
        async with _memory_db(db) as db:
            rows = (
                (await db.execute(select(Memory).where(Memory.user_id == user_id, Memory.status != 'deleted')))
                .scalars()
                .all()
            )
            for memory in rows:
                await self._soft_delete(db, memory, {'created_by': 'manual'}, commit=False)
            await db.commit()
            return True

    async def _soft_delete(self, db: AsyncSession, memory: Memory, meta: dict | None, commit: bool = True) -> bool:
        now = int(time.time())
        memory.status = 'deleted'
        memory.deleted_at = now
        memory.updated_at = now
        memory.version = int(memory.version or 0) + 1
        await self._append_revision(db, memory, 'remove', meta)
        await self._enqueue_sync_job(db, memory, 'delete')
        if commit:
            await db.commit()
        return True

    async def delete_memory_by_id_and_user_id(self, id: str, user_id: str, db: AsyncSession | None = None) -> bool:
        async with _memory_db(db) as db:
            result = await db.execute(
                select(Memory).where(Memory.id == id, Memory.user_id == user_id).with_for_update()
            )
            memory = result.scalars().first()
            if not memory or memory.status == 'deleted':
                return False
            return await self._soft_delete(db, memory, {'created_by': 'manual'})

    async def apply_memory_operations(
        self,
        user_id: str,
        operations: list[dict],
        db: AsyncSession | None = None,
    ) -> list[dict]:
        now = int(time.time())
        results: list[dict] = []
        async with _memory_db(db) as db:
            for operation in operations:
                action = operation.get('action')
                meta = operation.get('meta') or {}
                if action == 'add':
                    content = operation.get('content', '').strip()
                    memory_type = self.normalize_memory_type(operation.get('type'))
                    path = operation.get('path')
                    content_hash = normalize_memory_hash(content, memory_type, path)
                    existing = (
                        (
                            await db.execute(
                                select(Memory).where(
                                    Memory.user_id == user_id,
                                    Memory.normalized_hash == content_hash,
                                    Memory.status.in_(ACTIVE_MEMORY_STATUSES),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing:
                        results.append(
                            {
                                'action': action,
                                'status': 'skipped',
                                'memory': MemoryModel.model_validate(existing),
                                'reason': 'duplicate',
                            }
                        )
                        continue
                    memory = Memory(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        type=memory_type,
                        scope=meta.get('scope', 'long_term'),
                        kind=meta.get('kind', 'preference' if memory_type == 'user' else 'fact'),
                        path=path,
                        content=content,
                        normalized_hash=content_hash,
                        meta=meta,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(memory)
                    await db.flush()
                    await self._append_revision(db, memory, 'add', meta)
                    await self._enqueue_sync_job(db, memory, 'upsert')
                    results.append(
                        {'action': action, 'status': 'created', 'memory': MemoryModel.model_validate(memory)}
                    )
                elif action in {'replace', 'move'}:
                    memory_id = operation.get('id')
                    memory_result = await db.execute(
                        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update()
                    )
                    memory = memory_result.scalars().first()
                    if not memory or memory.status == 'deleted':
                        raise ValueError(f'Memory not found: {memory_id}')
                    expected_version = operation.get('expected_version')
                    if expected_version is not None and memory.version != expected_version:
                        raise MemoryConflictError(f'Memory changed: {memory_id}')
                    if action == 'replace':
                        memory.content = operation.get('content', '').strip()
                        if operation.get('type') is not None:
                            memory.type = self.normalize_memory_type(operation.get('type'))
                    if 'path' in operation:
                        memory.path = operation.get('path')
                    original_created_by = (memory.meta or {}).get('created_by')
                    memory.meta = {**(memory.meta or {}), **meta}
                    if original_created_by:
                        memory.meta['created_by'] = original_created_by
                    memory.normalized_hash = normalize_memory_hash(memory.content, memory.type, memory.path)
                    memory.updated_at = now
                    memory.version = int(memory.version or 0) + 1
                    await self._append_revision(db, memory, action, meta)
                    await self._enqueue_sync_job(db, memory, 'upsert')
                    results.append(
                        {'action': action, 'status': 'updated', 'memory': MemoryModel.model_validate(memory)}
                    )
                elif action == 'remove':
                    memory_id = operation.get('id')
                    memory_result = await db.execute(
                        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update()
                    )
                    memory = memory_result.scalars().first()
                    if not memory or memory.status == 'deleted':
                        raise ValueError(f'Memory not found: {memory_id}')
                    await self._soft_delete(db, memory, meta, commit=False)
                    results.append({'action': action, 'status': 'deleted', 'id': memory_id})
                else:
                    raise ValueError(f'Unsupported memory operation: {action}')
            await db.commit()
        return results

    async def create_proposals(
        self, user_id: str, operations: list[dict], metadata: dict | None = None, db: AsyncSession | None = None
    ) -> list[MemoryProposalModel]:
        metadata = metadata or {}
        now = int(time.time())
        async with _memory_db(db) as db:
            proposals = []
            for operation in operations:
                row = MemoryProposal(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    memory_id=operation.get('id'),
                    action=operation.get('action'),
                    payload={key: value for key, value in operation.items() if key != 'meta'},
                    status='pending',
                    confidence=operation.get('confidence'),
                    reason=operation.get('reason'),
                    chat_id=metadata.get('chat_id'),
                    message_id=metadata.get('message_id'),
                    model_id=metadata.get('model'),
                    created_at=now,
                    expires_at=now + 30 * 24 * 60 * 60,
                )
                db.add(row)
                proposals.append(row)
            await db.commit()
            return [MemoryProposalModel.model_validate(row) for row in proposals]

    async def get_proposals(self, user_id: str, status: str = 'pending', db: AsyncSession | None = None):
        async with _memory_db(db) as db:
            stmt = select(MemoryProposal).where(MemoryProposal.user_id == user_id)
            if status != 'all':
                stmt = stmt.where(MemoryProposal.status == status)
            result = await db.execute(stmt.order_by(MemoryProposal.created_at.desc()))
            return [MemoryProposalModel.model_validate(row) for row in result.scalars().all()]

    async def export_memory_bundle(self, user_id: str, db: AsyncSession | None = None) -> dict:
        """Export user-owned canonical memory data without exposing tenant IDs."""
        async with _memory_db(db) as db:
            memories = (
                (
                    await db.execute(
                        select(Memory).where(Memory.user_id == user_id).order_by(Memory.created_at, Memory.id)
                    )
                )
                .scalars()
                .all()
            )
            memory_ids = [memory.id for memory in memories]
            revisions = []
            evidence = []
            if memory_ids:
                revisions = (
                    (
                        await db.execute(
                            select(MemoryRevision)
                            .where(MemoryRevision.memory_id.in_(memory_ids))
                            .order_by(MemoryRevision.memory_id, MemoryRevision.revision)
                        )
                    )
                    .scalars()
                    .all()
                )
                evidence = (
                    (
                        await db.execute(
                            select(MemoryEvidence)
                            .where(MemoryEvidence.memory_id.in_(memory_ids))
                            .order_by(MemoryEvidence.memory_id, MemoryEvidence.observed_at)
                        )
                    )
                    .scalars()
                    .all()
                )
            profiles = (await db.execute(select(AgentProfile).where(AgentProfile.user_id == user_id))).scalars().all()
            profile_ids = [profile.id for profile in profiles]
            profile_revisions = []
            if profile_ids:
                profile_revisions = (
                    (
                        await db.execute(
                            select(AgentProfileRevision)
                            .where(AgentProfileRevision.profile_id.in_(profile_ids))
                            .order_by(AgentProfileRevision.profile_id, AgentProfileRevision.revision)
                        )
                    )
                    .scalars()
                    .all()
                )
            sessions = (
                (
                    await db.execute(
                        select(SessionMemoryState)
                        .where(SessionMemoryState.user_id == user_id)
                        .order_by(SessionMemoryState.updated_at, SessionMemoryState.id)
                    )
                )
                .scalars()
                .all()
            )

            def row_data(row, excluded: set[str] | None = None) -> dict:
                excluded = excluded or set()
                return {
                    column.name: getattr(row, column.name)
                    for column in row.__table__.columns
                    if column.name not in excluded
                }

            return {
                'schema_version': 1,
                'exported_at': int(time.time()),
                'memories': [row_data(memory, {'user_id'}) for memory in memories],
                'revisions': [row_data(revision) for revision in revisions],
                'evidence': [row_data(item) for item in evidence],
                'profiles': [row_data(profile, {'user_id'}) for profile in profiles],
                'profile_revisions': [row_data(revision) for revision in profile_revisions],
                'sessions': [row_data(session, {'user_id'}) for session in sessions],
            }

    @staticmethod
    def validate_memory_bundle(bundle: object, *, max_memories: int = 5000) -> dict:
        if not isinstance(bundle, dict) or bundle.get('schema_version') != 1:
            raise ValueError('Unsupported memory export schema')
        memories = bundle.get('memories')
        if not isinstance(memories, list) or len(memories) > max_memories:
            raise ValueError(f'Memory export must contain 0-{max_memories} memories')
        normalized = []
        for index, item in enumerate(memories):
            if not isinstance(item, dict):
                raise ValueError(f'Memory entry {index + 1} is invalid')
            content = item.get('content')
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f'Memory entry {index + 1} has no content')
            memory_type = item.get('type', 'context')
            if memory_type not in {'user', 'context'}:
                raise ValueError(f'Memory entry {index + 1} has an invalid type')
            path = item.get('path')
            if path is not None and not isinstance(path, str):
                raise ValueError(f'Memory entry {index + 1} has an invalid path')
            scope = item.get('scope', 'long_term')
            if scope not in {'session', 'working', 'long_term'}:
                raise ValueError(f'Memory entry {index + 1} has an invalid scope')
            kind = item.get('kind', 'fact')
            if not isinstance(kind, str) or not kind.strip() or len(kind) > 24:
                raise ValueError(f'Memory entry {index + 1} has an invalid kind')
            memory_status = item.get('status', 'active')
            if memory_status not in {'candidate', 'active', 'superseded', 'archived', 'deleted'}:
                raise ValueError(f'Memory entry {index + 1} has an invalid status')
            normalized.append(
                {
                    'content': content,
                    'type': memory_type,
                    'path': path,
                    'scope': scope,
                    'kind': kind,
                    'status': memory_status,
                    'structured_value': item.get('structured_value'),
                }
            )
        return {'schema_version': 1, 'memories': normalized}

    async def import_memory_bundle(
        self, user_id: str, bundle: object, *, dry_run: bool = False, db: AsyncSession | None = None
    ) -> dict:
        validated = self.validate_memory_bundle(bundle)
        memories = validated['memories']
        hashes = [normalize_memory_hash(item['content'], item['type'], item['path']) for item in memories]

        async with _memory_db(db) as db:
            existing_hashes: set[str] = set()
            if hashes:
                rows = await db.execute(
                    select(Memory.normalized_hash).where(
                        Memory.user_id == user_id,
                        Memory.normalized_hash.in_(hashes),
                        Memory.status.in_(ACTIVE_MEMORY_STATUSES),
                    )
                )
                existing_hashes = {value for value in rows.scalars().all() if value}

            pending: list[tuple[dict, str]] = []
            seen_hashes: set[str] = set()
            skipped = 0
            for item, content_hash in zip(memories, hashes, strict=True):
                if content_hash in existing_hashes or content_hash in seen_hashes:
                    skipped += 1
                    continue
                seen_hashes.add(content_hash)
                pending.append((item, content_hash))

            if dry_run:
                return {
                    'schema_version': 1,
                    'dry_run': True,
                    'total': len(memories),
                    'imported': len(pending),
                    'skipped': skipped,
                }

            imported = 0
            for item, _ in pending:
                memory = await self.insert_new_memory(
                    user_id,
                    item['content'],
                    memory_type=item['type'],
                    path=item['path'],
                    meta={
                        'created_by': 'import',
                        'scope': item['scope'],
                        'kind': item['kind'],
                        'structured_value': item['structured_value'],
                    },
                    db=db,
                )
                if memory is None:
                    skipped += 1
                    continue

                imported += 1
                if item['status'] == 'deleted':
                    await self.delete_memory_by_id_and_user_id(memory.id, user_id, db=db)
                elif item['status'] != 'active':
                    result = await db.execute(
                        select(Memory).where(Memory.id == memory.id, Memory.user_id == user_id).with_for_update()
                    )
                    record = result.scalars().first()
                    if record:
                        now = int(time.time())
                        record.status = item['status']
                        record.archived_at = now if item['status'] == 'archived' else None
                        record.updated_at = now
                        record.version = int(record.version or 0) + 1
                        await self._append_revision(
                            db,
                            record,
                            'import_status',
                            {'created_by': 'import', 'reason': f"Imported as {item['status']}"},
                        )
                        await self._enqueue_sync_job(
                            db,
                            record,
                            'upsert' if item['status'] == 'candidate' else 'delete',
                        )
                        await db.commit()

            return {
                'schema_version': 1,
                'dry_run': False,
                'total': len(memories),
                'imported': imported,
                'skipped': skipped,
            }

    async def review_proposal(
        self, proposal_id: str, user_id: str, approve: bool, db: AsyncSession | None = None
    ) -> tuple[MemoryProposalModel | None, list[dict]]:
        async with _memory_db(db) as db:
            proposal_status = 'approved' if approve else 'rejected'
            reviewed_at = int(time.time())
            claim = await db.execute(
                update(MemoryProposal)
                .where(
                    MemoryProposal.id == proposal_id,
                    MemoryProposal.user_id == user_id,
                    MemoryProposal.status == 'pending',
                )
                .values(status=proposal_status, reviewed_at=reviewed_at)
            )
            if claim.rowcount != 1:
                return None, []

            proposal = await db.get(MemoryProposal, proposal_id)
            db.add(
                MemoryAuditEvent(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    memory_id=proposal.memory_id,
                    proposal_id=proposal.id,
                    action=f'proposal_{proposal.status}',
                    actor_type='user',
                    actor_id=user_id,
                    created_at=int(time.time()),
                )
            )
            results = []
            if approve:
                payload = dict(proposal.payload or {})
                payload['meta'] = {
                    'created_by': 'background_review',
                    'updated_by': 'user_approval',
                    'chat_id': proposal.chat_id,
                    'message_id': proposal.message_id,
                    'model': proposal.model_id,
                }
                results = await self.apply_memory_operations(user_id, [payload], db=db)
            await db.commit()
            return MemoryProposalModel.model_validate(proposal), results

    async def get_or_create_profile(
        self, user_id: str, agent_id: str = 'default', db: AsyncSession | None = None
    ) -> AgentProfileModel:
        async with _memory_db(db) as db:
            result = await db.execute(
                select(AgentProfile).where(AgentProfile.user_id == user_id, AgentProfile.agent_id == agent_id)
            )
            profile = result.scalars().first()
            if not profile:
                now = int(time.time())
                profile = AgentProfile(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    agent_id=agent_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(profile)
                await db.commit()
            return AgentProfileModel.model_validate(profile)

    async def set_learning_paused(
        self, user_id: str, paused: bool, agent_id: str = 'default', db: AsyncSession | None = None
    ) -> AgentProfileModel:
        async with _memory_db(db) as db:
            profile = await self.get_or_create_profile(user_id, agent_id, db=db)
            row = await db.get(AgentProfile, profile.id)
            row.learning_paused = paused
            row.updated_at = int(time.time())
            await db.commit()
            return AgentProfileModel.model_validate(row)

    async def enqueue_job(
        self,
        *,
        user_id: str,
        job_type: str,
        idempotency_key: str,
        payload: dict | None = None,
        memory_id: str | None = None,
        available_at: int | None = None,
        db: AsyncSession | None = None,
    ) -> MemoryJobModel:
        async with _memory_db(db) as db:
            existing = (
                (await db.execute(select(MemoryJob).where(MemoryJob.idempotency_key == idempotency_key)))
                .scalars()
                .first()
            )
            if existing:
                return MemoryJobModel.model_validate(existing)
            now = int(time.time())
            job = MemoryJob(
                id=str(uuid.uuid4()),
                user_id=user_id,
                memory_id=memory_id,
                job_type=job_type,
                payload=payload,
                idempotency_key=idempotency_key,
                status='pending',
                available_at=available_at or now,
                created_at=now,
                updated_at=now,
            )
            db.add(job)
            await db.commit()
            return MemoryJobModel.model_validate(job)

    async def claim_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 120,
        db: AsyncSession | None = None,
    ) -> list[MemoryJobModel]:
        async with _memory_db(db) as db:
            now = int(time.time())
            stmt = (
                select(MemoryJob)
                .where(
                    MemoryJob.available_at <= now,
                    or_(
                        MemoryJob.status.in_(('pending', 'failed')),
                        (MemoryJob.status == 'running')
                        & (MemoryJob.lease_expires_at.is_not(None))
                        & (MemoryJob.lease_expires_at < now),
                    ),
                )
                .order_by(MemoryJob.available_at, MemoryJob.created_at)
                .limit(max(1, min(limit, 50)))
            )
            if db.bind and db.bind.dialect.name == 'postgresql':
                stmt = stmt.with_for_update(skip_locked=True)
            rows = (await db.execute(stmt)).scalars().all()
            for row in rows:
                row.status = 'running'
                row.lease_owner = worker_id
                row.lease_expires_at = now + lease_seconds
                row.attempt_count = int(row.attempt_count or 0) + 1
                row.updated_at = now
            await db.commit()
            return [MemoryJobModel.model_validate(row) for row in rows]

    async def complete_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        sync_memory: bool = False,
        db: AsyncSession | None = None,
    ) -> bool:
        async with _memory_db(db) as db:
            job = await db.get(MemoryJob, job_id)
            if not job or job.status != 'running' or job.lease_owner != worker_id:
                return False
            now = int(time.time())
            job.status = 'succeeded'
            job.completed_at = now
            job.updated_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            if sync_memory and job.memory_id:
                memory = await db.get(Memory, job.memory_id)
                expected_revision = (job.payload or {}).get('revision')
                if memory and (expected_revision is None or memory.current_revision == expected_revision):
                    memory.sync_status = 'synced'
            await db.commit()
            return True

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 5,
        db: AsyncSession | None = None,
    ) -> bool:
        async with _memory_db(db) as db:
            job = await db.get(MemoryJob, job_id)
            if not job or job.status != 'running' or job.lease_owner != worker_id:
                return False
            now = int(time.time())
            job.status = 'dead_letter' if job.attempt_count >= max_attempts else 'failed'
            job.available_at = now + min(3600, 2 ** min(job.attempt_count, 10))
            job.last_error = error[-4000:]
            job.updated_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            if job.memory_id:
                memory = await db.get(Memory, job.memory_id)
                expected_revision = (job.payload or {}).get('revision')
                if memory and (expected_revision is None or memory.current_revision == expected_revision):
                    memory.sync_status = 'failed'
            await db.commit()
            return True

    async def mark_sync_status(
        self, memory_id: str, status: str, error: str | None = None, db: AsyncSession | None = None
    ) -> None:
        async with _memory_db(db) as db:
            memory = await db.get(Memory, memory_id)
            if memory:
                memory.sync_status = status
            result = await db.execute(
                select(MemoryJob).where(
                    MemoryJob.memory_id == memory_id,
                    MemoryJob.status.in_(('pending', 'running', 'failed')),
                )
            )
            now = int(time.time())
            for job in result.scalars().all():
                job.status = 'succeeded' if status == 'synced' else 'failed'
                job.last_error = error
                job.updated_at = now
                job.completed_at = now if status == 'synced' else None
            await db.commit()


Memories = MemoriesTable()
