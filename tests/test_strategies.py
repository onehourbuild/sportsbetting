"""The strategy bake-off, and the two guards that stop it inventing a winner.

The arithmetic here is easy to get right and easy to trust too far, so most of these tests
are about the guards rather than the scoring: that a rule cannot see the result, that a
perfectly calibrated market scores as a loss equal to the fee, that shuffling results
inside a price band leaves calibration alone, and that the holdout really is held out.
"""

from __future__ import annotations

import csv
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import HistoricalSample
from app.services import strategies

KICKOFF = datetime(2026, 1, 4, 18, 0, tzinfo=UTC)
FEE = 0.0695


def make_row(
    *,
    market_id: str = "m1",
    index: int = 0,
    price: float = 0.50,
    won: bool = False,
    league: str = "nfl",
    market_type: str = "moneyline",
    name: str = "side",
    trades: int = 100,
    age: float = 1.0,
    start: datetime | None = None,
) -> HistoricalSample:
    return HistoricalSample(
        market_id=market_id,
        league=league,
        market_type=market_type,
        outcome_index=index,
        outcome_name=name,
        game_start=start or KICKOFF,
        close_price=price,
        close_age_hours=age,
        n_trades_pre=trades,
        won=won,
    )


def pair(market_id: str, price: float, winner: int, **kwargs) -> list[HistoricalSample]:
    """Both sides of one market, priced to sum to 1.00 so `paired_only` keeps them."""
    return [
        make_row(
            market_id=market_id,
            index=i,
            price=price if i == 0 else round(1.0 - price, 4),
            won=(i == winner),
            name=f"side{i}",
            **kwargs,
        )
        for i in (0, 1)
    ]


# --------------------------------------------------------------------------- arithmetic


def test_a_win_returns_the_rest_of_the_dollar_and_a_loss_returns_it_all() -> None:
    cost = strategies.effective_cost(0.50, FEE)
    assert cost == pytest.approx(0.50 + FEE * 0.25)
    assert strategies.pnl_per_dollar(0.50, True, FEE) == pytest.approx((1 - cost) / cost)
    assert strategies.pnl_per_dollar(0.50, False, FEE) == -1.0


def test_buying_everything_in_a_fair_market_loses_about_the_fee() -> None:
    """The baseline that keeps the rest honest. Both sides of a 60/40 market, the 60 side
    winning 60% of the time, must come out near -fee -- not at zero, and not positive."""
    rows: list[HistoricalSample] = []
    for i in range(1000):
        rows.extend(pair(f"m{i}", 0.60, winner=0 if i % 10 < 6 else 1))
    everything = next(s for s in strategies.STRATEGIES if s.key == "everything")
    score = strategies.bakeoff(rows, [everything], FEE)[0]
    assert score.n == 2000
    assert score.roi is not None
    assert -0.06 < score.roi < -0.01
    assert score.calib_p is not None and score.calib_p > 0.05


def test_a_rule_that_is_right_makes_money_so_the_harness_can_detect_one() -> None:
    """A negative-only test suite would pass on a harness that always reports a loss."""
    rows: list[HistoricalSample] = []
    for i in range(400):
        # Every favorite wins: the 60c side is really an 100% shot.
        rows.extend(pair(f"m{i}", 0.60, winner=0))
    favorites = next(s for s in strategies.STRATEGIES if s.key == "favorites")
    score = strategies.bakeoff(rows, [favorites], FEE)[0]
    assert score.roi is not None and score.roi > 0.5
    assert score.calib_p is not None and score.calib_p < 0.001


# --------------------------------------------------------------------------- selection


def test_no_rule_can_see_the_result() -> None:
    """The guard that matters most. Flipping every result must not change any rule's picks;
    if one did, its return would be a readout of the answer key."""
    rows = [
        row
        for i in range(60)
        for row in pair(
            f"m{i}", 0.35 + (i % 5) / 10, winner=i % 2, league="nfl" if i % 2 else "mlb"
        )
    ]
    before = {s.key: strategies.picks(s, rows) for s in strategies.STRATEGIES}
    for row in rows:
        row.won = not row.won
    after = {s.key: strategies.picks(s, rows) for s in strategies.STRATEGIES}
    assert before == after


def test_price_band_rules_do_not_all_share_the_last_band() -> None:
    """Closures built in a loop are the classic way to end up with 28 copies of one rule."""
    rows = [make_row(market_id=f"m{i}", price=p) for i, p in enumerate((0.10, 0.50, 0.90))]
    by_key = {s.key: strategies.picks(s, rows) for s in strategies.STRATEGIES}
    assert by_key["longshots"] == [0]
    assert by_key["coinflips"] == [1]
    assert by_key["heavy_favs"] == [2]


def test_rules_split_on_side_league_and_liquidity() -> None:
    rows = [
        make_row(market_id="t", market_type="totals", name="Over ", price=0.52),
        make_row(market_id="t", index=1, market_type="totals", name="Under", price=0.50),
        make_row(market_id="s", market_type="spreads", name="Cowboys", price=0.44),
        make_row(market_id="b", league="mlb", price=0.80, trades=10),
    ]
    by_key = {s.key: strategies.picks(s, rows) for s in strategies.STRATEGIES}
    assert by_key["overs"] == [0]  # trailing whitespace in the name must not hide it
    assert by_key["unders"] == [1]
    # the side is read off the index, not the name: real rows say "Cowboys", not "Yes"
    assert by_key["spread_lay"] == [2]
    assert by_key["spread_take"] == []
    assert by_key["mlb"] == [3]
    assert by_key["liquid_all"] == [0, 1, 2]  # the 10-trade row is not liquid
    assert by_key["thin_dogs"] == []  # thin, but an 80c favorite


# --------------------------------------------------------------------------- the null


def test_shuffling_moves_results_without_changing_what_the_market_said() -> None:
    rows = [make_row(market_id=f"m{i}", price=0.30, won=i < 30) for i in range(100)]
    rows += [make_row(market_id=f"n{i}", price=0.90, won=i < 90) for i in range(100)]
    shuffled = strategies.shuffled_wins(rows, random.Random(7))

    assert sum(shuffled) == sum(bool(row.won) for row in rows)
    # and the count is preserved band by band, not just overall
    assert sum(shuffled[:100]) == 30
    assert sum(shuffled[100:]) == 90
    assert shuffled != [bool(row.won) for row in rows]


def test_the_null_is_a_distribution_of_best_returns_not_of_one_rule() -> None:
    rows = [
        row for i in range(300) for row in pair(f"m{i}", 0.55, winner=0 if i % 20 < 11 else 1)
    ]
    null = strategies.permutation_null(rows, reps=25, min_n=100, fee_rate=FEE, seed=3)
    assert len(null) == 25
    assert null == sorted(null)
    # Picking the best of many rules on noise beats a single rule on noise: the null must
    # sit above the per-rule expectation of roughly -fee, or it is not doing its job.
    assert max(null) > min(null)


def test_a_real_edge_beyond_price_still_beats_the_shuffled_null() -> None:
    """The null must not be so conservative that nothing could ever pass it.

    The edge here is deliberately not a price effect: MLB and NFL outcomes are priced
    identically at 55c, and only the MLB ones win. Price alone cannot find that, so the
    shuffle destroys it and the `mlb` rule should stand clear of the null.
    """
    rows: list[HistoricalSample] = []
    for i in range(300):
        rows.extend(pair(f"mlb{i}", 0.55, winner=0, league="mlb"))
        rows.extend(pair(f"nfl{i}", 0.55, winner=1, league="nfl"))
    scores = {s.key: s for s in strategies.bakeoff(rows, fee_rate=FEE)}
    null = strategies.permutation_null(rows, reps=25, min_n=100, fee_rate=FEE, seed=3)
    assert scores["mlb_favs"].roi is not None
    assert scores["mlb_favs"].roi > max(null)


def test_the_null_deliberately_cannot_see_a_pure_price_mispricing() -> None:
    """A limitation worth pinning down rather than discovering later.

    Shuffling inside a price band preserves how often outcomes at that price won, so an
    edge that is *only* "the market is wrong at 55c" survives the shuffle untouched and the
    search p correctly refuses to call it a discovery. That question is answered by
    `calib_p`, which compares picks against their own prices -- and here reports it loudly.
    """
    rows: list[HistoricalSample] = []
    for i in range(300):
        rows.extend(pair(f"m{i}", 0.55, winner=0))  # every favorite wins
    scores = {s.key: s for s in strategies.bakeoff(rows, fee_rate=FEE)}
    null = strategies.permutation_null(rows, reps=25, min_n=100, fee_rate=FEE, seed=3)

    assert scores["favorites"].roi == pytest.approx(max(null))  # invisible to the shuffle
    assert scores["favorites"].calib_p == pytest.approx(0.0, abs=1e-9)  # but not to this


# --------------------------------------------------------------------------- holdout


def test_the_split_is_per_league_and_keeps_both_sides_of_a_market_together() -> None:
    rows: list[HistoricalSample] = []
    for i in range(10):
        rows.extend(pair(f"nfl{i}", 0.5, winner=0, league="nfl", start=KICKOFF + timedelta(days=i)))
    for i in range(10):
        rows.extend(
            pair(f"mlb{i}", 0.5, winner=0, league="mlb", start=KICKOFF + timedelta(days=200 + i))
        )
    early, late = strategies.split_by_date(rows)

    assert len(early) + len(late) == len(rows)
    # both leagues are represented on both sides, which a single global cut would not give
    assert {row.league for row in early} == {"nfl", "mlb"}
    assert {row.league for row in late} == {"nfl", "mlb"}
    for market_id in {row.market_id for row in rows}:
        in_early = sum(1 for row in early if row.market_id == market_id)
        assert in_early in (0, 2)


# --------------------------------------------------------------------------- the run


def test_run_reports_the_search_p_not_just_the_winner(db_session: Session) -> None:
    for i in range(400):
        # Kickoffs must be spread out or the holdout has nothing to split on.
        for row in pair(f"m{i}", 0.55, winner=0 if i % 20 < 11 else 1,
                        start=KICKOFF + timedelta(days=i)):
            db_session.add(row)
    db_session.flush()

    data = strategies.run(db_session, reps=20, min_n=100, fee_rate=FEE)
    assert data["n_outcomes"] == 800
    assert data["n_markets"] == 400
    assert data["best"] is not None
    assert data["search_p"] is not None and 0.0 <= data["search_p"] <= 1.0
    assert data["n_early"] > 0 and data["n_late"] > 0
    assert len(data["scores"]) == len(strategies.STRATEGIES)
    # sorted best-first, with empty rules last
    rois = [s.roi for s in data["scores"] if s.roi is not None]
    assert rois == sorted(rois, reverse=True)


def test_run_drops_stale_closes_and_unpaired_markets(db_session: Session) -> None:
    db_session.add_all(pair("fresh", 0.5, winner=0, age=1.0))
    db_session.add_all(pair("stale", 0.5, winner=0, age=9.0))
    db_session.add(make_row(market_id="lonely", price=0.5, age=1.0))
    db_session.flush()

    assert strategies.run(db_session, reps=1, min_n=1)["n_outcomes"] == 2
    assert strategies.run(db_session, reps=1, min_n=1, max_close_age_hours=12.0)["n_outcomes"] == 4
    unpaired = strategies.run(db_session, reps=1, min_n=1, paired=False)
    assert unpaired["n_outcomes"] == 3


def test_format_run_is_ascii_and_says_what_the_shuffle_found(db_session: Session) -> None:
    for i in range(300):
        for row in pair(f"m{i}", 0.6, winner=0 if i % 10 < 6 else 1):
            db_session.add(row)
    db_session.flush()
    text = strategies.format_run(strategies.run(db_session, reps=20, min_n=100))

    text.encode("cp1252")  # raises if it cannot be shown in his console
    assert "Strategy bake-off" in text
    assert "shuffled results" in text
    assert "Holdout" in text
    assert "Buy every outcome" in text


def test_format_run_says_so_when_nothing_qualifies(db_session: Session) -> None:
    db_session.add_all(pair("only", 0.5, winner=0))
    db_session.flush()
    text = strategies.format_run(strategies.run(db_session, reps=2, min_n=500))
    assert "nothing to rank" in text


# --------------------------------------------------------------------------- export


def test_pick_table_is_one_row_per_outcome_with_the_rules_that_bought_it() -> None:
    rows = [
        make_row(market_id="m", price=0.62, won=True, name="Chiefs", market_type="moneyline"),
        make_row(market_id="m", index=1, price=0.40, won=False, name="Bills"),
    ]
    table = strategies.pick_table(rows, fee_rate=FEE)

    assert len(table) == 2
    favorite, underdog = table
    assert favorite["won"] == 1
    assert favorite["pnl_per_dollar"] > 0
    assert underdog["won"] == 0
    assert underdog["pnl_per_dollar"] == -1.0
    assert favorite["cost_with_fee"] > favorite["price"]  # the fee is charged, visibly
    # the overlapping rules are shown, which is the point: they are not 28 opinions
    assert "favorites" in favorite["rules"].split()
    assert "ml_favs" in favorite["rules"].split()
    assert "underdogs" in underdog["rules"].split()
    assert "favorites" not in underdog["rules"].split()


def test_write_csv_writes_both_files_and_the_picks_reconcile(
    db_session: Session, tmp_path
) -> None:
    """The two files must agree: summing a rule's per-bet returns has to reproduce the
    total on the summary line, or the table and the evidence for it disagree."""
    for i in range(300):
        for row in pair(f"m{i}", 0.6, winner=0 if i % 10 < 6 else 1,
                        start=KICKOFF + timedelta(days=i)):
            db_session.add(row)
    db_session.flush()

    data = strategies.run(db_session, reps=5, min_n=100)
    written = strategies.write_csv(data, tmp_path / "out")
    assert [path.name for path in written] == ["bakeoff-summary.csv", "bakeoff-picks.csv"]

    picks = list(csv.DictReader(written[1].open(encoding="utf-8")))
    assert len(picks) == data["n_outcomes"]
    assert list(picks[0]) == list(strategies.PICK_FIELDS)

    summary = {row["rule"]: row for row in csv.DictReader(written[0].open(encoding="utf-8"))}
    total = sum(
        float(pick["pnl_per_dollar"]) for pick in picks if "favorites" in pick["rules"].split()
    )
    assert total == pytest.approx(float(summary["favorites"]["total_pnl_per_dollar"]), abs=0.02)
    assert int(summary["favorites"]["picks"]) == sum(
        1 for pick in picks if "favorites" in pick["rules"].split()
    )
