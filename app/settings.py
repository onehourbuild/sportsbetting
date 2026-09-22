"""Environment-only settings (pydantic-settings). Preferences live in the DB, not here.

Secrets (`APP_PASSWORD`, `SECRET_KEY`, `ODDS_API_KEY`) are `SecretStr`, so any accidental
`repr()`/log of a Settings object prints `**********` instead of the value. Read the plain
text through `app_password_value` / `secret_key_value` / `odds_api_key_value`.

`get_settings()` turns a configuration mistake into a clean `SystemExit`: pydantic's
`ValidationError` renders `input_value=<the whole env dict>`, so an uncaught one at import
time would print every secret in the environment to stderr (and to `fly logs`).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES_DIR = REPO_ROOT / "fixtures"
DEFAULT_SECRET_KEY = "dev-secret-change-me"  # public: only ever acceptable in dev
MIN_SECRET_KEY_LENGTH = 32
MIN_PASSWORD_LENGTH = 12


def secret_value(value: SecretStr | str | None) -> str:
    """Plain text behind a secret setting.

    A plain `str` is tolerated because `Settings.model_copy(update=...)` skips validation
    and can leave a raw string in a `SecretStr` field.
    """
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value)


class Settings(BaseSettings):
    """Every value here is documented in `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "dev"
    app_password: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr(DEFAULT_SECRET_KEY)
    database_url: str = "sqlite:///./data/app.db"
    odds_api_key: SecretStr = SecretStr("")
    # Polymarket US programmatic access. The key id is a UUID and the secret is the
    # Ed25519 private key, shown once when the owner creates it at polymarket.us/developer
    # after identity verification. Both empty (the default) means the importer stays off.
    # SecretStr so neither can reach a log through a repr of this object.
    pm_us_api_key: SecretStr = SecretStr("")
    pm_us_api_secret: SecretStr = SecretStr("")
    # Name of the proxy header that carries the real client address (Fly.io:
    # "fly-client-ip"). Empty (the default) means: trust nothing but the socket peer.
    # Any value here is attacker-controlled off a proxy that sets it, so it stays opt-in.
    trusted_proxy_header: str = ""
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

    @field_validator("trusted_proxy_header")
    @classmethod
    def _normalize_proxy_header(cls, value: str) -> str:
        return (value or "").strip().lower()

    @model_validator(mode="after")
    def _require_password_outside_dev(self) -> Settings:
        """Outside dev the login gate is the whole security model, so refuse to start with
        no password, a short password, or the public default SECRET_KEY (with which anyone
        who has read the repo can mint a valid session cookie)."""
        if self.app_env == "dev":
            return self
        password = self.app_password_value
        secret = self.secret_key_value
        if not password:
            raise ValueError(
                "APP_PASSWORD must be set when APP_ENV is not 'dev' "
                f"(APP_ENV={self.app_env!r}). Set it in the environment or .env."
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"APP_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters when "
                f"APP_ENV is not 'dev' (APP_ENV={self.app_env!r}); it is the only login "
                "factor and is exposed to online guessing."
            )
        if secret == DEFAULT_SECRET_KEY or len(secret) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                "SECRET_KEY must be a random value of at least "
                f"{MIN_SECRET_KEY_LENGTH} characters when APP_ENV is not 'dev' "
                f"(APP_ENV={self.app_env!r}): it signs the session cookie and the default "
                "is public. Generate one with "
                "python -c 'import secrets; print(secrets.token_hex(32))' and set it with "
                "fly secrets set SECRET_KEY=..."
            )
        return self

    # -- plain-text accessors for the secret fields --------------------------

    @property
    def app_password_value(self) -> str:
        return secret_value(self.app_password)

    @property
    def secret_key_value(self) -> str:
        return secret_value(self.secret_key)

    @property
    def odds_api_key_value(self) -> str:
        return secret_value(self.odds_api_key)

    @property
    def pm_us_api_key_value(self) -> str:
        return secret_value(self.pm_us_api_key)

    @property
    def pm_us_api_secret_value(self) -> str:
        return secret_value(self.pm_us_api_secret)

    @property
    def pm_us_configured(self) -> bool:
        """Both halves present. One without the other cannot sign anything."""
        return bool(self.pm_us_api_key_value and self.pm_us_api_secret_value)

    @property
    def auth_enabled(self) -> bool:
        """Auth is disabled only for local dev with no password configured."""
        return not (self.app_env == "dev" and self.app_password_value == "")

    @property
    def cookie_secure(self) -> bool:
        return self.app_env != "dev"


def config_error_message(exc: ValidationError) -> str:
    """A startup message built from field names and messages only — never input values.

    `str(ValidationError)` includes `input_value=...`, which for a settings model is the
    raw environment (every secret in it). `errors(include_input=False, include_url=False)`
    is the only rendering that is safe to print.
    """
    lines = ["Invalid configuration; refusing to start."]
    for error in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in error.get("loc", ()) if str(part))
        message = str(error.get("msg") or "invalid value").removeprefix("Value error, ")
        lines.append(f"  - {loc}: {message}" if loc else f"  - {message}")
    lines.append("Fix the environment (or .env) and start again; see .env.example.")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def _settings_from_env() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # `from None`: chaining would print the ValidationError (with input values) as the
        # "during handling of the above exception" context.
        raise SystemExit(config_error_message(exc)) from None


_active: Settings | None = None


def set_settings(settings: Settings | None) -> None:
    """Make `settings` the process-wide active Settings (None reverts to the environment).

    `create_app(settings)` calls this so the services layer, which has no request in
    hand, sees the same Settings the app was built with (tests build apps with their
    own Settings and must never read a developer's `.env`).
    """
    global _active
    _active = settings


def get_settings() -> Settings:
    """The active Settings: the one `create_app` was given, else the environment's."""
    return _active if _active is not None else _settings_from_env()


__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "DEFAULT_SECRET_KEY",
    "MIN_PASSWORD_LENGTH",
    "MIN_SECRET_KEY_LENGTH",
    "REPO_ROOT",
    "Settings",
    "config_error_message",
    "get_settings",
    "secret_value",
    "set_settings",
]
