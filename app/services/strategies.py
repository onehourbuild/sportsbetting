"""A bake-off: score many betting rules over the same harvested history.

`backtest.report` answers one question -- is this price band calibrated -- and answers it
for every band at once. This module answers a different one: **given only what was knowable
before kickoff, which rule for choosing bets would actually have made money?** A rule here
is any function of price, league, market type, side, liquidity or freshness. None of them
may look at the result, and none of them may look at a book line, because the harvested
history has no book line in it -- these are rules about Polymarket's own price.

Two things make an exercise like this dangerous, and both are handled here rather than left
to whoever reads the table.

**Testing many rules manufactures winners.** At p < 0.05 one rule in twenty clears the bar
by luck, and this file defines more than twenty. So the table is reported next to a
permutation null: the same rules, scored against results shuffled *within narrow price
bands*, which preserves the market's calibration exactly and destroys any real signal.
Running that a few hundred times gives the distribution of the best ROI obtainable from
this many rules on noise alone. If the best real rule does not beat that, there is no
finding, whatever its own p value says.

**A rule found in the data has not been tested on the data.** Each league's history is cut
in half at its own median kickoff; rules are ranked on the early half and re-scored,
unchanged, on the late half. A rule that only works in the half that chose it is a name for
noise.

The defaults are the conservative ones learned the hard way elsewhere in this project:
paired quotes only, and a closing trade within three hours of kickoff. See
`backtest.paired_only` for why an unpaired close is not a price anyone could have bet.
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HistoricalSample
from app.services.backtest import DEFAULT_MAX_PAIR_ERROR, normal_cdf, paired_only

# polymarket.us charges 0.0695, not the 0.05 polymarket.com charges, and the owner trades
# .us -- see docs/STATUS-2026-09-21.md. A fee set too low manufactures edges, so the
# bake-off prices every bet at the rate actually charged.
DEFAULT_FEE_RATE = 0.0695
# A close older than this is not a price anyone could have taken near kickoff.
DEFAULT_MAX_CLOSE_AGE_HOURS = 3.0
# Permutation settings. The band is narrow enough that shuffling inside it cannot change
# what the market said, only who happened to win.
DEFAULT_PERMUTATIONS = 300
PERMUTATION_BAND = 0.02
# Below this many picks a rule's return is decided by a handful of results. Such rules are
# still scored and printed, but they are kept out of the "best rule" comparisons.
DEFAULT_MIN_N = 200


def effective_cost(price: float, fee_rate: float) -> float:
    """What a taker really pays per share. Mirrors `app.core.edge.effective_price`."""
    return price + fee_rate * price * (1.0 - price)


def pnl_per_dollar(price: float, won: bool, fee_rate: float) -> float:
    """Return on one dollar staked: a share pays $1, so a win returns (1 - cost) / cost."""
    cost = effective_cost(price, fee_rate)
    if cost <= 0.0 or cost >= 1.0:
        return -1.0
    return ((1.0 - cost) / cost) if won else -1.0


# --------------------------------------------------------------------------- the rules


@dataclass(frozen=True)
class Strategy:
    """One rule for choosing what to buy. `select` sees only pre-game facts."""

    key: str
    label: str
    select: Callable[[HistoricalSample], bool]


def _side(row: HistoricalSample) -> str:
    return (row.outcome_name or "").strip().lower()


def _band(lo: float, hi: float) -> Callable[[HistoricalSample], bool]:
    # A factory, not a lambda written inside a loop: a closure over the loop variable would
    # give every band the last band's bounds.
    return lambda row: lo <= row.close_price < hi


def _all(*tests: Callable[[HistoricalSample], bool]) -> Callable[[HistoricalSample], bool]:
    return lambda row: all(test(row) for test in tests)


def _league(name: str) -> Callable[[HistoricalSample], bool]:
    return lambda row: row.league == name


def _market(kind: str) -> Callable[[HistoricalSample], bool]:
    return lambda row: row.market_type == kind


def _named(*names: str) -> Callable[[HistoricalSample], bool]:
    wanted = {name.lower() for name in names}
    return lambda row: _side(row) in wanted


def _index(value: int) -> Callable[[HistoricalSample], bool]:
    return lambda row: row.outcome_index == value


def _favorite(row: HistoricalSample) -> bool:
    return row.close_price >= 0.55


def _underdog(row: HistoricalSample) -> bool:
    return row.close_price <= 0.45


def _liquid(row: HistoricalSample) -> bool:
    return (row.n_trades_pre or 0) >= 50


def _thin(row: HistoricalSample) -> bool:
    return (row.n_trades_pre or 0) < 20


STRATEGIES: tuple[Strategy, ...] = (
    # The baseline. Buying both sides of everything must lose roughly the fee; if it does
    # not, the arithmetic is wrong somewhere and nothing below can be trusted.
    Strategy("everything", "Buy every outcome", lambda row: True),
    # Price shape. A favorite-longshot bias, if Polymarket has one, shows up here.
    Strategy("favorites", "Any favorite (>=55c)", _favorite),
    Strategy("underdogs", "Any underdog (<=45c)", _underdog),
    Strategy("coinflips", "Coin flips (45-55c)", _band(0.45, 0.55)),
    Strategy("heavy_favs", "Heavy favorites (>=80c)", _band(0.80, 1.01)),
    Strategy("longshots", "Longshots (<=20c)", _band(0.0, 0.20)),
    Strategy("mid_favs", "Modest favorites (55-70c)", _band(0.55, 0.70)),
    Strategy("mid_dogs", "Modest underdogs (30-45c)", _band(0.30, 0.45)),
    # Market type: does one kind of bet price worse than another?
    Strategy("moneyline", "Moneylines only", _market("moneyline")),
    Strategy("spreads", "Spreads only", _market("spreads")),
    Strategy("totals", "Totals only", _market("totals")),
    # Sides. On a total the side is Over or Under; a spread market always asks "will <team>
    # win by N or more", so Yes is the favorite covering.
    Strategy("overs", "Overs", _all(_market("totals"), _named("over"))),
    Strategy("unders", "Unders", _all(_market("totals"), _named("under"))),
    # A spread market is written "Spread: Cowboys (-7.5)", and outcome 0 is always that
    # named team covering -- the side laying the points -- with outcome 1 taking them. Only
    # 27 of 2,654 spread rows are the older "Yes"/"No" phrasing, and they index the same
    # way, so the index is the reliable handle and the name is not: matching on "Yes" found
    # 7 picks where the index finds thirteen hundred.
    Strategy("spread_lay", "Spread: lay the points", _all(_market("spreads"), _index(0))),
    Strategy("spread_take", "Spread: take the points", _all(_market("spreads"), _index(1))),
    # League.
    Strategy("nfl", "NFL, everything", _league("nfl")),
    Strategy("mlb", "MLB, everything", _league("mlb")),
    Strategy("nfl_favs", "NFL favorites", _all(_league("nfl"), _favorite)),
    Strategy("nfl_dogs", "NFL underdogs", _all(_league("nfl"), _underdog)),
    Strategy("mlb_favs", "MLB favorites", _all(_league("mlb"), _favorite)),
    Strategy("mlb_dogs", "MLB underdogs", _all(_league("mlb"), _underdog)),
    Strategy("nfl_overs", "NFL overs", _all(_league("nfl"), _market("totals"), _named("over"))),
    Strategy("nfl_unders", "NFL unders", _all(_league("nfl"), _market("totals"), _named("under"))),
    Strategy("ml_favs", "Moneyline favorites", _all(_market("moneyline"), _favorite)),
    Strategy("ml_dogs", "Moneyline underdogs", _all(_market("moneyline"), _underdog)),
    # Liquidity. A thin market is the one most likely to be mispriced and the least likely
    # to let a real bet down, so both directions are worth asking about.
    Strategy("liquid_favs", "Favorites, 50+ pre-game trades", _all(_liquid, _favorite)),
    Strategy("thin_dogs", "Underdogs, under 20 trades", _all(_thin, _underdog)),
    Strategy("liquid_all", "Anything with 50+ trades", _liquid),
)


# --------------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class Score:
    """What one rule did. `implied` is what the market said those picks were worth."""

    key: str
    label: str
    n: int
    wins: int
    win_rate: float | None
    implied: float | None
    roi: float | None
    total_pnl: float | None
    calib_p: float | None
    roi_p: float | None


def picks(strategy: Strategy, rows: Sequence[HistoricalSample]) -> list[int]:
    """Indices of the outcomes this rule buys. Fixed for a given history, which is what
    lets the permutation test reshuffle results without re-running every rule."""
    return [i for i, row in enumerate(rows) if strategy.select(row)]


def _calibration_p(prices: Sequence[float], wins: int) -> float | None:
    """Two-sided p for "these picks won more often than their own prices implied".

    The null is that every price is correct, so the number of winners is a sum of
    independent coin flips with different biases: mean is the sum of the prices, variance
    the sum of p(1-p) -- the normal approximation to a Poisson binomial. Using the average
    price and a plain binomial instead understates the variance whenever a rule spans a
    wide range of prices, which most of these do.
    """
    if not prices:
        return None
    expected = sum(prices)
    variance = sum(price * (1.0 - price) for price in prices)
    if variance <= 0.0:
        return None
    z = (wins - expected) / math.sqrt(variance)
    return 2.0 * (1.0 - normal_cdf(abs(z)))


def _roi_p(pnls: Sequence[float]) -> float | None:
    """Two-sided p for "this rule's return per dollar is not zero".

    This is the money question and it is not the calibration question: a rule can be
    perfectly calibrated and still lose every time, because the fee is charged whether or
    not the price was right.
    """
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    variance = sum((value - mean) ** 2 for value in pnls) / (n - 1)
    if variance <= 0.0:
        return None
    z = mean / math.sqrt(variance / n)
    return 2.0 * (1.0 - normal_cdf(abs(z)))


def score_picks(
    key: str,
    label: str,
    prices: Sequence[float],
    wons: Sequence[bool],
    fee_rate: float,
) -> Score:
    n = len(prices)
    if n == 0:
        return Score(key, label, 0, 0, None, None, None, None, None, None)
    wins = sum(1 for won in wons if won)
    pnls = [pnl_per_dollar(price, won, fee_rate) for price, won in zip(prices, wons, strict=True)]
    return Score(
        key=key,
        label=label,
        n=n,
        wins=wins,
        win_rate=wins / n,
        implied=sum(prices) / n,
        roi=sum(pnls) / n,
        total_pnl=round(sum(pnls), 2),
        calib_p=_calibration_p(prices, wins),
        roi_p=_roi_p(pnls),
    )


def _by_return(score: Score) -> tuple[bool, float]:
    return (score.roi is None, -(score.roi or 0.0))


def bakeoff(
    rows: Sequence[HistoricalSample],
    strategies: Sequence[Strategy] = STRATEGIES,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> list[Score]:
    """Every rule, scored over the same rows, best return first."""
    scores = []
    for strategy in strategies:
        chosen = picks(strategy, rows)
        scores.append(
            score_picks(
                strategy.key,
                strategy.label,
                [rows[i].close_price for i in chosen],
                [bool(rows[i].won) for i in chosen],
                fee_rate,
            )
        )
    return sorted(scores, key=_by_return)


# --------------------------------------------------------------------------- the null


def shuffled_wins(
    rows: Sequence[HistoricalSample], rng: random.Random, band: float = PERMUTATION_BAND
) -> list[bool]:
    """Results reshuffled among outcomes that were priced almost identically.

    Shuffling inside a 2c band leaves the market exactly as well calibrated as it really
    was -- the same number of 60c shots win -- while severing any connection between a
    rule's other criteria (league, side, liquidity) and the result. Anything a rule still
    appears to find afterwards is luck, by construction.
    """
    groups: dict[int, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(int(row.close_price / band), []).append(i)
    out = [False] * len(rows)
    for members in groups.values():
        values = [bool(rows[i].won) for i in members]
        rng.shuffle(values)
        for i, value in zip(members, values, strict=True):
            out[i] = value
    return out


def permutation_null(
    rows: Sequence[HistoricalSample],
    strategies: Sequence[Strategy] = STRATEGIES,
    fee_rate: float = DEFAULT_FEE_RATE,
    reps: int = DEFAULT_PERMUTATIONS,
    min_n: int = DEFAULT_MIN_N,
    seed: int = 1729,
) -> list[float]:
    """The best ROI this many rules reach on shuffled results, once per repetition.

    Returned sorted. Compare the real best against it: the fraction of repetitions that
    matched or beat the real best is the p value of the whole search, rather than of one
    rule considered as though it had been the only one tried.
    """
    rng = random.Random(seed)
    chosen = [(strategy, picks(strategy, rows)) for strategy in strategies]
    eligible = [indexes for _strategy, indexes in chosen if len(indexes) >= min_n]
    # Pre-computed per outcome: a win returns this much per dollar, a loss always -1.
    win_pnl = [
        ((1.0 - cost) / cost) if 0.0 < cost < 1.0 else -1.0
        for cost in (effective_cost(row.close_price, fee_rate) for row in rows)
    ]

    best_per_rep: list[float] = []
    for _ in range(reps):
        wons = shuffled_wins(rows, rng)
        best: float | None = None
        for indexes in eligible:
            roi = sum(win_pnl[i] if wons[i] else -1.0 for i in indexes) / len(indexes)
            if best is None or roi > best:
                best = roi
        if best is not None:
            best_per_rep.append(best)
    return sorted(best_per_rep)


# --------------------------------------------------------------------------- holdout


def split_by_date(
    rows: Sequence[HistoricalSample],
) -> tuple[list[HistoricalSample], list[HistoricalSample]]:
    """(early, late), cut at each league's own median kickoff.

    Per league because the leagues cover different spans -- one global cut would put all of
    a league in a single half and still call it a holdout. The cut is a date rather than a
    row count, so both sides of a market always land in the same half.
    """
    by_league: dict[str, list[HistoricalSample]] = {}
    for row in rows:
        by_league.setdefault(row.league, []).append(row)
    early: list[HistoricalSample] = []
    late: list[HistoricalSample] = []
    for league_rows in by_league.values():
        starts = sorted(row.game_start for row in league_rows if row.game_start is not None)
        if not starts:
            late.extend(league_rows)
            continue
        cut = starts[len(starts) // 2]
        for row in league_rows:
            target = early if (row.game_start is not None and row.game_start < cut) else late
            target.append(row)
    return early, late


# --------------------------------------------------------------------------- the run


def run(
    session: Session,
    *,
    league: str | None = None,
    fee_rate: float = DEFAULT_FEE_RATE,
    max_close_age_hours: float | None = DEFAULT_MAX_CLOSE_AGE_HOURS,
    paired: bool = True,
    max_pair_error: float = DEFAULT_MAX_PAIR_ERROR,
    reps: int = DEFAULT_PERMUTATIONS,
    min_n: int = DEFAULT_MIN_N,
    seed: int = 1729,
    strategies: Sequence[Strategy] = STRATEGIES,
) -> dict:
    """Score every rule, then try hard to show that the winner is nothing."""
    query = select(HistoricalSample)
    if league:
        query = query.where(HistoricalSample.league == league)
    if max_close_age_hours is not None:
        query = query.where(HistoricalSample.close_age_hours <= max_close_age_hours)
    rows = list(session.scalars(query))
    if paired:
        rows = paired_only(rows, max_pair_error)

    scores = bakeoff(rows, strategies, fee_rate)
    eligible = [score for score in scores if score.n >= min_n]
    best = eligible[0] if eligible else None
    null = permutation_null(rows, strategies, fee_rate, reps, min_n, seed)
    search_p = None
    if best is not None and null:
        search_p = sum(1 for value in null if value >= (best.roi or 0.0)) / len(null)

    early, late = split_by_date(rows)
    early_scores = sorted(bakeoff(early, strategies, fee_rate), key=_by_return)
    late_scores = {score.key: score for score in bakeoff(late, strategies, fee_rate)}
    ranked = [score for score in early_scores if score.n >= max(min_n // 2, 1)]
    holdout = [
        {"key": score.key, "label": score.label, "early": score, "late": late_scores.get(score.key)}
        for score in ranked[:5]
    ]

    starts = [row.game_start for row in rows if row.game_start is not None]
    return {
        "rows": rows,
        "n_outcomes": len(rows),
        "n_markets": len({row.market_id for row in rows}),
        "league": league or "all",
        "fee_rate": fee_rate,
        "paired": paired,
        "max_close_age_hours": max_close_age_hours,
        "span": (min(starts), max(starts)) if starts else (None, None),
        "min_n": min_n,
        "scores": scores,
        "best": best,
        "null": null,
        "search_p": search_p,
        "holdout": holdout,
        "n_early": len(early),
        "n_late": len(late),
    }


def _row(score: Score) -> str:
    if score.n == 0:
        return f"  {score.label:<32} {0:>6} {'-':>7} {'-':>8} {'-':>8} {'-':>8}"
    win = f"{(score.win_rate or 0.0) * 100:5.1f}%"
    implied = f"{(score.implied or 0.0) * 100:5.1f}%"
    roi = f"{(score.roi or 0.0) * 100:+6.2f}%"
    p = "-" if score.roi_p is None else f"{score.roi_p:5.3f}"
    return f"  {score.label:<32} {score.n:>6} {win:>7} {implied:>8} {roi:>8} {p:>8}"


def format_run(data: dict, top: int | None = None) -> str:
    """The bake-off as a text table. ASCII only: this prints to a cp1252 console."""
    span = data["span"]
    when = "" if span[0] is None else f", {str(span[0])[:10]} to {str(span[1])[:10]}"
    lines = [
        f"Strategy bake-off - {len(data['scores'])} rules over {data['n_outcomes']} outcomes "
        f"from {data['n_markets']} markets ({data['league']}{when})",
        f"Taker fee {data['fee_rate'] * 100:.2f}% of p(1-p); "
        f"{'paired quotes only' if data['paired'] else 'UNPAIRED - stale closes included'}; "
        f"close within {data['max_close_age_hours']}h of kickoff.",
        "",
        f"  {'rule':<32} {'picks':>6} {'won':>7} {'implied':>8} {'ROI/$':>8} {'p':>8}",
    ]
    lines.extend(_row(score) for score in (data["scores"] if top is None else data["scores"][:top]))
    lines.append("")

    best = data["best"]
    if best is None:
        lines.append(f"No rule made at least {data['min_n']} picks, so there is nothing to rank.")
        return "\n".join(lines)

    lines.append(
        f"Best rule with {data['min_n']}+ picks: {best.label}, "
        f"{(best.roi or 0.0) * 100:+.2f}% per dollar over {best.n} picks."
    )
    null = data["null"]
    if null:
        median = null[len(null) // 2]
        p95 = null[min(len(null) - 1, int(len(null) * 0.95))]
        beat = (data["search_p"] or 1.0) <= 0.05
        lines.append(
            f"The same rules on shuffled results: the best is typically {median * 100:+.2f}% "
            f"and reaches {p95 * 100:+.2f}% one time in twenty."
        )
        lines.append(
            f"Search p = {data['search_p']:.3f} - "
            + (
                "noise rarely picks a winner this good, so this is worth a second look."
                if beat
                else "picking the best of these rules on pure noise does this well that often."
            )
        )
    lines.append("")
    if not data["n_early"] or not data["n_late"]:
        # Every game sharing one kickoff, or one league with a single date: the split has
        # nothing to cut on, and a holdout that is really the whole sample would be worse
        # than no holdout at all.
        lines.append(
            f"Holdout unavailable: the split put {data['n_early']} outcomes early and "
            f"{data['n_late']} late, so there is nothing to hold out."
        )
        return "\n".join(lines)
    lines.append(
        f"Holdout - ranked on the early half ({data['n_early']} outcomes), then re-scored "
        f"unchanged on the late half ({data['n_late']}):"
    )
    lines.append(f"  {'rule':<32} {'early ROI':>10} {'late ROI':>10} {'late picks':>11}")
    for item in data["holdout"]:
        late = item["late"]
        early_roi = f"{(item['early'].roi or 0.0) * 100:+.2f}%"
        late_roi = "-" if late is None or late.roi is None else f"{late.roi * 100:+.2f}%"
        late_n = "-" if late is None else str(late.n)
        lines.append(f"  {item['label']:<32} {early_roi:>10} {late_roi:>10} {late_n:>11}")
    lines.append("")
    lines.append("ROI/$ is profit per dollar staked, net of the taker fee. The p column tests")
    lines.append("one rule against zero and does NOT know that this many rules were tried;")
    lines.append("the shuffled-results line is what accounts for that.")
    return "\n".join(lines)


PICK_FIELDS: tuple[str, ...] = (
    "game_start",
    "league",
    "market_type",
    "question",
    "side",
    "price",
    "cost_with_fee",
    "trades_pre",
    "close_age_hours",
    "won",
    "pnl_per_dollar",
    "rules",
)


def pick_table(
    rows: Sequence[HistoricalSample],
    strategies: Sequence[Strategy] = STRATEGIES,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> list[dict]:
    """Every outcome, what it cost, what happened, and which rules bought it.

    One row per outcome rather than one per rule, so the same bet is never counted twice
    and `rules` shows where the overlap is: most of these rules are not independent of each
    other, and a table that hid that would make 28 rules look like 28 opinions.
    """
    chosen: dict[int, list[str]] = {}
    for strategy in strategies:
        for i in picks(strategy, rows):
            chosen.setdefault(i, []).append(strategy.key)
    out = []
    for i, row in enumerate(rows):
        won = bool(row.won)
        out.append(
            {
                "game_start": "" if row.game_start is None else str(row.game_start)[:16],
                "league": row.league,
                "market_type": row.market_type,
                "question": (row.question or "").strip(),
                "side": (row.outcome_name or "").strip(),
                "price": round(row.close_price, 4),
                "cost_with_fee": round(effective_cost(row.close_price, fee_rate), 4),
                "trades_pre": row.n_trades_pre,
                "close_age_hours": row.close_age_hours,
                "won": int(won),
                "pnl_per_dollar": round(pnl_per_dollar(row.close_price, won, fee_rate), 4),
                "rules": " ".join(chosen.get(i, ())),
            }
        )
    return out


def write_csv(data: dict, directory: Path) -> list[Path]:
    """Write the summary and the bet-by-bet picks as two CSVs. Returns what it wrote."""
    directory.mkdir(parents=True, exist_ok=True)
    summary = directory / "bakeoff-summary.csv"
    with summary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rule", "label", "picks", "wins", "win_rate", "implied", "roi",
                         "total_pnl_per_dollar", "calibration_p", "roi_p"])
        for score in data["scores"]:
            writer.writerow([score.key, score.label, score.n, score.wins, score.win_rate,
                             score.implied, score.roi, score.total_pnl, score.calib_p,
                             score.roi_p])

    picks_path = directory / "bakeoff-picks.csv"
    table = pick_table(data["rows"], fee_rate=data["fee_rate"])
    with picks_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PICK_FIELDS))
        writer.writeheader()
        writer.writerows(table)
    return [summary, picks_path]


__all__ = [
    "DEFAULT_FEE_RATE",
    "DEFAULT_MAX_CLOSE_AGE_HOURS",
    "DEFAULT_MIN_N",
    "DEFAULT_PERMUTATIONS",
    "PERMUTATION_BAND",
    "STRATEGIES",
    "Score",
    "Strategy",
    "bakeoff",
    "effective_cost",
    "format_run",
    "permutation_null",
    "PICK_FIELDS",
    "pick_table",
    "picks",
    "pnl_per_dollar",
    "run",
    "score_picks",
    "shuffled_wins",
    "split_by_date",
    "write_csv",
]
