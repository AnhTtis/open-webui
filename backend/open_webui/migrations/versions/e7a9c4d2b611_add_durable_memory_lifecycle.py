"""add durable memory lifecycle

Revision ID: e7a9c4d2b611
Revises: d4c1a8e37b62
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import hashlib
import json
import time
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = 'e7a9c4d2b611'
down_revision: str | None = 'd4c1a8e37b62'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalized_hash(memory_type: str | None, path: str | None, content: str | None) -> str:
    normalized = '\n'.join(
        (
            (memory_type or 'context').strip().lower(),
            (path or '').strip().strip('/').lower(),
            ' '.join((content or '').split()).casefold(),
        )
    )
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _decode_meta(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _backfill_legacy_memories() -> None:
    """Create the initial immutable revision for legacy memory rows."""
    connection = op.get_bind()
    memory = sa.table(
        'memory',
        sa.column('id', sa.Text()),
        sa.column('user_id', sa.Text()),
        sa.column('type', sa.String()),
        sa.column('path', sa.Text()),
        sa.column('content', sa.Text()),
        sa.column('meta', sa.JSON()),
        sa.column('created_at', sa.BigInteger()),
        sa.column('updated_at', sa.BigInteger()),
        sa.column('normalized_hash', sa.String()),
        sa.column('current_revision', sa.Integer()),
        sa.column('sync_status', sa.String()),
    )
    revision = sa.table(
        'memory_revision',
        sa.column('id', sa.Text()),
        sa.column('memory_id', sa.Text()),
        sa.column('revision', sa.Integer()),
        sa.column('action', sa.String()),
        sa.column('content', sa.Text()),
        sa.column('structured_value', sa.JSON()),
        sa.column('memory_type', sa.String()),
        sa.column('path', sa.Text()),
        sa.column('status', sa.String()),
        sa.column('reason', sa.Text()),
        sa.column('actor_type', sa.String()),
        sa.column('actor_id', sa.Text()),
        sa.column('source', sa.String()),
        sa.column('chat_id', sa.Text()),
        sa.column('message_id', sa.Text()),
        sa.column('model_id', sa.Text()),
        sa.column('extractor_version', sa.Text()),
        sa.column('created_at', sa.BigInteger()),
    )

    last_id = None
    while True:
        query = (
            sa.select(
                memory.c.id,
                memory.c.type,
                memory.c.path,
                memory.c.content,
                memory.c.meta,
                memory.c.created_at,
                memory.c.updated_at,
            )
            .order_by(memory.c.id)
            .limit(500)
        )
        if last_id is not None:
            query = query.where(memory.c.id > last_id)
        rows = connection.execute(query).mappings().all()
        if not rows:
            break

        revision_rows = []
        for row in rows:
            meta = _decode_meta(row['meta'])
            created_at = row['created_at'] or row['updated_at'] or int(time.time())
            memory_type = row['type'] or 'context'
            connection.execute(
                memory.update()
                .where(memory.c.id == row['id'])
                .values(
                    normalized_hash=_normalized_hash(memory_type, row['path'], row['content']),
                    current_revision=1,
                    sync_status='pending',
                )
            )
            revision_rows.append(
                {
                    'id': str(uuid.uuid4()),
                    'memory_id': row['id'],
                    'revision': 1,
                    'action': 'import',
                    'content': row['content'],
                    'structured_value': None,
                    'memory_type': memory_type,
                    'path': row['path'],
                    'status': 'active',
                    'reason': 'Backfilled from legacy memory table',
                    'actor_type': meta.get('created_by') or 'migration',
                    'actor_id': meta.get('actor_id'),
                    'source': meta.get('source') or meta.get('created_by') or 'migration',
                    'chat_id': meta.get('chat_id'),
                    'message_id': meta.get('message_id'),
                    'model_id': meta.get('model'),
                    'extractor_version': meta.get('extractor_version'),
                    'created_at': created_at,
                }
            )
        connection.execute(revision.insert(), revision_rows)
        last_id = rows[-1]['id']


def upgrade() -> None:
    with op.batch_alter_table('memory') as batch:
        batch.add_column(sa.Column('agent_profile_id', sa.Text(), nullable=True))
        batch.add_column(sa.Column('scope', sa.String(20), nullable=False, server_default='long_term'))
        batch.add_column(sa.Column('kind', sa.String(24), nullable=False, server_default='fact'))
        batch.add_column(sa.Column('structured_value', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('normalized_hash', sa.String(64), nullable=True))
        batch.add_column(sa.Column('status', sa.String(20), nullable=False, server_default='active'))
        batch.add_column(sa.Column('confidence', sa.Float(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('trust', sa.Float(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('importance', sa.Float(), nullable=False, server_default='0.5'))
        batch.add_column(sa.Column('valid_from', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('valid_to', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('expires_at', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('last_recalled_at', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('recall_count', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('helpful_count', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('current_revision', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('sync_status', sa.String(20), nullable=False, server_default='pending'))
        batch.add_column(sa.Column('archived_at', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('deleted_at', sa.BigInteger(), nullable=True))
        batch.create_check_constraint('ck_memory_scope', "scope IN ('session', 'working', 'long_term')")
        batch.create_check_constraint(
            'ck_memory_status', "status IN ('candidate', 'active', 'superseded', 'archived', 'deleted')"
        )
        batch.create_check_constraint('ck_memory_confidence', 'confidence >= 0 AND confidence <= 1')
        batch.create_check_constraint('ck_memory_trust', 'trust >= 0 AND trust <= 1')
        batch.create_check_constraint('ck_memory_importance', 'importance >= 0 AND importance <= 1')
        batch.create_check_constraint('ck_memory_version', 'version >= 1')

    op.create_index('ix_memory_user_status_updated', 'memory', ['user_id', 'status', 'updated_at'])
    op.create_index('ix_memory_user_scope_status', 'memory', ['user_id', 'scope', 'status'])
    op.create_index('ix_memory_user_hash', 'memory', ['user_id', 'normalized_hash'])
    op.create_index('ix_memory_user_expires', 'memory', ['user_id', 'expires_at'])

    op.create_table(
        'agent_profile',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_id', sa.Text(), nullable=False, server_default='default'),
        sa.Column('current_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('learning_paused', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('user_id', 'agent_id', name='uq_agent_profile_user_agent'),
    )
    op.create_index('ix_agent_profile_user', 'agent_profile', ['user_id'])

    op.create_table(
        'agent_profile_revision',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('profile_id', sa.Text(), sa.ForeignKey('agent_profile.id', ondelete='CASCADE'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('narrative', sa.Text(), nullable=True),
        sa.Column('traits', sa.JSON(), nullable=True),
        sa.Column('preferences', sa.JSON(), nullable=True),
        sa.Column('boundaries', sa.JSON(), nullable=True),
        sa.Column('active_goals', sa.JSON(), nullable=True),
        sa.Column('source_run_id', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('profile_id', 'revision', name='uq_agent_profile_revision'),
    )

    op.create_table(
        'memory_revision',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('memory_id', sa.Text(), sa.ForeignKey('memory.id', ondelete='CASCADE'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(24), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('structured_value', sa.JSON(), nullable=True),
        sa.Column('memory_type', sa.String(20), nullable=True),
        sa.Column('path', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('actor_type', sa.String(24), nullable=False),
        sa.Column('actor_id', sa.Text(), nullable=True),
        sa.Column('source', sa.String(32), nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('message_id', sa.Text(), nullable=True),
        sa.Column('model_id', sa.Text(), nullable=True),
        sa.Column('extractor_version', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('memory_id', 'revision', name='uq_memory_revision_number'),
    )
    op.create_index('ix_memory_revision_memory_created', 'memory_revision', ['memory_id', 'created_at'])

    op.create_table(
        'memory_evidence',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('memory_id', sa.Text(), sa.ForeignKey('memory.id', ondelete='CASCADE'), nullable=False),
        sa.Column('revision_id', sa.Text(), sa.ForeignKey('memory_revision.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('message_id', sa.Text(), nullable=True),
        sa.Column('session_id', sa.Text(), nullable=True),
        sa.Column('source_excerpt', sa.Text(), nullable=True),
        sa.Column('source_hash', sa.String(64), nullable=True),
        sa.Column('source_role', sa.String(20), nullable=True),
        sa.Column('actor_type', sa.String(24), nullable=False),
        sa.Column('tool_name', sa.Text(), nullable=True),
        sa.Column('model_id', sa.Text(), nullable=True),
        sa.Column('extraction_run_id', sa.Text(), nullable=True),
        sa.Column('observed_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_memory_evidence_memory', 'memory_evidence', ['memory_id', 'observed_at'])

    op.create_table(
        'memory_relation',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_memory_id', sa.Text(), sa.ForeignKey('memory.id', ondelete='CASCADE'), nullable=False),
        sa.Column('target_memory_id', sa.Text(), sa.ForeignKey('memory.id', ondelete='CASCADE'), nullable=False),
        sa.Column('relation_type', sa.String(24), nullable=False),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('source_memory_id', 'target_memory_id', 'relation_type', name='uq_memory_relation'),
    )

    op.create_table(
        'session_memory_state',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_profile_id', sa.Text(), sa.ForeignKey('agent_profile.id', ondelete='SET NULL'), nullable=True),
        sa.Column('chat_id', sa.Text(), nullable=False),
        sa.Column('session_id', sa.Text(), nullable=False),
        sa.Column('rolling_summary', sa.Text(), nullable=True),
        sa.Column('current_goals', sa.JSON(), nullable=True),
        sa.Column('open_loops', sa.JSON(), nullable=True),
        sa.Column('active_entities', sa.JSON(), nullable=True),
        sa.Column('transcript_cursor', sa.Text(), nullable=True),
        sa.Column('last_checkpoint_message_id', sa.Text(), nullable=True),
        sa.Column('checkpoint_status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('expires_at', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('user_id', 'session_id', name='uq_session_memory_user_session'),
    )
    op.create_index('ix_session_memory_user_chat', 'session_memory_state', ['user_id', 'chat_id'])
    op.create_index('ix_session_memory_expires', 'session_memory_state', ['expires_at'])

    op.create_table(
        'memory_proposal',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('memory_id', sa.Text(), sa.ForeignKey('memory.id', ondelete='SET NULL'), nullable=True),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('message_id', sa.Text(), nullable=True),
        sa.Column('model_id', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=True),
    )
    op.create_index('ix_memory_proposal_user_status', 'memory_proposal', ['user_id', 'status', 'created_at'])

    op.create_table(
        'memory_job',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('memory_id', sa.Text(), nullable=True),
        sa.Column('job_type', sa.String(32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('payload_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('idempotency_key', sa.String(255), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('available_at', sa.BigInteger(), nullable=False),
        sa.Column('lease_owner', sa.Text(), nullable=True),
        sa.Column('lease_expires_at', sa.BigInteger(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('completed_at', sa.BigInteger(), nullable=True),
        sa.UniqueConstraint('idempotency_key', name='uq_memory_job_idempotency'),
    )
    op.create_index('ix_memory_job_claim', 'memory_job', ['status', 'available_at', 'lease_expires_at'])
    op.create_index('ix_memory_job_user', 'memory_job', ['user_id', 'created_at'])

    op.create_table(
        'memory_audit_event',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('memory_id', sa.Text(), nullable=True),
        sa.Column('revision_id', sa.Text(), nullable=True),
        sa.Column('proposal_id', sa.Text(), nullable=True),
        sa.Column('action', sa.String(32), nullable=False),
        sa.Column('actor_type', sa.String(24), nullable=False),
        sa.Column('actor_id', sa.Text(), nullable=True),
        sa.Column('correlation_id', sa.Text(), nullable=True),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
    )
    op.create_index('ix_memory_audit_user_created', 'memory_audit_event', ['user_id', 'created_at'])

    _backfill_legacy_memories()


def downgrade() -> None:
    op.drop_table('memory_audit_event')
    op.drop_table('memory_job')
    op.drop_table('memory_proposal')
    op.drop_table('session_memory_state')
    op.drop_table('memory_relation')
    op.drop_table('memory_evidence')
    op.drop_table('memory_revision')
    op.drop_table('agent_profile_revision')
    op.drop_table('agent_profile')

    op.drop_index('ix_memory_user_expires', table_name='memory')
    op.drop_index('ix_memory_user_hash', table_name='memory')
    op.drop_index('ix_memory_user_scope_status', table_name='memory')
    op.drop_index('ix_memory_user_status_updated', table_name='memory')
    with op.batch_alter_table('memory') as batch:
        for constraint in (
            'ck_memory_version',
            'ck_memory_importance',
            'ck_memory_trust',
            'ck_memory_confidence',
            'ck_memory_status',
            'ck_memory_scope',
        ):
            batch.drop_constraint(constraint, type_='check')
        for column in (
            'deleted_at',
            'archived_at',
            'sync_status',
            'version',
            'current_revision',
            'helpful_count',
            'recall_count',
            'last_recalled_at',
            'expires_at',
            'valid_to',
            'valid_from',
            'importance',
            'trust',
            'confidence',
            'status',
            'normalized_hash',
            'structured_value',
            'kind',
            'scope',
            'agent_profile_id',
        ):
            batch.drop_column(column)
