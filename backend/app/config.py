from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Prefixed vars come from CASELOOP_*, provider keys are unprefixed."""

    model_config = SettingsConfigDict(
        env_prefix="CASELOOP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "test", "prod"] = "dev"
    base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/caseloop"
    data_dir: Path = Path("./data")

    agent_runner: Literal["openai", "scripted"] = "openai"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = "gpt-5.5"

    resend_api_key: str | None = Field(default=None, validation_alias="RESEND_API_KEY")
    resend_webhook_secret: str | None = Field(
        default=None, validation_alias="RESEND_WEBHOOK_SECRET"
    )

    dial_api_key: str | None = Field(default=None, validation_alias="DIAL_API_KEY")
    dial_webhook_secret: str | None = Field(default=None, validation_alias="DIAL_WEBHOOK_SECRET")
    dial_base_url: str = "https://api.getdial.ai"

    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = 2.0
    reopen_window_days: int = 14

    @property
    def is_dev(self) -> bool:
        return self.env != "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
