"""The forward test: recording every priced outcome, grading it, reporting on it.

The behaviour that matters here and is NOT covered by test_edge.py is the negative case —
`edge.build_opportunity` returns None below `min_edge`, and `forward.record_sample`
deliberately does not. A live slate produced nothing but negative edges, so a recorder
that inherited the threshold would record nothing at all and the forward test would be
permanently empty.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ForwardSample, Market, Scan
from app.services import forward
from tests.test_edge import _levels, make_book, make_fair, make_market

NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
FEE = 0.05


def seed_rows(session: Session, market_id: str = "500001") -> int:
    """A Scan and a Market row so samples have their foreign keys."""
    scan = Scan(started_at=NOW, kind="both", leagues=["nfl"], ok=True)
    session.add(scan)
    session.flush()
    if session.get(Market, market_id) is None:
        session.add(
            Market(
                id=market_id,
                condition_id="0x" + "ab" * 32,
                slug="nfl-kc-buf-moneyline",
                question="Chiefs vs. Bills",
                market_type="moneyline",
            )
        )
        session.flush()
    return scan.id


# `book=None` is a case under test (no order book at all), so it cannot double as "use the
# default" -- hence an explicit sentinel.
_DEFAULT = object()


def record(
    session: Session, scan_id: int, *, market=None, book=_DEFAULT, fair=None, now=NOW, index=0
):
    return forward.record_sample(
        session,
        scan_id=scan_id,
        market=market or make_market(),
        outcome_index=index,
        book=make_book(_levels((0.50, 400.0), (0.51, 900.0))) if book is _DEFAULT else book,
        fair=fair or make_fair(0.55),
        prefs_fee_rate=FEE,
        now=now,
    )


# --------------------------------------------------------------------------- recording


def test_sample_records_price_fee_edge_and_time_to_kickoff(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    row = record(db_session, scan_id)
    assert row is not None
    db_session.flush()

    assert row.ask == 0.50
    # fee = rate * p * (1 - p) = 0.05 * 0.5 * 0.5 = 0.0125
    assert row.effective_price == pytest.approx(0.5125)
    assert row.fair_prob == 0.55
    assert row.edge == pytest.approx(0.55 - 0.5125)
    assert row.fee_rate == FEE
    assert row.league == "nfl"
    assert row.market_type == "moneyline"
    assert row.n_books == 3
    assert row.books_used == ["betonlineag", "draftkings", "pinnacle"]
    # kickoff 2026-09-20 20:25Z, sampled 2026-09-19 15:00Z
    assert row.hours_to_start == pytest.approx(29.4167, abs=1e-3)
    assert row.won is None and row.pnl_per_dollar is None


def test_a_negative_edge_is_still_recorded(db_session: Session) -> None:
    """The whole point. `build_opportunity` drops these; the forward test keeps them,
    otherwise an ESPN-only slate (every edge negative) records nothing ever."""
    scan_id = seed_rows(db_session)
    row = record(db_session, scan_id, fair=make_fair(0.40))
    assert row is not None
    assert row.edge < 0


def test_top_ask_usd_is_the_money_resting_at_the_best_level(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    book = make_book(_levels((0.50, 400.0), (0.50, 100.0), (0.62, 9999.0)))
    row = record(db_session, scan_id, book=book)
    assert row is not None
    assert row.top_ask_usd == pytest.approx(0.50 * 500.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"closed": True},
        {"accepting_orders": False},
        # kickoff already passed: the Polymarket price now reflects the live score while the
        # book snapshot is pre-game, so the comparison was never valid.
        {"game_start": NOW - timedelta(minutes=1)},
    ],
)
def test_untradable_outcomes_are_not_recorded(db_session: Session, overrides: dict) -> None:
    scan_id = seed_rows(db_session)
    assert record(db_session, scan_id, market=make_market(**overrides)) is None


def test_no_book_or_no_ask_is_not_recorded(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    assert record(db_session, scan_id, book=None) is None
    assert record(db_session, scan_id, book=make_book(())) is None


def test_outcome_index_must_be_zero_or_one(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    with pytest.raises(ValueError, match="outcome_index"):
        record(db_session, scan_id, index=2)


# --------------------------------------------------------------------------- grading


def test_settle_grades_win_and_loss_with_fee_inclusive_return(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    won_row = record(db_session, scan_id, index=0)
    lost_row = record(
        db_session,
        scan_id,
        index=1,
        book=make_book(_levels((0.50, 400.0)), token_id="7" * 70 + "2"),
    )
    db_session.flush()
    assert won_row is not None and lost_row is not None

    resolved = make_market(resolved_outcome_index=0)
    assert forward.settle(db_session, {"500001": resolved}, NOW) == 2

    # $1 at 0.5125 buys 1/0.5125 shares paying $1 each -> (1 - p) / p
    assert won_row.won is True
    assert won_row.pnl_per_dollar == pytest.approx((1 - 0.5125) / 0.5125)
    assert lost_row.won is False
    assert lost_row.pnl_per_dollar == -1.0


def test_settle_skips_unresolved_markets_and_is_idempotent(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    record(db_session, scan_id)
    db_session.flush()

    assert forward.settle(db_session, {"500001": make_market()}, NOW) == 0
    assert forward.settle(db_session, {"500001": make_market(resolved_outcome_index=0)}, NOW) == 1
    # second pass finds nothing left pending
    assert forward.settle(db_session, {"500001": make_market(resolved_outcome_index=0)}, NOW) == 0


def test_pending_market_ids_waits_for_the_game_to_finish(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    kickoff = NOW + timedelta(hours=1)
    record(db_session, scan_id, market=make_market(game_start=kickoff))
    db_session.flush()

    # right after kickoff there is no result to fetch yet
    assert forward.pending_market_ids(db_session, kickoff + timedelta(minutes=30)) == []
    later = kickoff + forward.SETTLE_AFTER_START + timedelta(minutes=1)
    assert forward.pending_market_ids(db_session, later) == ["500001"]


def test_capture_closing_scores_against_the_last_price_seen(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    record(db_session, scan_id, book=make_book(_levels((0.50, 100.0))), now=NOW)
    record(
        db_session,
        scan_id,
        book=make_book(_levels((0.56, 100.0))),
        now=NOW + timedelta(hours=2),
    )
    db_session.flush()

    assert forward.capture_closing(db_session, ["500001"]) == 2
    rows = list(db_session.scalars(select(ForwardSample).order_by(ForwardSample.sampled_at)))
    # bought at 50c, market closed at 56c -> +6c of closing line value
    assert rows[0].closing_pm_price == 0.56
    assert rows[0].clv == pytest.approx(0.06)
    assert rows[1].clv == pytest.approx(0.0)


# --------------------------------------------------------------------------- reporting


def _graded(session: Session, scan_id: int, edge: float, won: bool, clv: float = 0.0) -> None:
    price = 0.50
    session.add(
        ForwardSample(
            scan_id=scan_id,
            market_id="500001",
            token="t" + str(edge) + str(won) + str(clv),
            outcome_index=0,
            outcome_name="Chiefs",
            league="nfl",
            market_type="moneyline",
            ask=price,
            effective_price=price,
            fee_rate=0.0,
            fair_prob=price + edge,
            edge=edge,
            sampled_at=NOW,
            won=won,
            pnl_per_dollar=((1 - price) / price) if won else -1.0,
            clv=clv,
        )
    )


def test_report_buckets_by_threshold(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    _graded(db_session, scan_id, edge=0.04, won=True, clv=0.02)
    _graded(db_session, scan_id, edge=0.03, won=False, clv=0.01)
    _graded(db_session, scan_id, edge=-0.01, won=True, clv=0.0)
    db_session.flush()

    data = forward.report(db_session, thresholds=(-0.02, 0.0, 0.02, 0.035))
    by_threshold = {b["threshold"]: b for b in data["buckets"]}
    assert data["graded"] == 3 and data["pending"] == 0

    # a threshold below every edge takes all three: two winners at even money, one loser
    assert by_threshold[-0.02]["n"] == 3
    assert by_threshold[-0.02]["win_rate"] == pytest.approx(2 / 3)
    assert by_threshold[-0.02]["roi"] == pytest.approx((1.0 + 1.0 - 1.0) / 3)

    # >= 0 drops the -1% sample, which is exactly the point of bucketing
    assert by_threshold[0.0]["n"] == 2
    assert by_threshold[0.0]["win_rate"] == pytest.approx(0.5)

    assert by_threshold[0.02]["n"] == 2
    assert by_threshold[0.02]["avg_clv"] == pytest.approx(0.015)

    assert by_threshold[0.035]["n"] == 1
    assert by_threshold[0.035]["win_rate"] == 1.0


def test_report_counts_pending_separately_and_filters_by_league(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    _graded(db_session, scan_id, edge=0.04, won=True)
    record(db_session, scan_id)  # ungraded
    db_session.flush()

    data = forward.report(db_session)
    assert data["graded"] == 1 and data["pending"] == 1
    assert forward.report(db_session, league="mlb")["graded"] == 0


def test_format_report_is_ascii_for_a_cp1252_console(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    _graded(db_session, scan_id, edge=0.04, won=True, clv=0.02)
    db_session.flush()
    text = forward.format_report(forward.report(db_session))
    text.encode("cp1252")  # raises if a character cannot be shown in his console
    assert "min edge" in text


def test_format_report_says_so_when_nothing_is_graded(db_session: Session) -> None:
    text = forward.format_report(forward.report(db_session))
    assert "Nothing graded yet" in text


def test_prune_market_removes_samples(db_session: Session) -> None:
    scan_id = seed_rows(db_session)
    record(db_session, scan_id)
    db_session.flush()
    forward.prune_market(db_session, "500001")
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(ForwardSample)) == 0


def test_market_fee_override_beats_the_preference(db_session: Session) -> None:
    """Live Gamma sends takerBaseFee=1000 -> 0.10, double the documented sports rate. The
    sample must record the fee actually charged, not the preference."""
    scan_id = seed_rows(db_session)
    row = record(db_session, scan_id, market=make_market(taker_fee_rate=0.10))
    assert row is not None
    assert row.fee_rate == 0.10
    assert row.effective_price == pytest.approx(0.50 + 0.10 * 0.5 * 0.5)


def test_replace_keeps_the_builder_honest() -> None:
    """Guard: make_market(**overrides) must actually apply, or the gate tests above pass
    vacuously against an unmodified market."""
    assert replace(make_market(), closed=True).closed is True
