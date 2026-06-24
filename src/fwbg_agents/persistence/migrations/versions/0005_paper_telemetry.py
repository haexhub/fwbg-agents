"""Strategy paper-trading columns (M6a)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-24

Adds two columns to `strategy`:
- paper_account_id (nullable String(128)): pointer to fwbg-side
  accounts/<slug>.yaml. agents never reads the file, only stores the slug.
- paper_phase_target_days (NOT NULL Integer, server_default="90"):
  dashboard-configurable target duration of the paper-trading phase.
  Existing rows are backfilled to 90 via the server_default.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "strategy",
        sa.Column("paper_account_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "strategy",
        sa.Column(
            "paper_phase_target_days",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy", "paper_phase_target_days")
    op.drop_column("strategy", "paper_account_id")
