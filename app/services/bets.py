"""Bet ledger: create, settle, closing capture, summary.

Contract: docs/ARCHITECTURE.md "Services API".

STUB — implemented by the bets/CLV implementer.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.types import PmMarket
from app.models import Bet


def create_bet(
    session: Session,
    opportunity_id: int,
    stake_usd: float,
    price: float,
    mode: str,
    notes: str = "",
) -> Bet:
    """Log a bet from an Opportunity row: shares, fee, fair/edge at bet time; mode taker|maker."""
    raise NotImplementedError


def settle_bet_manual(session: Session, bet_id: int, result: str) -> Bet:
    """Manual settle: result in {"won", "lost", "void"}; computes pnl_usd and settled_at."""
    raise NotImplementedError


def settle_open_bets(session: Session, markets_by_id: Mapping[str, PmMarket]) -> int:
    """Settle every open Bet whose market is closed with a resolved outcome; returns count."""
    raise NotImplementedError


def capture_closing(session: Session, scan_id: int, now: datetime) -> int:
    """For open bets whose game has started and lack closing data, store closing fair /
    Polymarket price from this scan and compute CLV; returns count."""
    raise NotImplementedError


def ledger_summary(session: Session) -> dict:
    """Keys: n_open, n_settled, n_won, n_lost, total_staked, total_pnl, roi, avg_clv,
    n_clv_positive, n_clv_recorded."""
    raise NotImplementedError
