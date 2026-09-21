"""The back test: harvest Polymarket's resolved sports markets and grade its own prices.

What this can and cannot answer, stated up front because the limit is the whole story.

It CANNOT test the app's actual strategy. That strategy is "Polymarket price vs de-vigged
book consensus", and historical book lines are paid data (The Odds API keeps them back to
June 2020; ESPN strips odds off finished games entirely). With no historical book there is
no historical fair value and no historical edge.

It CAN test the question underneath: **is Polymarket itself well calibrated?** For every
resolved game market we know the price it last traded at before kickoff and whether that
outcome then won. If 60c favourites win 60% of the time, there is no free money in the
price alone and any edge has to come from the books. If they win 64%, that is an edge that
needs no book data at all. The classic failure mode of betting markets — the
favourite-longshot bias, where 5c longshots win far less than 5% of the time — shows up
here directly.

Two sources, chosen because they are the ones that survive resolution:

* Gamma `/events?tag_slug=<league>` — resolved markets keep `outcomePrices`, which becomes
  ["1","0"] or ["0","1"] and names the winner.
* `data-api.polymarket.com/trades?market=<conditionId>` — still lists every individual
  trade, with a timestamp, long after the market closed.

Deliberately NOT used: CLOB `/prices-history`, which returns an empty series for closed
markets (verified against markets up to $400M of volume).

Coverage ceiling: Polymarket's per-game sports markets begin Oct 2023 (NFL), Dec 2023
(NBA) and Aug 2024 (MLB). Asking for five years is asking for data that does not exist.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.edge import taker_fee_per_share
from app.models import HistoricalSample

log = logging.getLogger(__name__)

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
TRADES = "https://data-api.polymarket.com/trades"

LEAGUES: tuple[str, ...] = ("nfl", "nba", "mlb")
GAME_MARKET_TYPES: frozenset[str] = frozenset({"moneyline", "spreads", "totals"})
# Gamma pages events; 100 is its practical maximum per request.
PAGE_SIZE = 100
TRADE_LIMIT = 1000
# Commit this often so an interrupted harvest keeps everything it already fetched.
COMMIT_EVERY = 50
# data-api sits behind Cloudflare and answers 429 ("Error 1015") under a fast loop -- a
# full-speed harvest was cut off after ~700 markets. A small pause between trade requests
# and a backoff on 429 turns a run that dies into one that finishes.
TRADE_PAUSE_S = 0.12
RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (5.0, 20.0, 60.0)
# A "closing" price from a day before kickoff is a stale market, not a close.
DEFAULT_MAX_CLOSE_AGE_HOURS = 12.0
# How far a market's two closing prices may sum from 1.00 and still count as one quote.
# Last-traded prices straddle the spread, so the real pairs sit a little over 1 (observed
# median 1.010); anything far off means the two sides last traded at different moments.
DEFAULT_MAX_PAIR_ERROR = 0.06

# Price bands for the calibration table. Chosen so each band is wide enough to hold a
# usable number of games rather than to flatter any particular result.
DEFAULT_BANDS: tuple[tuple[float, float], ...] = (
    (0.01, 0.10),
    (0.10, 0.20),
    (0.20, 0.35),
    (0.35, 0.50),
    (0.50, 0.65),
    (0.65, 0.80),
    (0.80, 0.90),
    (0.90, 0.99),
)


@dataclass(frozen=True)
class HarvestStats:
    events: int = 0
    markets_seen: int = 0
    resolved: int = 0
    stored: int = 0
    skipped_no_trades: int = 0
    skipped_existing: int = 0

    def merge(self, **deltas: int) -> HarvestStats:
        fields = {f: getattr(self, f) + deltas.get(f, 0) for f in self.__annotations__}
        return HarvestStats(**fields)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_gamma_time(value: object) -> datetime | None:
    """Gamma sends both "2024-09-27 02:10:00+00" and ISO-8601 with a Z."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if text.endswith("+00"):
        text = text[:-3] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def resolved_index(outcome_prices: object) -> int | None:
    """Which outcome won, from `outcomePrices` (a JSON-encoded string list)."""
    raw = outcome_prices
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        values = [float(x) for x in raw]
    except (TypeError, ValueError):
        return None
    # A resolved market pays exactly one side.
    if abs(values[0] - 1.0) < 1e-9 and abs(values[1]) < 1e-9:
        return 0
    if abs(values[1] - 1.0) < 1e-9 and abs(values[0]) < 1e-9:
        return 1
    return None


def closing_trade(
    trades: Sequence[dict], outcome_index: int, kickoff: datetime
) -> tuple[float, datetime, int] | None:
    """(price, when, n_pre_kickoff_trades) of the last trade on this outcome before kickoff.

    Trades after kickoff are the in-play market pricing a live score and would grade a bet
    nobody could have placed, so they are dropped rather than used as a fallback.
    """
    kickoff = _as_utc(kickoff)  # type: ignore[assignment]
    best: tuple[float, datetime] | None = None
    count = 0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        try:
            if int(trade.get("outcomeIndex", -1)) != outcome_index:
                continue
            when = datetime.fromtimestamp(int(trade["timestamp"]), tz=UTC)
            price = float(trade["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if when >= kickoff or not (0.0 < price < 1.0):
            continue
        count += 1
        if best is None or when > best[1]:
            best = (price, when)
    if best is None:
        return None
    return best[0], best[1], count


def iter_resolved_markets(
    get_json: Callable[[str, dict], object],
    league: str,
    *,
    max_pages: int = 60,
    since: datetime | None = None,
) -> Iterator[tuple[dict, dict, int, datetime]]:
    """Yield (event, market, winning_index, kickoff) for resolved per-game markets.

    Paging is by `startDate` ascending so a partial run always covers a contiguous, oldest-
    first slice rather than a random sample.
    """
    for page in range(max_pages):
        params = {
            "tag_slug": league,
            "limit": str(PAGE_SIZE),
            "offset": str(page * PAGE_SIZE),
            "order": "startDate",
            "ascending": "true",
        }
        events = get_json(GAMMA_EVENTS, params)
        if not isinstance(events, list) or not events:
            return
        for event in events:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                if market.get("sportsMarketType") not in GAME_MARKET_TYPES:
                    continue
                if not market.get("closed"):
                    continue
                kickoff = parse_gamma_time(market.get("gameStartTime"))
                if kickoff is None:
                    continue
                if since is not None and kickoff < since:
                    continue
                index = resolved_index(market.get("outcomePrices"))
                if index is None:
                    continue
                yield event, market, index, kickoff


def _outcome_names(market: dict) -> list[str]:
    raw = market.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _is_rate_limited(exc: BaseException) -> bool:
    return getattr(exc, "status", None) == 429


def _fetch_trades(
    get_json: Callable[[str, dict], object],
    condition_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Trades for one market, backing off when Cloudflare rate-limits us.

    Re-raises anything that is not a 429: a harvest that quietly swallowed real errors
    would look like "this market had no trades" and silently thin the sample.
    """
    params = {"market": condition_id, "limit": str(TRADE_LIMIT)}
    for delay in RATE_LIMIT_BACKOFF_S:
        try:
            return get_json(TRADES, params)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless it is a 429
            if not _is_rate_limited(exc):
                raise
            log.warning("data-api rate limited; waiting %ss", delay)
            sleep(delay)
    return get_json(TRADES, params)


def harvest(
    session: Session,
    get_json: Callable[[str, dict], object],
    *,
    sleep: Callable[[float], None] = time.sleep,
    leagues: Sequence[str] = LEAGUES,
    max_pages: int = 60,
    since: datetime | None = None,
    limit: int | None = None,
    on_progress: Callable[[HarvestStats], None] | None = None,
) -> HarvestStats:
    """Walk resolved markets and store one sample per outcome. Resumable: a market already
    in the table is skipped, so an interrupted run costs nothing to repeat."""
    stats = HarvestStats()
    last_commit = 0
    known = {
        market_id for (market_id,) in session.execute(select(HistoricalSample.market_id).distinct())
    }
    for league in leagues:
        for event, market, winner, kickoff in iter_resolved_markets(
            get_json, league, max_pages=max_pages, since=since
        ):
            stats = stats.merge(markets_seen=1, resolved=1)
            market_id = str(market.get("id") or "")
            if not market_id:
                continue
            if market_id in known:
                stats = stats.merge(skipped_existing=1)
                continue
            condition_id = market.get("conditionId")
            if not condition_id:
                continue

            trades = _fetch_trades(get_json, str(condition_id), sleep=sleep)
            if not isinstance(trades, list) or not trades:
                stats = stats.merge(skipped_no_trades=1)
                known.add(market_id)
                continue

            names = _outcome_names(market)
            stored_any = False
            for index in (0, 1):
                found = closing_trade(trades, index, kickoff)
                if found is None:
                    continue
                price, when, n_pre = found
                session.add(
                    HistoricalSample(
                        market_id=market_id,
                        condition_id=str(condition_id),
                        league=league,
                        market_type=str(market.get("sportsMarketType")),
                        line=(
                            float(market["line"])
                            if isinstance(market.get("line"), int | float)
                            else None
                        ),
                        question=str(market.get("question") or event.get("title") or ""),
                        outcome_index=index,
                        outcome_name=names[index] if index < len(names) else "",
                        game_start=kickoff,
                        close_price=price,
                        close_trade_at=when,
                        close_age_hours=round((kickoff - when).total_seconds() / 3600.0, 3),
                        n_trades_pre=n_pre,
                        won=(index == winner),
                    )
                )
                stored_any = True
                stats = stats.merge(stored=1)
            known.add(market_id)
            if not stored_any:
                stats = stats.merge(skipped_no_trades=1)
            sleep(TRADE_PAUSE_S)
            if stats.stored and stats.stored - last_commit >= COMMIT_EVERY:
                # COMMIT, not just flush: "already stored, skip it" reads committed rows, so
                # an interrupted run only keeps its place if the work is actually durable.
                # Flushing alone made a 40-minute run that was cancelled worth nothing.
                session.commit()
                last_commit = stats.stored
                if on_progress is not None:
                    on_progress(stats)
            if limit is not None and stats.stored >= limit:
                session.commit()
                return stats
    session.commit()
    return stats


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def significance(n: int, wins: int, implied: float) -> tuple[float, float, float]:
    """(standard error, z, two-sided p) for "this band beat its own price".

    Without this the table invites exactly the mistake the exercise exists to avoid. On the
    first real harvest the 65-80c band came out +7.0% over its implied probability, which
    reads like a found edge -- but on 102 outcomes the standard error is 4.6%, so z = +1.5
    and p = 0.12. Tuning a strategy to that band would have been fitting noise. The null
    being tested is "the price is right", so the standard error uses the implied
    probability, not the observed one.
    """
    if n <= 0:
        return 0.0, 0.0, 1.0
    se = math.sqrt(max(implied * (1.0 - implied), 0.0) / n)
    if se <= 0.0:
        return 0.0, 0.0, 1.0
    z = ((wins / n) - implied) / se
    return se, z, 2.0 * (1.0 - _normal_cdf(abs(z)))


def _paired_only(rows: Sequence[HistoricalSample], max_pair_error: float) -> list[HistoricalSample]:
    """Keep only markets whose two sides form one believable simultaneous quote.

    Each side's close is its own last trade, so an illiquid side can carry a price from
    hours earlier than the other. That is not a quote anyone could have traded against,
    and it is not harmless: on live NFL data the unpaired 90-99c band read -7.8% against
    its implied probability at p=0.003, the only "significant" result in the whole table.
    Requiring a fresh two-sided quote cut that band from 91 outcomes to 36 and the gap to
    -1.2%, and tightening the freshness further flipped its sign. A finding that changes
    direction with the staleness filter is an artefact of the filter.
    """
    by_market: dict[str, list[HistoricalSample]] = {}
    for row in rows:
        by_market.setdefault(row.market_id, []).append(row)
    kept: list[HistoricalSample] = []
    for sides in by_market.values():
        if len(sides) != 2:
            continue
        if abs(sum(side.close_price for side in sides) - 1.0) > max_pair_error:
            continue
        kept.extend(sides)
    return kept


def report(
    session: Session,
    *,
    league: str | None = None,
    market_type: str | None = None,
    bands: Sequence[tuple[float, float]] = DEFAULT_BANDS,
    fee_rate: float = 0.05,
    max_close_age_hours: float | None = DEFAULT_MAX_CLOSE_AGE_HOURS,
    paired: bool = False,
    max_pair_error: float = DEFAULT_MAX_PAIR_ERROR,
) -> dict:
    """Calibration and return by price band.

    `implied` is the average price paid in the band, i.e. what the market said the chance
    was. `actual` is how often those outcomes actually won. `roi_net` is the return per
    dollar staked after the Polymarket taker fee, which is what you would really have made
    buying every outcome in that band at the close.
    """
    query = select(HistoricalSample)
    if league:
        query = query.where(HistoricalSample.league == league)
    if market_type:
        query = query.where(HistoricalSample.market_type == market_type)
    if max_close_age_hours is not None:
        query = query.where(HistoricalSample.close_age_hours <= max_close_age_hours)
    rows = list(session.scalars(query))
    if paired:
        rows = _paired_only(rows, max_pair_error)

    total = session.scalar(select(func.count()).select_from(HistoricalSample)) or 0
    span = session.execute(
        select(func.min(HistoricalSample.game_start), func.max(HistoricalSample.game_start))
    ).first()

    out = []
    for low, high in bands:
        band = [r for r in rows if low <= r.close_price < high]
        n = len(band)
        if not n:
            out.append({"low": low, "high": high, "n": 0})
            continue
        wins = sum(1 for r in band if r.won)
        implied = sum(r.close_price for r in band) / n
        gross = sum(((1.0 - r.close_price) / r.close_price) if r.won else -1.0 for r in band) / n
        net_values = []
        for r in band:
            cost = r.close_price + taker_fee_per_share(r.close_price, fee_rate)
            net_values.append(((1.0 - cost) / cost) if r.won else -1.0)
        se, z, p_value = significance(n, wins, implied)
        out.append(
            {
                "low": low,
                "high": high,
                "n": n,
                "wins": wins,
                "implied": implied,
                "actual": wins / n,
                "calibration": (wins / n) - implied,
                "roi_gross": gross,
                "roi_net": sum(net_values) / len(net_values),
                "std_error": se,
                "z": z,
                "p_value": p_value,
                "significant": p_value < 0.05,
            }
        )
    return {
        "rows_used": len(rows),
        "rows_total": int(total),
        "paired": paired,
        "league": league or "all",
        "market_type": market_type or "all",
        "fee_rate": fee_rate,
        "max_close_age_hours": max_close_age_hours,
        "first_game": span[0] if span else None,
        "last_game": span[1] if span else None,
        "bands": out,
    }


def format_report(data: dict) -> str:
    """ASCII table (his console is cp1252, so no box drawing and no dashes-that-aren't)."""
    lines = [
        f"Back test - {data['rows_used']} graded outcome(s) of {data['rows_total']} harvested "
        f"({data['league']}, {data['market_type']})",
    ]
    if data.get("paired"):
        lines.append(
            "Paired quotes only: both sides of the market, priced together (sum within "
            f"{DEFAULT_MAX_PAIR_ERROR:.0%} of 1.00)."
        )
    if data["first_game"] and data["last_game"]:
        lines.append(
            f"Games from {data['first_game']:%Y-%m-%d} to {data['last_game']:%Y-%m-%d}; "
            f"close taken within {data['max_close_age_hours']}h of kickoff."
        )
    if not data["rows_used"]:
        lines.append("")
        lines.append("Nothing harvested yet. Run: python -m app.cli backtest-harvest")
        return "\n".join(lines)
    lines.append("")
    lines.append(
        f"{'price band':>12}  {'n':>6}  {'implied':>8}  {'actual':>8}  {'diff':>8}  "
        f"{'ROI/$':>8}  {'p':>6}"
    )
    any_significant = False
    for band in data["bands"]:
        label = f"{band['low'] * 100:.0f}-{band['high'] * 100:.0f}c"
        if not band["n"]:
            lines.append(f"{label:>12}  {0:6d}  {'-':>8}  {'-':>8}  {'-':>8}  {'-':>8}  {'-':>6}")
            continue
        mark = " *" if band["significant"] else ""
        any_significant = any_significant or band["significant"]
        lines.append(
            f"{label:>12}  {band['n']:6d}  {band['implied'] * 100:7.1f}%  "
            f"{band['actual'] * 100:7.1f}%  {band['calibration'] * 100:+7.1f}%  "
            f"{band['roi_net'] * 100:+7.1f}%  {band['p_value']:6.3f}{mark}"
        )
    lines.append("")
    lines.append(
        f"implied = average price paid; actual = how often it won; ROI/$ is net of the "
        f"{data['fee_rate']:.0%} taker fee."
    )
    lines.append("p is the chance of a gap this big if the price were exactly right. A band")
    lines.append("counts as an edge only at p < 0.05 (marked *). Everything else is noise,")
    lines.append("however good its ROI looks - a big diff on a small n is the usual trap.")
    if not any_significant:
        lines.append("")
        lines.append("No band beat its own price significantly: on this data Polymarket is")
        lines.append("priced about right, and any edge would have to come from the books.")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_BANDS",
    "DEFAULT_MAX_CLOSE_AGE_HOURS",
    "DEFAULT_MAX_PAIR_ERROR",
    "GAME_MARKET_TYPES",
    "HarvestStats",
    "closing_trade",
    "format_report",
    "harvest",
    "iter_resolved_markets",
    "parse_gamma_time",
    "report",
    "resolved_index",
    "significance",
]
