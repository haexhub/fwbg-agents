"""Application settings, loaded from environment / .env."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    anthropic_base_url: str = Field(
        default="http://localhost:8080",
        description="haex-claude-proxy base URL",
    )
    anthropic_api_key: str = Field(
        default="proxy-not-used",
        description="Required by SDK; proxy ignores it in favor of OAuth",
    )
    anthropic_model: str = Field(default="claude-opus-4-7")
    llm_timeout_seconds: float = Field(
        default=600.0,
        description=(
            "Per-request read timeout for every LLM call, in seconds. 600s "
            "matches Anthropic's non-streaming ceiling so a legitimately long "
            "Opus generation isn't guillotined. The old 120s cut real requests "
            "off, and the SDK's default 3 attempts turned that into ~6min "
            "stacked failures. Bounded together with llm_max_retries."
        ),
    )
    llm_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "Anthropic client retry budget per LLM call (SDK default, =3 "
            "attempts). Covers transient 429/5xx/connection blips — proxy "
            "502s ate whole researcher fanouts at 1. Worst case stays bounded "
            "at 3 x llm_timeout_seconds for a wedged proxy."
        ),
    )

    # Web research
    tavily_api_key: str | None = None
    brave_api_key: str | None = None
    researcher_fanout_n: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Max sequential researcher attempts per candidate slot; each attempt "
            "runs alone — on failure the next starts immediately."
        ),
    )
    researcher_candidates_n: int = Field(
        default=3,
        ge=1,
        le=5,
        description=(
            "Number of valid hypothesis candidates to collect before picking one. "
            "1 = today's first-valid-wins behaviour (no Critic). >1 collects up to "
            "this many valid candidates (each within its own researcher_fanout_n "
            "retry budget) and has the Critic agent score + pick a winner."
        ),
    )
    pipeline_min_proposed: int = Field(
        default=1,
        ge=0,
        le=20,
        description=(
            "Minimum number of active strategies (PROPOSED + BACKTESTED) to "
            "keep in the pipeline (active when runner-auto is enabled). When "
            "below this threshold and no research_flow is running, one "
            "asset-agnostic research run is triggered. Default 1 enforces "
            "iteration-first: the current strategy line (PROPOSED → backtest "
            "→ reiterate …) must fully resolve before a new one is researched."
        ),
    )
    pipeline_fill_poll_seconds: float = Field(
        default=300.0,
        description="How often (seconds) the pipeline fill loop checks the PROPOSED count.",
    )

    # fwbg
    fwbg_api_url: str = Field(default="http://localhost:8420")
    fwbg_test_results_dir: Path = Field(
        default=Path.home() / "fwbg" / "test_results",
        description="Where fwbg writes per-run output directories. Scanned by Calibrator.",
    )
    fwbg_repo_root: Path = Field(
        default=Path.home() / "Projekte" / "fwbg",
        description="fwbg source tree root; read off disk only by the dev-time backfill script.",
    )
    fwbg_data_dir: Path = Field(
        default=Path.home() / "Projekte" / "fwbg" / "data",
        description=(
            "Root of fwbg's runtime data tree. Paper-trading telemetry lives "
            "under <fwbg_data_dir>/account-trades/<slug>/{trades.jsonl,status.json,positions.json}."
        ),
    )

    # Runner
    runner_poll_interval_seconds: float = 5.0
    # Full-history multi-asset M15 backtests legitimately run for hours —
    # the old 2h cap killed them mid-run.
    runner_poll_timeout_seconds: float = 60 * 60 * 8  # 8h hard cap per backtest
    # How long fwbg may be unreachable mid-backtest before the Runner gives
    # up (watchtower recreates, keep-alive races). The backtest itself keeps
    # running on the fwbg side during such blips.
    runner_poll_outage_tolerance_seconds: float = 120.0
    # fwbg enforces a single backtest slot (429 while busy) — how long to
    # sleep between attempts to grab it.
    runner_busy_wait_seconds: float = 30.0

    # On-demand data provisioning (fwbg POST /api/data/ensure, Phase 1c).
    # The adaptive Runner ensures data for its suggested symbols before a
    # backtest. Ensure now defaults to the FULL available history (e.g. FX
    # minute data since ~2003) — a cold download can take a while.
    data_ensure_poll_interval_seconds: float = 2.0
    data_ensure_timeout_seconds: float = 60 * 60  # 1h per symbol download
    default_timeframe: str = "HOUR_1"

    # Runner auto mode: when enabled (persisted flag, see
    # orchestrator/auto_runner.py), waiting PROPOSED strategies are
    # backtested automatically, one at a time.
    runner_auto_poll_seconds: float = 60.0
    runner_auto_max_attempts: int = 2
    translator_auto_max_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "How many times the auto-runner retries a failed Translator before "
            "abandoning the PROPOSED strategy. Each attempt is independent — the "
            "LLM may produce a valid strategy.json on a subsequent try."
        ),
    )
    reiterate_max_depth: int = Field(
        default=12,
        ge=1,
        le=20,
        description=(
            "Max generation depth of an iteration chain (root = 1). Once a "
            "strategy at this depth is analyzed, reiterate refuses to create "
            "another child — the Analyst is told it is on its final iteration "
            "and should promote or abandon."
        ),
    )
    universe_narrowing_min_iteration: int = Field(
        default=5,
        ge=1,
        description=(
            "Phase-funnel boundary: iterations below this generation depth must "
            "optimize the whole universe (no `target_assets` narrowing). From "
            "this depth on, evidence-based narrowing is allowed."
        ),
    )
    universe_min_size: int = Field(
        default=3,
        ge=1,
        description=(
            "Floor for evidence-based universe narrowing — a non-asset-specific "
            "strategy may never be narrowed below this many assets."
        ),
    )
    holdout_months: int = Field(
        default=24,
        ge=1,
        description=(
            "Months of most-recent data reserved as an out-of-sample holdout. "
            "Iteration backtests end at today - holdout_months (no iteration ever "
            "sees this window); the promote gate then runs a holdout backtest on "
            "[today - holdout_months, today] before a strategy may advance to paper."
        ),
    )
    dsr_min: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum Deflated Sharpe Ratio (Bailey/López de Prado 2014) at the "
            "promote gate. The candidate's per-trade Sharpe on the holdout run "
            "must beat the expected max Sharpe of N zero-skill trials (N = all "
            "backtests ever run, incl. grid combinations) with this "
            "probability; below it, promote is blocked like a holdout fail."
        ),
    )
    min_iterations_before_abandon: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "Hard minimum number of iterations a strategy must go through before "
            "the orchestrator honours an `abandon` recommendation. If the Analyst "
            "emits `abandon` and the strategy is below this depth, the orchestrator "
            "overrides it with `tune_params` using the Analyst's own reasoning as "
            "the change description, forcing at least one more iteration. Set to 1 "
            "to disable the guard (abandon always honoured)."
        ),
    )

    # Plugin authoring (M5d: Planner/Implementer split)
    plugin_planner_model: str = Field(
        default="claude-opus-4-8",
        description="Stronger model for the PluginPlanner agent (reasoning-heavy plan emission).",
    )
    plugin_implementer_model: str = Field(
        default="claude-opus-4-7",
        description="Weaker model for the PluginImplementer agent (mechanical code generation).",
    )
    plugin_impl_max_rounds: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max refinement rounds for the PluginImplementer gate-loop.",
    )
    plugin_author_auto_max_attempts: int = Field(
        default=2,
        ge=1,
        description=(
            "Auto-retry cap for the add_indicator plugin-author chain "
            "(plugin_planner attempts per strategy, DONE plus genuine "
            "failures). Independent of runner_auto_max_attempts, which caps "
            "backtest retries."
        ),
    )
    plugin_resync_enabled: bool = Field(
        default=True,
        description=(
            "Re-register VERIFIED plugins missing from fwbg's catalog at startup. "
            "Covers the case where fwbg was offline when a plugin reached VERIFIED."
        ),
    )

    # Periodic run-janitor: a backstop for runs that hang in the live process
    # (as opposed to orphans from a restart, handled at startup). Runner-borne
    # runs may legitimately last hours (see runner_poll_timeout_seconds), so
    # only pure-LLM agents get the shorter cap; the sweep never touches a run
    # younger than its cap.
    run_stale_sweep_seconds: float = Field(
        default=300.0,
        description="How often the periodic janitor sweeps for over-long RUNNING agent runs.",
    )
    llm_run_cap_seconds: float = Field(
        default=60 * 30,
        description=(
            "Wall-clock cap for pure-LLM agent runs (researcher, translator, "
            "analyst, ...). Backtest-bearing runs use runner_poll_timeout_seconds."
        ),
    )
    run_events_retention_days: int = Field(
        default=30,
        ge=0,
        description=(
            "Days to keep agent-run event directories (data/agent-runs/<id>/). "
            "Directories are removed once the run is terminal and older than this "
            "threshold. 0 = disabled."
        ),
    )

    # Service
    api_port: int = 8421
    log_level: str = "INFO"

    # Paths
    data_dir: Path = Path("data")

    @property
    def criteria_dir(self) -> Path:
        """Return the directory path for backtest-to-paper criteria YAML files."""
        return self.data_dir / "criteria"

    @property
    def db_url(self) -> str:
        """Return the async SQLite connection URL for aiosqlite."""
        return f"sqlite+aiosqlite:///{self.data_dir / 'state.db'}"

    @property
    def db_url_sync(self) -> str:
        """Return the synchronous SQLite connection URL."""
        return f"sqlite:///{self.data_dir / 'state.db'}"


settings = Settings()
