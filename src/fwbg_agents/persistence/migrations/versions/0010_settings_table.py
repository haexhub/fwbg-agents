"""Add settings table for persistent key-value application config

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12

Replaces the file-backed runner_auto.json with a DB-backed key-value store
so toggle state (enabled, pipeline_min_proposed) survives container restarts
alongside state.db.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "setting",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("setting")
