"""Strategy metadata_json column (M6b)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-24

Adds a JSON column to `strategy`:
- metadata_json (NOT NULL JSON, server_default="{}"):
  generic vehicle for recommendation flags (M6b's Paper-Analyst will write
  Promote-Live readiness markers here without further migrations). Existing
  rows are backfilled to {} via the server_default.

Note: The Python attribute and DB column are both named `metadata_json`
because SQLAlchemy's DeclarativeBase reserves `metadata` on the model class.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "strategy",
        sa.Column(
            "metadata_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy", "metadata_json")
