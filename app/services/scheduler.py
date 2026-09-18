"""Optional in-process periodic scans (APScheduler BackgroundScheduler). Off by default.

Two interval jobs: a free Polymarket refresh every `SCHEDULER_POLY_MINUTES` and a
credit-costing Books refresh every `SCHEDULER_BOOKS_HOURS`. Each job has
`max_instances=1` and `coalesce=True`, and a process-wide lock makes sure the two jobs
never run at the same time either (a scan that finds the lock taken is skipped, not
queued, so a slow API can never build a backlog of credit-costing refreshes).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.settings import Settings

log = logging.getLogger(__name__)

JOB_POLY = "scan-poly"
JOB_BOOKS = "scan-books"
MISFIRE_GRACE_S = 300

_scan_lock = threading.Lock()


def scan_job(kind: str) -> None:
    """Run one scan of `kind` in its own session; never raises (the scheduler logs)."""
    if not _scan_lock.acquire(blocking=False):
        log.warning("scheduled %s scan skipped: another scan is still running", kind)
        return
    try:
        from app.db import get_session_factory
        from app.services.scan import run_scan_default

        session = get_session_factory()()
        try:
            result = run_scan_default(session, kind)
            log.info(
                "scheduled %s scan #%s: %d markets, %d matched, %d opps, %d errors",
                kind,
                result.scan_id,
                result.n_markets,
                result.n_matched,
                result.n_opps,
                len(result.errors),
            )
        except Exception:  # noqa: BLE001 - a failed scan must not kill the scheduler
            log.exception("scheduled %s scan failed", kind)
        finally:
            session.close()
    finally:
        _scan_lock.release()


def build_scheduler(settings: Settings) -> Any | None:
    """Return a configured BackgroundScheduler, or None when both intervals are 0."""
    poly_minutes = int(settings.scheduler_poly_minutes or 0)
    books_hours = int(settings.scheduler_books_hours or 0)
    if poly_minutes <= 0 and books_hours <= 0:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE_S,
        },
    )
    if poly_minutes > 0:
        scheduler.add_job(
            scan_job,
            "interval",
            minutes=poly_minutes,
            args=["poly"],
            id=JOB_POLY,
            name=f"Polymarket refresh every {poly_minutes} min",
            replace_existing=True,
        )
    if books_hours > 0:
        scheduler.add_job(
            scan_job,
            "interval",
            hours=books_hours,
            args=["books"],
            id=JOB_BOOKS,
            name=f"Books refresh every {books_hours} h",
            replace_existing=True,
        )
    return scheduler


def start_scheduler(settings: Settings) -> Any | None:
    """Build and start the scheduler; returns it (or None when disabled)."""
    scheduler = build_scheduler(settings)
    if scheduler is None:
        return None
    scheduler.start()
    log.info(
        "scheduler started (poly every %s min, books every %s h)",
        settings.scheduler_poly_minutes or "-",
        settings.scheduler_books_hours or "-",
    )
    return scheduler


def stop_scheduler(scheduler: Any | None) -> None:
    """Shut down a running scheduler (no-op on None)."""
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001 - shutting down must never raise
        log.exception("scheduler shutdown failed")


__all__ = [
    "JOB_BOOKS",
    "JOB_POLY",
    "build_scheduler",
    "scan_job",
    "start_scheduler",
    "stop_scheduler",
]
