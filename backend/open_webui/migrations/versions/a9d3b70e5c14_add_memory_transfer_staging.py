"""add memory transfer staging for bounded JSON/NDJSON import and export

Revision ID: a9d3b70e5c14
Revises: f8c2a91d4e73
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a9d3b70e5c14'
down_revision: str | None = 'f8c2a91d4e73'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'memory_transfer',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('direction', sa.String(16), nullable=False),
        sa.Column('format', sa.String(16), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='staging'),
        sa.Column('dry_run', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('checksum', sa.String(64), nullable=True),
        sa.Column('total_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('total_records', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('processed_records', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=True),
        sa.Column('completed_at', sa.BigInteger(), nullable=True),
        sa.CheckConstraint("direction IN ('import', 'export')", name='ck_memory_transfer_direction'),
        sa.CheckConstraint("format IN ('json_v1', 'ndjson_v2')", name='ck_memory_transfer_format'),
        sa.CheckConstraint(
            "status IN ('staging', 'validating', 'publishing', 'succeeded', 'failed', 'expired')",
            name='ck_memory_transfer_status',
        ),
    )
    op.create_index('ix_memory_transfer_user_created', 'memory_transfer', ['user_id', 'created_at'])
    op.create_index('ix_memory_transfer_status_expires', 'memory_transfer', ['status', 'expires_at'])

    op.create_table(
        'memory_transfer_record',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('transfer_id', sa.Text(), sa.ForeignKey('memory_transfer.id', ondelete='CASCADE'), nullable=False),
        sa.Column('record_index', sa.Integer(), nullable=False),
        sa.Column('record_type', sa.String(32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.UniqueConstraint('transfer_id', 'record_index', name='uq_memory_transfer_record_index'),
        sa.CheckConstraint(
            "status IN ('pending', 'valid', 'invalid', 'published')", name='ck_memory_transfer_record_status'
        ),
    )
    op.create_index('ix_memory_transfer_record_transfer', 'memory_transfer_record', ['transfer_id', 'record_index'])

    op.create_table(
        'memory_transfer_id_map',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('transfer_id', sa.Text(), sa.ForeignKey('memory_transfer.id', ondelete='CASCADE'), nullable=False),
        sa.Column('entity_type', sa.String(32), nullable=False),
        sa.Column('source_id', sa.Text(), nullable=False),
        sa.Column('target_id', sa.Text(), nullable=False),
        sa.UniqueConstraint('transfer_id', 'entity_type', 'source_id', name='uq_memory_transfer_id_map'),
    )
    op.create_index('ix_memory_transfer_id_map_transfer', 'memory_transfer_id_map', ['transfer_id', 'entity_type'])


def downgrade() -> None:
    op.drop_index('ix_memory_transfer_id_map_transfer', table_name='memory_transfer_id_map')
    op.drop_table('memory_transfer_id_map')
    op.drop_index('ix_memory_transfer_record_transfer', table_name='memory_transfer_record')
    op.drop_table('memory_transfer_record')
    op.drop_index('ix_memory_transfer_status_expires', table_name='memory_transfer')
    op.drop_index('ix_memory_transfer_user_created', table_name='memory_transfer')
    op.drop_table('memory_transfer')
