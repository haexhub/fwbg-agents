"""agent_run + llm_call tables — M3 prerequisite

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-24

Records every agent invocation (Runner/Analyst/...) and every LLM call inside.
Append-only with a small set of mutable columns on agent_run (status, ended_at,
output_artifact_path, error) to support the "insert at start, update on
completion" pattern.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategy.id"), nullable=True),
        sa.Column("plugin_id", sa.Integer(), sa.ForeignKey("plugin.id"), nullable=True),
        sa.Column("input_artifact_path", sa.String(length=512), nullable=True),
        sa.Column("output_artifact_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_run_agent_name", "agent_run", ["agent_name"])
    op.create_index("ix_agent_run_status", "agent_run", ["status"])
    op.create_index("ix_agent_run_strategy_id", "agent_run", ["strategy_id"])
    op.create_index("ix_agent_run_plugin_id", "agent_run", ["plugin_id"])

    op.create_table(
        "llm_call",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_run.id"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_call_agent_run_id", "llm_call", ["agent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_agent_run_id", table_name="llm_call")
    op.drop_table("llm_call")

    op.drop_index("ix_agent_run_plugin_id", table_name="agent_run")
    op.drop_index("ix_agent_run_strategy_id", table_name="agent_run")
    op.drop_index("ix_agent_run_status", table_name="agent_run")
    op.drop_index("ix_agent_run_agent_name", table_name="agent_run")
    op.drop_table("agent_run")
