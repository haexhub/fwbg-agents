"""Add parent_run_id to agent_run — flow drill-down

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-11

Changes to `agent_run`:
- parent_run_id: new nullable self-referential FK (agent_run.id) + index. Links
  a child agent run (researcher, translator, plugin_planner, ...) to the flow
  run that spawned it (research_flow, plugin_author_flow, ...). NULL for
  top-level / flow runs. Enables the flow ↔ child drill-down on the run detail
  page — Plan 008 Schritt 5.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_run") as batch_op:
        batch_op.add_column(
            sa.Column(
                "parent_run_id",
                sa.Integer(),
                sa.ForeignKey("agent_run.id", name="fk_agent_run_parent_run_id"),
                nullable=True,
            )
        )
        batch_op.create_index("ix_agent_run_parent_run_id", ["parent_run_id"])


def downgrade() -> None:
    with op.batch_alter_table("agent_run") as batch_op:
        batch_op.drop_index("ix_agent_run_parent_run_id")
        batch_op.drop_column("parent_run_id")
