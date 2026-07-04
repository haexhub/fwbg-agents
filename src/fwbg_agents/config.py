"""Application settings, loaded from environment / .env."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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

    # Web research
    tavily_api_key: str | None = None
    brave_api_key: str | None = None
    researcher_fanout_n: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Max sequential researcher attempts per /research/brief call; "
            "each attempt runs alone — on failure the next starts immediately."
        ),
    )
    pipeline_min_proposed: int = Field(
        default=5,
        ge=0,
        le=20,
        description=(
            "Minimum number of PROPOSED strategies to keep in the pipeline "
            "(active when runner-auto is enabled). When below this threshold "
            "and no research_flow is running, one asset-agnostic research run "
            "is triggered automatically."
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
        description="Root of the fwbg source tree; scanned for plugin manifests by PluginCatalog.",
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

    # Service
    api_port: int = 8421
    log_level: str = "INFO"

    # Paths
    data_dir: Path = Path("data")

    @property
    def criteria_dir(self) -> Path:
        return self.data_dir / "criteria"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'state.db'}"

    @property
    def db_url_sync(self) -> str:
        return f"sqlite:///{self.data_dir / 'state.db'}"


settings = Settings()
