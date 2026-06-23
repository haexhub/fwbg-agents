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

    # fwbg
    fwbg_api_url: str = Field(default="http://localhost:8420")
    fwbg_test_results_dir: Path = Field(
        default=Path.home() / "fwbg" / "test_results",
        description="Where fwbg writes per-run output directories. Scanned by Calibrator.",
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
