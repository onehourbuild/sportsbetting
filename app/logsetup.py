"""Process-wide logging configuration shared by the app factory and the CLI.

Two jobs beyond `logging.basicConfig`:

- Quiet the `httpx` / `httpcore` loggers to WARNING. httpx logs every request as
  ``HTTP Request: GET <full URL with query>`` at INFO, and The Odds API key travels in the
  query string (``apiKey=...``), so at the default INFO level every Books refresh would
  print the key to stdout (and on Fly to `fly logs`).
- Install a redaction filter on those loggers and on every root handler so that any
  record from any logger that still carries ``apiKey=<value>`` is rewritten to
  ``apiKey=***`` before it is formatted.
"""

from __future__ import annotations

import logging
import re

API_KEY_PATTERN = re.compile(r"(apiKey=)[^&\s\"']+", re.IGNORECASE)
REDACTED = "***"
QUIET_LOGGERS: tuple[str, ...] = ("httpx", "httpcore")


class RedactApiKeyFilter(logging.Filter):
    """Rewrites ``apiKey=<value>`` to ``apiKey=***`` in the record's rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let redaction break logging
            return True
        redacted = API_KEY_PATTERN.sub(rf"\g<1>{REDACTED}", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact_api_key(text: str) -> str:
    """The same rewrite for ad-hoc strings (error messages, toasts)."""
    return API_KEY_PATTERN.sub(rf"\g<1>{REDACTED}", text)


def _install(target: logging.Logger | logging.Handler) -> None:
    if not any(isinstance(f, RedactApiKeyFilter) for f in target.filters):
        target.addFilter(RedactApiKeyFilter())


def configure_logging(level: str | int = "INFO") -> None:
    """basicConfig at `level`, quiet the HTTP client loggers, install the key redaction."""
    logging.basicConfig(level=level)
    root = logging.getLogger()
    _install(root)
    for handler in root.handlers:
        _install(handler)
    for name in QUIET_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        _install(logger)


__all__ = [
    "API_KEY_PATTERN",
    "QUIET_LOGGERS",
    "RedactApiKeyFilter",
    "configure_logging",
    "redact_api_key",
]
