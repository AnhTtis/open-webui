"""Durable, per-user memory storage and lifecycle records."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from open_webui.internal.db import Base, get_async_db_context
from open_webui.utils.memory_limits import (
    LIVE_MEMORY_STATUSES,
    MEMORY_DELETE_BATCH,
    MEMORY_EXPORT_BATCH,
    MEMORY_EXTRACTION_BYTE_CAP,
    MEMORY_EXTRACTION_ROW_CAP,
    MEMORY_JSON_IMPORT_MAX_MEMORIES,
    MEMORY_MAX_CONTENT_BYTES,
    MEMORY_MAX_PATH_BYTES,
    MEMORY_MAX_STRUCTURED_BYTES,
    MEMORY_PROMPT_BYTE_CAP,
    MEMORY_PROMPT_ROW_CAP,
    MEMORY_REINDEX_BATCH,
    NON_DELETED_MEMORY_STATUSES,
    RECALLABLE_MEMORY_STATUSES,
    clamp_page_size,
    get_memory_quota_limits,
    utf8_bytes,
)
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
    and_,
    case,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ACTIVE_MEMORY_STATUSES = LIVE_MEMORY_STATUSES


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
        Index('ix_memory_user_session', 'user_id', 'session_id'),
        Index('ix_memory_user_chat', 'user_id', 'chat_id'),
        Index(
            'uq_memory_user_live_hash',
            'user_id',
            'normalized_hash',
            unique=True,
            sqlite_where=text("status IN ('active', 'candidate') AND normalized_hash IS NOT NULL"),
            postgresql_where=text("status IN ('active', 'candidate') AND normalized_hash IS NOT NULL"),
        ),
        CheckConstraint("scope IN ('session', 'working', 'long_term')", name='ck_memory_scope'),
        CheckConstraint(
            "status IN ('candidate', 'active', 'superseded', 'archived', 'deleted')",
            name='ck_memory_status',
        ),
        CheckConstraint('confidence >= 0 AND confidence <= 1', name='ck_memory_confidence'),
        CheckConstraint('trust >= 0 AND trust <= 1', name='ck_memory_trust'),
        CheckConstraint('importance >= 0 AND importance <= 1', name='ck_memory_importance'),
        CheckConstraint('version >= 1', name='ck_memory_version'),
        CheckConstraint('content_bytes >= 0', name='ck_memory_content_bytes'),
    )

    id = Column(String, primary_key=True, unique=True)
    user_id = Column(String, ForeignKey('user.id', ondelete='CASCADE'), index=True, nullable=False)
    agent_profile_id = Column(Text, nullable=True)
    type = Column(String, default='context', server_default='context', index=True)
    scope = Column(String(20), nullable=False, default='long_term', server_default='long_term')
    kind = Column(String(24), nullable=False, default='fact', server_default='fact')
    path = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    content_bytes = Column(Integer, nullable=False, default=0, server_default='0')
    structured_value = Column(JSON, nullable=True)
    normalized_hash = Column(String(64), nullable=True)
    session_id = Column(Text, nullable=True)
    chat_id = Column(Text, nullable=True)
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
    __table_args__ = (
        Index('ix_memory_proposal_user_status', 'user_id', 'status', 'created_at'),
        Index('ix_memory_proposal_idempotency', 'user_id', 'idempotency_key', unique=True),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    memory_id = Column(Text, ForeignKey('memory.id', ondelete='SET NULL'), nullable=True)
    action = Column(String(20), nullable=False)
    payload = Column(JSON, nullable=False)
    idempotency_key = Column(String(255), nullable=True)
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
    claim_token = Column(Text, nullable=True)
    lease_generation = Column(Integer, nullable=False, default=0, server_default='0')
    heartbeat_at = Column(BigInteger, nullable=True)
    started_at = Column(BigInteger, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)


class MemoryUsage(Base):
    __tablename__ = 'memory_usage'
    __table_args__ = (
        CheckConstraint('counted_items >= 0', name='ck_memory_usage_items'),
        CheckConstraint('counted_content_bytes >= 0', name='ck_memory_usage_bytes'),
    )

    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), primary_key=True)
    counted_items = Column(Integer, nullable=False, default=0, server_default='0')
    counted_content_bytes = Column(BigInteger, nullable=False, default=0, server_default='0')
    updated_at = Column(BigInteger, nullable=False)


class MemoryVectorGeneration(Base):
    __tablename__ = 'memory_vector_generation'
    __table_args__ = (
        UniqueConstraint('user_id', 'generation', name='uq_memory_vector_generation'),
        Index('ix_memory_vector_generation_user_status', 'user_id', 'status'),
        CheckConstraint("status IN ('building', 'active', 'retired')", name='ck_memory_vector_generation_status'),
        CheckConstraint('generation >= 1', name='ck_memory_vector_generation_number'),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    generation = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default='building', server_default='building')
    collection_name = Column(Text, nullable=False)
    embedding_fingerprint = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    activated_at = Column(BigInteger, nullable=True)
    retired_at = Column(BigInteger, nullable=True)


class MemoryVectorManifest(Base):
    __tablename__ = 'memory_vector_manifest'
    __table_args__ = (
        UniqueConstraint('user_id', 'memory_id', 'revision', 'generation', name='uq_memory_vector_manifest'),
        Index('ix_memory_vector_manifest_user_memory', 'user_id', 'memory_id'),
        Index('ix_memory_vector_manifest_doc', 'vector_doc_id'),
        CheckConstraint("status IN ('pending', 'synced', 'stale', 'deleted')", name='ck_memory_vector_manifest_status'),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    memory_id = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False)
    generation = Column(Integer, nullable=False)
    vector_doc_id = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    updated_at = Column(BigInteger, nullable=False)


class MemoryAccountCleanup(Base):
    """Derivative vector cleanup that outlives the user row. No FK to user."""

    __tablename__ = 'memory_account_cleanup'
    __table_args__ = (
        UniqueConstraint('user_id', name='uq_memory_account_cleanup_user'),
        Index('ix_memory_account_cleanup_status', 'status', 'available_at'),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name='ck_memory_account_cleanup_status'
        ),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    collections = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    attempt_count = Column(Integer, nullable=False, default=0, server_default='0')
    available_at = Column(BigInteger, nullable=False)
    lease_owner = Column(Text, nullable=True)
    lease_expires_at = Column(BigInteger, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)


class MemoryTransfer(Base):
    __tablename__ = 'memory_transfer'
    __table_args__ = (
        Index('ix_memory_transfer_user_created', 'user_id', 'created_at'),
        Index('ix_memory_transfer_status_expires', 'status', 'expires_at'),
        CheckConstraint("direction IN ('import', 'export')", name='ck_memory_transfer_direction'),
        CheckConstraint("format IN ('json_v1', 'ndjson_v2')", name='ck_memory_transfer_format'),
        CheckConstraint(
            "status IN ('staging', 'validating', 'publishing', 'succeeded', 'failed', 'expired')",
            name='ck_memory_transfer_status',
        ),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    direction = Column(String(16), nullable=False)
    format = Column(String(16), nullable=False)
    status = Column(String(20), nullable=False, default='staging', server_default='staging')
    dry_run = Column(Boolean, nullable=False, default=False, server_default='0')
    checksum = Column(String(64), nullable=True)
    total_bytes = Column(BigInteger, nullable=False, default=0, server_default='0')
    total_records = Column(Integer, nullable=False, default=0, server_default='0')
    processed_records = Column(Integer, nullable=False, default=0, server_default='0')
    error_summary = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=True)
    completed_at = Column(BigInteger, nullable=True)


class MemoryTransferRecord(Base):
    __tablename__ = 'memory_transfer_record'
    __table_args__ = (
        UniqueConstraint('transfer_id', 'record_index', name='uq_memory_transfer_record_index'),
        Index('ix_memory_transfer_record_transfer', 'transfer_id', 'record_index'),
        CheckConstraint(
            "status IN ('pending', 'valid', 'invalid', 'published')", name='ck_memory_transfer_record_status'
        ),
    )

    id = Column(Text, primary_key=True)
    transfer_id = Column(Text, ForeignKey('memory_transfer.id', ondelete='CASCADE'), nullable=False)
    record_index = Column(Integer, nullable=False)
    record_type = Column(String(32), nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    error = Column(Text, nullable=True)


class MemoryTransferIdMap(Base):
    __tablename__ = 'memory_transfer_id_map'
    __table_args__ = (
        UniqueConstraint('transfer_id', 'entity_type', 'source_id', name='uq_memory_transfer_id_map'),
        Index('ix_memory_transfer_id_map_transfer', 'transfer_id', 'entity_type'),
    )

    id = Column(Text, primary_key=True)
    transfer_id = Column(Text, ForeignKey('memory_transfer.id', ondelete='CASCADE'), nullable=False)
    entity_type = Column(String(32), nullable=False)
    source_id = Column(Text, nullable=False)
    target_id = Column(Text, nullable=False)


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
    content_bytes: int = 0
    structured_value: dict | list | str | int | float | bool | None = None
    normalized_hash: str | None = None
    session_id: str | None = None
    chat_id: str | None = None
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
    idempotency_key: str | None = None
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
    claim_token: str | None = None
    lease_generation: int = 0
    heartbeat_at: int | None = None
    started_at: int | None = None
    last_error: str | None = None
    created_at: int
    updated_at: int
    completed_at: int | None = None
    model_config = ConfigDict(from_attributes=True)


class MemoryConflictError(ValueError):
    code = 'memory_conflict'


class MemoryQuotaExceeded(ValueError):
    code = 'memory_quota_exceeded'

    def __init__(
        self,
        message: str = 'Memory quota exceeded',
        *,
        counted_items: int = 0,
        counted_bytes: int = 0,
        max_items: int = 0,
        max_bytes: int = 0,
        items_delta: int = 0,
        bytes_delta: int = 0,
    ):
        super().__init__(message)
        self.counted_items = counted_items
        self.counted_bytes = counted_bytes
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.items_delta = items_delta
        self.bytes_delta = bytes_delta

    def as_dict(self) -> dict:
        return {
            'code': self.code,
            'message': str(self),
            'counted_items': self.counted_items,
            'counted_bytes': self.counted_bytes,
            'max_items': self.max_items,
            'max_bytes': self.max_bytes,
        }


class MemoryRequestTooLarge(ValueError):
    code = 'memory_request_too_large'


def memory_vector_doc_id(generation: int, memory_id: str, revision: int) -> str:
    return f'mem:{generation}:{memory_id}:r{revision}'


def memory_collection_name(user_id: str, generation: int | None = None) -> str:
    if generation is None or generation <= 1:
        return f'user-memory-{user_id}'
    return f'user-memory-{user_id}-g{generation}'


def parse_memory_vector_doc_id(doc_id: str) -> tuple[int | None, str, int | None]:
    """Return (generation, memory_id, revision) from a vector document id."""
    value = str(doc_id or '')
    if value.startswith('mem:'):
        parts = value.split(':')
        if len(parts) >= 4 and parts[3].startswith('r'):
            try:
                return int(parts[1]), parts[2], int(parts[3][1:])
            except ValueError:
                return None, value, None
        if len(parts) >= 3:
            return None, parts[2] if len(parts) > 2 else value, None
    return None, value, None


class MemoriesTable:
    @staticmethod
    def normalize_memory_type(memory_type: str | None = None) -> str:
        return 'user' if memory_type == 'user' else 'context'

    @asynccontextmanager
    async def _session(self, db: AsyncSession | None = None):
        if db is not None:
            yield db, False
        else:
            async with get_async_db_context() as session:
                yield session, True

    async def _commit_if_owned(self, db: AsyncSession, owned: bool) -> None:
        if owned:
            await db.commit()

    @staticmethod
    def _structured_bytes(value) -> int:
        if value is None:
            return 0
        if isinstance(value, (bytes, bytearray)):
            return len(value)
        if isinstance(value, str):
            return utf8_bytes(value)
        try:
            from open_webui.utils.json_codec import JSONCodec

            return utf8_bytes(JSONCodec.dumps(value, separators=(',', ':')))
        except Exception:
            return utf8_bytes(str(value))

    def _validate_item_caps(self, content: str, path: str | None, structured_value=None) -> None:
        if utf8_bytes(content) > MEMORY_MAX_CONTENT_BYTES:
            raise MemoryRequestTooLarge('Memory content exceeds the server byte cap')
        if path is not None and utf8_bytes(path) > MEMORY_MAX_PATH_BYTES:
            raise MemoryRequestTooLarge('Memory path exceeds the server byte cap')
        if structured_value is not None and self._structured_bytes(structured_value) > MEMORY_MAX_STRUCTURED_BYTES:
            raise MemoryRequestTooLarge('Memory structured value exceeds the server byte cap')

    def _apply_content_fields(self, memory: Memory, content: str, memory_type: str, path: str | None, meta: dict | None = None) -> None:
        memory.content = content
        memory.type = memory_type
        memory.path = path
        memory.content_bytes = utf8_bytes(content)
        memory.normalized_hash = normalize_memory_hash(content, memory_type, path)
        if meta:
            if isinstance(meta.get('session_id'), str):
                memory.session_id = meta.get('session_id')
            if isinstance(meta.get('chat_id'), str):
                memory.chat_id = meta.get('chat_id')

    async def _ensure_usage_tx(self, db: AsyncSession, user_id: str) -> MemoryUsage:
        result = await db.execute(select(MemoryUsage).where(MemoryUsage.user_id == user_id).with_for_update())
        usage = result.scalars().first()
        if usage:
            return usage
        usage = MemoryUsage(user_id=user_id, counted_items=0, counted_content_bytes=0, updated_at=int(time.time()))
        nested = await db.begin_nested()
        try:
            db.add(usage)
            await db.flush()
        except IntegrityError:
            await nested.rollback()
            result = await db.execute(select(MemoryUsage).where(MemoryUsage.user_id == user_id).with_for_update())
            usage = result.scalars().first()
            if not usage:
                raise
        else:
            await nested.commit()
        return usage

    async def _reserve_quota_tx(self, db: AsyncSession, user_id: str, items_delta: int, bytes_delta: int) -> MemoryUsage:
        usage = await self._ensure_usage_tx(db, user_id)
        max_items, max_bytes = await get_memory_quota_limits()
        next_items = int(usage.counted_items or 0) + items_delta
        next_bytes = int(usage.counted_content_bytes or 0) + bytes_delta
        if items_delta > 0 or bytes_delta > 0:
            if next_items > max_items or next_bytes > max_bytes:
                raise MemoryQuotaExceeded(
                    'Memory quota exceeded',
                    counted_items=int(usage.counted_items or 0),
                    counted_bytes=int(usage.counted_content_bytes or 0),
                    max_items=max_items,
                    max_bytes=max_bytes,
                    items_delta=items_delta,
                    bytes_delta=bytes_delta,
                )
        usage.counted_items = max(0, next_items)
        usage.counted_content_bytes = max(0, next_bytes)
        usage.updated_at = int(time.time())
        return usage

    async def _get_live_by_hash_tx(self, db: AsyncSession, user_id: str, content_hash: str) -> Memory | None:
        result = await db.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.normalized_hash == content_hash,
                Memory.status.in_(ACTIVE_MEMORY_STATUSES),
            )
        )
        return result.scalars().first()

    async def _get_or_create_active_generation_tx(
        self, db: AsyncSession, user_id: str, fingerprint: str | None = None
    ) -> MemoryVectorGeneration:
        result = await db.execute(
            select(MemoryVectorGeneration)
            .where(MemoryVectorGeneration.user_id == user_id, MemoryVectorGeneration.status == 'active')
            .order_by(MemoryVectorGeneration.generation.desc())
        )
        generation = result.scalars().first()
        if generation:
            return generation
        now = int(time.time())
        generation = MemoryVectorGeneration(
            id=str(uuid.uuid4()),
            user_id=user_id,
            generation=1,
            status='active',
            collection_name=memory_collection_name(user_id, 1),
            embedding_fingerprint=fingerprint,
            created_at=now,
            activated_at=now,
        )
        db.add(generation)
        await db.flush()
        return generation

    def _eligible_recall_clause(self, now: int | None = None, session_id: str | None = None, chat_id: str | None = None):
        now = int(time.time()) if now is None else now
        clauses = [
            Memory.status.in_(RECALLABLE_MEMORY_STATUSES),
            or_(Memory.expires_at.is_(None), Memory.expires_at > now),
            or_(Memory.valid_to.is_(None), Memory.valid_to > now),
            or_(Memory.valid_from.is_(None), Memory.valid_from <= now),
        ]
        scope_ok = [Memory.scope.in_(('working', 'long_term'))]
        if session_id:
            scope_ok.append(and_(Memory.scope == 'session', Memory.session_id == session_id))
        elif chat_id:
            scope_ok.append(and_(Memory.scope == 'session', Memory.chat_id == chat_id))
        clauses.append(or_(*scope_ok))
        return and_(*clauses)

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

    async def _enqueue_sync_job(
        self,
        db: AsyncSession,
        memory: Memory,
        operation: str,
        *,
        run_token: str | None = None,
    ) -> None:
        now = int(time.time())
        memory.sync_status = 'pending'
        generation = await self._get_or_create_active_generation_tx(db, memory.user_id)
        building = (
            await db.execute(
                select(MemoryVectorGeneration).where(
                    MemoryVectorGeneration.user_id == memory.user_id,
                    MemoryVectorGeneration.status == 'building',
                )
            )
        ).scalars().first()
        generations = [generation] + ([building] if building and building.id != generation.id else [])
        job_type = 'delete_embedding' if operation == 'delete' else 'upsert_embedding'
        for target in generations:
            doc_id = memory_vector_doc_id(target.generation, memory.id, memory.current_revision)
            token = run_token or 'mutation'
            idempotency_key = (
                f'memory:{memory.id}:revision:{memory.current_revision}:{operation}:g{target.generation}:{token}'
            )
            existing_manifest = (
                await db.execute(
                    select(MemoryVectorManifest).where(
                        MemoryVectorManifest.user_id == memory.user_id,
                        MemoryVectorManifest.memory_id == memory.id,
                        MemoryVectorManifest.revision == memory.current_revision,
                        MemoryVectorManifest.generation == target.generation,
                    )
                )
            ).scalars().first()
            if existing_manifest:
                existing_manifest.vector_doc_id = doc_id
                existing_manifest.status = 'pending'
                existing_manifest.updated_at = now
            else:
                nested = await db.begin_nested()
                try:
                    db.add(
                        MemoryVectorManifest(
                            id=str(uuid.uuid4()),
                            user_id=memory.user_id,
                            memory_id=memory.id,
                            revision=memory.current_revision,
                            generation=target.generation,
                            vector_doc_id=doc_id,
                            status='pending',
                            updated_at=now,
                        )
                    )
                    await db.flush()
                except IntegrityError:
                    await nested.rollback()
                else:
                    await nested.commit()
            await self._enqueue_job_tx(
                db,
                user_id=memory.user_id,
                job_type=job_type,
                idempotency_key=idempotency_key,
                payload={
                    'revision': memory.current_revision,
                    'generation': target.generation,
                    'vector_doc_id': doc_id,
                    'collection_name': target.collection_name,
                    'operation': operation,
                    'run_token': token,
                },
                memory_id=memory.id,
            )

    async def _enqueue_job_tx(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        job_type: str,
        idempotency_key: str,
        payload: dict | None = None,
        memory_id: str | None = None,
        available_at: int | None = None,
    ) -> MemoryJob:
        existing = (
            (await db.execute(select(MemoryJob).where(MemoryJob.idempotency_key == idempotency_key)))
            .scalars()
            .first()
        )
        if existing:
            if existing.user_id != user_id or existing.job_type != job_type or (memory_id and existing.memory_id != memory_id):
                raise MemoryConflictError('Memory job idempotency key is already owned by a different job')
            return existing
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
        nested = await db.begin_nested()
        try:
            db.add(job)
            await db.flush()
        except IntegrityError:
            await nested.rollback()
            existing = (
                (await db.execute(select(MemoryJob).where(MemoryJob.idempotency_key == idempotency_key)))
                .scalars()
                .first()
            )
            if not existing:
                raise
            if existing.user_id != user_id or existing.job_type != job_type:
                raise MemoryConflictError('Memory job idempotency key is already owned by a different job')
            return existing
        else:
            await nested.commit()
        return job

    async def insert_new_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str | None = None,
        path: str | None = None,
        meta: dict | None = None,
        db: AsyncSession | None = None,
    ) -> MemoryModel | None:
        async with self._session(db) as (session, owned):
            record, _status = await self._insert_new_memory_tx(
                session, user_id, content, memory_type=memory_type, path=path, meta=meta
            )
            await self._commit_if_owned(session, owned)
            return MemoryModel.model_validate(record) if record else None

    async def _insert_new_memory_tx(
        self,
        db: AsyncSession,
        user_id: str,
        content: str,
        memory_type: str | None = None,
        path: str | None = None,
        meta: dict | None = None,
        memory_id: str | None = None,
        status: str = 'active',
        append_revision: bool = True,
        enqueue_sync: bool = True,
    ) -> tuple[Memory | None, str]:
        now = int(time.time())
        memory_type = self.normalize_memory_type(memory_type)
        meta = meta or {}
        structured_value = meta.get('structured_value')
        self._validate_item_caps(content, path, structured_value)
        content_hash = normalize_memory_hash(content, memory_type, path)
        existing = await self._get_live_by_hash_tx(db, user_id, content_hash)
        if existing:
            return existing, 'duplicate'

        content_size = utf8_bytes(content)
        if status != 'deleted':
            await self._reserve_quota_tx(db, user_id, 1, content_size)

        record = Memory(
            id=memory_id or str(uuid.uuid4()),
            user_id=user_id,
            type=memory_type,
            scope=meta.get('scope', 'long_term'),
            kind=meta.get('kind', 'preference' if memory_type == 'user' else 'fact'),
            path=path,
            content=content,
            content_bytes=content_size,
            structured_value=structured_value,
            normalized_hash=content_hash,
            status=status,
            session_id=meta.get('session_id') if isinstance(meta.get('session_id'), str) else None,
            chat_id=meta.get('chat_id') if isinstance(meta.get('chat_id'), str) else None,
            meta=meta,
            created_at=now,
            updated_at=now,
        )
        nested = await db.begin_nested()
        try:
            db.add(record)
            await db.flush()
        except IntegrityError:
            await nested.rollback()
            if status != 'deleted':
                await self._reserve_quota_tx(db, user_id, -1, -content_size)
            existing = await self._get_live_by_hash_tx(db, user_id, content_hash)
            if existing:
                return existing, 'duplicate'
            raise
        else:
            await nested.commit()

        if append_revision:
            await self._append_revision(db, record, 'add', meta)
        if enqueue_sync:
            await self._enqueue_sync_job(db, record, 'upsert' if status in ACTIVE_MEMORY_STATUSES else 'delete')
        return record, 'created'

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
        async with self._session(db) as (session, owned):
            memory = await self._update_memory_tx(
                session,
                id,
                user_id,
                content=content,
                memory_type=memory_type,
                path=path,
                update_path=update_path,
                meta=meta,
                expected_version=expected_version,
            )
            await self._commit_if_owned(session, owned)
            return MemoryModel.model_validate(memory) if memory else None

    async def _update_memory_tx(
        self,
        db: AsyncSession,
        id: str,
        user_id: str,
        *,
        content: str | None,
        memory_type: str | None = None,
        path: str | None = None,
        update_path: bool = False,
        meta: dict | None = None,
        expected_version: int | None = None,
        action: str = 'replace',
    ) -> Memory | None:
        result = await db.execute(select(Memory).where(Memory.id == id, Memory.user_id == user_id).with_for_update())
        memory = result.scalars().first()
        if not memory or memory.status == 'deleted':
            return None
        if expected_version is not None and memory.version != expected_version:
            raise MemoryConflictError('Memory was changed by another request')

        previous_bytes = int(memory.content_bytes or utf8_bytes(memory.content))
        previous_hash = memory.normalized_hash
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
            if isinstance(meta.get('session_id'), str):
                memory.session_id = meta.get('session_id')
            if isinstance(meta.get('chat_id'), str):
                memory.chat_id = meta.get('chat_id')
            if meta.get('structured_value') is not None:
                memory.structured_value = meta.get('structured_value')
        memory.content_bytes = utf8_bytes(memory.content)
        memory.normalized_hash = normalize_memory_hash(memory.content, memory.type, memory.path)
        self._validate_item_caps(memory.content, memory.path, memory.structured_value)
        if memory.status in ACTIVE_MEMORY_STATUSES and memory.normalized_hash != previous_hash:
            duplicate = await self._get_live_by_hash_tx(db, user_id, memory.normalized_hash)
            if duplicate and duplicate.id != memory.id:
                raise MemoryConflictError('A live memory with the same content already exists')
        byte_delta = int(memory.content_bytes) - previous_bytes
        if memory.status != 'deleted' and byte_delta:
            await self._reserve_quota_tx(db, user_id, 0, byte_delta)
        memory.updated_at = int(time.time())
        memory.version = int(memory.version or 0) + 1
        await self._append_revision(db, memory, action, meta)
        await self._enqueue_sync_job(db, memory, 'upsert')
        return memory

    async def get_memories(self, db: AsyncSession | None = None, *, after_id: str | None = None, limit: int = MEMORY_REINDEX_BATCH) -> list[MemoryModel]:
        """Bounded installation-wide scan. Prefer per-user coordinators in production."""
        async with self._session(db) as (session, _owned):
            stmt = select(Memory).where(Memory.status != 'deleted').order_by(Memory.id).limit(max(1, min(limit, MEMORY_REINDEX_BATCH)))
            if after_id:
                stmt = stmt.where(Memory.id > after_id)
            result = await session.execute(stmt)
            return [MemoryModel.model_validate(memory) for memory in result.scalars().all()]

    async def get_memories_by_user_id(
        self,
        user_id: str,
        db: AsyncSession | None = None,
        include_archived: bool = False,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int | None = None,
        cursor: str | None = None,
        cursor_updated_at: int | None = None,
    ) -> list[MemoryModel]:
        page, _total, _next = await self.list_memories_page(
            user_id,
            db=db,
            include_archived=include_archived,
            include_deleted=include_deleted,
            skip=skip,
            limit=limit if limit is not None else clamp_page_size(None),
            cursor=cursor,
            cursor_updated_at=cursor_updated_at,
        )
        return page

    async def list_memories_page(
        self,
        user_id: str,
        db: AsyncSession | None = None,
        include_archived: bool = False,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int | None = None,
        cursor: str | None = None,
        cursor_updated_at: int | None = None,
        status: str | None = None,
        memory_type: str | None = None,
        query: str | None = None,
        path: str | None = None,
        memory_id: str | None = None,
        eligible_only: bool = False,
        session_id: str | None = None,
        chat_id: str | None = None,
    ) -> tuple[list[MemoryModel], int, str | None]:
        page_size = clamp_page_size(limit)
        async with self._session(db) as (session, _owned):
            stmt = select(Memory).where(Memory.user_id == user_id)
            if memory_id:
                stmt = stmt.where(Memory.id == memory_id)
            if status and status != 'all':
                stmt = stmt.where(Memory.status == status)
            else:
                if not include_deleted:
                    stmt = stmt.where(Memory.status != 'deleted')
                if not include_archived:
                    stmt = stmt.where(Memory.status != 'archived')
            if memory_type and memory_type != 'all':
                stmt = stmt.where(Memory.type == memory_type)
            if query:
                value = f'%{query.strip()}%'
                stmt = stmt.where(or_(Memory.content.ilike(value), Memory.path.ilike(value)))
            if path:
                stmt = stmt.where(or_(Memory.path == path, Memory.path.startswith(f'{path}/')))
            if eligible_only:
                stmt = stmt.where(self._eligible_recall_clause(session_id=session_id, chat_id=chat_id))
            if cursor and cursor_updated_at is not None:
                stmt = stmt.where(
                    or_(
                        Memory.updated_at < cursor_updated_at,
                        and_(Memory.updated_at == cursor_updated_at, Memory.id < cursor),
                    )
                )
            total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
            stmt = stmt.order_by(Memory.updated_at.desc(), Memory.id.desc())
            if not cursor:
                stmt = stmt.offset(max(0, skip))
            rows = (await session.execute(stmt.limit(page_size + 1))).scalars().all()
            next_cursor = None
            if len(rows) > page_size:
                rows = rows[:page_size]
                next_cursor = rows[-1].id if rows else None
            return [MemoryModel.model_validate(row) for row in rows], total, next_cursor

    async def search_memories(
        self,
        user_id: str,
        query: str | None = None,
        memory_type: str = 'all',
        status: str = 'active',
        skip: int = 0,
        limit: int = 50,
        db: AsyncSession | None = None,
        path: str | None = None,
        memory_id: str | None = None,
    ) -> tuple[list[MemoryModel], int]:
        page, total, _next = await self.list_memories_page(
            user_id,
            db=db,
            include_archived=status in {'archived', 'all'},
            include_deleted=status in {'deleted', 'all'},
            skip=skip,
            limit=limit,
            status=status,
            memory_type=memory_type,
            query=query,
            path=path,
            memory_id=memory_id,
        )
        return page, total

    async def user_has_memories(self, user_id: str, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, _owned):
            result = await session.execute(
                select(Memory.id).where(Memory.user_id == user_id, Memory.status != 'deleted').limit(1)
            )
            return result.scalar() is not None

    async def get_memories_by_ids(
        self,
        user_id: str,
        memory_ids: list[str],
        db: AsyncSession | None = None,
        eligible_only: bool = True,
        session_id: str | None = None,
        chat_id: str | None = None,
    ) -> list[MemoryModel]:
        if not memory_ids:
            return []
        async with self._session(db) as (session, _owned):
            stmt = select(Memory).where(Memory.user_id == user_id, Memory.id.in_(memory_ids[:200]))
            if eligible_only:
                stmt = stmt.where(self._eligible_recall_clause(session_id=session_id, chat_id=chat_id))
            rows = (await session.execute(stmt)).scalars().all()
            by_id = {row.id: MemoryModel.model_validate(row) for row in rows}
            return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]

    async def get_prompt_memories(
        self,
        user_id: str,
        db: AsyncSession | None = None,
        session_id: str | None = None,
        chat_id: str | None = None,
        row_cap: int = MEMORY_PROMPT_ROW_CAP,
        byte_cap: int = MEMORY_PROMPT_BYTE_CAP,
    ) -> list[MemoryModel]:
        async with self._session(db) as (session, _owned):
            stmt = (
                select(Memory)
                .where(Memory.user_id == user_id, self._eligible_recall_clause(session_id=session_id, chat_id=chat_id))
                .order_by(Memory.importance.desc(), Memory.updated_at.desc(), Memory.id.desc())
                .limit(max(1, min(row_cap, MEMORY_PROMPT_ROW_CAP)))
            )
            rows = (await session.execute(stmt)).scalars().all()
            selected: list[MemoryModel] = []
            used = 0
            for row in rows:
                size = int(row.content_bytes or utf8_bytes(row.content))
                if selected and used + size > byte_cap:
                    break
                selected.append(MemoryModel.model_validate(row))
                used += size
            return selected

    async def get_extraction_snapshot(
        self,
        user_id: str,
        db: AsyncSession | None = None,
        row_cap: int = MEMORY_EXTRACTION_ROW_CAP,
        byte_cap: int = MEMORY_EXTRACTION_BYTE_CAP,
    ) -> list[MemoryModel]:
        async with self._session(db) as (session, _owned):
            stmt = (
                select(Memory)
                .where(Memory.user_id == user_id, Memory.status.in_(ACTIVE_MEMORY_STATUSES))
                .order_by(Memory.updated_at.desc(), Memory.id.desc())
                .limit(max(1, min(row_cap, MEMORY_EXTRACTION_ROW_CAP)))
            )
            rows = (await session.execute(stmt)).scalars().all()
            selected: list[MemoryModel] = []
            used = 0
            for row in rows:
                size = int(row.content_bytes or utf8_bytes(row.content))
                if selected and used + size > byte_cap:
                    break
                selected.append(MemoryModel.model_validate(row))
                used += size
            return selected

    async def iter_user_memory_batches(
        self,
        user_id: str,
        db: AsyncSession | None = None,
        batch_size: int = MEMORY_REINDEX_BATCH,
        statuses: tuple[str, ...] | None = None,
    ):
        after_updated_at = None
        after_id = None
        page_size = max(1, min(batch_size, MEMORY_REINDEX_BATCH))
        while True:
            async with self._session(db) as (session, _owned):
                stmt = select(Memory).where(Memory.user_id == user_id)
                if statuses:
                    stmt = stmt.where(Memory.status.in_(statuses))
                if after_id is not None and after_updated_at is not None:
                    stmt = stmt.where(
                        or_(
                            Memory.updated_at < after_updated_at,
                            and_(Memory.updated_at == after_updated_at, Memory.id < after_id),
                        )
                    )
                rows = (
                    (await session.execute(stmt.order_by(Memory.updated_at.desc(), Memory.id.desc()).limit(page_size)))
                    .scalars()
                    .all()
                )
            if not rows:
                break
            yield [MemoryModel.model_validate(row) for row in rows]
            after_updated_at = rows[-1].updated_at
            after_id = rows[-1].id
            if len(rows) < page_size:
                break

    async def iter_users_with_memories(self, after_user_id: str | None = None, limit: int = 100, db: AsyncSession | None = None):
        async with self._session(db) as (session, _owned):
            stmt = select(Memory.user_id).where(Memory.status != 'deleted').distinct().order_by(Memory.user_id).limit(max(1, min(limit, 500)))
            if after_user_id:
                stmt = stmt.where(Memory.user_id > after_user_id)
            return list((await session.execute(stmt)).scalars().all())

    async def get_memory_by_id(self, id: str, db: AsyncSession | None = None) -> MemoryModel | None:
        async with self._session(db) as (session, _owned):
            memory = await session.get(Memory, id)
            return MemoryModel.model_validate(memory) if memory else None

    async def get_memory_by_id_and_user_id(
        self, id: str, user_id: str, db: AsyncSession | None = None
    ) -> MemoryModel | None:
        async with self._session(db) as (session, _owned):
            result = await session.execute(select(Memory).where(Memory.id == id, Memory.user_id == user_id))
            memory = result.scalars().first()
            return MemoryModel.model_validate(memory) if memory else None

    async def get_memory_revisions(
        self,
        memory_id: str,
        user_id: str,
        db: AsyncSession | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> list[MemoryRevisionModel]:
        page, _total = await self.get_memory_revisions_page(memory_id, user_id, db=db, skip=skip, limit=limit)
        return page

    async def get_memory_revisions_page(
        self,
        memory_id: str,
        user_id: str,
        db: AsyncSession | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> tuple[list[MemoryRevisionModel], int]:
        page_size = clamp_page_size(limit)
        async with self._session(db) as (session, _owned):
            memory = await session.get(Memory, memory_id)
            if not memory or memory.user_id != user_id:
                return [], 0
            total = (
                await session.execute(
                    select(func.count()).select_from(MemoryRevision).where(MemoryRevision.memory_id == memory_id)
                )
            ).scalar() or 0
            result = await session.execute(
                select(MemoryRevision)
                .where(MemoryRevision.memory_id == memory_id)
                .order_by(MemoryRevision.revision.desc())
                .offset(max(0, skip))
                .limit(page_size)
            )
            return [MemoryRevisionModel.model_validate(row) for row in result.scalars().all()], total

    async def restore_memory_revision(
        self, memory_id: str, revision: int, user_id: str, meta: dict | None = None, db: AsyncSession | None = None
    ) -> MemoryModel | None:
        async with self._session(db) as (session, owned):
            memory = await self._restore_memory_revision_tx(session, memory_id, revision, user_id, meta=meta)
            await self._commit_if_owned(session, owned)
            return MemoryModel.model_validate(memory) if memory else None

    async def _restore_memory_revision_tx(
        self, db: AsyncSession, memory_id: str, revision: int, user_id: str, meta: dict | None = None
    ) -> Memory | None:
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
        was_deleted = memory.status == 'deleted'
        previous_bytes = int(memory.content_bytes or utf8_bytes(memory.content))
        memory.content = source.content or ''
        memory.type = self.normalize_memory_type(source.memory_type)
        memory.path = source.path
        memory.status = 'active'
        memory.archived_at = None
        memory.deleted_at = None
        memory.content_bytes = utf8_bytes(memory.content)
        memory.normalized_hash = normalize_memory_hash(memory.content, memory.type, memory.path)
        self._validate_item_caps(memory.content, memory.path, memory.structured_value)
        duplicate = await self._get_live_by_hash_tx(db, user_id, memory.normalized_hash)
        if duplicate and duplicate.id != memory.id:
            raise MemoryConflictError('A live memory with the same content already exists')
        if was_deleted:
            await self._reserve_quota_tx(db, user_id, 1, int(memory.content_bytes))
        else:
            await self._reserve_quota_tx(db, user_id, 0, int(memory.content_bytes) - previous_bytes)
        memory.version = int(memory.version or 0) + 1
        memory.updated_at = int(time.time())
        await self._append_revision(db, memory, 'restore', {**(meta or {}), 'reason': f'Restored revision {revision}'})
        await self._enqueue_sync_job(db, memory, 'upsert')
        return memory

    async def delete_memory_by_id(self, id: str, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, owned):
            memory = await session.get(Memory, id)
            if not memory:
                return False
            await self._soft_delete_tx(session, memory, {'created_by': 'system'})
            await self._commit_if_owned(session, owned)
            return True

    async def delete_memories_by_user_id(self, user_id: str, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, owned):
            after_id = ''
            while True:
                rows = (
                    (
                        await session.execute(
                            select(Memory)
                            .where(Memory.user_id == user_id, Memory.status != 'deleted', Memory.id > after_id)
                            .order_by(Memory.id)
                            .limit(MEMORY_DELETE_BATCH)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    break
                for memory in rows:
                    await self._soft_delete_tx(session, memory, {'created_by': 'manual'})
                after_id = rows[-1].id
            await self._commit_if_owned(session, owned)
            return True

    async def _soft_delete_tx(self, db: AsyncSession, memory: Memory, meta: dict | None) -> bool:
        if memory.status == 'deleted':
            return False
        now = int(time.time())
        await self._reserve_quota_tx(db, memory.user_id, -1, -int(memory.content_bytes or utf8_bytes(memory.content)))
        memory.status = 'deleted'
        memory.deleted_at = now
        memory.updated_at = now
        memory.version = int(memory.version or 0) + 1
        await self._append_revision(db, memory, 'remove', meta)
        await self._enqueue_sync_job(db, memory, 'delete')
        return True

    async def delete_memory_by_id_and_user_id(self, id: str, user_id: str, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, owned):
            result = await session.execute(
                select(Memory).where(Memory.id == id, Memory.user_id == user_id).with_for_update()
            )
            memory = result.scalars().first()
            if not memory or memory.status == 'deleted':
                return False
            await self._soft_delete_tx(session, memory, {'created_by': 'manual'})
            await self._commit_if_owned(session, owned)
            return True

    async def apply_memory_operations(
        self,
        user_id: str,
        operations: list[dict],
        db: AsyncSession | None = None,
    ) -> list[dict]:
        async with self._session(db) as (session, owned):
            results = await self._apply_memory_operations_tx(session, user_id, operations)
            await self._commit_if_owned(session, owned)
            return results

    async def _apply_memory_operations_tx(
        self,
        db: AsyncSession,
        user_id: str,
        operations: list[dict],
    ) -> list[dict]:
        results: list[dict] = []
        for operation in operations:
            action = operation.get('action')
            meta = operation.get('meta') or {}
            if action == 'add':
                memory, status = await self._insert_new_memory_tx(
                    db,
                    user_id,
                    operation.get('content', '').strip(),
                    memory_type=operation.get('type'),
                    path=operation.get('path'),
                    meta=meta,
                )
                if status == 'duplicate':
                    results.append(
                        {
                            'action': action,
                            'status': 'skipped',
                            'memory': MemoryModel.model_validate(memory),
                            'reason': 'duplicate',
                        }
                    )
                else:
                    results.append({'action': action, 'status': 'created', 'memory': MemoryModel.model_validate(memory)})
            elif action in {'replace', 'move'}:
                memory_id = operation.get('id')
                memory = await self._update_memory_tx(
                    db,
                    memory_id,
                    user_id,
                    content=operation.get('content', '').strip() if action == 'replace' else None,
                    memory_type=operation.get('type') if action == 'replace' else None,
                    path=operation.get('path') if 'path' in operation else None,
                    update_path='path' in operation,
                    meta=meta,
                    expected_version=operation.get('expected_version'),
                    action=action,
                )
                if not memory:
                    raise ValueError(f'Memory not found: {memory_id}')
                results.append({'action': action, 'status': 'updated', 'memory': MemoryModel.model_validate(memory)})
            elif action == 'remove':
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
                await self._soft_delete_tx(db, memory, meta)
                results.append({'action': action, 'status': 'deleted', 'id': memory_id})
            else:
                raise ValueError(f'Unsupported memory operation: {action}')
        return results

    async def create_proposals(
        self, user_id: str, operations: list[dict], metadata: dict | None = None, db: AsyncSession | None = None
    ) -> list[MemoryProposalModel]:
        metadata = metadata or {}
        now = int(time.time())
        async with self._session(db) as (session, owned):
            target_ids = [operation.get('id') for operation in operations if operation.get('id')]
            targets = {}
            if target_ids:
                rows = (
                    (
                        await session.execute(
                            select(Memory).where(Memory.user_id == user_id, Memory.id.in_(target_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                targets = {row.id: row for row in rows}

            proposals = []
            for index, operation in enumerate(operations):
                action = operation.get('action')
                memory_id = operation.get('id')
                if action in {'replace', 'move', 'remove'}:
                    target = targets.get(memory_id)
                    if not target:
                        continue
                    operation = {**operation, 'expected_version': target.version}
                payload = {key: value for key, value in operation.items() if key != 'meta'}
                idempotency_key = metadata.get('idempotency_prefix')
                if idempotency_key:
                    op_hash = hashlib.sha256(
                        f'{action}:{memory_id or ""}:{index}:{payload.get("content") or ""}'.encode()
                    ).hexdigest()[:32]
                    idempotency_key = f'{idempotency_key}:{op_hash}'
                    existing = (
                        (
                            await session.execute(
                                select(MemoryProposal).where(
                                    MemoryProposal.user_id == user_id,
                                    MemoryProposal.idempotency_key == idempotency_key,
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing:
                        proposals.append(existing)
                        continue
                row = MemoryProposal(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    memory_id=memory_id,
                    action=action,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    status='pending',
                    confidence=operation.get('confidence'),
                    reason=operation.get('reason'),
                    chat_id=metadata.get('chat_id'),
                    message_id=metadata.get('message_id'),
                    model_id=metadata.get('model'),
                    created_at=now,
                    expires_at=now + 30 * 24 * 60 * 60,
                )
                session.add(row)
                proposals.append(row)
            await self._commit_if_owned(session, owned)
            return [MemoryProposalModel.model_validate(row) for row in proposals]

    async def get_proposals(
        self,
        user_id: str,
        status: str = 'pending',
        db: AsyncSession | None = None,
        skip: int = 0,
        limit: int | None = None,
    ):
        page, _total = await self.get_proposals_page(user_id, status=status, db=db, skip=skip, limit=limit)
        return page

    async def get_proposals_page(
        self,
        user_id: str,
        status: str = 'pending',
        db: AsyncSession | None = None,
        skip: int = 0,
        limit: int | None = None,
    ) -> tuple[list[MemoryProposalModel], int]:
        page_size = clamp_page_size(limit)
        async with self._session(db) as (session, _owned):
            stmt = select(MemoryProposal).where(MemoryProposal.user_id == user_id)
            if status != 'all':
                stmt = stmt.where(MemoryProposal.status == status)
            total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
            result = await session.execute(
                stmt.order_by(MemoryProposal.created_at.desc()).offset(max(0, skip)).limit(page_size)
            )
            return [MemoryProposalModel.model_validate(row) for row in result.scalars().all()], total

    async def export_memory_bundle(self, user_id: str, db: AsyncSession | None = None) -> dict:
        """Export user-owned canonical memory data without exposing tenant IDs."""
        async with self._session(db) as (db, _owned):
            memories = []
            after_id = ''
            while True:
                batch = (
                    (
                        await db.execute(
                            select(Memory)
                            .where(Memory.user_id == user_id, Memory.id > after_id)
                            .order_by(Memory.id)
                            .limit(MEMORY_EXPORT_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not batch:
                    break
                memories.extend(batch)
                after_id = batch[-1].id
                if len(memories) > MEMORY_JSON_IMPORT_MAX_MEMORIES:
                    raise MemoryRequestTooLarge('Memory export exceeds the JSON v1 memory cap')
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
                    'id': item.get('id') if isinstance(item.get('id'), str) else None,
                    'content': content,
                    'type': memory_type,
                    'path': path,
                    'scope': scope,
                    'kind': kind,
                    'status': memory_status,
                    'structured_value': item.get('structured_value'),
                    'session_id': item.get('session_id') if isinstance(item.get('session_id'), str) else None,
                    'chat_id': item.get('chat_id') if isinstance(item.get('chat_id'), str) else None,
                }
            )

        def _rows(key: str) -> list[dict]:
            value = bundle.get(key)
            if value is None:
                return []
            if not isinstance(value, list):
                raise ValueError(f'Memory export {key} must be a list')
            for entry in value:
                if not isinstance(entry, dict):
                    raise ValueError(f'Memory export {key} contains an invalid entry')
            return value

        return {
            'schema_version': 1,
            'memories': normalized,
            'revisions': _rows('revisions'),
            'evidence': _rows('evidence'),
            'profiles': _rows('profiles'),
            'profile_revisions': _rows('profile_revisions'),
            'sessions': _rows('sessions'),
        }

    async def import_memory_bundle(
        self, user_id: str, bundle: object, *, dry_run: bool = False, db: AsyncSession | None = None
    ) -> dict:
        validated = self.validate_memory_bundle(bundle, max_memories=MEMORY_JSON_IMPORT_MAX_MEMORIES)
        memories = validated['memories']
        hashes = [normalize_memory_hash(item['content'], item['type'], item['path']) for item in memories]

        async with self._session(db) as (db, owned):
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
            id_map: dict[str, str] = {}
            try:
                for item, _ in pending:
                    record, status = await self._insert_new_memory_tx(
                        db,
                        user_id,
                        item['content'],
                        memory_type=item['type'],
                        path=item['path'],
                        meta={
                            'created_by': 'import',
                            'scope': item['scope'],
                            'kind': item['kind'],
                            'structured_value': item['structured_value'],
                            'session_id': item.get('session_id'),
                            'chat_id': item.get('chat_id'),
                        },
                    )
                    if record is None or status == 'duplicate':
                        skipped += 1
                        continue
                    imported += 1
                    if item.get('id'):
                        id_map[item['id']] = record.id
                    if item['status'] == 'deleted':
                        await self._soft_delete_tx(db, record, {'created_by': 'import'})
                    elif item['status'] != 'active':
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
                await self._import_related_rows_tx(db, user_id, validated, id_map)
                await self._commit_if_owned(db, owned)
            except Exception:
                if owned:
                    await db.rollback()
                raise

            return {
                'schema_version': 1,
                'dry_run': False,
                'total': len(memories),
                'imported': imported,
                'skipped': skipped,
            }

    async def _import_related_rows_tx(
        self, db: AsyncSession, user_id: str, validated: dict, id_map: dict[str, str]
    ) -> None:
        revision_id_map: dict[str, str] = {}
        profile_id_map: dict[str, str] = {}
        now = int(time.time())

        async def _savepoint_add(row) -> bool:
            nested = await db.begin_nested()
            try:
                db.add(row)
                await db.flush()
            except IntegrityError:
                await nested.rollback()
                return False
            await nested.commit()
            return True

        for item in validated.get('revisions') or []:
            source_memory_id = item.get('memory_id')
            memory_id = id_map.get(source_memory_id) if isinstance(source_memory_id, str) else None
            if not memory_id:
                continue
            existing = (
                await db.execute(
                    select(MemoryRevision.id).where(
                        MemoryRevision.memory_id == memory_id,
                        MemoryRevision.revision == int(item.get('revision') or 1),
                    )
                )
            ).scalar()
            if existing:
                if isinstance(item.get('id'), str):
                    revision_id_map[item['id']] = existing
                continue
            new_id = str(uuid.uuid4())
            if await _savepoint_add(
                MemoryRevision(
                    id=new_id,
                    memory_id=memory_id,
                    revision=int(item.get('revision') or 1),
                    action=str(item.get('action') or 'import'),
                    content=item.get('content'),
                    structured_value=item.get('structured_value'),
                    memory_type=item.get('memory_type'),
                    path=item.get('path'),
                    status=str(item.get('status') or 'active'),
                    reason=item.get('reason'),
                    actor_type=str(item.get('actor_type') or 'import'),
                    actor_id=item.get('actor_id'),
                    source=str(item.get('source') or 'import'),
                    chat_id=item.get('chat_id'),
                    message_id=item.get('message_id'),
                    model_id=item.get('model_id'),
                    extractor_version=item.get('extractor_version'),
                    created_at=int(item.get('created_at') or now),
                )
            ) and isinstance(item.get('id'), str):
                revision_id_map[item['id']] = new_id

        for item in validated.get('evidence') or []:
            source_memory_id = item.get('memory_id')
            memory_id = id_map.get(source_memory_id) if isinstance(source_memory_id, str) else None
            source_revision_id = item.get('revision_id')
            revision_id = revision_id_map.get(source_revision_id) if isinstance(source_revision_id, str) else None
            if not memory_id or not revision_id:
                continue
            await _savepoint_add(
                MemoryEvidence(
                    id=str(uuid.uuid4()),
                    memory_id=memory_id,
                    revision_id=revision_id,
                    chat_id=item.get('chat_id'),
                    message_id=item.get('message_id'),
                    session_id=item.get('session_id'),
                    source_excerpt=item.get('source_excerpt'),
                    source_hash=item.get('source_hash'),
                    source_role=item.get('source_role'),
                    actor_type=str(item.get('actor_type') or 'import'),
                    tool_name=item.get('tool_name'),
                    model_id=item.get('model_id'),
                    extraction_run_id=item.get('extraction_run_id'),
                    observed_at=int(item.get('observed_at') or now),
                )
            )

        for item in validated.get('profiles') or []:
            agent_id = str(item.get('agent_id') or 'default')
            existing = (
                await db.execute(
                    select(AgentProfile).where(AgentProfile.user_id == user_id, AgentProfile.agent_id == agent_id)
                )
            ).scalars().first()
            source_id = item.get('id') if isinstance(item.get('id'), str) else None
            if existing:
                if source_id:
                    profile_id_map[source_id] = existing.id
                continue
            new_id = str(uuid.uuid4())
            if source_id:
                profile_id_map[source_id] = new_id
            await _savepoint_add(
                AgentProfile(
                    id=new_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    current_revision=int(item.get('current_revision') or 0),
                    learning_paused=bool(item.get('learning_paused')),
                    created_at=int(item.get('created_at') or now),
                    updated_at=int(item.get('updated_at') or now),
                )
            )

        for item in validated.get('profile_revisions') or []:
            source_profile_id = item.get('profile_id')
            profile_id = profile_id_map.get(source_profile_id) if isinstance(source_profile_id, str) else None
            if not profile_id:
                continue
            await _savepoint_add(
                AgentProfileRevision(
                    id=str(uuid.uuid4()),
                    profile_id=profile_id,
                    revision=int(item.get('revision') or 1),
                    narrative=item.get('narrative'),
                    traits=item.get('traits'),
                    preferences=item.get('preferences'),
                    boundaries=item.get('boundaries'),
                    active_goals=item.get('active_goals'),
                    source_run_id=item.get('source_run_id'),
                    created_at=int(item.get('created_at') or now),
                )
            )

        for item in validated.get('sessions') or []:
            source_profile_id = item.get('agent_profile_id')
            profile_id = profile_id_map.get(source_profile_id) if isinstance(source_profile_id, str) else None
            await _savepoint_add(
                SessionMemoryState(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    agent_profile_id=profile_id,
                    chat_id=str(item.get('chat_id') or ''),
                    session_id=str(item.get('session_id') or str(uuid.uuid4())),
                    rolling_summary=item.get('rolling_summary'),
                    current_goals=item.get('current_goals'),
                    open_loops=item.get('open_loops'),
                    active_entities=item.get('active_entities'),
                    transcript_cursor=item.get('transcript_cursor'),
                    last_checkpoint_message_id=item.get('last_checkpoint_message_id'),
                    checkpoint_status=str(item.get('checkpoint_status') or 'pending'),
                    expires_at=item.get('expires_at'),
                    created_at=int(item.get('created_at') or now),
                    updated_at=int(item.get('updated_at') or now),
                )
            )

    async def review_proposal(
        self, proposal_id: str, user_id: str, approve: bool, db: AsyncSession | None = None
    ) -> tuple[MemoryProposalModel | None, list[dict]]:
        async with self._session(db) as (session, owned):
            proposal_status = 'approved' if approve else 'rejected'
            reviewed_at = int(time.time())
            claim = await session.execute(
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

            proposal = await session.get(MemoryProposal, proposal_id)
            session.add(
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
            try:
                if approve:
                    payload = dict(proposal.payload or {})
                    payload['meta'] = {
                        'created_by': 'background_review',
                        'updated_by': 'user_approval',
                        'chat_id': proposal.chat_id,
                        'message_id': proposal.message_id,
                        'model': proposal.model_id,
                    }
                    results = await self._apply_memory_operations_tx(session, user_id, [payload])
                await self._commit_if_owned(session, owned)
            except MemoryQuotaExceeded:
                proposal.status = 'pending'
                proposal.reviewed_at = None
                if owned:
                    await session.commit()
                raise
            except MemoryConflictError:
                proposal.status = 'conflicted'
                if owned:
                    await session.commit()
                raise
            return MemoryProposalModel.model_validate(proposal), results

    async def get_or_create_profile(
        self, user_id: str, agent_id: str = 'default', db: AsyncSession | None = None
    ) -> AgentProfileModel:
        async with self._session(db) as (session, owned):
            result = await session.execute(
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
                session.add(profile)
                await self._commit_if_owned(session, owned)
            return AgentProfileModel.model_validate(profile)

    async def set_learning_paused(
        self, user_id: str, paused: bool, agent_id: str = 'default', db: AsyncSession | None = None
    ) -> AgentProfileModel:
        async with self._session(db) as (session, owned):
            profile = await self.get_or_create_profile(user_id, agent_id, db=session)
            row = await session.get(AgentProfile, profile.id)
            row.learning_paused = paused
            row.updated_at = int(time.time())
            await self._commit_if_owned(session, owned)
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
        async with self._session(db) as (session, owned):
            job = await self._enqueue_job_tx(
                session,
                user_id=user_id,
                job_type=job_type,
                idempotency_key=idempotency_key,
                payload=payload,
                memory_id=memory_id,
                available_at=available_at,
            )
            await self._commit_if_owned(session, owned)
            return MemoryJobModel.model_validate(job)

    async def claim_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 120,
        db: AsyncSession | None = None,
    ) -> list[MemoryJobModel]:
        async with self._session(db) as (session, owned):
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
            bind = session.get_bind() if hasattr(session, 'get_bind') else getattr(session, 'bind', None)
            dialect_name = getattr(getattr(bind, 'dialect', None), 'name', None)
            if dialect_name == 'postgresql':
                stmt = stmt.with_for_update(skip_locked=True)
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                row.status = 'running'
                row.lease_owner = worker_id
                row.lease_expires_at = now + lease_seconds
                row.attempt_count = int(row.attempt_count or 0) + 1
                row.updated_at = now
                row.started_at = now
                row.heartbeat_at = now
                row.lease_generation = int(row.lease_generation or 0) + 1
                row.claim_token = secrets.token_hex(16)
            await self._commit_if_owned(session, owned)
            return [MemoryJobModel.model_validate(row) for row in rows]

    def _job_claim_matches(self, job: MemoryJob, worker_id: str, claim_token: str | None, lease_generation: int | None) -> bool:
        if not job or job.status != 'running' or job.lease_owner != worker_id:
            return False
        if claim_token is not None and job.claim_token and job.claim_token != claim_token:
            return False
        if lease_generation is not None and int(job.lease_generation or 0) != int(lease_generation):
            return False
        return True

    async def complete_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        sync_memory: bool = False,
        claim_token: str | None = None,
        lease_generation: int | None = None,
        db: AsyncSession | None = None,
    ) -> bool:
        async with self._session(db) as (session, owned):
            job = await session.get(MemoryJob, job_id)
            if not self._job_claim_matches(job, worker_id, claim_token, lease_generation):
                return False
            now = int(time.time())
            job.status = 'succeeded'
            job.completed_at = now
            job.updated_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            if sync_memory and job.memory_id:
                await self._finalize_sync_tx(session, job, 'synced')
            await self._commit_if_owned(session, owned)
            return True

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 5,
        claim_token: str | None = None,
        lease_generation: int | None = None,
        db: AsyncSession | None = None,
    ) -> bool:
        async with self._session(db) as (session, owned):
            job = await session.get(MemoryJob, job_id)
            if not self._job_claim_matches(job, worker_id, claim_token, lease_generation):
                return False
            now = int(time.time())
            job.status = 'dead_letter' if job.attempt_count >= max_attempts else 'failed'
            job.available_at = now + min(3600, 2 ** min(job.attempt_count, 10))
            job.last_error = error[-4000:]
            job.updated_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            if job.memory_id:
                await self._finalize_sync_tx(session, job, 'failed')
            await self._commit_if_owned(session, owned)
            return True

    async def heartbeat_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        claim_token: str | None = None,
        lease_generation: int | None = None,
        lease_seconds: int = 120,
        db: AsyncSession | None = None,
    ) -> bool:
        async with self._session(db) as (session, owned):
            job = await session.get(MemoryJob, job_id)
            if not self._job_claim_matches(job, worker_id, claim_token, lease_generation):
                return False
            now = int(time.time())
            job.heartbeat_at = now
            job.lease_expires_at = now + lease_seconds
            job.updated_at = now
            await self._commit_if_owned(session, owned)
            return True

    async def _finalize_sync_tx(self, db: AsyncSession, job: MemoryJob, status: str) -> None:
        payload = job.payload or {}
        expected_revision = payload.get('revision')
        generation = payload.get('generation')
        result = await db.execute(
            select(Memory).where(Memory.id == job.memory_id, Memory.user_id == job.user_id).with_for_update()
        )
        memory = result.scalars().first()
        if memory and (expected_revision is None or memory.current_revision == expected_revision):
            memory.sync_status = status
        if generation is not None and expected_revision is not None:
            manifest = (
                await db.execute(
                    select(MemoryVectorManifest).where(
                        MemoryVectorManifest.user_id == job.user_id,
                        MemoryVectorManifest.memory_id == job.memory_id,
                        MemoryVectorManifest.revision == expected_revision,
                        MemoryVectorManifest.generation == generation,
                    )
                )
            ).scalars().first()
            if manifest:
                manifest.status = 'synced' if status == 'synced' else 'stale'
                manifest.updated_at = int(time.time())

    async def mark_sync_status(
        self, memory_id: str, status: str, error: str | None = None, db: AsyncSession | None = None
    ) -> None:
        async with self._session(db) as (session, owned):
            memory = await session.get(Memory, memory_id)
            if memory:
                memory.sync_status = status
            result = await session.execute(
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
            await self._commit_if_owned(session, owned)

    async def account_cleanup_pending(self, user_id: str, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, _owned):
            row = (
                await session.execute(
                    select(MemoryAccountCleanup.id).where(
                        MemoryAccountCleanup.user_id == user_id,
                        MemoryAccountCleanup.status.in_(('pending', 'running')),
                    )
                )
            ).scalar()
            return row is not None

    async def insert_account_cleanup_tx(
        self, db: AsyncSession, user_id: str, collections: list[str] | None = None
    ) -> MemoryAccountCleanup:
        existing = (
            await db.execute(select(MemoryAccountCleanup).where(MemoryAccountCleanup.user_id == user_id).with_for_update())
        ).scalars().first()
        now = int(time.time())
        names = collections or [memory_collection_name(user_id)]
        generations = (
            await db.execute(select(MemoryVectorGeneration).where(MemoryVectorGeneration.user_id == user_id))
        ).scalars().all()
        for generation in generations:
            if generation.collection_name not in names:
                names.append(generation.collection_name)
        if existing:
            existing.collections = list({*(existing.collections or []), *names})
            if existing.status == 'succeeded':
                existing.status = 'pending'
                existing.completed_at = None
                existing.available_at = now
            existing.updated_at = now
            return existing
        row = MemoryAccountCleanup(
            id=str(uuid.uuid4()),
            user_id=user_id,
            collections=names,
            status='pending',
            attempt_count=0,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        return row

    async def cancel_user_jobs_tx(self, db: AsyncSession, user_id: str) -> None:
        now = int(time.time())
        await db.execute(
            update(MemoryJob)
            .where(MemoryJob.user_id == user_id, MemoryJob.status.in_(('pending', 'running', 'failed')))
            .values(status='dead_letter', updated_at=now, last_error='account_deleted', lease_owner=None, lease_expires_at=None)
        )

    async def get_user_summary(self, user_id: str, db: AsyncSession | None = None) -> dict:
        async with self._session(db) as (session, _owned):
            usage = (await session.execute(select(MemoryUsage).where(MemoryUsage.user_id == user_id))).scalars().first()
            status_rows = (
                await session.execute(
                    select(Memory.status, func.count()).where(Memory.user_id == user_id).group_by(Memory.status)
                )
            ).all()
            sync_rows = (
                await session.execute(
                    select(Memory.sync_status, func.count())
                    .where(Memory.user_id == user_id, Memory.status != 'deleted')
                    .group_by(Memory.sync_status)
                )
            ).all()
            pending_proposals = (
                await session.execute(
                    select(func.count())
                    .select_from(MemoryProposal)
                    .where(MemoryProposal.user_id == user_id, MemoryProposal.status == 'pending')
                )
            ).scalar() or 0
            max_items, max_bytes = await get_memory_quota_limits()
            return {
                'counted_items': int(usage.counted_items) if usage else 0,
                'counted_content_bytes': int(usage.counted_content_bytes) if usage else 0,
                'max_items': max_items,
                'max_content_bytes': max_bytes,
                'status_counts': {status: count for status, count in status_rows},
                'sync_counts': {status: count for status, count in sync_rows},
                'pending_proposals': pending_proposals,
            }

    async def get_admin_health(self, db: AsyncSession | None = None) -> dict:
        async with self._session(db) as (session, _owned):
            now = int(time.time())
            status_rows = (await session.execute(select(Memory.status, func.count()).group_by(Memory.status))).all()
            job_rows = (await session.execute(select(MemoryJob.status, func.count()).group_by(MemoryJob.status))).all()
            expired_leases = (
                await session.execute(
                    select(func.count())
                    .select_from(MemoryJob)
                    .where(MemoryJob.status == 'running', MemoryJob.lease_expires_at.is_not(None), MemoryJob.lease_expires_at < now)
                )
            ).scalar() or 0
            oldest_pending = (
                await session.execute(
                    select(func.min(MemoryJob.available_at)).where(MemoryJob.status.in_(('pending', 'failed')))
                )
            ).scalar()
            cleanup_rows = (
                await session.execute(select(MemoryAccountCleanup.status, func.count()).group_by(MemoryAccountCleanup.status))
            ).all()
            transfer_rows = (
                await session.execute(select(MemoryTransfer.status, func.count()).group_by(MemoryTransfer.status))
            ).all()
            generation_rows = (
                await session.execute(
                    select(MemoryVectorGeneration.status, func.count()).group_by(MemoryVectorGeneration.status)
                )
            ).all()
            quota_buckets = (
                await session.execute(
                    select(
                        case((MemoryUsage.counted_items == 0, 'empty'), else_='in_use'),
                        func.count(),
                    ).group_by(case((MemoryUsage.counted_items == 0, 'empty'), else_='in_use'))
                )
            ).all()
            return {
                'memory_status_counts': {status: count for status, count in status_rows},
                'job_status_counts': {status: count for status, count in job_rows},
                'expired_leases': expired_leases,
                'oldest_pending_age_seconds': (now - int(oldest_pending)) if oldest_pending else 0,
                'cleanup_status_counts': {status: count for status, count in cleanup_rows},
                'transfer_status_counts': {status: count for status, count in transfer_rows},
                'generation_status_counts': {status: count for status, count in generation_rows},
                'quota_utilization_buckets': {status: count for status, count in quota_buckets},
            }

    async def start_reindex_generation(self, user_id: str, fingerprint: str | None = None, db: AsyncSession | None = None) -> dict:
        async with self._session(db) as (session, owned):
            active = await self._get_or_create_active_generation_tx(session, user_id, fingerprint)
            latest = (
                await session.execute(
                    select(func.max(MemoryVectorGeneration.generation)).where(MemoryVectorGeneration.user_id == user_id)
                )
            ).scalar() or active.generation
            now = int(time.time())
            run_token = secrets.token_hex(8)
            building = MemoryVectorGeneration(
                id=str(uuid.uuid4()),
                user_id=user_id,
                generation=int(latest) + 1,
                status='building',
                collection_name=memory_collection_name(user_id, int(latest) + 1),
                embedding_fingerprint=fingerprint,
                created_at=now,
            )
            session.add(building)
            await session.flush()
            queued = 0
            async for batch in self.iter_user_memory_batches(user_id, db=session, statuses=ACTIVE_MEMORY_STATUSES):
                for memory in batch:
                    row = await session.get(Memory, memory.id)
                    if not row:
                        continue
                    await self._enqueue_sync_job(session, row, 'upsert', run_token=f'reindex:{run_token}')
                    queued += 1
            await self._commit_if_owned(session, owned)
            return {
                'run_token': run_token,
                'generation': building.generation,
                'collection_name': building.collection_name,
                'queued': queued,
            }

    async def activate_generation(self, user_id: str, generation: int, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, owned):
            building = (
                await session.execute(
                    select(MemoryVectorGeneration).where(
                        MemoryVectorGeneration.user_id == user_id,
                        MemoryVectorGeneration.generation == generation,
                        MemoryVectorGeneration.status == 'building',
                    ).with_for_update()
                )
            ).scalars().first()
            if not building:
                return False
            now = int(time.time())
            actives = (
                await session.execute(
                    select(MemoryVectorGeneration)
                    .where(MemoryVectorGeneration.user_id == user_id, MemoryVectorGeneration.status == 'active')
                    .with_for_update()
                )
            ).scalars().all()
            for active in actives:
                active.status = 'retired'
                active.retired_at = now
            building.status = 'active'
            building.activated_at = now
            await self._commit_if_owned(session, owned)
            return True

    async def get_active_generation(self, user_id: str, db: AsyncSession | None = None) -> MemoryVectorGeneration | None:
        async with self._session(db) as (session, _owned):
            return (
                await session.execute(
                    select(MemoryVectorGeneration)
                    .where(MemoryVectorGeneration.user_id == user_id, MemoryVectorGeneration.status == 'active')
                    .order_by(MemoryVectorGeneration.generation.desc())
                )
            ).scalars().first()

    async def claim_account_cleanup(
        self, worker_id: str, *, limit: int = 5, lease_seconds: int = 180, db: AsyncSession | None = None
    ) -> list[MemoryAccountCleanup]:
        async with self._session(db) as (session, owned):
            now = int(time.time())
            stmt = (
                select(MemoryAccountCleanup)
                .where(
                    MemoryAccountCleanup.available_at <= now,
                    or_(
                        MemoryAccountCleanup.status.in_(('pending', 'failed')),
                        (MemoryAccountCleanup.status == 'running')
                        & (MemoryAccountCleanup.lease_expires_at.is_not(None))
                        & (MemoryAccountCleanup.lease_expires_at < now),
                    ),
                )
                .order_by(MemoryAccountCleanup.available_at)
                .limit(max(1, min(limit, 20)))
            )
            bind = session.get_bind() if hasattr(session, 'get_bind') else getattr(session, 'bind', None)
            dialect_name = getattr(getattr(bind, 'dialect', None), 'name', None)
            if dialect_name == 'postgresql':
                stmt = stmt.with_for_update(skip_locked=True)
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                row.status = 'running'
                row.lease_owner = worker_id
                row.lease_expires_at = now + lease_seconds
                row.attempt_count = int(row.attempt_count or 0) + 1
                row.updated_at = now
            await self._commit_if_owned(session, owned)
            return list(rows)

    async def complete_account_cleanup(self, cleanup_id: str, worker_id: str, db: AsyncSession | None = None) -> bool:
        async with self._session(db) as (session, owned):
            row = await session.get(MemoryAccountCleanup, cleanup_id)
            if not row or row.status != 'running' or row.lease_owner != worker_id:
                return False
            now = int(time.time())
            row.status = 'succeeded'
            row.completed_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            await self._commit_if_owned(session, owned)
            return True

    async def fail_account_cleanup(
        self, cleanup_id: str, worker_id: str, error: str, *, max_attempts: int = 8, db: AsyncSession | None = None
    ) -> bool:
        async with self._session(db) as (session, owned):
            row = await session.get(MemoryAccountCleanup, cleanup_id)
            if not row or row.status != 'running' or row.lease_owner != worker_id:
                return False
            now = int(time.time())
            row.status = 'failed'
            row.available_at = now + min(3600, 2 ** min(row.attempt_count, 10))
            row.last_error = (error or '')[-400:]
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            if row.attempt_count >= max_attempts:
                row.status = 'failed'
            await self._commit_if_owned(session, owned)
            return True

    async def list_path_groups(
        self,
        user_id: str,
        *,
        query: str | None = None,
        memory_type: str = 'all',
        limit: int = 100,
        db: AsyncSession | None = None,
    ) -> dict:
        page_size = max(1, min(int(limit or 100), 500))
        async with self._session(db) as (session, _owned):
            stmt = select(
                Memory.path,
                Memory.type,
                func.count().label('count'),
                func.max(Memory.updated_at).label('updated_at'),
            ).where(Memory.user_id == user_id, Memory.status != 'deleted')
            if memory_type != 'all':
                stmt = stmt.where(Memory.type == memory_type)
            if query and query.strip():
                value = f'%{query.strip()}%'
                stmt = stmt.where(or_(Memory.path.ilike(value), Memory.content.ilike(value)))
            rows = (
                await session.execute(stmt.group_by(Memory.path, Memory.type).order_by(func.max(Memory.updated_at).desc()).limit(page_size))
            ).all()
            return {
                'paths': [
                    {'path': path, 'type': memory_type_value, 'count': count, 'updated_at': updated_at, 'children': []}
                    for path, memory_type_value, count, updated_at in rows
                ],
                'count': len(rows),
            }

    async def create_transfer(
        self,
        user_id: str,
        *,
        direction: str,
        format: str,
        dry_run: bool = False,
        db: AsyncSession | None = None,
    ) -> MemoryTransfer:
        async with self._session(db) as (session, owned):
            now = int(time.time())
            row = MemoryTransfer(
                id=str(uuid.uuid4()),
                user_id=user_id,
                direction=direction,
                format=format,
                status='staging',
                dry_run=dry_run,
                created_at=now,
                updated_at=now,
                expires_at=now + 24 * 60 * 60,
            )
            session.add(row)
            await self._commit_if_owned(session, owned)
            return row

    async def append_transfer_records(
        self, transfer_id: str, records: list[tuple[int, str, dict]], db: AsyncSession | None = None
    ) -> int:
        async with self._session(db) as (session, owned):
            transfer = await session.get(MemoryTransfer, transfer_id)
            if not transfer or transfer.status not in {'staging', 'validating'}:
                return 0
            added = 0
            for index, record_type, payload in records:
                nested = await session.begin_nested()
                try:
                    session.add(
                        MemoryTransferRecord(
                            id=str(uuid.uuid4()),
                            transfer_id=transfer_id,
                            record_index=index,
                            record_type=record_type,
                            payload=payload,
                            status='pending',
                        )
                    )
                    await session.flush()
                except IntegrityError:
                    await nested.rollback()
                    continue
                else:
                    await nested.commit()
                    added += 1
            transfer.total_records = int(transfer.total_records or 0) + added
            transfer.updated_at = int(time.time())
            await self._commit_if_owned(session, owned)
            return added

    async def get_transfer(self, transfer_id: str, user_id: str, db: AsyncSession | None = None) -> MemoryTransfer | None:
        async with self._session(db) as (session, _owned):
            result = await session.execute(
                select(MemoryTransfer).where(MemoryTransfer.id == transfer_id, MemoryTransfer.user_id == user_id)
            )
            return result.scalars().first()

    async def publish_transfer(self, transfer_id: str, user_id: str, db: AsyncSession | None = None) -> dict:
        async with self._session(db) as (session, owned):
            transfer = (
                await session.execute(
                    select(MemoryTransfer)
                    .where(MemoryTransfer.id == transfer_id, MemoryTransfer.user_id == user_id)
                    .with_for_update()
                )
            ).scalars().first()
            if not transfer:
                raise ValueError('Transfer not found')
            transfer.status = 'publishing'
            transfer.updated_at = int(time.time())
            imported = 0
            skipped = 0
            after_index = -1
            while True:
                rows = (
                    await session.execute(
                        select(MemoryTransferRecord)
                        .where(
                            MemoryTransferRecord.transfer_id == transfer_id,
                            MemoryTransferRecord.record_type == 'memory',
                            MemoryTransferRecord.record_index > after_index,
                        )
                        .order_by(MemoryTransferRecord.record_index)
                        .limit(MEMORY_EXPORT_BATCH)
                    )
                ).scalars().all()
                if not rows:
                    break
                for row in rows:
                    payload = row.payload or {}
                    content = payload.get('content')
                    if not isinstance(content, str) or not content.strip():
                        skipped += 1
                        row.status = 'invalid'
                        continue
                    if transfer.dry_run:
                        imported += 1
                        row.status = 'valid'
                        continue
                    record, status = await self._insert_new_memory_tx(
                        session,
                        user_id,
                        content,
                        memory_type=payload.get('type'),
                        path=payload.get('path'),
                        meta={
                            'created_by': 'import',
                            'scope': payload.get('scope', 'long_term'),
                            'kind': payload.get('kind', 'fact'),
                            'structured_value': payload.get('structured_value'),
                            'session_id': payload.get('session_id'),
                            'chat_id': payload.get('chat_id'),
                        },
                    )
                    if status == 'duplicate' or record is None:
                        skipped += 1
                        row.status = 'published'
                        continue
                    imported += 1
                    row.status = 'published'
                    if isinstance(payload.get('id'), str):
                        session.add(
                            MemoryTransferIdMap(
                                id=str(uuid.uuid4()),
                                transfer_id=transfer_id,
                                entity_type='memory',
                                source_id=payload['id'],
                                target_id=record.id,
                            )
                        )
                after_index = rows[-1].record_index
            now = int(time.time())
            transfer.processed_records = imported + skipped
            if transfer.dry_run:
                transfer.status = 'succeeded'
                transfer.completed_at = now
                transfer.updated_at = now
                await self._commit_if_owned(session, owned)
                return {
                    'schema_version': 2,
                    'dry_run': True,
                    'total': transfer.total_records,
                    'imported': imported,
                    'skipped': skipped,
                    'status': 'validated',
                }
            transfer.status = 'succeeded'
            transfer.completed_at = now
            transfer.updated_at = now
            await self._commit_if_owned(session, owned)
            return {
                'schema_version': 2,
                'dry_run': False,
                'total': transfer.total_records,
                'imported': imported,
                'skipped': skipped,
                'status': 'succeeded',
            }


Memories = MemoriesTable()


