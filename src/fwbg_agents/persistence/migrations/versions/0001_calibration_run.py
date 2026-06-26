"""calibration_run

Revision ID: 0001
Revises:
Create Date: 2026-06-23

Initial schema for M1: tracks Calibrator passes. M2 will add strategy /
plugin / transition tables; intentionally not pre-creating them here so
the schema for those lands together with the code that uses them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calibration_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runs_scanned", sa.Integer(), nullable=False),
        sa.Column("runs_with_elite", sa.Integer(), nullable=False),
        sa.Column("asset_classes_processed", sa.JSON(), nullable=False),
        sa.Column("baseline_path", sa.String(length=512), nullable=False),
    )
    op.create_index("ix_calibration_run_ran_at", "calibration_run", ["ran_at"])


def downgrade() -> None:
    op.drop_index("ix_calibration_run_ran_at", table_name="calibration_run")
    op.drop_table("calibration_run")
