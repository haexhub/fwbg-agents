"""Strategy-first research schema (Phase 1b)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-01

Changes to `strategy`:
- asset_class: NOT NULL -> NULL (strategy is now the primary entity; asset
  class is an optional scope, not part of identity)
- suggested_universe: new nullable JSON column — list of SuggestedUniverse
  entries produced by the Researcher (scope/value/timeframe/rationale)
- model_knowledge_only: new boolean NOT NULL (server_default=0) — True when
  the Researcher had no web-search access and all sources are model knowledge

Existing rows keep their asset_class values; suggested_universe defaults to
NULL and model_knowledge_only defaults to 0 (False).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("strategy") as batch_op:
        batch_op.alter_column(
            "asset_class",
            existing_type=sa.String(32),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column("suggested_universe", sa.JSON(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "model_knowledge_only",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("strategy") as batch_op:
        batch_op.drop_column("model_knowledge_only")
        batch_op.drop_column("suggested_universe")
        batch_op.alter_column(
            "asset_class",
            existing_type=sa.String(32),
            nullable=False,
        )
