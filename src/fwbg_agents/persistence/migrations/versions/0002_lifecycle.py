"""lifecycle tables — strategy, plugin, transition, strategy_tag

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-24

M2 schema. Append-only by design: no ON DELETE CASCADE, no UNIQUE constraints
other than slug. State enums are stored as plain strings so future state
additions (per design section 5) do not require schema migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("current_state", sa.String(length=32), nullable=False),
        sa.Column("iteration_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_strategy_id", sa.Integer(), sa.ForeignKey("strategy.id"), nullable=True),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("strategy_family", sa.String(length=64), nullable=False),
        sa.Column("hypothesis_path", sa.String(length=512), nullable=True),
        sa.Column("spec_path", sa.String(length=512), nullable=True),
        sa.Column("post_mortem_path", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_strategy_slug", "strategy", ["slug"], unique=True)
    op.create_index("ix_strategy_current_state", "strategy", ["current_state"])
    op.create_index("ix_strategy_asset_class", "strategy", ["asset_class"])

    op.create_table(
        "strategy_tag",
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategy.id"), primary_key=True),
        sa.Column("tag", sa.String(length=128), primary_key=True),
    )
    op.create_index("ix_strategy_tag_tag", "strategy_tag", ["tag"])

    op.create_table(
        "plugin",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("current_state", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("spec_path", sa.String(length=512), nullable=True),
        sa.Column("contract_path", sa.String(length=512), nullable=True),
        sa.Column("post_mortem_path", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plugin_slug", "plugin", ["slug"], unique=True)
    op.create_index("ix_plugin_current_state", "plugin", ["current_state"])

    op.create_table(
        "transition",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_transition_entity_type", "transition", ["entity_type"])
    op.create_index("ix_transition_entity_id", "transition", ["entity_id"])
    op.create_index("ix_transition_created_at", "transition", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_transition_created_at", table_name="transition")
    op.drop_index("ix_transition_entity_id", table_name="transition")
    op.drop_index("ix_transition_entity_type", table_name="transition")
    op.drop_table("transition")

    op.drop_index("ix_plugin_current_state", table_name="plugin")
    op.drop_index("ix_plugin_slug", table_name="plugin")
    op.drop_table("plugin")

    op.drop_index("ix_strategy_tag_tag", table_name="strategy_tag")
    op.drop_table("strategy_tag")

    op.drop_index("ix_strategy_asset_class", table_name="strategy")
    op.drop_index("ix_strategy_current_state", table_name="strategy")
    op.drop_index("ix_strategy_slug", table_name="strategy")
    op.drop_table("strategy")
