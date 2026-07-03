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
            "Parallel hypothesis candidates per /research/brief call; "
            "first to pass validate_hypothesis wins."
        ),
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
    runner_poll_timeout_seconds: float = 60 * 60 * 2  # 2h hard cap per backtest

    # On-demand data provisioning (fwbg POST /api/data/ensure, Phase 1c).
    # The adaptive Runner ensures data for its suggested symbols before a
    # backtest; a cold Dukascopy download can take a few minutes.
    data_ensure_poll_interval_seconds: float = 2.0
    data_ensure_timeout_seconds: float = 60 * 15  # 15 min per symbol download
    default_timeframe: str = "HOUR_1"

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
