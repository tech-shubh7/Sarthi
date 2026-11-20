"""Runtime settings, loaded from environment variables and an optional ``.env`` file.

Secrets are typed as ``SecretStr`` so they never appear in ``repr()``, logs or tracebacks.
Read a secret's value only at the point of use with ``.get_secret_value()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All Sarthi settings. Environment variables use the ``SARTHI_`` prefix."""

    model_config = SettingsConfigDict(
        env_prefix="SARTHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"), description="User data: database, resume, keys.")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    telegram_bot_token: SecretStr | None = None
    llm_fast_model: str = Field(default="ollama/llama3.1", description="LiteLLM model id for matching.")
    llm_quality_model: str = Field(default="ollama/llama3.1", description="LiteLLM model id for writing.")
    llm_api_key: SecretStr | None = None
    llm_daily_budget_usd: float = Field(default=1.0, ge=0)

    @property
    def db_path(self) -> Path:
        """Path of the single SQLite database file."""
        return self.data_dir / "sarthi.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
