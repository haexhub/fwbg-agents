"""Add durable per-backtest trial statistics.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trial_stat",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column("strategy_family", sa.String(64), nullable=False),
        sa.Column("n_trials", sa.Integer(), nullable=False),
        sa.Column("trade_sharpe", sa.Float(), nullable=True),
        sa.Column("n_trades", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategy.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index(op.f("ix_trial_stat_strategy_id"), "trial_stat", ["strategy_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_trial_stat_strategy_id"), table_name="trial_stat")
    op.drop_table("trial_stat")
