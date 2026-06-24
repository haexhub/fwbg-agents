"""verification_run table + PluginKind extension to 8 categories (M5b)

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-24

Schema change: new `verification_run` table for PluginEvaluator results.
Data migration: any existing `plugin.kind = 'exit'` row (M3 placeholder) is
rewritten to `kind = 'exit_strategy'` so it matches the extended PluginKind
enum / PluginContract Literal.

The `kind` column itself remains a 32-char String — no DB enum constraint.
The Python `PluginKind` enum is the source of truth.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "plugin_id",
            sa.Integer(),
            sa.ForeignKey("plugin.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scenarios_run", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scenarios_passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_log_path", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_verification_run_plugin_id", "verification_run", ["plugin_id"]
    )
    op.create_index(
        "ix_verification_run_created_at", "verification_run", ["created_at"]
    )

    op.execute(
        sa.text("UPDATE plugin SET kind = 'exit_strategy' WHERE kind = 'exit'")
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE plugin SET kind = 'exit' WHERE kind = 'exit_strategy'")
    )
    op.drop_index("ix_verification_run_created_at", table_name="verification_run")
    op.drop_index("ix_verification_run_plugin_id", table_name="verification_run")
    op.drop_table("verification_run")
