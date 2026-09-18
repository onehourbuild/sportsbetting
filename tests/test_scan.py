"""run_scan / run_scan_default over the fixture transports (docs/FIXTURES.md slate).

Expected opportunities with the SPEC defaults (min_edge 0.02, fee 0.05, power de-vig):
Chiefs ML (+0.023), Celtics ML (+0.037), Yankees ML (+0.024), Under 7.5 ATH@SEA (+0.040).
DAL@PHI and GSW@DEN produce none; the GSW@DEN spread (Polymarket -3.5 vs books -2.5) is
reported as "no book at line".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.espn import EspnClient
from app.clients.oddsapi import OddsApiClient
from app.clients.polymarket import GAMMA, PolymarketClient
from app.clients.transport import FixtureTransport, Route, TransportError, load_fixture
from app.models import Bet, BookQuote, Game, Market, Opportunity, PmQuote, Scan
from app.services import scan as scan_service
from app.services.demo import DEMO_NOW, build_demo_transport, demo_routes
from app.services.prefs import get_prefs, update_prefs
from app.services.scan import (
    GAME_STARTED,
    HOME_AWAY_CONVENTION,
    MARKET_CLOSED,
    MARKET_PAUSED,
    NO_BOOK_AT_LINE,
    estimate_books_cost,
    quota_status,
    run_scan,
    run_scan_default,
)
from app.settings import Settings
from tests.conftest import FIXED_NOW

ALL = ["nfl", "nba", "mlb"]
EXPECTED_EDGES: dict[tuple[str, str], float] = {
    ("500101", "Chiefs"): 0.023,
    ("500301", "Celtics"): 0.037,
    ("500501", "Yankees"): 0.024,
    ("500602", "Under"): 0.040,
}
TOL = 0.01


def _transport(settings: Settings, routes: Sequence[Route] | None = None) -> FixtureTransport:
    if routes is None:
        return build_demo_transport(settings)
    return FixtureTransport(
        settings.fixtures_dir, list(routes), default_headers={"x-requests-remaining": "497"}
    )


def _clients(transport: FixtureTransport, *, api_key: str = "fixture-key"):
    return (
        PolymarketClient(transport),
        OddsApiClient(transport, api_key) if api_key else None,
        EspnClient(transport),
    )


def _run(
    session: Session,
    transport: FixtureTransport,
    kind: str,
    *,
    leagues: list[str] | None = None,
    now: datetime = FIXED_NOW,
    api_key: str = "fixture-key",
    espn: bool = True,
):
    polymarket, oddsapi, espn_client = _clients(transport, api_key=api_key)
    return run_scan(
        session,
        polymarket=polymarket,
        oddsapi=oddsapi,
        espn=espn_client if espn else None,
        prefs=get_prefs(session),
        kind=kind,
        leagues=leagues or ALL,
        now=now,
    )


def _opps(session: Session, scan_id: int) -> dict[tuple[str, str], Opportunity]:
    rows = session.scalars(select(Opportunity).where(Opportunity.scan_id == scan_id))
    return {(o.market_id, o.outcome_name): o for o in rows}


def _count(session: Session, model: Any, **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(session.scalar(stmt) or 0)


# --------------------------------------------------------------------------- both


def test_both_scan_produces_the_expected_opportunities(db_session: Session, settings: Settings):
    transport = _transport(settings)
    result = _run(db_session, transport, "both")

    assert result.kind == "both" and result.leagues == ALL
    assert result.errors == []
    assert result.n_markets == 17  # 6 nfl + 6 nba + 5 mlb (BAL@TOR is closed)
    assert result.n_matched == 17
    assert result.n_opps == 4
    assert (result.credits_used, result.credits_remaining) == (9, 497)

    opps = _opps(db_session, result.scan_id)
    assert set(opps) == set(EXPECTED_EDGES)
    for key, expected in EXPECTED_EDGES.items():
        assert abs(opps[key].edge - expected) <= TOL, (key, opps[key].edge)
        assert opps[key].suggested_stake > 0
        assert opps[key].fair_method == "power" and opps[key].n_books == 5
        assert opps[key].limit_price is not None and opps[key].fill_price is not None
        assert opps[key].books_used == sorted(opps[key].books_used)
    # nothing from DAL@PHI (5002xx) or GSW@DEN (5004xx)
    assert not [k for k in opps if k[0].startswith(("5002", "5004"))]

    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok is True and scan.finished_at >= scan.started_at == FIXED_NOW
    assert [(u["market_id"], u["reason"]) for u in scan.notes["unmatched"]] == [
        ("500402", NO_BOOK_AT_LINE)
    ]
    assert "Nuggets" in scan.notes["unmatched"][0]["question"]
    assert {u["market_id"] for u in scan.notes["unparseable"]} == {
        "500104",
        "500105",
        "500304",
        "500404",
        "500504",
        "500603",
    }
    assert all("reason" in u and "league" in u for u in scan.notes["unparseable"])
    assert scan.notes["book_source"] == {"nfl": "oddsapi", "nba": "oddsapi", "mlb": "oddsapi"}
    assert scan.notes["quota"] == {"used": 3, "remaining": 497}
    assert result.unmatched == scan.notes["unmatched"]
    assert result.unparseable == scan.notes["unparseable"]


def test_both_scan_persists_games_markets_and_snapshots(db_session: Session, settings: Settings):
    transport = _transport(settings)
    result = _run(db_session, transport, "both")

    assert _count(db_session, Game) == 6
    assert _count(db_session, Market) == 17
    assert _count(db_session, PmQuote, scan_id=result.scan_id) == 34
    assert _count(db_session, BookQuote, scan_id=result.scan_id) > 0

    game = db_session.scalars(
        select(Game).where(Game.pm_event_slug == "nfl-kc-buf-2026-09-20")
    ).one()
    assert (game.league, game.home_key, game.away_key) == ("nfl", "BUF", "KC")
    assert (game.home_name, game.away_name) == ("Buffalo Bills", "Kansas City Chiefs")
    assert game.start_time == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert game.pm_event_id == "10001" and game.status == "scheduled"
    assert game.book_game_id and len(game.book_game_id) == 32  # Odds API id
    assert {m.id for m in game.markets} == {"500101", "500102", "500103"}

    market = db_session.get(Market, "500102")
    assert (market.market_type, market.line, market.line_team_key) == ("spread", -3.5, "KC")
    assert (market.outcome_a_name, market.outcome_a_key) == ("Chiefs", "KC")
    assert (market.outcome_b_name, market.outcome_b_key) == ("Bills", "BUF")
    assert len(market.outcome_a_token) >= 70 and market.last_seen_at == FIXED_NOW
    assert market.closed is False and market.resolved_outcome is None

    quote = db_session.scalars(
        select(PmQuote).where(PmQuote.token == db_session.get(Market, "500101").outcome_a_token)
    ).one()
    assert (quote.best_bid, quote.best_ask, quote.mid) == (0.54, 0.55, 0.545)
    assert quote.ask_depth_json == [[0.55, 126.0], [0.57, 518.0], [0.6, 1234.0]]
    assert quote.bid_depth_json[0] == [0.54, 274.0]
    assert len(quote.ask_depth_json) <= 10

    books = list(
        db_session.scalars(
            select(BookQuote).where(BookQuote.game_id == game.id, BookQuote.market_key == "h2h")
        )
    )
    assert {b.bookmaker for b in books} == {
        "pinnacle",
        "betonlineag",
        "lowvig",
        "draftkings",
        "fanduel",
    }
    pinnacle = {b.outcome_name: b.price_american for b in books if b.bookmaker == "pinnacle"}
    assert pinnacle == {"Kansas City Chiefs": -150, "Buffalo Bills": 130}
    spreads = [
        b for b in db_session.scalars(select(BookQuote).where(BookQuote.market_key == "spreads"))
    ]
    assert all(b.point is not None for b in spreads)

    # a second scan updates rows in place instead of duplicating games/markets
    _run(db_session, transport, "both", now=FIXED_NOW + timedelta(minutes=30))
    assert _count(db_session, Game) == 6 and _count(db_session, Market) == 17
    assert _count(db_session, Scan) == 2


# --------------------------------------------------------------------------- poly


def test_poly_rescan_reuses_stored_books_and_never_calls_the_odds_api(
    db_session: Session, settings: Settings
):
    transport = _transport(settings)
    first = _run(db_session, transport, "both")
    book_rows = _count(db_session, BookQuote)
    transport.calls.clear()

    later = FIXED_NOW + timedelta(hours=1)
    second = _run(db_session, transport, "poly", now=later, api_key="")
    assert second.errors == []
    assert set(_opps(db_session, second.scan_id)) == set(EXPECTED_EDGES)
    assert (second.credits_used, second.credits_remaining) == (None, None)
    assert not [c for c in transport.calls if "the-odds-api" in c["url"] or "espn" in c["url"]]
    assert _count(db_session, BookQuote) == book_rows  # reused, not copied
    scan = db_session.get(Scan, second.scan_id)
    assert scan.notes["book_source"] == {lg: f"stored:{first.scan_id}" for lg in ALL}

    # a poly scan with the Odds API client present still never spends credits
    transport.calls.clear()
    third = _run(db_session, transport, "poly", now=later)
    assert third.credits_used is None
    assert not [c for c in transport.calls if "the-odds-api" in c["url"]]


def test_poly_scan_ignores_stale_books(db_session: Session, settings: Settings):
    transport = _transport(settings)
    _run(db_session, transport, "both")
    update_prefs(db_session, {"stale_book_minutes": 30})
    result = _run(db_session, transport, "poly", now=FIXED_NOW + timedelta(hours=2))
    assert result.n_markets == 17 and result.n_matched == 0 and result.n_opps == 0
    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok is True
    assert scan.notes["book_source"] == {lg: "none" for lg in ALL}
    assert {u["reason"] for u in scan.notes["unmatched"]} == {"no book game"}
    assert len(scan.notes["unmatched"]) == 17

    # a books snapshot "from the future" (demo clock ahead of the wall clock) is fresh
    early = _run(db_session, transport, "poly", now=FIXED_NOW - timedelta(days=2))
    assert early.n_opps == 4


def test_poly_scan_without_any_books_yet(db_session: Session, settings: Settings):
    result = _run(db_session, _transport(settings), "poly")
    assert result.n_markets == 17 and result.n_matched == 0 and result.n_opps == 0
    assert _count(db_session, PmQuote) == 34 and _count(db_session, BookQuote) == 0


# --------------------------------------------------------------------------- books


def test_books_scan_without_api_key_falls_back_to_espn(db_session: Session, settings: Settings):
    transport = _transport(settings)
    result = _run(db_session, transport, "books", leagues=["nfl"], api_key="")
    assert result.errors == []
    assert result.credits_used is None and result.credits_remaining is None
    rows = list(db_session.scalars(select(BookQuote)))
    assert rows and {r.bookmaker for r in rows} == {"espn"}
    # The ESPN client contributes h2h + spreads + totals "when present" and never invents a
    # price, so the totals market appears exactly when the scoreboard prices both sides.
    scoreboard = load_fixture("espn_scoreboard_nfl.json", settings.fixtures_dir)
    totals_priced = any(
        "overOdds" in odds and "underOdds" in odds
        for event in scoreboard.get("events", [])
        for competition in event.get("competitions", [])
        for odds in competition.get("odds") or []
    )
    expected_keys = {"h2h", "spreads"} | ({"totals"} if totals_priced else set())
    assert {r.market_key for r in rows} == expected_keys
    opps = _opps(db_session, result.scan_id)
    assert set(opps) == {("500101", "Chiefs")}
    assert opps[("500101", "Chiefs")].books_used == ["espn"]
    assert abs(opps[("500101", "Chiefs")].edge - 0.021) <= TOL
    game = db_session.scalars(select(Game).where(Game.away_key == "KC")).one()
    assert game.book_game_id == "espn:401772101" and game.espn_event_id == "401772101"
    scan = db_session.get(Scan, result.scan_id)
    assert scan.notes["book_source"] == {"nfl": "espn"}
    # ESPN buckets by US Eastern date: yesterday, today and tomorrow ET, once each
    # (2026-09-19T15:00Z is 11:00 ET on the 19th)
    dates = [c["params"]["dates"] for c in transport.calls if c["url"].endswith("/scoreboard")]
    assert dates == ["20260918", "20260919", "20260920"]


def test_books_scan_skips_espn_when_fallback_disabled(db_session: Session, settings: Settings):
    update_prefs(db_session, {"espn_fallback_enabled": False})
    result = _run(db_session, _transport(settings), "books", leagues=["nfl"], api_key="")
    assert result.n_opps == 0 and _count(db_session, BookQuote) == 0
    assert db_session.get(Scan, result.scan_id).notes["book_source"] == {"nfl": "none"}


def test_books_scan_falls_back_to_espn_when_the_odds_api_fails(
    db_session: Session, settings: Settings
):
    def boom(url: str, params: Any, body: Any):
        raise TransportError("upstream down", status=503, url=url)

    routes = [
        r if "the-odds-api" not in r[1] else (r[0], r[1], boom)
        for r in demo_routes(settings.fixtures_dir)
    ]
    transport = _transport(settings, routes)
    result = _run(db_session, transport, "books", leagues=["nfl"])
    assert len(result.errors) == 1 and "nfl: odds api" in result.errors[0]
    assert "fixture-key" not in result.errors[0]
    assert {r.bookmaker for r in db_session.scalars(select(BookQuote))} == {"espn"}
    assert set(_opps(db_session, result.scan_id)) == {("500101", "Chiefs")}
    assert db_session.get(Scan, result.scan_id).ok is True


# --------------------------------------------------------------------------- failures


def test_one_league_failing_does_not_abort_the_others(db_session: Session, settings: Settings):
    def boom(url: str, params: Any, body: Any):
        raise TransportError("gamma 500", status=500, url=url)

    routes = [
        (r[0], r[1], boom) if r[1] == f"{GAMMA}/events?tag_slug=nba" else r
        for r in demo_routes(settings.fixtures_dir)
    ]
    result = _run(db_session, _transport(settings, routes), "both")
    assert len(result.errors) == 1
    assert result.errors[0].startswith("nba: scan: PolymarketError")
    assert result.n_markets == 11 and result.n_opps == 3
    assert set(_opps(db_session, result.scan_id)) == {
        ("500101", "Chiefs"),
        ("500501", "Yankees"),
        ("500602", "Under"),
    }
    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok is True and scan.errors == result.errors
    assert scan.notes["book_source"] == {"nfl": "oddsapi", "mlb": "oddsapi"}


def test_every_league_failing_marks_the_scan_not_ok(db_session: Session, settings: Settings):
    result = _run(db_session, _transport(settings, []), "poly")
    assert len(result.errors) == 3 and result.n_markets == 0
    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok is False and scan.finished_at is not None


def test_total_failure_persists_the_scan_and_reraises(
    db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    def explode(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("settlement exploded")

    monkeypatch.setattr(scan_service.bets_service, "settle_open_bets", explode)
    with pytest.raises(RuntimeError, match="settlement exploded"):
        _run(db_session, _transport(settings), "both")
    scan = db_session.scalars(select(Scan)).one()
    assert scan.ok is False
    assert scan.errors[-1] == "scan: RuntimeError: settlement exploded"


def test_unknown_kind_is_rejected(db_session: Session, settings: Settings):
    with pytest.raises(ValueError, match="unknown scan kind"):
        _run(db_session, _transport(settings), "magic")


def test_order_book_failure_still_persists_markets(db_session: Session, settings: Settings):
    def boom(url: str, params: Any, body: Any):
        raise TransportError("clob down", status=502, url=url)

    routes = [
        (r[0], r[1], boom) if r[1].endswith("/books") else r
        for r in demo_routes(settings.fixtures_dir)
    ]
    result = _run(db_session, _transport(settings, routes), "both", leagues=["nfl"])
    assert result.n_markets == 6 and result.n_matched == 6 and result.n_opps == 0
    assert [e.split(":")[1].strip() for e in result.errors] == ["order books"]
    quotes = list(db_session.scalars(select(PmQuote)))
    assert len(quotes) == 12 and all(q.best_ask is None for q in quotes)


# --------------------------------------------------------------------------- settlement hook


def test_scan_settles_open_bets_on_closed_markets(db_session: Session, settings: Settings):
    transport = _transport(settings)
    polymarket = PolymarketClient(transport)
    bal = polymarket.market("500701")
    game = scan_service.upsert_game(db_session, bal, FIXED_NOW)
    scan_service.upsert_market(db_session, bal, game, FIXED_NOW)
    orioles = Bet(
        market_id="500701",
        token=bal.outcomes[0].token_id,
        outcome_key="BAL",
        outcome_name="Orioles",
        mode="taker",
        price=0.42,
        shares=23.1385,
        stake_usd=10.0,
        fee_usd=0.28,
        placed_at=FIXED_NOW - timedelta(days=2),
        status="open",
    )
    jays = Bet(
        market_id="500701",
        token=bal.outcomes[1].token_id,
        outcome_key="TOR",
        outcome_name="Blue Jays",
        mode="maker",
        price=0.60,
        shares=25.0,
        stake_usd=15.0,
        fee_usd=0.0,
        placed_at=FIXED_NOW - timedelta(days=2),
        status="open",
    )
    db_session.add_all([orioles, jays])
    db_session.commit()

    result = _run(db_session, transport, "poly")
    assert result.errors == []
    db_session.refresh(orioles)
    db_session.refresh(jays)
    assert (orioles.status, orioles.pnl_usd, orioles.settled_at) == ("lost", -10.0, FIXED_NOW)
    assert (jays.status, jays.pnl_usd) == ("won", 10.0)
    assert db_session.get(Scan, result.scan_id).notes["settled"] == 2
    market = db_session.get(Market, "500701")
    assert market.closed is True and market.resolved_outcome == "b"
    assert any(c["url"] == f"{GAMMA}/markets/500701" for c in transport.calls)


# --------------------------------------------------------------------------- default / helpers


def test_run_scan_default_in_demo_mode_uses_fixtures_and_the_demo_clock(
    db_session: Session, settings: Settings
):
    demo = settings.model_copy(update={"demo_mode": True})
    result = run_scan_default(db_session, "both", settings=demo)
    assert result.n_opps == 4 and result.errors == []
    assert result.credits_remaining == 497
    scan = db_session.get(Scan, result.scan_id)
    assert scan.started_at == DEMO_NOW and scan.leagues == ALL

    only_nfl = run_scan_default(db_session, "poly", leagues=["nfl"], settings=demo)
    assert only_nfl.leagues == ["nfl"] and only_nfl.n_opps == 1


def test_run_scan_default_honours_enabled_leagues(db_session: Session, settings: Settings):
    update_prefs(db_session, {"leagues_enabled": ["mlb"]})
    demo = settings.model_copy(update={"demo_mode": True})
    result = run_scan_default(db_session, "both", settings=demo)
    assert result.leagues == ["mlb"] and result.n_markets == 5 and result.n_opps == 2


def test_estimate_books_cost_and_quota_status(db_session: Session, settings: Settings):
    prefs = get_prefs(db_session)
    assert estimate_books_cost(prefs) == 9  # 3 markets x 1 region x 3 leagues
    update_prefs(
        db_session, {"bookmakers": [f"book{i}" for i in range(11)], "leagues_enabled": ["nfl"]}
    )
    assert estimate_books_cost(get_prefs(db_session)) == 6  # 3 markets x 2 regions x 1 league

    assert quota_status(db_session) == {"remaining": None, "used": None, "as_of": None}
    update_prefs(db_session, {"leagues_enabled": ALL, "bookmakers": ["pinnacle"]})
    transport = _transport(settings)
    _run(db_session, transport, "both")
    status = quota_status(db_session)
    assert (status["remaining"], status["used"], status["as_of"]) == (497, 3, FIXED_NOW)
    # a later poly scan records no quota, so the books scan stays the source of truth
    _run(db_session, transport, "poly", now=FIXED_NOW + timedelta(hours=1))
    assert quota_status(db_session)["as_of"] == FIXED_NOW


# --------------------------------------------------------------------------- review fixes


def _boom(status: int, message: str = "upstream down"):
    def handler(url: str, params: Any, body: Any):
        raise TransportError(message, status=status, url=url)

    return handler


def _static(payload: Any):
    def handler(url: str, params: Any, body: Any):
        return payload, {}

    return handler


def _routes_with(settings: Settings, needle: str, target) -> list[Route]:
    """The demo routes with every route whose URL prefix contains `needle` replaced."""
    return [
        (r[0], r[1], target) if needle in r[1] else r for r in demo_routes(settings.fixtures_dir)
    ]


def _orioles_bet(session: Session, transport: FixtureTransport) -> Bet:
    """An open bet on the (closed, lost) BAL@TOR market that the active slate no longer lists."""
    bal = PolymarketClient(transport).market("500701")
    game = scan_service.upsert_game(session, bal, FIXED_NOW)
    scan_service.upsert_market(session, bal, game, FIXED_NOW)
    bet = Bet(
        market_id="500701",
        token=bal.outcomes[0].token_id,
        outcome_key="BAL",
        outcome_name="Orioles",
        mode="taker",
        price=0.42,
        shares=23.1385,
        stake_usd=10.0,
        fee_usd=0.28,
        placed_at=FIXED_NOW - timedelta(days=2),
        status="open",
    )
    session.add(bet)
    session.commit()
    return bet


def test_books_scan_uses_the_stored_snapshot_when_odds_api_and_espn_both_fail(
    db_session: Session, settings: Settings
):
    """DECISIONS: a league with no fresh source at all re-prices against the stored snapshot."""
    first = _run(db_session, _transport(settings), "both", leagues=["nfl"])
    assert first.n_opps == 1
    routes = [
        (r[0], r[1], _boom(503)) if ("the-odds-api" in r[1] or "espn" in r[1]) else r
        for r in demo_routes(settings.fixtures_dir)
    ]
    result = _run(
        db_session,
        _transport(settings, routes),
        "books",
        leagues=["nfl"],
        now=FIXED_NOW + timedelta(minutes=10),
    )
    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok is True
    assert scan.notes["book_source"] == {"nfl": f"stored:{first.scan_id}"}
    assert set(_opps(db_session, result.scan_id)) == {("500101", "Chiefs")}
    assert result.n_matched == 6
    assert [e.split(":")[1].strip() for e in result.errors] == [
        "odds api",
        "espn scoreboard 2026-09-18",
        "espn scoreboard 2026-09-19",
        "espn scoreboard 2026-09-20",
    ]
    assert "fixture-key" not in " ".join(result.errors)
    assert _count(db_session, BookQuote, scan_id=result.scan_id) == 0  # reused, not copied


def test_espn_with_no_games_falls_back_to_the_stored_snapshot(
    db_session: Session, settings: Settings
):
    first = _run(db_session, _transport(settings), "both", leagues=["nfl"])
    routes = _routes_with(settings, "the-odds-api", _boom(503))
    routes = [(r[0], r[1], _static({"events": []})) if "espn" in r[1] else r for r in routes]
    result = _run(
        db_session,
        _transport(settings, routes),
        "books",
        leagues=["nfl"],
        now=FIXED_NOW + timedelta(minutes=10),
    )
    scan = db_session.get(Scan, result.scan_id)
    assert scan.notes["book_source"] == {"nfl": f"stored:{first.scan_id}"}
    assert set(_opps(db_session, result.scan_id)) == {("500101", "Chiefs")}


def test_settlement_lookup_failure_is_recorded_and_keeps_the_bet_open(
    db_session: Session, settings: Settings
):
    transport = _transport(settings)
    bet = _orioles_bet(db_session, transport)
    routes = _routes_with(settings, f"{GAMMA}/markets/", _boom(503))
    result = _run(db_session, _transport(settings, routes), "both")
    assert len(result.errors) == 1
    assert result.errors[0].startswith("settle: market 500701: PolymarketError")
    db_session.refresh(bet)
    assert bet.status == "open" and bet.pnl_usd is None
    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok is True and scan.notes["settled"] == 0
    assert result.n_opps == 4  # the slate is unaffected

    # the next scan settles it once Gamma answers again
    later = _run(db_session, transport, "poly", now=FIXED_NOW + timedelta(hours=1))
    assert later.errors == []
    db_session.refresh(bet)
    assert bet.status == "lost"


def test_settlement_lookup_404_is_not_an_error(db_session: Session, settings: Settings):
    transport = _transport(settings)
    bet = _orioles_bet(db_session, transport)
    routes = _routes_with(settings, f"{GAMMA}/markets/", _boom(404, "gone"))
    result = _run(db_session, _transport(settings, routes), "poly")
    assert result.errors == []
    db_session.refresh(bet)
    assert bet.status == "open"
    assert db_session.get(Scan, result.scan_id).notes["settled"] == 0


def test_settlement_recreates_a_deleted_market_row(db_session: Session, settings: Settings):
    from sqlalchemy import delete

    transport = _transport(settings)
    bet = _orioles_bet(db_session, transport)
    db_session.execute(delete(Market).where(Market.id == "500701"))
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Market, "500701") is None

    result = _run(db_session, transport, "poly")
    assert result.errors == []
    row = db_session.get(Market, "500701")
    assert row is not None and row.game_id is None and row.closed is True
    db_session.refresh(bet)
    assert (bet.status, bet.pnl_usd) == ("lost", -10.0)


def test_started_games_are_not_priced_and_a_never_stale_snapshot_serves_the_rest(
    db_session: Session, settings: Settings
):
    """During a game the live Polymarket price must not be compared with the pre-game book
    snapshot; stale_book_minutes=0 means the snapshot never expires."""
    transport = _transport(settings)
    first = _run(db_session, transport, "both")
    update_prefs(db_session, {"stale_book_minutes": 0})
    live_now = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)  # KC@BUF kicked off 20:25Z; MLB is over
    live = _run(db_session, transport, "poly", now=live_now)
    assert live.errors == []
    assert set(_opps(db_session, live.scan_id)) == {("500301", "Celtics")}  # LAL@BOS is in October
    scan = db_session.get(Scan, live.scan_id)
    started = {u["market_id"] for u in scan.notes["unmatched"] if u["reason"] == GAME_STARTED}
    assert started == {
        "500101",
        "500102",
        "500103",
        "500201",
        "500202",
        "500203",
        "500501",
        "500502",
        "500503",
        "500601",
        "500602",
    }
    assert live.n_matched == 6
    assert scan.notes["book_source"]["nba"] == f"stored:{first.scan_id}"  # 31 h old, still used
    assert scan.notes["conventions"]["stale_book_minutes"] == 0
    game = db_session.scalars(select(Game).where(Game.away_key == "KC")).one()
    assert game.status == "live"
    # live quotes are still stored for the ledger marks; they are just never priced
    assert _count(db_session, PmQuote, scan_id=live.scan_id) == 34


def test_paused_and_closed_markets_are_not_priced(db_session: Session, settings: Settings):
    events = load_fixture("gamma_events_nfl.json", settings.fixtures_dir)
    kc_markets = {m["id"]: m for m in events[0]["markets"]}
    kc_markets["500101"]["acceptingOrders"] = False
    kc_markets["500102"]["closed"] = True
    routes = _routes_with(settings, f"{GAMMA}/events?tag_slug=nfl", _static(events))
    result = _run(db_session, _transport(settings, routes), "both", leagues=["nfl"])
    assert result.n_opps == 0 and result.n_matched == 4
    scan = db_session.get(Scan, result.scan_id)
    reasons = {u["market_id"]: u["reason"] for u in scan.notes["unmatched"]}
    assert reasons["500101"] == MARKET_PAUSED and reasons["500102"] == MARKET_CLOSED
    assert "500103" not in reasons
    assert db_session.get(Market, "500101").accepting_orders is False


def test_same_day_doubleheader_events_are_two_games_with_their_own_book_lines(
    db_session: Session, settings: Settings
):
    import copy
    import json

    events = load_fixture("gamma_events_mlb.json", settings.fixtures_dir)
    game1_event = next(e for e in events if e["slug"] == "mlb-nyy-lad-2026-09-19")
    game2_event = copy.deepcopy(game1_event)
    game2_event["id"], game2_event["slug"] = "10099", "mlb-nyy-lad-2026-09-19-game-2"
    game2_event["startDate"] = "2026-09-20T09:10:00Z"  # seven hours after game 1, same UTC day
    game2_event["markets"] = [
        dict(
            game1_event["markets"][0],
            id="500991",
            gameStartTime="2026-09-20T09:10:00Z",
            clobTokenIds=json.dumps(["5" * 75, "6" * 75]),
        )
    ]
    odds = load_fixture("oddsapi_mlb.json", settings.fixtures_dir)
    book1 = next(g for g in odds if g["home_team"] == "Los Angeles Dodgers")
    book2 = copy.deepcopy(book1)
    book2["id"], book2["commence_time"] = "f" * 32, "2026-09-20T09:10:00Z"
    for book in book2["bookmakers"]:
        for market in book["markets"]:
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    outcome["price"] = -200 if outcome["name"] == "Los Angeles Dodgers" else 170
    routes = _routes_with(settings, f"{GAMMA}/events?tag_slug=mlb", _static([*events, game2_event]))
    routes = [
        (r[0], r[1], _static([*odds, book2])) if "baseball_mlb" in r[1] else r for r in routes
    ]

    result = _run(db_session, _transport(settings, routes), "both", leagues=["mlb"])
    assert result.n_markets == 6
    games = list(
        db_session.scalars(
            select(Game)
            .where(Game.home_key == "LAD", Game.away_key == "NYY")
            .order_by(Game.start_time.asc())
        )
    )
    assert len(games) == 2
    assert [g.start_time for g in games] == [
        datetime(2026, 9, 20, 2, 10, tzinfo=UTC),
        datetime(2026, 9, 20, 9, 10, tzinfo=UTC),
    ]
    assert games[1].pm_event_id == "10099" and games[0].pm_event_id != "10099"
    assert games[1].book_game_id == "f" * 32 and games[0].book_game_id == book1["id"]
    assert db_session.get(Market, "500991").game_id == games[1].id
    pinnacle = {}
    for game in games:
        rows = db_session.scalars(
            select(BookQuote).where(
                BookQuote.game_id == game.id,
                BookQuote.bookmaker == "pinnacle",
                BookQuote.market_key == "h2h",
            )
        )
        pinnacle[game.id] = {q.outcome_name: q.price_american for q in rows}
    assert pinnacle[games[0].id]["Los Angeles Dodgers"] == -140
    assert pinnacle[games[1].id]["Los Angeles Dodgers"] == -200


def test_upsert_game_keys_by_event_id_and_never_reverts_final(
    db_session: Session, settings: Settings
):
    from dataclasses import replace

    market = PolymarketClient(_transport(settings)).market("500701")  # BAL@TOR, closed
    teamless = replace(
        market,
        home_team_key=None,
        away_team_key=None,
        outcomes=tuple(replace(o, team_key=None) for o in market.outcomes),
    )
    g1 = scan_service.upsert_game(db_session, teamless, FIXED_NOW)
    g2 = scan_service.upsert_game(db_session, teamless, FIXED_NOW + timedelta(hours=1))
    assert g1.id == g2.id and _count(db_session, Game) == 1
    assert g1.pm_event_id == "10007" and g1.home_key is None and g1.status == "final"
    reopened = replace(teamless, closed=False, resolved_outcome_index=None)
    assert scan_service.upsert_game(db_session, reopened, FIXED_NOW).status == "final"

    # outcome keys but no event team keys: the outcomes are read as [away, home]
    by_outcomes = replace(
        market, home_team_key=None, away_team_key=None, event_id="", event_slug=""
    )
    g3 = scan_service.upsert_game(db_session, by_outcomes, FIXED_NOW)
    assert g3.id != g1.id
    assert (g3.home_key, g3.away_key) == ("TOR", "BAL")
    assert (g3.home_name, g3.away_name) == ("Toronto Blue Jays", "Baltimore Orioles")
    # the same teams within three hours is the same game; seven hours later is another game
    same = replace(by_outcomes, game_start=market.game_start + timedelta(hours=2))
    assert scan_service.upsert_game(db_session, same, FIXED_NOW).id == g3.id
    later = replace(by_outcomes, game_start=market.game_start + timedelta(hours=7))
    assert scan_service.upsert_game(db_session, later, FIXED_NOW).id != g3.id


# ------------------------------------------------------------------- review round 2 fixes


def _nfl_routes(settings: Settings, events: Any = None, odds: Any = None) -> list[Route]:
    routes = demo_routes(settings.fixtures_dir)
    if events is not None:
        routes = [
            (r[0], r[1], _static(events)) if r[1] == f"{GAMMA}/events?tag_slug=nfl" else r
            for r in routes
        ]
    if odds is not None:
        routes = [
            (r[0], r[1], _static(odds)) if "americanfootball_nfl" in r[1] else r for r in routes
        ]
    return routes


def test_upsert_game_never_takes_its_kickoff_from_a_sibling_markets_listing_date(
    db_session: Session, settings: Settings
):
    """Gamma's event `startDate` is the listing timestamp, not kickoff. A typed market with
    no `gameStartTime` that inherits it must not rewrite the game's start time: that made the
    whole game look "live" since the day it was listed, so nothing was ever priced."""
    from dataclasses import replace

    markets, _ = PolymarketClient(_transport(settings)).events("nfl")
    kc = {m.market_id: m for m in markets if m.market_id in ("500101", "500102", "500103")}
    kickoff = datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    listing = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)  # the event's startDate / creationDate
    moneyline = kc["500101"]
    assert moneyline.market_type == "moneyline" and moneyline.game_start == kickoff
    spread = replace(kc["500102"], game_start=listing)
    total = replace(kc["500103"], game_start=listing)

    # the siblings are seen first and carry the listing timestamp
    game = scan_service.upsert_game(db_session, total, FIXED_NOW)
    assert game.start_time == listing and game.status == "live"  # what the bug looked like
    game = scan_service.upsert_game(db_session, spread, FIXED_NOW)
    game = scan_service.upsert_game(db_session, moneyline, FIXED_NOW)
    assert game.start_time == kickoff  # the moneyline market is the kickoff authority
    assert game.status == "scheduled"

    # and no sibling may drag it backwards afterwards, however often it is seen
    for market in (total, spread, total):
        scan_service.upsert_game(db_session, market, FIXED_NOW)
        assert game.start_time == kickoff and game.status == "scheduled"

    # a market that carries no kickoff at all leaves the stored one alone
    scan_service.upsert_game(db_session, replace(total, game_start=None), FIXED_NOW)
    assert game.start_time == kickoff and game.status == "scheduled"
    assert _count(db_session, Game) == 1

    # a later real kickoff (a postponement) still moves it forward
    moved = kickoff + timedelta(hours=2)
    scan_service.upsert_game(db_session, replace(total, game_start=moved), FIXED_NOW)
    assert game.start_time == moved


def test_a_game_with_no_kickoff_at_all_is_left_without_a_start_time(
    db_session: Session, settings: Settings
):
    from dataclasses import replace

    markets, _ = PolymarketClient(_transport(settings)).events("nfl")
    total = replace(next(m for m in markets if m.market_id == "500103"), game_start=None)
    game = scan_service.upsert_game(db_session, total, FIXED_NOW)
    assert game.start_time is None and game.status == "scheduled"


def test_a_game_the_book_says_has_started_is_never_priced(db_session: Session, settings: Settings):
    """Polymarket has no gameStartTime for the market, but the sportsbook's commence_time is
    in the past: the book's word is enough to keep the market out of the slate."""
    events = load_fixture("gamma_events_nfl.json", settings.fixtures_dir)
    kc_event = next(e for e in events if e["slug"] == "nfl-kc-buf-2026-09-20")
    kc_event["startDate"] = None  # no usable event start either
    for market in kc_event["markets"]:
        if market["id"] in ("500101", "500102", "500103"):
            market["gameStartTime"] = None
    odds = load_fixture("oddsapi_nfl.json", settings.fixtures_dir)
    kc_buf = next(g for g in odds if g["home_team"] == "Buffalo Bills")
    kc_buf["commence_time"] = "2026-09-19T14:00:00Z"  # FIXED_NOW - 1 h

    transport = _transport(settings, _nfl_routes(settings, events, odds))
    result = _run(db_session, transport, "both", leagues=["nfl"])
    assert result.errors == []
    scan = db_session.get(Scan, result.scan_id)
    started = {u["market_id"] for u in scan.notes["unmatched"] if u["reason"] == GAME_STARTED}
    assert started == {"500101", "500102", "500103"}
    assert result.n_matched == 3  # DAL@PHI only
    assert not [k for k in _opps(db_session, result.scan_id) if k[0].startswith("5001")]
    assert ("500101", "Chiefs") not in _opps(db_session, result.scan_id)
    # the in-play book lines are not stored under the game either
    game = db_session.scalars(select(Game).where(Game.away_key == "KC")).one()
    assert _count(db_session, BookQuote, game_id=game.id) == 0


def test_run_scan_default_outside_demo_mode_builds_http_clients_and_closes_them(
    db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    """The production branch: HttpTransport, the configured Odds API key, and the transport
    closed on the way out."""
    closed: list[str] = []
    built: dict[str, Any] = {}

    def fake_http_transport(*args: Any, **kwargs: Any):
        transport = build_demo_transport(settings)
        transport.close = lambda: closed.append("transport")  # type: ignore[method-assign]
        built["transport"] = transport
        return transport

    real_espn = scan_service.EspnClient

    def spy_espn(*args: Any, **kwargs: Any):
        built["espn"] = True
        return real_espn(*args, **kwargs)

    monkeypatch.setattr(scan_service, "HttpTransport", fake_http_transport)
    monkeypatch.setattr(scan_service, "EspnClient", spy_espn)

    production = settings.model_copy(update={"demo_mode": False, "odds_api_key": "fixture-key"})
    result = run_scan_default(db_session, "both", now=FIXED_NOW, settings=production)

    assert result.errors == []
    assert built.get("espn") is True
    assert closed == ["transport"]
    # credits only come back when the key reached OddsApiClient
    assert (result.credits_used, result.credits_remaining) == (9, 497)
    assert result.n_opps == 4
    assert db_session.get(Scan, result.scan_id).started_at == FIXED_NOW
    # settings.odds_api_key, not the demo placeholder, is what OddsApiClient signed with
    odds_calls = [c for c in built["transport"].calls if "the-odds-api" in c["url"]]
    assert odds_calls and {c["params"]["apiKey"] for c in odds_calls} == {"fixture-key"}


def test_a_second_scan_while_one_is_running_raises_scan_busy(
    db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    demo = settings.model_copy(update={"demo_mode": True})
    tried: list[str] = []
    real_run_scan = scan_service.run_scan

    def reentrant(session: Session, **kwargs: Any):
        with pytest.raises(scan_service.ScanBusy, match="a scan is already running"):
            run_scan_default(session, "poly", settings=demo)
        tried.append("busy")
        return real_run_scan(session, **kwargs)

    monkeypatch.setattr(scan_service, "run_scan", reentrant)
    result = run_scan_default(db_session, "both", settings=demo)
    assert tried == ["busy"] and result.n_opps == 4
    assert scan_service.SCAN_LOCK.locked() is False  # released on the way out


def test_scan_busy_is_raised_across_threads_and_the_lock_is_always_released(
    db_session: Session, engine: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    import threading

    from app.db import create_session_factory

    demo = settings.model_copy(update={"demo_mode": True})
    holding, release = threading.Event(), threading.Event()
    real_run_scan = scan_service.run_scan
    outcome: dict[str, Any] = {}

    def parked(session: Session, **kwargs: Any):
        holding.set()
        assert release.wait(10)
        return real_run_scan(session, **kwargs)

    monkeypatch.setattr(scan_service, "run_scan", parked)
    factory = create_session_factory(engine)

    def worker() -> None:
        session = factory()
        try:
            outcome["result"] = run_scan_default(session, "poly", leagues=["nba"], settings=demo)
        except Exception as exc:  # noqa: BLE001 - reported back to the test thread
            outcome["error"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert holding.wait(10)
        with pytest.raises(scan_service.ScanBusy):
            run_scan_default(db_session, "poly", leagues=["nba"], settings=demo)
    finally:
        release.set()
        thread.join(30)
    assert "error" not in outcome, outcome.get("error")
    assert scan_service.SCAN_LOCK.locked() is False

    # a scan that blows up still hands the lock back
    def boom(session: Session, **kwargs: Any):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(scan_service, "run_scan", boom)
    with pytest.raises(RuntimeError, match="scan exploded"):
        run_scan_default(db_session, "poly", settings=demo)
    assert scan_service.SCAN_LOCK.locked() is False


def test_the_stake_note_reaches_the_opportunity_row(db_session: Session, settings: Settings):
    """A bankroll too small for the market's minimum order size stores 0 with the reason."""
    update_prefs(db_session, {"bankroll": 100.0, "leagues_enabled": ["nfl"]})
    result = _run(db_session, _transport(settings), "both", leagues=["nfl"])
    chiefs = _opps(db_session, result.scan_id)[("500101", "Chiefs")]
    assert db_session.get(Market, "500101").min_order_size == 5.0
    assert chiefs.suggested_stake == 0.0
    assert chiefs.stake_note is not None and "minimum order" in chiefs.stake_note

    # the default bankroll clears the minimum and stores no note
    update_prefs(db_session, {"bankroll": 1000.0})
    bigger = _run(db_session, _transport(settings), "both", leagues=["nfl"])
    row = _opps(db_session, bigger.scan_id)[("500101", "Chiefs")]
    assert row.suggested_stake > 0 and row.stake_note is None


def test_scan_notes_record_conventions_and_unresolved_book_teams(
    db_session: Session, settings: Settings
):
    odds = load_fixture("oddsapi_nfl.json", settings.fixtures_dir)
    kc_buf = next(g for g in odds if g["home_team"] == "Buffalo Bills")
    kc_buf["home_team"] = "Buffalo Bisons"  # a relocation the alias table has not learned
    routes = _routes_with(settings, "americanfootball_nfl", _static(odds))
    update_prefs(db_session, {"match_window_hours": 12, "stale_book_minutes": 90})
    result = _run(db_session, _transport(settings, routes), "both", leagues=["nfl"])
    scan = db_session.get(Scan, result.scan_id)
    assert scan.notes["conventions"] == {
        "home_away": HOME_AWAY_CONVENTION,
        "match_window_hours": 12.0,
        "stale_book_minutes": 90,
        # The Gamma fixture quotes 500 bps on one market; the client accepts it as 0.05 and
        # the scan records every override so Diagnostics can show which markets are not
        # priced at the preference.
        "taker_fee_overrides": [
            {"league": "nfl", "market_id": "500103", "raw": 500, "rate": 0.05},
        ],
    }
    assert scan.notes["unresolved_book_teams"] == [
        {"league": "nfl", "name": "Buffalo Bisons", "source": "oddsapi"}
    ]
    no_game = {u["market_id"] for u in scan.notes["unmatched"] if u["reason"] == "no book game"}
    assert no_game == {"500101", "500102", "500103"}
    assert _opps(db_session, result.scan_id) == {}
    # a clean scan records an empty list, not a missing key
    clean = _run(db_session, _transport(settings), "both", leagues=["nba"])
    assert db_session.get(Scan, clean.scan_id).notes["unresolved_book_teams"] == []
