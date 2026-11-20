"""phase3.3 user isolation and confirmation ledger

Revision ID: 9f2a1c7d4e83
Revises: 48bbf6632460
Create Date: 2026-11-20 21:40:00.000000

Adds per-user ownership to resume versions (so one host user cannot read
another's generated PDFs), a plan hash for binding approvals to content, and a
SQLite-backed confirmation ledger with atomic single-consumption semantics.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

import offerbomber.storage.models  # noqa: F401 - DateTimeUTC

revision = '9f2a1c7d4e83'
down_revision = '48bbf6632460'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('resume_versions', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'user_id', sa.String(length=100), nullable=False,
            server_default='default',
        ))
        batch_op.add_column(sa.Column('plan_sha256', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_resume_versions_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_resume_versions_plan_sha256', ['plan_sha256'], unique=False)

    op.create_table(
        'mcp_confirmations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('action', sa.String(length=60), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('source_resume_id', sa.String(length=200), nullable=True),
        sa.Column('plan_sha256', sa.String(length=64), nullable=True),
        sa.Column('plan_snapshot', sa.Text(), nullable=True),
        sa.Column('fact_versions', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', offerbomber.storage.models.DateTimeUTC(), nullable=False),
        sa.Column('expires_at', offerbomber.storage.models.DateTimeUTC(), nullable=False),
        sa.Column('approved_at', offerbomber.storage.models.DateTimeUTC(), nullable=True),
        sa.Column('consumed_at', offerbomber.storage.models.DateTimeUTC(), nullable=True),
    )
    op.create_index('ix_mcp_confirmations_token_hash', 'mcp_confirmations', ['token_hash'], unique=True)
    op.create_index('ix_mcp_confirmations_user_id', 'mcp_confirmations', ['user_id'], unique=False)
    op.create_index('ix_mcp_confirmations_status', 'mcp_confirmations', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('mcp_confirmations')
    with op.batch_alter_table('resume_versions', schema=None) as batch_op:
        batch_op.drop_index('ix_resume_versions_plan_sha256')
        batch_op.drop_index('ix_resume_versions_user_id')
        batch_op.drop_column('plan_sha256')
        batch_op.drop_column('user_id')
