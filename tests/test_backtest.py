"""Back test: harvesting resolved Polymarket history and grading its own closing prices.

Every network shape here is copied from a real response (see docs/RESEARCH.md): Gamma
sends `outcomePrices` / `outcomes` as JSON-encoded STRINGS and timestamps as
"2024-09-27 02:10:00+00", and data-api sends trades newest-first with string-ish numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import HistoricalSample
from app.services import backtest

KICKOFF = datetime(2024, 9, 27, 2, 10, tzinfo=UTC)


def ts(when: datetime) -> int:
    return int(when.timestamp())


def trade(outcome_index: int, price: float, when: datetime) -> dict:
    return {
        "outcomeIndex": outcome_index,
        "price": price,
        "size": 100.0,
        "side": "BUY",
        "timestamp": ts(when),
    }


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-09-27 02:10:00+00", KICKOFF),  # the shape Gamma actually sends
        ("2024-09-27T02:10:00Z", KICKOFF),
        ("2024-09-27T02:10:00+00:00", KICKOFF),
        ("", None),
        ("not a date", None),
        (None, None),
        (12345, None),
    ],
)
def test_parse_gamma_time(raw: object, expected: datetime | None) -> None:
    assert backtest.parse_gamma_time(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["1", "0"]', 0),
        ('["0", "1"]', 1),
        (["1", "0"], 0),
        ('["0.62", "0.38"]', None),  # still trading, not resolved
        ('["1", "1"]', None),
        ("nonsense", None),
        (None, None),
    ],
)
def test_resolved_index(raw: object, expected: int | None) -> None:
    assert backtest.resolved_index(raw) == expected


# --------------------------------------------------------------------------- closing price


def test_closing_trade_takes_the_last_trade_before_kickoff() -> None:
    trades = [
        trade(0, 0.71, KICKOFF.replace(hour=1, minute=55)),
        trade(0, 0.64, KICKOFF.replace(hour=0, minute=30)),
        trade(1, 0.36, KICKOFF.replace(hour=1, minute=50)),
    ]
    found = backtest.closing_trade(trades, 0, KICKOFF)
    assert found is not None
    price, when, n = found
    assert price == 0.71
    assert when == KICKOFF.replace(hour=1, minute=55)
    assert n == 2


def test_closing_trade_ignores_in_play_trades() -> None:
    """After kickoff the market is pricing a live score. Using it would grade a bet nobody
    could have placed -- and it is the single easiest way to fake a profitable back test."""
    trades = [
        trade(0, 0.97, KICKOFF.replace(hour=3)),  # in-play, must be ignored
        trade(0, 0.55, KICKOFF.replace(hour=1)),
    ]
    found = backtest.closing_trade(trades, 0, KICKOFF)
    assert found is not None
    assert found[0] == 0.55
    assert found[2] == 1


def test_closing_trade_returns_none_without_a_pre_kickoff_trade() -> None:
    assert backtest.closing_trade([trade(0, 0.9, KICKOFF.replace(hour=4))], 0, KICKOFF) is None
    assert backtest.closing_trade([], 0, KICKOFF) is None


def test_closing_trade_skips_junk_rows_and_impossible_prices() -> None:
    trades = [
        "not a dict",
        {"outcomeIndex": 0, "price": "oops", "timestamp": ts(KICKOFF.replace(hour=1))},
        {"outcomeIndex": 0, "timestamp": ts(KICKOFF.replace(hour=1))},  # no price
        trade(0, 0.0, KICKOFF.replace(hour=1)),  # a 0c fill is not a tradable price
        trade(0, 1.0, KICKOFF.replace(hour=1)),
        trade(0, 0.42, KICKOFF.replace(hour=1, minute=30)),
    ]
    found = backtest.closing_trade(trades, 0, KICKOFF)  # type: ignore[arg-type]
    assert found is not None and found[0] == 0.42


# --------------------------------------------------------------------------- harvest


def make_market(market_id: str = "900001", winner: int = 0) -> dict:
    prices = '["1", "0"]' if winner == 0 else '["0", "1"]'
    return {
        "id": market_id,
        "conditionId": "0x" + "cd" * 32,
        "sportsMarketType": "moneyline",
        "closed": True,
        "gameStartTime": "2024-09-27 02:10:00+00",
        "outcomePrices": prices,
        "outcomes": '["Dodgers", "Padres"]',
        "question": "MLB: Dodgers vs. Padres",
    }


def fake_api(markets: list[dict], trades: list[dict]):
    """A get_json that serves one page of events then stops, plus trades for any market."""
    pages = {0: [{"title": "MLB: Dodgers vs. Padres", "markets": markets}]}

    def get_json(url: str, params: dict) -> object:
        if url == backtest.GAMMA_EVENTS:
            return pages.get(int(params.get("offset", 0)) // backtest.PAGE_SIZE, [])
        if url == backtest.TRADES:
            return trades
        raise AssertionError(f"unexpected url {url}")

    return get_json


def test_harvest_stores_both_outcomes_with_their_result(db_session: Session) -> None:
    trades = [
        trade(0, 0.62, KICKOFF.replace(hour=1, minute=40)),
        trade(1, 0.38, KICKOFF.replace(hour=1, minute=40)),
    ]
    stats = backtest.harvest(
        db_session, fake_api([make_market(winner=0)], trades), leagues=("mlb",), max_pages=1
    )
    assert stats.stored == 2

    rows = {r.outcome_index: r for r in db_session.scalars(select(HistoricalSample))}
    assert rows[0].close_price == 0.62
    assert rows[0].won is True
    assert rows[0].outcome_name == "Dodgers"
    assert rows[0].league == "mlb"
    assert rows[1].won is False
    assert rows[1].outcome_name == "Padres"
    # 01:40 -> 02:10 kickoff
    assert rows[0].close_age_hours == pytest.approx(0.5)


def test_harvest_is_resumable(db_session: Session) -> None:
    """A market already stored is skipped, so an interrupted run costs nothing to repeat."""
    trades = [trade(0, 0.62, KICKOFF.replace(hour=1)), trade(1, 0.38, KICKOFF.replace(hour=1))]
    api = fake_api([make_market()], trades)
    first = backtest.harvest(db_session, api, leagues=("mlb",), max_pages=1)
    second = backtest.harvest(db_session, api, leagues=("mlb",), max_pages=1)

    assert first.stored == 2
    assert second.stored == 0 and second.skipped_existing == 1


def test_harvest_commits_as_it_goes(engine) -> None:
    """Resumability is a COMMIT, not a flush: the skip list is read from committed rows, so
    a run killed part-way through has to have kept what it already fetched. An earlier
    version only committed at the very end and a cancelled 40-minute run stored nothing."""
    from app.db import create_session_factory

    factory = create_session_factory(engine)
    writer = factory()
    trades = [trade(0, 0.62, KICKOFF.replace(hour=1)), trade(1, 0.38, KICKOFF.replace(hour=1))]
    backtest.harvest(writer, fake_api([make_market()], trades), leagues=("mlb",), max_pages=1)
    writer.close()

    # a completely separate session sees the rows
    reader = factory()
    try:
        assert reader.scalar(select(func.count()).select_from(HistoricalSample)) == 2
    finally:
        reader.close()


def test_harvest_skips_unresolved_and_non_game_markets(db_session: Session) -> None:
    open_market = make_market("900002") | {"closed": False}
    futures = make_market("900003") | {"sportsMarketType": None}
    prop = make_market("900004") | {"sportsMarketType": "anytime_touchdowns"}
    no_kickoff = make_market("900005") | {"gameStartTime": None}
    trades = [trade(0, 0.5, KICKOFF.replace(hour=1)), trade(1, 0.5, KICKOFF.replace(hour=1))]

    stats = backtest.harvest(
        db_session,
        fake_api([open_market, futures, prop, no_kickoff], trades),
        leagues=("mlb",),
        max_pages=1,
    )
    assert stats.stored == 0


def test_harvest_records_a_market_with_no_usable_trades(db_session: Session) -> None:
    stats = backtest.harvest(
        db_session, fake_api([make_market()], []), leagues=("mlb",), max_pages=1
    )
    assert stats.stored == 0 and stats.skipped_no_trades == 1


# --------------------------------------------------------------------------- report


def add_sample(session: Session, price: float, won: bool, n: int = 1, age: float = 1.0) -> None:
    for i in range(n):
        session.add(
            HistoricalSample(
                market_id=f"{price}-{won}-{age}-{i}",
                league="mlb",
                market_type="moneyline",
                outcome_index=0,
                outcome_name="X",
                game_start=KICKOFF,
                close_price=price,
                close_age_hours=age,
                won=won,
            )
        )


def test_report_measures_calibration_against_the_price(db_session: Session) -> None:
    # 60c band that wins exactly 60% of the time is perfectly calibrated
    add_sample(db_session, 0.60, True, n=6)
    add_sample(db_session, 0.60, False, n=4)
    db_session.flush()

    band = next(b for b in backtest.report(db_session)["bands"] if b["low"] == 0.50)
    assert band["n"] == 10
    assert band["implied"] == pytest.approx(0.60)
    assert band["actual"] == pytest.approx(0.60)
    assert band["calibration"] == pytest.approx(0.0, abs=1e-9)
    # gross ROI of a perfectly calibrated price is zero; the fee makes it negative
    assert band["roi_gross"] == pytest.approx(0.0, abs=1e-9)
    assert band["roi_net"] < 0


def test_report_finds_a_band_that_beats_its_price(db_session: Session) -> None:
    add_sample(db_session, 0.50, True, n=8)
    add_sample(db_session, 0.50, False, n=2)
    db_session.flush()
    band = next(b for b in backtest.report(db_session, fee_rate=0.0)["bands"] if b["low"] == 0.50)
    assert band["actual"] == pytest.approx(0.80)
    assert band["calibration"] == pytest.approx(0.30)
    assert band["roi_gross"] == pytest.approx(0.6)  # 8 * (+1) + 2 * (-1) over 10


def test_report_excludes_a_stale_close(db_session: Session) -> None:
    """A last trade 30 hours before kickoff is a dead market, not a closing price."""
    add_sample(db_session, 0.60, True, n=5, age=1.0)
    add_sample(db_session, 0.60, True, n=5, age=30.0)
    db_session.flush()
    assert backtest.report(db_session, max_close_age_hours=12.0)["rows_used"] == 5
    assert backtest.report(db_session, max_close_age_hours=None)["rows_used"] == 10


def test_report_filters_by_league_and_market_type(db_session: Session) -> None:
    add_sample(db_session, 0.60, True, n=3)
    db_session.flush()
    assert backtest.report(db_session, league="mlb")["rows_used"] == 3
    assert backtest.report(db_session, league="nfl")["rows_used"] == 0
    assert backtest.report(db_session, market_type="totals")["rows_used"] == 0


def test_format_report_is_ascii_and_says_when_empty(db_session: Session) -> None:
    empty = backtest.format_report(backtest.report(db_session))
    empty.encode("cp1252")
    assert "Nothing harvested yet" in empty

    add_sample(db_session, 0.60, True, n=3)
    db_session.flush()
    text = backtest.format_report(backtest.report(db_session))
    text.encode("cp1252")  # his console is cp1252
    assert "price band" in text


# --------------------------------------------------------------------------- significance


def test_significance_calls_a_big_gap_on_a_small_sample_noise() -> None:
    """The real 65-80c band: 102 outcomes, priced at 69.5%, won 76.5%. It reads like a
    +7% edge and is not one -- this is the guard against tuning a strategy to noise."""
    se, z, p = backtest.significance(n=102, wins=78, implied=0.695)
    assert se == pytest.approx(0.0456, abs=5e-4)
    assert z == pytest.approx(1.54, abs=0.02)
    assert p > 0.05  # not significant


def test_significance_calls_the_same_gap_on_a_big_sample_real() -> None:
    """Identical rates, ten times the sample: now it is an edge. Only n changed."""
    _se, _z, p = backtest.significance(n=1020, wins=780, implied=0.695)
    assert p < 0.05


def test_significance_of_a_perfectly_priced_band_is_one() -> None:
    _se, z, p = backtest.significance(n=600, wins=336, implied=0.56)
    assert z == pytest.approx(0.0, abs=1e-9)
    assert p == pytest.approx(1.0)


def test_significance_handles_degenerate_input() -> None:
    assert backtest.significance(0, 0, 0.5) == (0.0, 0.0, 1.0)
    assert backtest.significance(10, 0, 0.0) == (0.0, 0.0, 1.0)
    assert backtest.significance(10, 10, 1.0) == (0.0, 0.0, 1.0)


def test_report_marks_a_significant_band(db_session: Session) -> None:
    add_sample(db_session, 0.50, True, n=400)
    add_sample(db_session, 0.50, False, n=200)
    db_session.flush()
    band = next(b for b in backtest.report(db_session)["bands"] if b["low"] == 0.50)
    assert band["significant"] is True
    assert "*" in backtest.format_report(backtest.report(db_session))


def test_report_says_so_when_nothing_is_significant(db_session: Session) -> None:
    add_sample(db_session, 0.56, True, n=56)
    add_sample(db_session, 0.56, False, n=44)
    db_session.flush()
    text = backtest.format_report(backtest.report(db_session))
    assert "No band beat its own price significantly" in text


# --------------------------------------------------------------------------- rate limits


class RateLimited(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def test_trades_fetch_backs_off_on_429_then_succeeds() -> None:
    """data-api is behind Cloudflare and answers 429 under a fast loop; the first real
    harvest died at ~700 markets because of it."""
    calls = {"n": 0}
    waited: list[float] = []

    def get_json(url: str, params: dict) -> object:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited(429)
        return [{"ok": True}]

    result = backtest._fetch_trades(get_json, "0xabc", sleep=waited.append)
    assert result == [{"ok": True}]
    assert calls["n"] == 3
    assert waited == [5.0, 20.0]


def test_trades_fetch_reraises_anything_that_is_not_a_rate_limit() -> None:
    """Swallowing a real error would look like "this market had no trades" and quietly
    thin the sample instead of failing loudly."""

    def get_json(url: str, params: dict) -> object:
        raise RateLimited(500)

    with pytest.raises(RateLimited):
        backtest._fetch_trades(get_json, "0xabc", sleep=lambda _s: None)


def test_harvest_paces_itself_between_markets(db_session: Session) -> None:
    waited: list[float] = []
    trades = [trade(0, 0.62, KICKOFF.replace(hour=1)), trade(1, 0.38, KICKOFF.replace(hour=1))]
    backtest.harvest(
        db_session,
        fake_api([make_market()], trades),
        sleep=waited.append,
        leagues=("mlb",),
        max_pages=1,
    )
    assert backtest.TRADE_PAUSE_S in waited


# --------------------------------------------------------------------------- paired quotes


def add_pair(
    session: Session, market_id: str, p0: float, p1: float, won0: bool, age: float = 1.0
) -> None:
    for index, (price, won) in enumerate(((p0, won0), (p1, not won0))):
        session.add(
            HistoricalSample(
                market_id=market_id,
                league="nfl",
                market_type="moneyline",
                outcome_index=index,
                outcome_name=f"side{index}",
                game_start=KICKOFF,
                close_price=price,
                close_age_hours=age,
                won=won,
            )
        )


def test_paired_report_drops_a_one_sided_market(db_session: Session) -> None:
    add_pair(db_session, "m1", 0.60, 0.41, True)
    db_session.add(  # only one side ever traded
        HistoricalSample(
            market_id="m2",
            league="nfl",
            market_type="moneyline",
            outcome_index=0,
            outcome_name="lonely",
            game_start=KICKOFF,
            close_price=0.93,
            close_age_hours=1.0,
            won=False,
        )
    )
    db_session.flush()

    assert backtest.report(db_session)["rows_used"] == 3
    assert backtest.report(db_session, paired=True)["rows_used"] == 2


def test_paired_report_drops_prices_that_cannot_be_the_same_moment(db_session: Session) -> None:
    """Two sides summing to 0.78 are quotes from different times, not one market."""
    add_pair(db_session, "good", 0.60, 0.41, True)
    add_pair(db_session, "stale", 0.60, 0.18, True)
    db_session.flush()

    kept = backtest.report(db_session, paired=True)
    assert kept["rows_used"] == 2
    assert kept["paired"] is True


def test_paired_filter_removes_the_artefact_it_was_built_for(db_session: Session) -> None:
    """The live NFL case: unpaired, the 90-99c band read as the only significant result in
    the table (p=0.003). Every one of those rows was a one-sided close."""
    for i in range(40):  # clean, correctly-priced pairs
        add_pair(db_session, f"pair{i}", 0.93, 0.08, won0=(i % 10 != 0))
    for i in range(40):  # one-sided stragglers that all lost
        db_session.add(
            HistoricalSample(
                market_id=f"solo{i}",
                league="nfl",
                market_type="moneyline",
                outcome_index=0,
                outcome_name="solo",
                game_start=KICKOFF,
                close_price=0.93,
                close_age_hours=1.0,
                won=False,
            )
        )
    db_session.flush()

    unpaired = next(b for b in backtest.report(db_session)["bands"] if b["low"] == 0.90)
    assert unpaired["significant"] is True  # the artefact

    paired = next(b for b in backtest.report(db_session, paired=True)["bands"] if b["low"] == 0.90)
    assert paired["n"] == 40
    assert paired["significant"] is False  # and it is gone


def test_paired_note_appears_in_the_text_report(db_session: Session) -> None:
    add_pair(db_session, "m1", 0.60, 0.41, True)
    db_session.flush()
    text = backtest.format_report(backtest.report(db_session, paired=True))
    text.encode("cp1252")
    assert "Paired quotes only" in text
