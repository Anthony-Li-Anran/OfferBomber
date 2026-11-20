"""phase4-6 general-purpose job hunting agent

Revision ID: a7c3f90b1d24
Revises: 9f2a1c7d4e83
Create Date: 2026-11-20 22:30:00.000000

Adds user-scoped search campaigns, mailbox metadata (no plaintext credentials),
reply templates, application drafts, single-row automation control and discovery
run statistics. All preference data is JSON so no code change is needed to
support a new industry, role, language or country.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

import offerbomber.storage.models  # noqa: F401 - DateTimeUTC type

revision = "a7c3f90b1d24"
down_revision = "9f2a1c7d4e83"
branch_labels = None
depends_on = None


def _dt() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "search_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False, default="default"),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("criteria", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, default=True),
        sa.Column("created_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
    )
    op.create_index("ix_search_profiles_user", "search_profiles", ["user_id"])

    op.create_table(
        "email_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False, default="default"),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("email", sa.String(300), nullable=False),
        sa.Column("credential_ref", sa.String(300)),
        sa.Column("status", sa.String(30), nullable=False, default="DISCONNECTED"),
        sa.Column("scopes", sa.Text()),
        sa.Column("last_synced_at", _dt()),
        sa.Column("created_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("user_id", "email", name="uq_email_account_user_email"),
    )

    op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False, default="default"),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("category", sa.String(60), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False, default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("language", sa.String(20), nullable=False, default=""),
        sa.Column("created_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_table(
        "application_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False, default="default"),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(40), nullable=False),
        sa.Column("recipient", sa.String(300), nullable=False, default=""),
        sa.Column("subject", sa.String(500), nullable=False, default=""),
        sa.Column("body", sa.Text(), nullable=False, default=""),
        sa.Column("resume_version_id", sa.Integer()),
        sa.Column("attachment_path", sa.Text()),
        sa.Column("confirmation_token", sa.String(200)),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("status", sa.String(30), nullable=False, default="READY"),
        sa.Column("created_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resume_version_id"], ["resume_versions.id"]),
    )
    op.create_index("ix_app_drafts_user", "application_drafts", ["user_id"])
    op.create_index("ix_app_drafts_token", "application_drafts", ["confirmation_token"])
    op.create_index("ix_app_drafts_hash", "application_drafts", ["payload_hash"])

    op.create_table(
        "automation_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False, unique=True),
        sa.Column("paused", sa.Boolean(), nullable=False, default=True),
        sa.Column("mode", sa.String(20), nullable=False, default="MANUAL"),
        sa.Column("max_per_day", sa.Integer(), nullable=False, default=0),
        sa.Column("sent_today", sa.Integer(), nullable=False, default=0),
        sa.Column("counter_date", sa.String(10)),
        sa.Column("created_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
    )

    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False, default="default"),
        sa.Column("profile_id", sa.Integer()),
        sa.Column("stats", sa.Text(), nullable=False),
        sa.Column("created_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", _dt(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["search_profiles.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("discovery_runs")
    op.drop_table("automation_states")
    op.drop_table("application_drafts")
    op.drop_table("email_templates")
    op.drop_table("email_accounts")
    op.drop_table("search_profiles")
