"""One row of every model round-trips through SQLite with tz-aware datetimes."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import Bet, BookQuote, Game, Market, Opportunity, PmQuote, Prefs, Scan
from tests.conftest import FIXED_NOW


def test_all_tables_created(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert {
        "games",
        "markets",
        "scans",
        "pm_quotes",
        "book_quotes",
        "opportunities",
        "bets",
        "prefs",
    } <= names


def test_indexes_exist(engine: Engine) -> None:
    insp = inspect(engine)

    def cols(table: str) -> set[tuple[str, ...]]:
        return {tuple(ix["column_names"]) for ix in insp.get_indexes(table)}

    assert ("game_id",) in cols("markets")
    assert ("scan_id",) in cols("opportunities")
    assert ("scan_id", "market_id") in cols("pm_quotes")
    assert ("scan_id", "game_id") in cols("book_quotes")
    assert ("status",) in cols("bets")


def test_create_one_of_everything_and_query_back(db_session: Session) -> None:
    start = FIXED_NOW + timedelta(days=1, hours=5, minutes=25)
    game = Game(
        league="nfl",
        home_key="BUF",
        away_key="KC",
        home_name="Buffalo Bills",
        away_name="Kansas City Chiefs",
        start_time=start,
        pm_event_slug="nfl-kc-buf-2026-09-20",
        pm_event_id="10001",
        book_game_id="a" * 32,
        espn_event_id="401000001",
        status="scheduled",
    )
    db_session.add(game)
    db_session.flush()

    market = Market(
        id="500001",
        game_id=game.id,
        market_type="moneyline",
        line=None,
        line_team_key=None,
        question="Chiefs vs. Bills",
        slug="nfl-kc-buf-2026-09-20-moneyline",
        condition_id="0x" + "1" * 64,
        outcome_a_name="Chiefs",
        outcome_a_key="KC",
        outcome_a_token="7" * 70,
        outcome_b_name="Bills",
        outcome_b_key="BUF",
        outcome_b_token="8" * 70,
        tick_size=0.01,
        min_order_size=5.0,
        accepting_orders=True,
        closed=False,
        resolved_outcome=None,
        liquidity=12000.0,
        volume=55000.0,
        last_seen_at=FIXED_NOW,
    )
    scan = Scan(
        started_at=FIXED_NOW,
        finished_at=FIXED_NOW + timedelta(seconds=4),
        kind="both",
        leagues=["nfl", "nba", "mlb"],
        ok=True,
        credits_used=9,
        credits_remaining=491,
        n_markets=1,
        n_matched=1,
        n_opps=1,
        errors=[],
        notes={"unmatched": [], "unparseable": []},
    )
    db_session.add_all([market, scan])
    db_session.flush()

    pm_quote = PmQuote(
        scan_id=scan.id,
        market_id=market.id,
        token=market.outcome_a_token,
        best_bid=0.54,
        best_ask=0.55,
        mid=0.545,
        ask_depth_json=[[0.55, 100.0], [0.56, 400.0]],
        bid_depth_json=[[0.54, 150.0]],
        fetched_at=FIXED_NOW,
    )
    book_quote = BookQuote(
        scan_id=scan.id,
        game_id=game.id,
        bookmaker="pinnacle",
        market_key="h2h",
        outcome_name="Kansas City Chiefs",
        price_american=-150,
        point=None,
        last_update=FIXED_NOW - timedelta(minutes=3),
        fetched_at=FIXED_NOW,
    )
    opportunity = Opportunity(
        scan_id=scan.id,
        market_id=market.id,
        token=market.outcome_a_token,
        outcome_key="KC",
        outcome_name="Chiefs",
        ask=0.55,
        effective_price=0.562375,
        fair_prob=0.5839,
        fair_method="power",
        n_books=5,
        books_used=["pinnacle", "betonlineag", "lowvig", "draftkings", "fanduel"],
        edge=0.0215,
        ev_per_dollar=0.0383,
        kelly=0.0492,
        suggested_stake=12.30,
        fill_price=0.5624,
        limit_price=0.56,
        computed_at=FIXED_NOW,
    )
    db_session.add_all([pm_quote, book_quote, opportunity])
    db_session.flush()

    bet = Bet(
        market_id=market.id,
        token=market.outcome_a_token,
        outcome_key="KC",
        outcome_name="Chiefs",
        mode="taker",
        price=0.55,
        shares=22.36,
        stake_usd=12.30,
        fee_usd=0.28,
        fair_at_bet=0.5839,
        edge_at_bet=0.0215,
        placed_at=FIXED_NOW,
        status="open",
        notes="demo",
    )
    prefs = Prefs(id=1)
    db_session.add_all([bet, prefs])
    db_session.commit()
    db_session.expire_all()

    # query back
    got_game = db_session.scalars(select(Game).where(Game.pm_event_id == "10001")).one()
    assert got_game.start_time == start
    assert got_game.start_time.tzinfo is not None
    assert [m.id for m in got_game.markets] == ["500001"]

    got_market = db_session.get(Market, "500001")
    assert got_market is not None
    assert got_market.game is got_game
    assert got_market.last_seen_at == FIXED_NOW
    assert [o.id for o in got_market.opportunities] == [opportunity.id]
    assert [b.id for b in got_market.bets] == [bet.id]

    got_scan = db_session.get(Scan, scan.id)
    assert got_scan is not None
    assert got_scan.leagues == ["nfl", "nba", "mlb"]
    assert got_scan.notes == {"unmatched": [], "unparseable": []}
    assert got_scan.finished_at == FIXED_NOW + timedelta(seconds=4)
    assert [o.id for o in got_scan.opportunities] == [opportunity.id]

    got_pm = db_session.scalars(select(PmQuote).where(PmQuote.scan_id == scan.id)).one()
    assert got_pm.ask_depth_json == [[0.55, 100.0], [0.56, 400.0]]
    assert got_pm.market.id == "500001"

    got_bq = db_session.scalars(select(BookQuote).where(BookQuote.game_id == game.id)).one()
    assert got_bq.price_american == -150
    assert got_bq.last_update == FIXED_NOW - timedelta(minutes=3)
    assert got_bq.game.home_key == "BUF"

    got_opp = db_session.get(Opportunity, opportunity.id)
    assert got_opp is not None
    assert got_opp.market.outcome_a_name == "Chiefs"
    assert got_opp.books_used[0] == "pinnacle"
    assert got_opp.computed_at == FIXED_NOW

    got_bet = db_session.scalars(select(Bet).where(Bet.status == "open")).one()
    assert got_bet.market.game.away_key == "KC"
    assert got_bet.placed_at == FIXED_NOW
    assert got_bet.pnl_usd is None and got_bet.clv is None

    got_prefs = db_session.get(Prefs, 1)
    assert got_prefs is not None
    assert got_prefs.bankroll == 1000.0
    assert got_prefs.book_weights["pinnacle"] == 3.0

    for obj in (got_game, got_market, got_scan, got_pm, got_bq, got_opp, got_bet, got_prefs):
        assert type(obj).__name__ in repr(obj)


def test_naive_datetimes_are_stored_as_utc(db_session: Session) -> None:
    naive = FIXED_NOW.replace(tzinfo=None)
    scan = Scan(started_at=naive, kind="poly", leagues=["nfl"])
    db_session.add(scan)
    db_session.commit()
    db_session.expire_all()
    got = db_session.get(Scan, scan.id)
    assert got is not None
    assert got.started_at == FIXED_NOW
