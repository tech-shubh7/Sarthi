"""Runtime settings, loaded from environment variables and an optional ``.env`` file.

Secrets are typed as ``SecretStr`` so they never appear in ``repr()``, logs or tracebacks.
Read a secret's value only at the point of use with ``.get_secret_value()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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

    telegram_bot_token: SecretStr | None = Field(
        default=None,
        description="Only for a bot of your own; without it Sarthi uses the shared bot (relay_url).",
    )
    relay_url: str = Field(
        default="",
        description="The shared Sarthi bot's relay. Nothing to create: open the link Sarthi shows.",
    )
    # Running the relay itself (the project's server for the shared bot, not something users run):
    relay_bot_token: SecretStr | None = None
    relay_port: int = Field(default=8780, ge=1, le=65535)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object, info: object) -> object:
        """``SARTHI_X=`` (a line copied from .env.example and left empty) means "not set" for secrets."""
        name = getattr(info, "field_name", "")
        if isinstance(value, str) and not value.strip() and (name.endswith(("_token", "_key", "_password"))):
            return None
        return value

    relay_trust_forwarded: bool = Field(
        default=False, description="Behind a reverse proxy: count sign-ups per X-Forwarded-For address."
    )
    digest_hour: int = Field(default=19, ge=0, le=23, description="Local hour for the daily digest.")

    # Job sources. Company boards are "<hiring system>/<company slug>", e.g. "greenhouse/razorpay".
    job_boards: list[str] = Field(default=["linkedin", "indeed"], description="JobSpy boards to search.")
    watch_companies: list[str] = Field(default=[], description="Company boards to watch.")
    boards_every_minutes: int = Field(default=60, ge=15)
    companies_every_minutes: int = Field(default=30, ge=5)
    feeds: list[str] = Field(
        default=[
            "remotive",
            "remoteok",
            "weworkremotely",
            "himalayas",
            "jobicy",
            "arbeitnow",
            "workingnomads",
            "hackernews",
        ],
        description="Free public job feeds (no key).",
    )
    feeds_every_minutes: int = Field(default=60, ge=15)
    freehire: bool = Field(default=True, description="Search Freehire (~90 hiring systems) for your roles.")
    catalog: bool = Field(
        default=True,
        description="Search ATS-Scrapers' daily snapshot of ~192k company boards (~100 MB a day).",
    )
    fill_text: bool = Field(
        default=True,
        description="Read the full description from the job's own page when a source sends less.",
    )
    fill_render_js: bool = Field(
        default=False, description="Render JavaScript-only job pages in Chromium (uses ~300 MB more memory)."
    )
    # Big free aggregators, searched for your roles when a key is set (both keys are free).
    adzuna_app_id: str = Field(default="", description="From https://developer.adzuna.com")
    adzuna_app_key: SecretStr | None = None
    jooble_api_key: SecretStr | None = Field(default=None, description="From https://jooble.org/api/about")

    # The local dashboard (read-only), at http://127.0.0.1:<port> while `sarthi run` runs. This computer only.
    dashboard: bool = True
    dashboard_port: int = Field(default=8765, ge=1024, le=65535)
    screen_port: int = Field(
        default=6080, ge=1024, le=65535, description="Where a site login is shown when there is no desktop."
    )
    # 0.0.0.0 only inside the Docker container (compose publishes it to the host's 127.0.0.1 only).
    dashboard_host: Literal["127.0.0.1", "0.0.0.0"] = "127.0.0.1"  # noqa: S104  (container only)

    # Applying: forms are filled in a browser without a window; nothing is submitted without your approval.
    apply_headless: bool = True
    apply_daily_limit: int = Field(default=30, ge=1, le=500, description="Applications per site per day.")

    # Reply tracking: Sarthi reads (never changes) your mailbox over IMAP.
    # Gmail: imap.gmail.com and an app password.
    mail_imap_host: str = ""
    mail_imap_port: int = Field(default=993, ge=1, le=65535)
    mail_user: str = ""
    mail_password: SecretStr | None = None
    mail_folder: str = "INBOX"
    mail_every_minutes: int = Field(default=10, ge=2, le=1440)
    followup_after_days: int = Field(default=10, ge=3, le=60)

    @property
    def mail_enabled(self) -> bool:
        """True when a mailbox is set up for reply tracking."""
        return bool(self.mail_imap_host and self.mail_user and self.mail_password)

    # AI models, tried in order until one answers: "<provider>/<model>", e.g. "gemini/gemini-2.5-flash".
    # Providers: ollama, gemini, openai, anthropic, groq, openrouter, deepseek (see sarthi.ai.providers).
    llm_fast_models: list[str] = Field(default=["ollama/llama3.1"], description="For matching.")
    llm_quality_models: list[str] = Field(default=["ollama/llama3.1"], description="For writing.")
    llm_daily_token_budget: int = Field(
        default=500_000, ge=0, description="Paid providers only. 0: no limit."
    )
    llm_requests_per_minute: int = Field(default=30, ge=1)
    ollama_url: str = "http://localhost:11434/v1"
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None

    @property
    def db_path(self) -> Path:
        """Path of the single SQLite database file."""
        return self.data_dir / "sarthi.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
