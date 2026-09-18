"""Optional APScheduler wiring: off by default, one non-overlapping job per interval."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services import scheduler as scheduler_service
from app.services.scheduler import (
    JOB_BOOKS,
    JOB_POLY,
    build_scheduler,
    scan_job,
    start_scheduler,
    stop_scheduler,
)
from app.settings import Settings


def _settings(tmp_path: Path, **overrides: int) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="dev",
        database_url=f"sqlite:///{tmp_path / 's.db'}",
        **overrides,
    )


def test_scheduler_is_off_by_default(tmp_path: Path):
    settings = _settings(tmp_path)
    assert settings.scheduler_poly_minutes == 0 and settings.scheduler_books_hours == 0
    assert build_scheduler(settings) is None
    assert start_scheduler(settings) is None
    stop_scheduler(None)  # no-op


def test_build_scheduler_configures_non_overlapping_interval_jobs(tmp_path: Path):
    scheduler = build_scheduler(
        _settings(tmp_path, scheduler_poly_minutes=15, scheduler_books_hours=6)
    )
    assert scheduler is not None and not scheduler.running
    # job defaults are merged into the jobs when the scheduler starts; start it paused
    scheduler.start(paused=True)
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {JOB_POLY, JOB_BOOKS}
        assert jobs[JOB_POLY].args == ("poly",) and jobs[JOB_BOOKS].args == ("books",)
        assert jobs[JOB_POLY].trigger.interval.total_seconds() == 15 * 60
        assert jobs[JOB_BOOKS].trigger.interval.total_seconds() == 6 * 3600
        for job in jobs.values():
            assert job.max_instances == 1 and job.coalesce is True
            assert job.func is scan_job
    finally:
        scheduler.shutdown(wait=False)

    only_poly = build_scheduler(_settings(tmp_path, scheduler_poly_minutes=5))
    assert [job.id for job in only_poly.get_jobs()] == [JOB_POLY]


def test_start_and_stop_scheduler(tmp_path: Path):
    scheduler = start_scheduler(_settings(tmp_path, scheduler_poly_minutes=60))
    try:
        assert scheduler is not None and scheduler.running
    finally:
        stop_scheduler(scheduler)
    assert not scheduler.running
    stop_scheduler(scheduler)  # idempotent


def test_scan_job_runs_a_scan_in_its_own_session_and_swallows_errors(
    engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    seen: list[str] = []

    class _Result:
        scan_id, n_markets, n_matched, n_opps, errors = 1, 0, 0, 0, []

    def fake_run(session, kind):
        seen.append(kind)
        return _Result()

    monkeypatch.setattr("app.services.scan.run_scan_default", fake_run)
    scan_job("poly")
    assert seen == ["poly"]

    def boom(session, kind):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("app.services.scan.run_scan_default", boom)
    with caplog.at_level(logging.ERROR, logger="app.services.scheduler"):
        scan_job("books")  # logged, not raised
    failures = [r for r in caplog.records if "scheduled books scan failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info is not None  # the traceback is logged
    # the process-wide lock is released again, so the next scheduled scan can run
    lock = scheduler_service._scan_lock
    assert lock.acquire(blocking=False)
    lock.release()


def test_scan_job_skips_when_another_scan_holds_the_lock(engine, monkeypatch: pytest.MonkeyPatch):
    called: list[str] = []
    monkeypatch.setattr(
        "app.services.scan.run_scan_default", lambda session, kind: called.append(kind)
    )
    lock = scheduler_service._scan_lock
    assert lock.acquire(blocking=False)
    try:
        scan_job("poly")
    finally:
        lock.release()
    assert called == []
