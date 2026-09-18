"""Environment-only settings (pydantic-settings). Preferences live in the DB, not here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES_DIR = REPO_ROOT / "fixtures"


class Settings(BaseSettings):
    """Every value here is documented in `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "dev"
    app_password: str = ""
    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./data/app.db"
    odds_api_key: str = ""
    demo_mode: bool = False
    scheduler_poly_minutes: int = 0
    scheduler_books_hours: int = 0
    log_level: str = "INFO"
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR

    @field_validator("app_env")
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        return (value or "dev").strip().lower()

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return (value or "INFO").strip().upper()

    @model_validator(mode="after")
    def _require_password_outside_dev(self) -> Settings:
        if self.app_env != "dev" and not self.app_password:
            raise ValueError(
                "APP_PASSWORD must be set when APP_ENV is not 'dev' "
                f"(APP_ENV={self.app_env!r}). Set it in the environment or .env."
            )
        return self

    @property
    def auth_enabled(self) -> bool:
        """Auth is disabled only for local dev with no password configured."""
        return not (self.app_env == "dev" and self.app_password == "")

    @property
    def cookie_secure(self) -> bool:
        return self.app_env != "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
