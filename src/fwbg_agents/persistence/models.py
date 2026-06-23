"""ORM models.

Design constraints (from project docs + feedback memory):
- Append-only for strategy/plugin/transition: no cascade deletes, no DELETE endpoints.
- `current_state` on strategy/plugin is a denormalized pointer to the latest state;
  the authoritative history lives in `transition` rows. State *updates* are
  allowed on `current_state`; transition rows themselves are insert-only.
- Slug is the external/URL/filesystem ID; integer id is internal only.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from fwbg_agents.persistence.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StrategyState(str, enum.Enum):
    """Collapsed strategy lifecycle for M2.

    Section 5.1 of the design has a richer granularity (proposed → specified →
    backtested_pending → ...); M2 deliberately collapses to four substantive
    states plus a terminal `abandoned`. Later milestones can split states
    without rewriting the transition log — the slugs persist as strings.
    """

    PROPOSED = "proposed"
    BACKTESTED = "backtested"
    PAPER_TRADING = "paper_trading"
    LIVE_TRADING = "live_trading"
    ABANDONED = "abandoned"


class PluginState(str, enum.Enum):
    SPECIFIED = "specified"
    AUTHORED = "authored"
    VERIFIED = "verified"
    ADOPTED_IN_FWBG = "adopted_in_fwbg"
    ABANDONED = "abandoned"


class PluginKind(str, enum.Enum):
    INDICATOR = "indicator"
    EXIT = "exit"


class EntityType(str, enum.Enum):
    STRATEGY = "strategy"
    PLUGIN = "plugin"


# Terminal states cannot be left.
STRATEGY_TERMINAL_STATES: frozenset[StrategyState] = frozenset(
    {StrategyState.LIVE_TRADING, StrategyState.ABANDONED}
)
PLUGIN_TERMINAL_STATES: frozenset[PluginState] = frozenset(
    {PluginState.ADOPTED_IN_FWBG, PluginState.ABANDONED}
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CalibrationRun(Base):
    """Record of one Calibrator pass over fwbg's test_results.

    Carried over from M1 — calibrator is independent of the lifecycle but its
    output (criteria YAMLs) feeds the backtested→paper_trading guard.
    """

    __tablename__ = "calibration_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    runs_scanned: Mapped[int] = mapped_column(Integer, nullable=False)
    runs_with_elite: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_classes_processed: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    baseline_path: Mapped[str] = mapped_column(String(512), nullable=False)


class Strategy(Base):
    __tablename__ = "strategy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    current_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrategyState.PROPOSED.value, index=True
    )
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("strategy.id"), nullable=True
    )
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    strategy_family: Mapped[str] = mapped_column(String(64), nullable=False)
    hypothesis_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    spec_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    post_mortem_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategyTag(Base):
    """Tag-based prior-art lookup for the Researcher (M4).

    Composite PK (strategy_id, tag) keeps it append-only-ish per strategy
    without needing a separate id.
    """

    __tablename__ = "strategy_tag"

    strategy_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("strategy.id"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(128), primary_key=True, index=True)


class Plugin(Base):
    __tablename__ = "plugin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    current_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PluginState.SPECIFIED.value, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    spec_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contract_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    post_mortem_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Transition(Base):
    """Insert-only audit log for every state change."""

    __tablename__ = "transition"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
