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
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from fwbg_agents.persistence.database import Base

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StrategyState(enum.StrEnum):
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


class PluginState(enum.StrEnum):
    """Lifecycle states for a plugin authoring workflow."""

    SPECIFIED = "specified"
    AUTHORED = "authored"
    VERIFIED = "verified"
    ADOPTED_IN_FWBG = "adopted_in_fwbg"
    ABANDONED = "abandoned"


class PluginKind(enum.StrEnum):
    """fwbg plugin category — matches `PluginContract.kind` Literal 1:1.

    Migration 0004 (M5b) replaced the M3 placeholder set `{INDICATOR, EXIT}`
    with the full fwbg taxonomy and rewrites any legacy `kind='exit'` plugin
    rows to `kind='exit_strategy'`.
    """

    INDICATOR = "indicator"
    MODEL = "model"
    EXIT_STRATEGY = "exit_strategy"
    RISK_MANAGEMENT = "risk_management"
    ENTRY_MODIFIER = "entry_modifier"
    PREPROCESSING = "preprocessing"
    FEATURE_SELECTION = "feature_selection"
    DATA_LOADING = "data_loading"


class EntityType(enum.StrEnum):
    """Discriminator enum for Transition rows (strategy or plugin)."""

    STRATEGY = "strategy"
    PLUGIN = "plugin"


class AgentRunStatus(enum.StrEnum):
    """Execution status of an autonomous agent run."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


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
    """ORM model for a trading strategy and its lifecycle state."""

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
    asset_class: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    strategy_family: Mapped[str] = mapped_column(String(64), nullable=False)
    hypothesis_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    spec_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    post_mortem_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # M6a: per-strategy paper-trading wiring. paper_account_id is a free-form
    # slug pointing at a fwbg-side accounts/<slug>.yaml — agents never reads
    # that file, only stores the pointer. paper_phase_target_days is the
    # dashboard-configurable target duration of the paper phase (M6b's Analyst
    # will compare elapsed days against it; unused in M6a code).
    paper_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    paper_phase_target_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="90"
    )
    metadata_json: Mapped[dict] = mapped_column(
        "metadata_json", JSON, nullable=False, server_default="{}", default=dict
    )
    suggested_universe: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    model_knowledge_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )
    queue_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategyTag(Base):
    """Tag-based prior-art lookup for the Researcher (M4).

    Composite PK (strategy_id, tag) keeps it append-only-ish per strategy
    without needing a separate id.
    """

    __tablename__ = "strategy_tag"

    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategy.id"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(128), primary_key=True, index=True)


class Plugin(Base):
    """ORM model for an agent-authored plugin and its lifecycle state."""

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


class AgentRun(Base):
    """One invocation of an agent (Runner, Analyst, Researcher, ...).

    Append-only: row inserted at start in `running`, updated to `done`/`failed`
    on completion. status is the only mutated column other than ended_at /
    output_artifact_path / error.
    """

    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AgentRunStatus.PENDING.value, index=True
    )
    strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("strategy.id"), nullable=True, index=True
    )
    plugin_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("plugin.id"), nullable=True, index=True
    )
    parent_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("agent_run.id"), nullable=True, index=True
    )
    input_artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    output_artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VerificationRun(Base):
    """One PluginEvaluator pass over a plugin's contract.

    Inserted at start with `status='running'`; the evaluator updates status,
    counts, ended_at, and error_log_path on completion. The structured
    per-scenario error log lives on disk at `error_log_path` (overwritten by
    subsequent runs — only the latest snapshot is kept; the row itself stays
    as the historical index).
    """

    __tablename__ = "verification_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("plugin.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scenarios_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scenarios_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class LlmCall(Base):
    """One LLM round-trip inside an agent run.

    Cost is nullable because the haex-claude-proxy uses subscription pricing —
    M3 records tokens, future infra plugs in a USD estimator.
    """

    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_run.id"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Setting(Base):
    """Persistent key-value store for application settings.

    Survives restarts as part of state.db. Keys are namespaced by convention
    (e.g. "runner_auto.enabled") — no enforcement at the DB level.
    """

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
