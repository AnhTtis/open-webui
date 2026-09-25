"""add memory integrity, quota, fencing, and independent account cleanup

Revision ID: f8c2a91d4e73
Revises: e7a9c4d2b611
Create Date: 2026-09-25
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = 'f8c2a91d4e73'
down_revision: str | None = 'e7a9c4d2b611'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE_HASH_INDEX = 'uq_memory_user_live_hash'
LIVE_HASH_WHERE = sa.text("status IN ('active', 'candidate') AND normalized_hash IS NOT NULL")
BATCH_SIZE = 500


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


def _duplicate_live_hash_stats(connection) -> tuple[int, int, int]:
    row = connection.execute(
        sa.text(
            """
            SELECT
                COUNT(*) AS group_count,
                COALESCE(SUM(duplicate_count), 0) AS row_count,
                COUNT(DISTINCT user_id) AS user_count
            FROM (
                SELECT user_id, normalized_hash, COUNT(*) AS duplicate_count
                FROM memory
                WHERE status IN ('active', 'candidate')
                  AND normalized_hash IS NOT NULL
                GROUP BY user_id, normalized_hash
                HAVING COUNT(*) > 1
            ) duplicates
            """
        )
    ).one()
    return int(row.group_count or 0), int(row.row_count or 0), int(row.user_count or 0)


def _fail_on_duplicate_live_hashes(connection) -> None:
    groups, rows, users = _duplicate_live_hash_stats(connection)
    if not groups:
        return
    extra = rows - groups
    raise RuntimeError(
        'Cannot add unique live memory hash index because duplicate active/candidate '
        f'hashes exist: {groups} groups across {users} users ({extra} extra rows). '
        'Keep one live row per (user_id, normalized_hash) where status IN '
        "('active','candidate') and archive or delete the extras, then rerun migrations. "
        'Remediation query (does not print content): '
        'SELECT user_id, normalized_hash, COUNT(*) AS n FROM memory '
        "WHERE status IN ('active','candidate') AND normalized_hash IS NOT NULL "
        'GROUP BY user_id, normalized_hash HAVING COUNT(*) > 1.'
    )


def _backfill_memory_columns() -> None:
    connection = op.get_bind()
    memory = sa.table(
        'memory',
        sa.column('id', sa.Text),
        sa.column('content', sa.Text),
        sa.column('content_bytes', sa.Integer),
        sa.column('session_id', sa.Text),
        sa.column('chat_id', sa.Text),
        sa.column('meta', sa.JSON),
    )
    last_id = ''
    while True:
        rows = connection.execute(
            sa.select(memory.c.id, memory.c.content, memory.c.meta)
            .where(memory.c.id > last_id)
            .order_by(memory.c.id)
            .limit(BATCH_SIZE)
        ).mappings().all()
        if not rows:
            break
        for row in rows:
            meta = _decode_meta(row['meta'])
            content = row['content'] or ''
            connection.execute(
                memory.update()
                .where(memory.c.id == row['id'])
                .values(
                    content_bytes=len(content.encode('utf-8')),
                    session_id=meta.get('session_id') if isinstance(meta.get('session_id'), str) else None,
                    chat_id=meta.get('chat_id') if isinstance(meta.get('chat_id'), str) else None,
                )
            )
        last_id = rows[-1]['id']


def _purge_orphan_memories() -> None:
    """Create independent cleanup intents, then drop canonical rows with no user."""
    connection = op.get_bind()
    now = int(time.time())
    orphan_users = connection.execute(
        sa.text(
            """
            SELECT DISTINCT m.user_id AS user_id
            FROM memory AS m
            LEFT JOIN "user" AS u ON u.id = m.user_id
            WHERE u.id IS NULL
            """
        )
    ).mappings().all()
    cleanup = sa.table(
        'memory_account_cleanup',
        sa.column('id', sa.Text),
        sa.column('user_id', sa.Text),
        sa.column('collections', sa.JSON),
        sa.column('status', sa.String),
        sa.column('attempt_count', sa.Integer),
        sa.column('available_at', sa.BigInteger),
        sa.column('created_at', sa.BigInteger),
        sa.column('updated_at', sa.BigInteger),
    )
    for row in orphan_users:
        user_id = row['user_id']
        exists = connection.execute(sa.select(cleanup.c.id).where(cleanup.c.user_id == user_id).limit(1)).first()
        if exists:
            continue
        connection.execute(
            cleanup.insert().values(
                id=str(uuid.uuid4()),
                user_id=user_id,
                collections=[f'user-memory-{user_id}'],
                status='pending',
                attempt_count=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    connection.execute(
        sa.text(
            """
            DELETE FROM memory
            WHERE user_id NOT IN (SELECT id FROM "user")
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM memory_job
            WHERE user_id NOT IN (SELECT id FROM "user")
            """
        )
    )


def _backfill_usage() -> None:
    connection = op.get_bind()
    now = int(time.time())
    connection.execute(
        sa.text(
            """
            INSERT INTO memory_usage (user_id, counted_items, counted_content_bytes, updated_at)
            SELECT user_id, COUNT(*), COALESCE(SUM(content_bytes), 0), :now
            FROM memory
            WHERE status != 'deleted'
            GROUP BY user_id
            """
        ),
        {'now': now},
    )


def _seed_vector_generations() -> None:
    connection = op.get_bind()
    now = int(time.time())
    generation = sa.table(
        'memory_vector_generation',
        sa.column('id', sa.Text),
        sa.column('user_id', sa.Text),
        sa.column('generation', sa.Integer),
        sa.column('status', sa.String),
        sa.column('collection_name', sa.Text),
        sa.column('embedding_fingerprint', sa.Text),
        sa.column('created_at', sa.BigInteger),
        sa.column('activated_at', sa.BigInteger),
    )
    users = connection.execute(sa.text('SELECT DISTINCT user_id FROM memory')).scalars().all()
    for user_id in users:
        connection.execute(
            generation.insert().values(
                id=str(uuid.uuid4()),
                user_id=user_id,
                generation=1,
                status='active',
                collection_name=f'user-memory-{user_id}',
                embedding_fingerprint=None,
                created_at=now,
                activated_at=now,
            )
        )


def upgrade() -> None:
    op.create_table(
        'memory_usage',
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('counted_items', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('counted_content_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('counted_items >= 0', name='ck_memory_usage_items'),
        sa.CheckConstraint('counted_content_bytes >= 0', name='ck_memory_usage_bytes'),
    )

    op.create_table(
        'memory_vector_generation',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='building'),
        sa.Column('collection_name', sa.Text(), nullable=False),
        sa.Column('embedding_fingerprint', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('activated_at', sa.BigInteger(), nullable=True),
        sa.Column('retired_at', sa.BigInteger(), nullable=True),
        sa.UniqueConstraint('user_id', 'generation', name='uq_memory_vector_generation'),
        sa.CheckConstraint("status IN ('building', 'active', 'retired')", name='ck_memory_vector_generation_status'),
        sa.CheckConstraint('generation >= 1', name='ck_memory_vector_generation_number'),
    )
    op.create_index('ix_memory_vector_generation_user_status', 'memory_vector_generation', ['user_id', 'status'])

    op.create_table(
        'memory_vector_manifest',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('memory_id', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('vector_doc_id', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('user_id', 'memory_id', 'revision', 'generation', name='uq_memory_vector_manifest'),
        sa.CheckConstraint(
            "status IN ('pending', 'synced', 'stale', 'deleted')", name='ck_memory_vector_manifest_status'
        ),
    )
    op.create_index('ix_memory_vector_manifest_user_memory', 'memory_vector_manifest', ['user_id', 'memory_id'])
    op.create_index('ix_memory_vector_manifest_doc', 'memory_vector_manifest', ['vector_doc_id'])

    op.create_table(
        'memory_account_cleanup',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('collections', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('available_at', sa.BigInteger(), nullable=False),
        sa.Column('lease_owner', sa.Text(), nullable=True),
        sa.Column('lease_expires_at', sa.BigInteger(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('completed_at', sa.BigInteger(), nullable=True),
        sa.UniqueConstraint('user_id', name='uq_memory_account_cleanup_user'),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name='ck_memory_account_cleanup_status'
        ),
    )
    op.create_index('ix_memory_account_cleanup_status', 'memory_account_cleanup', ['status', 'available_at'])

    with op.batch_alter_table('memory') as batch:
        batch.add_column(sa.Column('content_bytes', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('session_id', sa.Text(), nullable=True))
        batch.add_column(sa.Column('chat_id', sa.Text(), nullable=True))

    with op.batch_alter_table('memory_proposal') as batch:
        batch.add_column(sa.Column('idempotency_key', sa.String(255), nullable=True))

    with op.batch_alter_table('memory_job') as batch:
        batch.add_column(sa.Column('claim_token', sa.Text(), nullable=True))
        batch.add_column(sa.Column('lease_generation', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('heartbeat_at', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('started_at', sa.BigInteger(), nullable=True))

    op.create_index('ix_memory_user_session', 'memory', ['user_id', 'session_id'])
    op.create_index('ix_memory_user_chat', 'memory', ['user_id', 'chat_id'])
    op.create_index('ix_memory_proposal_idempotency', 'memory_proposal', ['user_id', 'idempotency_key'], unique=True)

    if context.is_offline_mode():
        with op.batch_alter_table('memory') as batch:
            batch.create_foreign_key('fk_memory_user_id', 'user', ['user_id'], ['id'], ondelete='CASCADE')
        op.create_index(
            LIVE_HASH_INDEX,
            'memory',
            ['user_id', 'normalized_hash'],
            unique=True,
            postgresql_where=LIVE_HASH_WHERE,
            sqlite_where=LIVE_HASH_WHERE,
        )
        return

    _backfill_memory_columns()
    _fail_on_duplicate_live_hashes(op.get_bind())
    _purge_orphan_memories()

    with op.batch_alter_table('memory') as batch:
        batch.create_foreign_key('fk_memory_user_id', 'user', ['user_id'], ['id'], ondelete='CASCADE')

    op.create_index(
        LIVE_HASH_INDEX,
        'memory',
        ['user_id', 'normalized_hash'],
        unique=True,
        postgresql_where=LIVE_HASH_WHERE,
        sqlite_where=LIVE_HASH_WHERE,
    )
    _backfill_usage()
    _seed_vector_generations()


def downgrade() -> None:
    op.drop_index(LIVE_HASH_INDEX, table_name='memory')
    with op.batch_alter_table('memory') as batch:
        batch.drop_constraint('fk_memory_user_id', type_='foreignkey')
        batch.drop_column('chat_id')
        batch.drop_column('session_id')
        batch.drop_column('content_bytes')
    op.drop_index('ix_memory_user_chat', table_name='memory')
    op.drop_index('ix_memory_user_session', table_name='memory')
    op.drop_index('ix_memory_proposal_idempotency', table_name='memory_proposal')
    with op.batch_alter_table('memory_proposal') as batch:
        batch.drop_column('idempotency_key')
    with op.batch_alter_table('memory_job') as batch:
        batch.drop_column('started_at')
        batch.drop_column('heartbeat_at')
        batch.drop_column('lease_generation')
        batch.drop_column('claim_token')
    op.drop_index('ix_memory_account_cleanup_status', table_name='memory_account_cleanup')
    op.drop_table('memory_account_cleanup')
    op.drop_index('ix_memory_vector_manifest_doc', table_name='memory_vector_manifest')
    op.drop_index('ix_memory_vector_manifest_user_memory', table_name='memory_vector_manifest')
    op.drop_table('memory_vector_manifest')
    op.drop_index('ix_memory_vector_generation_user_status', table_name='memory_vector_generation')
    op.drop_table('memory_vector_generation')
    op.drop_table('memory_usage')
