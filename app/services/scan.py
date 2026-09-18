"""Scan orchestration. Contract: docs/ARCHITECTURE.md "Services API".

STUB — `run_scan` is implemented by the integration step; the convenience
functions are used by routes and the CLI.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.clients.espn import EspnClient
from app.clients.oddsapi import OddsApiClient
from app.clients.polymarket import PolymarketClient
from app.core.types import PrefsLike, ScanResult
from app.models import Prefs


def run_scan(
    session: Session,
    *,
    polymarket: PolymarketClient,
    oddsapi: OddsApiClient | None,
    espn: EspnClient | None,
    prefs: Prefs | PrefsLike,
    kind: str,
    leagues: list[str],
    now: datetime,
) -> ScanResult:
    """Pure orchestration: fetch, match, compute (core), persist Scan/Game/Market/PmQuote/
    BookQuote/Opportunity, then `bets.settle_open_bets` and `bets.capture_closing`."""
    raise NotImplementedError


def run_scan_default(
    session: Session,
    kind: str,
    leagues: list[str] | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """Build clients from Settings (fixtures in demo mode, HTTP otherwise) and call `run_scan`."""
    raise NotImplementedError


def estimate_books_cost(prefs: Prefs | PrefsLike) -> int:
    """Credits a Books refresh would cost.

    3 markets x ceil(len(bookmakers) / 10) per enabled league.
    """
    raise NotImplementedError


def quota_status(session: Session) -> dict:
    """Latest known Odds API quota.

    Keys: "remaining" (int|None), "used" (int|None), "as_of" (datetime|None).
    """
    raise NotImplementedError
