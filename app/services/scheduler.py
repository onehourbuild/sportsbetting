"""Optional in-process periodic scans (APScheduler). Off by default. STUB."""

from __future__ import annotations

from typing import Any

from app.settings import Settings


def build_scheduler(settings: Settings) -> Any | None:
    """Return a configured BackgroundScheduler, or None when both intervals are 0."""
    raise NotImplementedError


def start_scheduler(settings: Settings) -> Any | None:
    """Build and start the scheduler; returns it (or None when disabled)."""
    raise NotImplementedError


def stop_scheduler(scheduler: Any | None) -> None:
    """Shut down a running scheduler (no-op on None)."""
    raise NotImplementedError
