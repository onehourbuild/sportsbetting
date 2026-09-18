"""Fee, EV, Kelly, stake sizing, fill price, limit price. Contract: docs/ARCHITECTURE.md.

STUB — replaced by the math implementer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.core.types import (
    BookGame,
    BookLevel,
    FairProb,
    Opportunity,
    OrderBook,
    PmMarket,
    PrefsLike,
)


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """fee_rate * price * (1 - price)."""
    raise NotImplementedError


def effective_price(price: float, fee_rate: float) -> float:
    """price + taker_fee_per_share(price, fee_rate)."""
    raise NotImplementedError


def edge(fair: float, cost: float) -> float:
    """fair - cost."""
    raise NotImplementedError


def ev_per_dollar(fair: float, cost: float) -> float:
    """fair / cost - 1."""
    raise NotImplementedError


def kelly_fraction(fair: float, cost: float) -> float:
    """max(0, (fair - cost) / (1 - cost))."""
    raise NotImplementedError


def stake_for(
    bankroll: float,
    kelly: float,
    kelly_fraction: float,
    max_stake_pct: float,
    min_stake: float = 1.0,
) -> float:
    """bankroll * kelly * kelly_fraction, capped at max_stake_pct% of bankroll.

    Returns 0.0 when the result is below `min_stake`.
    """
    raise NotImplementedError


def walk_asks(asks: Sequence[BookLevel], usd: float, fee_rate: float) -> tuple[float, float, bool]:
    """Walk the ask ladder spending `usd`; returns (avg_effective_price, shares, fully_filled)."""
    raise NotImplementedError


def limit_price_for_edge(fair: float, min_edge: float, tick: float = 0.01) -> float | None:
    """floor((fair - min_edge) / tick) * tick, or None if <= 0."""
    raise NotImplementedError


def build_opportunity(
    market: PmMarket,
    outcome_index: int,
    book: OrderBook | None,
    fair: FairProb,
    prefs: PrefsLike,
    book_game: BookGame | None,
    now: datetime,
) -> Opportunity | None:
    """Assemble an Opportunity; None when there is no ask, edge < prefs.min_edge,
    or liquidity below prefs.min_liquidity_usd."""
    raise NotImplementedError
