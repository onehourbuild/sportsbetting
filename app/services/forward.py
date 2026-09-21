"""The forward test: record every priced outcome, grade it when the game resolves.

Why this exists. `Opportunity` rows are only written for outcomes that already clear
`prefs.min_edge`, so they can never answer "was 2% the right threshold?" — they are the
2% bets. And with ESPN as the only book almost nothing clears 2% at all (a live MLB slate
topped out around +0.17%), so a ledger built from opportunities alone stays empty forever
and there is nothing to learn from.

So a `ForwardSample` is written for **every** outcome the scan can price, whatever its
edge, and graded from Polymarket's own resolution once the market closes. The threshold
question then becomes a query over stored rows rather than a decision made in advance.

Nothing here places or suggests a bet. It records what the app saw and what happened next.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.edge import effective_price
from app.core.types import FairProb, OrderBook, PmMarket
from app.models import ForwardSample

log = logging.getLogger(__name__)

# A market resolves some time after kickoff; do not go looking for a result before then.
SETTLE_AFTER_START = timedelta(hours=3)
# Cap the per-scan settlement fetches: each is one GET /markets/{id}.
MAX_SETTLE_PER_SCAN = 250

# Report buckets. Edges are fractions of probability, so 0.02 is "two points of edge".
DEFAULT_THRESHOLDS: tuple[float, ...] = (0.0, 0.005, 0.01, 0.02, 0.03, 0.05)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _top_ask_usd(book: OrderBook) -> float | None:
    """Dollars resting at the best ask. A big edge on $12 of depth is not a bet, and the
    report needs to be able to say so."""
    if not book.asks:
        return None
    best = min(level.price for level in book.asks)
    return round(sum(lvl.price * lvl.size for lvl in book.asks if lvl.price <= best + 1e-12), 2)


def record_sample(
    session: Session,
    *,
    scan_id: int,
    market: PmMarket,
    outcome_index: int,
    book: OrderBook | None,
    fair: FairProb,
    prefs_fee_rate: float,
    now: datetime,
) -> ForwardSample | None:
    """Write one sample, or None when the outcome is not a priceable pre-game bet.

    The tradability gates mirror `edge.build_opportunity` deliberately — a started game's
    Polymarket price reflects the live score while the book snapshot is pre-game, so
    recording it would grade a comparison that was never valid. What is NOT mirrored is
    the `min_edge` cut: that is the whole point.
    """
    if outcome_index not in (0, 1):
        raise ValueError(f"outcome_index must be 0 or 1, got {outcome_index}")
    if market.closed or not market.accepting_orders:
        return None
    start = _as_utc(market.game_start)
    if start is not None and start <= _as_utc(now):
        return None
    if book is None or not book.asks:
        return None
    ask = book.best_ask
    if ask is None or ask <= 0.0 or ask >= 1.0:
        return None

    outcome = market.outcomes[outcome_index]
    fee_rate = market.taker_fee_rate if market.taker_fee_rate is not None else prefs_fee_rate
    cost = effective_price(ask, fee_rate)
    hours = None if start is None else round((start - _as_utc(now)).total_seconds() / 3600.0, 3)

    row = ForwardSample(
        scan_id=scan_id,
        market_id=market.market_id,
        token=outcome.token_id,
        outcome_index=outcome_index,
        outcome_name=outcome.name,
        outcome_key=outcome.team_key,
        league=market.league,
        market_type=market.market_type,
        line=market.line,
        ask=ask,
        effective_price=cost,
        fee_rate=fee_rate,
        top_ask_usd=_top_ask_usd(book),
        liquidity=market.liquidity,
        fair_prob=fair.value,
        fair_method=fair.method,
        n_books=fair.n_books,
        books_used=list(fair.books_used),
        edge=fair.value - cost,
        game_start=start,
        hours_to_start=hours,
        sampled_at=now,
    )
    session.add(row)
    return row


def pending_market_ids(session: Session, now: datetime) -> list[str]:
    """Markets holding ungraded samples whose game should have finished by now."""
    cutoff = _as_utc(now) - SETTLE_AFTER_START
    rows = session.execute(
        select(ForwardSample.market_id)
        .where(ForwardSample.won.is_(None))
        .where(ForwardSample.game_start.is_not(None))
        .where(ForwardSample.game_start <= cutoff)
        .distinct()
        .limit(MAX_SETTLE_PER_SCAN)
    )
    return [market_id for (market_id,) in rows]


def settle(session: Session, markets_by_id: Mapping[str, PmMarket], now: datetime) -> int:
    """Grade every pending sample whose market has resolved. Returns the number graded.

    A win pays $1 per share bought at `effective_price`, so one dollar staked returns
    `(1 - price) / price`; a loss returns -1. Both are *net of the taker fee*, because the
    fee is already inside `effective_price`.
    """
    graded = 0
    for market_id, market in markets_by_id.items():
        if market.resolved_outcome_index is None:
            continue
        pending = list(
            session.scalars(
                select(ForwardSample)
                .where(ForwardSample.market_id == market_id)
                .where(ForwardSample.won.is_(None))
            )
        )
        for row in pending:
            won = row.outcome_index == market.resolved_outcome_index
            price = row.effective_price
            row.won = won
            row.settled_at = _as_utc(now)
            row.pnl_per_dollar = ((1.0 - price) / price) if won else -1.0
            graded += 1
    if graded:
        # Flush before returning: a second call in the same session must see these rows as
        # graded, or a retry regrades them and double-counts.
        session.flush()
        log.info("forward test: graded %d sample(s)", graded)
    return graded


def capture_closing(session: Session, market_ids: Sequence[str]) -> int:
    """Fill `closing_pm_price` / `clv` for graded samples, from the last pre-kickoff ask
    this app itself recorded for that token.

    CLV here is `closing_ask - ask`: positive means we saw a cheaper price than the market
    settled on, which is the standard "did you beat the close" signal and is far less noisy
    than profit over a few hundred bets. It is only meaningful once there are several scans
    per game, so early on it will be mostly zero (one sample = its own close).
    """
    filled = 0
    for market_id in market_ids:
        rows = list(
            session.scalars(
                select(ForwardSample)
                .where(ForwardSample.market_id == market_id)
                .order_by(ForwardSample.sampled_at)
            )
        )
        by_token: dict[str, list[ForwardSample]] = {}
        for row in rows:
            by_token.setdefault(row.token, []).append(row)
        for samples in by_token.values():
            closing = samples[-1].ask
            for row in samples:
                if row.closing_pm_price is None:
                    row.closing_pm_price = closing
                    row.clv = round(closing - row.ask, 6)
                    filled += 1
    return filled


def report(
    session: Session, thresholds: Sequence[float] = DEFAULT_THRESHOLDS, league: str | None = None
) -> dict:
    """What every edge threshold would have returned, over the graded samples.

    One row per threshold: how many bets it would have taken, how often they won, the
    return per dollar staked, and the average closing-line value. `roi` is the mean of
    `pnl_per_dollar`, i.e. profit per dollar staked, not per dollar of bankroll.
    """
    query = select(ForwardSample).where(ForwardSample.won.is_not(None))
    if league:
        query = query.where(ForwardSample.league == league)
    graded = list(session.scalars(query))

    pending = session.scalar(
        select(func.count()).select_from(ForwardSample).where(ForwardSample.won.is_(None))
    )

    buckets = []
    for threshold in thresholds:
        taken = [row for row in graded if row.edge >= threshold]
        n = len(taken)
        wins = sum(1 for row in taken if row.won)
        pnls = [row.pnl_per_dollar for row in taken if row.pnl_per_dollar is not None]
        clvs = [row.clv for row in taken if row.clv is not None]
        buckets.append(
            {
                "threshold": threshold,
                "n": n,
                "wins": wins,
                "win_rate": (wins / n) if n else None,
                "roi": (sum(pnls) / len(pnls)) if pnls else None,
                "total_pnl_per_dollar": round(sum(pnls), 4) if pnls else None,
                "avg_clv": (sum(clvs) / len(clvs)) if clvs else None,
            }
        )
    return {
        "graded": len(graded),
        "pending": int(pending or 0),
        "league": league or "all",
        "buckets": buckets,
    }


def format_report(data: dict) -> str:
    """The report as a text table for the CLI."""
    lines = [
        # ASCII only: this prints to a Windows console using cp1252, where an em dash
        # comes out as a replacement character.
        f"Forward test - {data['graded']} graded sample(s), {data['pending']} pending "
        f"({data['league']})",
    ]
    if not data["graded"]:
        lines.append("")
        lines.append("Nothing graded yet. Samples are graded once their game resolves,")
        lines.append("so run scans regularly and come back after some games have finished.")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"{'min edge':>9}  {'bets':>6}  {'won':>6}  {'win%':>7}  {'ROI/$':>8}  {'CLV':>8}")
    for bucket in data["buckets"]:
        win = "-" if bucket["win_rate"] is None else f"{bucket['win_rate'] * 100:6.1f}%"
        roi = "-" if bucket["roi"] is None else f"{bucket['roi'] * 100:+7.2f}%"
        clv = "-" if bucket["avg_clv"] is None else f"{bucket['avg_clv'] * 100:+7.2f}c"
        lines.append(
            f"{bucket['threshold'] * 100:8.1f}%  {bucket['n']:6d}  {bucket['wins']:6d}  "
            f"{win:>7}  {roi:>8}  {clv:>8}"
        )
    lines.append("")
    lines.append("ROI/$ is profit per dollar staked, net of the Polymarket taker fee.")
    lines.append("Treat anything under a few hundred bets as noise, not a result.")
    return "\n".join(lines)


def prune_market(session: Session, market_id: str) -> None:
    """Drop samples for a market that can never be graded (used by demo-clear)."""
    for row in session.scalars(select(ForwardSample).where(ForwardSample.market_id == market_id)):
        session.delete(row)


__all__ = [
    "DEFAULT_THRESHOLDS",
    "MAX_SETTLE_PER_SCAN",
    "SETTLE_AFTER_START",
    "capture_closing",
    "format_report",
    "pending_market_ids",
    "prune_market",
    "record_sample",
    "report",
    "settle",
]
