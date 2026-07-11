"""Add queue_position to strategy table

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-04

Changes to `strategy`:
- queue_position: new nullable INTEGER column — explicit ordering for the
  backtest queue. NULL means "unordered" and sorts after all positioned rows
  (via NULLS LAST). Set by PUT /runner/queue.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("strategy") as batch_op:
        batch_op.add_column(sa.Column("queue_position", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("strategy") as batch_op:
        batch_op.drop_column("queue_position")
