"""Fee, EV, Kelly, stake sizing, fill price, limit price. Contract: docs/ARCHITECTURE.md.

All prices here are Polymarket share prices in dollars per share, i.e. probabilities in
(0, 1). "Cost" always means the fee-inclusive price a taker actually pays. Pure
functions, no I/O; invalid inputs raise ``ValueError`` instead of producing a number.
"""

from __future__ import annotations

import math
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

DEFAULT_TICK = 0.01
STAKE_DECIMALS = 2  # stakes are quoted to the cent


# ----------------------------------------------------------------------------- fees


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Polymarket sports taker fee per share: fee_rate * price * (1 - price).

    Peaks at price 0.50 (1.25 cents per share at the 5% rate) and vanishes at the ends.
    """
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0, 1], got {price}")
    if fee_rate < 0.0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")
    return fee_rate * price * (1.0 - price)


def effective_price(price: float, fee_rate: float) -> float:
    """What a taker really pays per share: price + taker_fee_per_share(price, fee_rate)."""
    return price + taker_fee_per_share(price, fee_rate)


# ----------------------------------------------------------------------------- value


def edge(fair: float, cost: float) -> float:
    """Probability points of edge: fair - cost."""
    return fair - cost


def ev_per_dollar(fair: float, cost: float) -> float:
    """Expected profit per dollar risked: fair / cost - 1. Raises ValueError unless cost > 0."""
    if cost <= 0.0:
        raise ValueError(f"cost must be > 0, got {cost}")
    return fair / cost - 1.0


def kelly_fraction(fair: float, cost: float) -> float:
    """Full-Kelly fraction of bankroll for a binary share: max(0, (fair - cost) / (1 - cost)).

    A share bought at `cost` pays 1 if it wins, so the net odds are (1 - cost) / cost and
    Kelly's f* = (fair * 1 - (1 - fair) * cost) / (1 - cost) = (fair - cost) / (1 - cost).
    Negative edge clamps to 0. A cost of 1 or more can never profit, so it also returns 0.
    Raises ValueError unless cost > 0.
    """
    if cost <= 0.0:
        raise ValueError(f"cost must be > 0, got {cost}")
    if cost >= 1.0:
        return 0.0
    return max(0.0, (fair - cost) / (1.0 - cost))


def stake_for(
    bankroll: float,
    kelly: float,
    kelly_fraction: float,
    max_stake_pct: float,
    min_stake: float = 1.0,
) -> float:
    """USD to stake: bankroll * kelly * kelly_fraction, capped at max_stake_pct% of bankroll.

    Rounded to the cent. Returns 0.0 when the rounded stake is below `min_stake`.
    Raises ValueError on any negative argument.
    """
    for name, value in (
        ("bankroll", bankroll),
        ("kelly", kelly),
        ("kelly_fraction", kelly_fraction),
        ("max_stake_pct", max_stake_pct),
        ("min_stake", min_stake),
    ):
        if value < 0.0:
            raise ValueError(f"{name} must be >= 0, got {value}")
    raw = bankroll * kelly * kelly_fraction
    cap = bankroll * max_stake_pct / 100.0
    stake = round(min(raw, cap), STAKE_DECIMALS)
    return stake if stake >= min_stake else 0.0


# ----------------------------------------------------------------------------- order book


def walk_asks(asks: Sequence[BookLevel], usd: float, fee_rate: float) -> tuple[float, float, bool]:
    """Spend `usd` up the ask ladder at fee-inclusive prices.

    Returns (avg_effective_price, shares, fully_filled). Levels are taken cheapest first;
    the last level is taken partially so the spend is exactly `usd` when the ladder is
    deep enough. When the ladder cannot absorb `usd`, the whole ladder is bought and
    `fully_filled` is False. With no shares bought the average price is reported as 0.0.
    Raises ValueError when `usd` is negative.
    """
    if usd < 0.0:
        raise ValueError(f"usd must be >= 0, got {usd}")
    if usd == 0.0:
        return 0.0, 0.0, True

    remaining = usd
    spent = 0.0
    shares = 0.0
    fully_filled = False
    # The contract sorts asks best (lowest) first; sorting again is free insurance.
    for level in sorted(asks, key=lambda lvl: lvl.price):
        if level.size <= 0.0:
            continue
        unit_cost = effective_price(level.price, fee_rate)
        if unit_cost <= 0.0:
            continue  # a free share is a data error, not a fill
        level_cost = level.size * unit_cost
        if level_cost >= remaining:
            shares += remaining / unit_cost
            spent += remaining
            remaining = 0.0
            fully_filled = True
            break
        shares += level.size
        spent += level_cost
        remaining -= level_cost

    avg_price = spent / shares if shares > 0.0 else 0.0
    return avg_price, shares, fully_filled


def limit_price_for_edge(fair: float, min_edge: float, tick: float = DEFAULT_TICK) -> float | None:
    """Highest maker price (fee 0) that still clears `min_edge`.

    floor((fair - min_edge) / tick) * tick. Flooring to the tick means the resting order
    never sits above the target; the +1e-9 guards against float error turning an exact
    53.0 into 52.999... . Returns None when the price would be 0 or below. Raises
    ValueError unless tick > 0.
    """
    if tick <= 0.0:
        raise ValueError(f"tick must be > 0, got {tick}")
    ticks = math.floor((fair - min_edge) / tick + 1e-9)
    price = round(ticks * tick, 10)  # strip float dust such as 0.5300000000000001
    return price if price > 0.0 else None


# ----------------------------------------------------------------------------- assembly


def build_opportunity(
    market: PmMarket,
    outcome_index: int,
    book: OrderBook | None,
    fair: FairProb,
    prefs: PrefsLike,
    book_game: BookGame | None,
    now: datetime,
) -> Opportunity | None:
    """Assemble an Opportunity for one outcome, or None when it is not worth showing.

    None when there is no usable ask, when `fair - effective_price < prefs.min_edge`, or
    when the market reports liquidity below `prefs.min_liquidity_usd` (unknown liquidity
    passes). The fee is the market's own `taker_fee_rate` when present, else the prefs'.
    """
    if outcome_index not in (0, 1):
        raise ValueError(f"outcome_index must be 0 or 1, got {outcome_index}")
    if book is None or not book.asks:
        return None
    ask = book.best_ask
    if ask is None or ask <= 0.0:
        return None

    fee_rate = market.taker_fee_rate if market.taker_fee_rate is not None else prefs.taker_fee_rate
    cost = effective_price(ask, fee_rate)
    edge_value = edge(fair.value, cost)
    if edge_value < prefs.min_edge:
        return None
    if market.liquidity is not None and market.liquidity < prefs.min_liquidity_usd:
        return None

    kelly = kelly_fraction(fair.value, cost)
    suggested_stake = stake_for(prefs.bankroll, kelly, prefs.kelly_fraction, prefs.max_stake_pct)
    if suggested_stake > 0.0:
        fill_price, _shares, fill_complete = walk_asks(book.asks, suggested_stake, fee_rate)
    else:
        fill_price, fill_complete = None, True

    tick = market.tick_size or DEFAULT_TICK
    return Opportunity(
        market=market,
        outcome_index=outcome_index,
        ask=ask,
        effective_price=cost,
        fair=fair,
        edge=edge_value,
        ev_per_dollar=ev_per_dollar(fair.value, cost),
        kelly=kelly,
        suggested_stake=suggested_stake,
        fill_price=fill_price,
        fill_complete=fill_complete,
        limit_price=limit_price_for_edge(fair.value, prefs.min_edge, tick),
        book_game=book_game,
        computed_at=now,
    )


__all__ = [
    "DEFAULT_TICK",
    "build_opportunity",
    "edge",
    "effective_price",
    "ev_per_dollar",
    "kelly_fraction",
    "limit_price_for_edge",
    "stake_for",
    "taker_fee_per_share",
    "walk_asks",
]
