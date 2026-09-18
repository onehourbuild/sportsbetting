"""Fee, EV, Kelly, stake sizing, fill price, limit price. Contract: docs/ARCHITECTURE.md.

All prices here are Polymarket share prices in dollars per share, i.e. probabilities in
(0, 1). "Cost" always means the fee-inclusive price a taker actually pays. Pure
functions, no I/O; invalid inputs raise ``ValueError`` instead of producing a number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

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
MIN_STAKE = 1.0  # a suggestion below a dollar is noise, not a bet
EDGE_EPS = 1e-12  # float slack when comparing an edge with min_edge

# Why `suggested_stake` is not simply "what Kelly asked for" (stored on the Opportunity row
# so the UI can say so). See docs/notes_pricing.md.
NOTE_EDGE_CAPPED = "capped at the depth that still clears the minimum edge"
NOTE_NO_EDGE_DEPTH = "less than $1 of depth clears the minimum edge"
NOTE_MIN_ORDER = "{shares:.2f} shares is below the {min_size:g}-share minimum order"


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
    min_stake: float = MIN_STAKE,
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


def edge_clearing_depth(
    asks: Sequence[BookLevel], fair: float, min_edge: float, fee_rate: float
) -> tuple[float, float, bool]:
    """How far up the ladder the edge survives.

    Returns (usd, shares, exhausted): the fee-inclusive dollars and shares available on the
    levels whose *own* effective price still satisfies ``fair - cost >= min_edge``, and
    whether the walk ran out of ladder (``True``) rather than out of edge (``False``).

    ``walk_asks`` prices a given spend; this says how much may be spent at all. The
    distinction matters: a thin ladder whose every level is cheap is a *liquidity* limit
    (buy what is there), while a ladder whose next level is dearer than fair is an *edge*
    limit (buying it is negative EV, so the stake must be cut). Contract note: this is a
    separate step, so ``walk_asks``' hand-checked vectors are untouched.
    """
    usd = 0.0
    shares = 0.0
    for level in sorted(asks, key=lambda lvl: lvl.price):
        if level.size <= 0.0:
            continue
        cost = effective_price(level.price, fee_rate)
        if cost <= 0.0:
            continue  # a free share is a data error, not depth
        if fair - cost < min_edge - EDGE_EPS:
            return usd, shares, False
        usd += level.size * cost
        shares += level.size
    return usd, shares, True


@dataclass(frozen=True)
class StakePlan:
    """What to suggest staking, how it would fill, and why it is not more."""

    stake: float
    fill_price: float | None
    fill_complete: bool
    fill_usd: float | None
    note: str | None


def plan_stake(
    asks: Sequence[BookLevel],
    fair: float,
    kelly: float,
    fee_rate: float,
    prefs: PrefsLike,
    min_order_size: float | None = None,
) -> StakePlan:
    """Size the bet: fractional Kelly, capped by the bankroll rules, by the depth that still
    clears ``prefs.min_edge``, and by the market's minimum order size.

    Sizing at the best ask alone is how a recommendation turns negative: the money above the
    first level buys shares that are dearer than fair. So the Kelly stake is truncated to
    ``edge_clearing_depth`` whenever the ladder runs out of *edge*, and the truncation is
    reported as a partial fill (``fill_complete`` False, ``fill_usd`` = the edge-clearing
    dollars) so the caller prefills the fillable amount instead of the Kelly amount. When the
    ladder merely runs out of *shares* the stake stands and ``walk_asks`` reports the partial
    fill, exactly as before.

    A stake that would buy fewer shares than ``min_order_size`` cannot be placed at all, so it
    is reported as 0 with a note rather than as an order the exchange would reject.
    """
    wanted = stake_for(prefs.bankroll, kelly, prefs.kelly_fraction, prefs.max_stake_pct)
    if wanted <= 0.0:
        return StakePlan(0.0, None, True, None, None)

    clearing_usd, _clearing_shares, exhausted = edge_clearing_depth(
        asks, fair, prefs.min_edge, fee_rate
    )
    stake, capped = wanted, False
    if not exhausted and wanted > clearing_usd:
        # Floor to the cent: rounding up would buy a share the edge does not cover.
        stake = math.floor(clearing_usd * 10**STAKE_DECIMALS) / 10**STAKE_DECIMALS
        capped = True
        if stake < MIN_STAKE:
            return StakePlan(0.0, None, True, None, NOTE_NO_EDGE_DEPTH)

    fill_price, shares, complete = walk_asks(asks, stake, fee_rate)
    if min_order_size is not None and min_order_size > 0.0 and shares < min_order_size:
        return StakePlan(
            0.0,
            None,
            True,
            None,
            NOTE_MIN_ORDER.format(shares=shares, min_size=min_order_size),
        )
    if capped:
        return StakePlan(stake, fill_price, False, round(stake, STAKE_DECIMALS), NOTE_EDGE_CAPPED)
    fill_usd = None if complete else round(fill_price * shares, STAKE_DECIMALS)
    return StakePlan(stake, fill_price, complete, fill_usd, None)


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


def resting_limit_price(
    fair: float, min_edge: float, best_ask: float | None, tick: float = DEFAULT_TICK
) -> float | None:
    """`limit_price_for_edge` capped one tick below the best ask so the order can rest.

    A bid at or above the best ask crosses the book and fills as a taker (with the taker
    fee), so a "maker" price there is fictitious. Returns None when no resting price
    clears `min_edge`.
    """
    price = limit_price_for_edge(fair, min_edge, tick)
    if price is None or best_ask is None:
        return price
    ceiling = round(best_ask - tick, 10)
    if ceiling <= 0.0:
        return None
    return min(price, ceiling)


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

    Also None when the market is not tradable as a pre-game full-game market: closed,
    not accepting orders, or already started (`game_start <= now`). A started game's
    Polymarket price reflects the live score while the book snapshot is pre-game, so the
    difference is not an edge. The scan service skips those markets before pricing; this
    is defence in depth.

    When the ask ladder cannot absorb the suggested stake, `fill_complete` is False and
    `fill_usd` carries the fee-inclusive dollars the ladder can take at `fill_price`. The
    stake itself is capped at the depth that still clears `prefs.min_edge` (see
    `plan_stake`), so the suggestion is never sized on the best ask alone, and a stake that
    would buy fewer shares than the market's minimum order size is reported as 0.
    `stake_note` re-derives why for the caller that stores the row.
    """
    if outcome_index not in (0, 1):
        raise ValueError(f"outcome_index must be 0 or 1, got {outcome_index}")
    if market.closed or not market.accepting_orders:
        return None
    if market.game_start is not None and _as_utc(market.game_start) <= _as_utc(now):
        return None
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
    plan = plan_stake(book.asks, fair.value, kelly, fee_rate, prefs, market.min_order_size)

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
        suggested_stake=plan.stake,
        fill_price=plan.fill_price,
        fill_complete=plan.fill_complete,
        limit_price=resting_limit_price(fair.value, prefs.min_edge, ask, tick),
        book_game=book_game,
        computed_at=now,
        fill_usd=plan.fill_usd,
    )


def stake_note(opportunity: Opportunity, book: OrderBook | None, prefs: PrefsLike) -> str | None:
    """Why `opportunity.suggested_stake` is what it is, or None when there is nothing to say.

    A pure re-derivation from the same inputs `build_opportunity` used (the `Opportunity`
    dataclass is the module contract and has no note field), so the scan service can persist
    the reason on the row without the pricing logic living in two places.
    """
    if book is None:
        return None
    market = opportunity.market
    fee_rate = market.taker_fee_rate if market.taker_fee_rate is not None else prefs.taker_fee_rate
    plan = plan_stake(
        book.asks, opportunity.fair.value, opportunity.kelly, fee_rate, prefs, market.min_order_size
    )
    return plan.note


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_TICK",
    "MIN_STAKE",
    "NOTE_EDGE_CAPPED",
    "NOTE_MIN_ORDER",
    "NOTE_NO_EDGE_DEPTH",
    "StakePlan",
    "build_opportunity",
    "edge",
    "edge_clearing_depth",
    "effective_price",
    "ev_per_dollar",
    "kelly_fraction",
    "limit_price_for_edge",
    "plan_stake",
    "resting_limit_price",
    "stake_for",
    "stake_note",
    "taker_fee_per_share",
    "walk_asks",
]
