"""Phone-first UI: edges, game, bets, settings, diagnostics.

Display data is seeded straight into the ORM (modeled on docs/FIXTURES.md); the
services layer is monkeypatched on its module so the routes' `scan_service.x` /
`bets_service.x` lookups see the fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.types import ScanResult
from app.models import Bet, BookQuote, Game, Market, Opportunity, PmQuote, Scan
from app.services import bets as bets_service
from app.services import prefs as prefs_service
from app.services import scan as scan_service
from tests.conftest import FIXED_NOW

TOK_KC_A = "1" * 70  # Chiefs (moneyline)
TOK_KC_B = "2" * 70  # Bills (moneyline)
TOK_SP_A = "3" * 70  # Chiefs -3.5
TOK_SP_B = "4" * 70  # Bills +3.5
TOK_TOT_A = "5" * 70  # Over 47.5
TOK_TOT_B = "6" * 70  # Under 47.5
TOK_BOS_A = "7" * 70  # Lakers
TOK_BOS_B = "8" * 70  # Celtics
TOK_LAD_A = "9" * 70  # Yankees
TOK_LAD_B = "10" * 35  # Dodgers
TOK_TOR_A = "11" * 35  # Orioles
TOK_TOR_B = "12" * 35  # Blue Jays

NFL_START = datetime(2026, 9, 20, 20, 25, tzinfo=UTC)


def _market(market_id: str, game: Game, kind: str, a: tuple, b: tuple, **kw: Any) -> Market:
    return Market(
        id=market_id,
        game_id=game.id,
        market_type=kind,
        outcome_a_name=a[0],
        outcome_a_key=a[1],
        outcome_a_token=a[2],
        outcome_b_name=b[0],
        outcome_b_key=b[1],
        outcome_b_token=b[2],
        tick_size=0.01,
        min_order_size=5.0,
        accepting_orders=True,
        liquidity=32718.0,
        volume=107548.0,
        last_seen_at=FIXED_NOW,
        **kw,
    )


def seed_slate(session: Session) -> dict[str, Any]:
    """One ok scan; NFL/NBA/MLB games; quotes, book rows, three opportunities, two bets."""
    scan = Scan(
        started_at=FIXED_NOW - timedelta(minutes=5),
        finished_at=FIXED_NOW - timedelta(minutes=5, seconds=-4),
        kind="both",
        leagues=["nfl", "nba", "mlb"],
        ok=True,
        credits_used=9,
        credits_remaining=491,
        n_markets=6,
        n_matched=5,
        n_opps=3,
        errors=[],
        notes={"unmatched": [], "unparseable": []},
    )
    session.add(scan)
    session.flush()

    g_nfl = Game(
        league="nfl",
        home_key="BUF",
        away_key="KC",
        home_name="Buffalo Bills",
        away_name="Kansas City Chiefs",
        start_time=NFL_START,
        pm_event_slug="nfl-kc-buf-2026-09-20",
        pm_event_id="10001",
        book_game_id="a" * 32,
        status="scheduled",
    )
    g_nba = Game(
        league="nba",
        home_key="BOS",
        away_key="LAL",
        home_name="Boston Celtics",
        away_name="Los Angeles Lakers",
        start_time=datetime(2026, 10, 22, 23, 30, tzinfo=UTC),
        pm_event_slug="nba-lal-bos-2026-10-22",
        pm_event_id="10003",
        status="scheduled",
    )
    g_mlb = Game(
        league="mlb",
        home_key="LAD",
        away_key="NYY",
        home_name="Los Angeles Dodgers",
        away_name="New York Yankees",
        start_time=datetime(2026, 9, 20, 2, 10, tzinfo=UTC),
        pm_event_slug="mlb-nyy-lad-2026-09-19",
        pm_event_id="10005",
        status="scheduled",
    )
    g_done = Game(
        league="mlb",
        home_key="TOR",
        away_key="BAL",
        home_name="Toronto Blue Jays",
        away_name="Baltimore Orioles",
        start_time=datetime(2026, 9, 17, 23, 7, tzinfo=UTC),
        pm_event_slug="mlb-bal-tor-2026-09-17",
        pm_event_id="10007",
        status="final",
    )
    session.add_all([g_nfl, g_nba, g_mlb, g_done])
    session.flush()

    m_kc_ml = _market(
        "500101",
        g_nfl,
        "moneyline",
        ("Chiefs", "KC", TOK_KC_A),
        ("Bills", "BUF", TOK_KC_B),
        question="Chiefs vs. Bills",
        slug="nfl-kc-buf-2026-09-20-moneyline",
    )
    m_kc_sp = _market(
        "500102",
        g_nfl,
        "spread",
        ("Chiefs", "KC", TOK_SP_A),
        ("Bills", "BUF", TOK_SP_B),
        question="Spread: Chiefs (-3.5)",
        line=-3.5,
        line_team_key="KC",
    )
    m_kc_tot = _market(
        "500103",
        g_nfl,
        "total",
        ("Over", None, TOK_TOT_A),
        ("Under", None, TOK_TOT_B),
        question="O/U 47.5",
        line=47.5,
    )
    m_bos_ml = _market(
        "500301",
        g_nba,
        "moneyline",
        ("Lakers", "LAL", TOK_BOS_A),
        ("Celtics", "BOS", TOK_BOS_B),
        question="Lakers vs. Celtics",
    )
    m_lad_ml = _market(
        "500501",
        g_mlb,
        "moneyline",
        ("Yankees", "NYY", TOK_LAD_A),
        ("Dodgers", "LAD", TOK_LAD_B),
        question="Yankees vs. Dodgers",
    )
    m_tor_ml = _market(
        "500701",
        g_done,
        "moneyline",
        ("Orioles", "BAL", TOK_TOR_A),
        ("Blue Jays", "TOR", TOK_TOR_B),
        question="Orioles vs. Blue Jays",
        closed=True,
        resolved_outcome="b",
    )
    session.add_all([m_kc_ml, m_kc_sp, m_kc_tot, m_bos_ml, m_lad_ml, m_tor_ml])
    session.flush()

    def quote(market: Market, token: str, bid: float, ask: float, depth: list) -> PmQuote:
        return PmQuote(
            scan_id=scan.id,
            market_id=market.id,
            token=token,
            best_bid=bid,
            best_ask=ask,
            mid=round((bid + ask) / 2, 4),
            ask_depth_json=depth,
            bid_depth_json=[[bid, 150.0]],
            fetched_at=FIXED_NOW - timedelta(minutes=5),
        )

    kc_depth = [[0.55, 126], [0.57, 518], [0.60, 1234], [0.62, 300], [0.65, 400], [0.70, 500]]
    session.add_all(
        [
            quote(m_kc_ml, TOK_KC_A, 0.54, 0.55, kc_depth),
            quote(m_kc_ml, TOK_KC_B, 0.45, 0.46, [[0.46, 200]]),
            quote(m_kc_sp, TOK_SP_A, 0.49, 0.50, [[0.50, 300]]),
            quote(m_kc_sp, TOK_SP_B, 0.51, 0.52, [[0.52, 300]]),
            quote(m_kc_tot, TOK_TOT_A, 0.48, 0.49, [[0.49, 250]]),
            quote(m_kc_tot, TOK_TOT_B, 0.52, 0.53, [[0.53, 250]]),
            # dict-shaped depth with string numbers, as the CLOB returns them
            quote(m_bos_ml, TOK_BOS_B, 0.65, 0.66, [{"price": "0.66", "size": "200"}]),
            quote(m_lad_ml, TOK_LAD_A, 0.39, 0.40, [[0.40, 800]]),
        ]
    )

    def book(bookmaker: str, key: str, name: str, price: int, point: float | None) -> BookQuote:
        return BookQuote(
            scan_id=scan.id,
            game_id=g_nfl.id,
            bookmaker=bookmaker,
            market_key=key,
            outcome_name=name,
            price_american=price,
            point=point,
            last_update=FIXED_NOW - timedelta(minutes=8),
            fetched_at=FIXED_NOW - timedelta(minutes=5),
        )

    session.add_all(
        [
            book("pinnacle", "h2h", "Kansas City Chiefs", -150, None),
            book("pinnacle", "h2h", "Buffalo Bills", 130, None),
            book("betonlineag", "h2h", "Kansas City Chiefs", -148, None),
            book("betonlineag", "h2h", "Buffalo Bills", 128, None),
            book("draftkings", "h2h", "Kansas City Chiefs", -155, None),
            book("draftkings", "h2h", "Buffalo Bills", 135, None),
            book("pinnacle", "spreads", "Kansas City Chiefs", -110, -3.5),
            book("pinnacle", "spreads", "Buffalo Bills", -110, 3.5),
            book("draftkings", "spreads", "Kansas City Chiefs", -105, -3.0),
            book("draftkings", "spreads", "Buffalo Bills", -115, 3.0),
            book("pinnacle", "totals", "Over", -110, 47.5),
            book("pinnacle", "totals", "Under", -110, 47.5),
        ]
    )

    def opp(market: Market, token: str, key: str | None, name: str, **kw: Any) -> Opportunity:
        return Opportunity(
            scan_id=scan.id,
            market_id=market.id,
            token=token,
            outcome_key=key,
            outcome_name=name,
            fair_method="power",
            n_books=3,
            books_used=["pinnacle", "betonlineag", "draftkings"],
            computed_at=FIXED_NOW - timedelta(minutes=5),
            **kw,
        )

    o_kc = opp(
        m_kc_ml,
        TOK_KC_A,
        "KC",
        "Chiefs",
        ask=0.55,
        effective_price=0.562375,
        fair_prob=0.5839,
        edge=0.0214,
        ev_per_dollar=0.0383,
        kelly=0.0492,
        suggested_stake=12.30,
        fill_price=0.5624,
        limit_price=0.56,
    )
    o_bos = opp(
        m_bos_ml,
        TOK_BOS_B,
        "BOS",
        "Celtics",
        ask=0.66,
        effective_price=0.67122,
        fair_prob=0.6933,
        edge=0.0221,
        ev_per_dollar=0.0329,
        kelly=0.0672,
        suggested_stake=15.00,
        fill_price=0.6715,
        limit_price=0.67,
    )
    o_nyy = opp(
        m_lad_ml,
        TOK_LAD_A,
        "NYY",
        "Yankees",
        ask=0.40,
        effective_price=0.412,
        fair_prob=0.438,
        edge=0.026,
        ev_per_dollar=0.0631,
        kelly=0.0442,
        suggested_stake=18.50,
        fill_price=0.4121,
        limit_price=0.41,
    )
    session.add_all([o_kc, o_bos, o_nyy])

    b_open = Bet(
        market_id=m_kc_ml.id,
        token=TOK_KC_A,
        outcome_key="KC",
        outcome_name="Chiefs",
        mode="taker",
        price=0.55,
        shares=22.36,
        stake_usd=12.30,
        fee_usd=0.28,
        fair_at_bet=0.5839,
        edge_at_bet=0.0215,
        placed_at=FIXED_NOW - timedelta(hours=1),
        status="open",
        notes="demo",
    )
    b_lost = Bet(
        market_id=m_tor_ml.id,
        token=TOK_TOR_A,
        outcome_key="BAL",
        outcome_name="Orioles",
        mode="taker",
        price=0.42,
        shares=23.8,
        stake_usd=10.0,
        fee_usd=0.24,
        fair_at_bet=0.45,
        edge_at_bet=0.02,
        placed_at=FIXED_NOW - timedelta(days=2),
        status="lost",
        settled_at=FIXED_NOW - timedelta(days=1),
        pnl_usd=-10.0,
        closing_fair=0.40,
        closing_pm_price=0.41,
        clv=-0.02,
    )
    session.add_all([b_open, b_lost])
    session.commit()
    for obj in (scan, g_nfl, g_nba, g_mlb, g_done, o_kc, o_bos, o_nyy, b_open, b_lost):
        session.refresh(obj)
    return {
        "scan": scan,
        "g_nfl": g_nfl,
        "g_nba": g_nba,
        "g_mlb": g_mlb,
        "g_done": g_done,
        "o_kc": o_kc,
        "o_bos": o_bos,
        "o_nyy": o_nyy,
        "b_open": b_open,
        "b_lost": b_lost,
    }


# --------------------------------------------------------------------------- edges


def test_edges_lists_opportunity_cards_best_edge_first(client: TestClient, db_session: Session):
    slate = seed_slate(db_session)
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'id="edges-list"' in html
    assert html.index("Yankees") < html.index("Celtics") < html.index("Chiefs")
    assert "3 edges" in html
    # Chiefs card
    assert "Kansas City Chiefs @ Buffalo Bills" in html
    assert "Moneyline" in html
    assert 'class="badge badge-league league-nfl">NFL<' in html
    assert "55¢" in html  # ask
    assert "58.4%" in html  # fair
    assert "+2.1%" in html  # edge
    assert "+3.8%" in html  # EV
    assert "$12.30" in html  # suggested stake
    assert "56.2¢" in html  # fill price
    assert "56¢" in html  # limit price
    assert "cost incl. fee 56.2¢" in html
    assert 'href="https://polymarket.com/event/nfl-kc-buf-2026-09-20"' in html
    assert 'target="_blank"' in html
    assert f'hx-get="/bets/new?opportunity_id={slate["o_kc"].id}"' in html
    assert 'hx-target="#sheet"' in html
    assert 'datetime="2026-09-20T20:25:00Z"' in html
    # scan bar
    assert 'hx-post="/scan?kind=poly&amp;league=all"' in html
    assert 'hx-post="/scan?kind=books&amp;league=all"' in html
    assert "Last scan" in html
    assert '<time datetime="2026-09-19T14:55:00+00:00">' in html  # ago() uses the real clock
    assert "NotImplementedError" not in html


def test_edges_league_filter_chips(client: TestClient, db_session: Session):
    seed_slate(db_session)
    nfl = client.get("/?league=nfl").text
    assert "Chiefs" in nfl and "Celtics" not in nfl and "Yankees" not in nfl
    assert 'class="chip active" href="/?league=nfl"' in nfl
    assert 'hx-post="/scan?kind=poly&amp;league=nfl"' in nfl  # filter survives a refresh
    nba = client.get("/?league=nba").text
    assert "Celtics" in nba and "Chiefs" not in nba and "Yankees" not in nba
    mlb = client.get("/?league=mlb").text
    assert "Yankees" in mlb and "Chiefs" not in mlb
    bogus = client.get("/?league=nhl").text  # unknown -> all
    assert "Chiefs" in bogus and "Celtics" in bogus and "Yankees" in bogus
    assert 'class="chip active" href="/?league=all"' in bogus


def test_edges_empty_state_explains_buttons_and_demo(client: TestClient):
    html = client.get("/").text
    assert "No scans yet" in html
    assert "Refresh Polymarket" in html and "Refresh Books" in html
    assert "DEMO_MODE" in html
    assert 'id="edges-list"' in html
    assert "NotImplementedError" not in html


def test_edges_respects_min_edge_pref(client: TestClient, db_session: Session):
    seed_slate(db_session)
    prefs_service.update_prefs(db_session, {"min_edge": 0.025})
    html = client.get("/").text
    assert "Yankees" in html and "Chiefs" not in html and "Celtics" not in html
    prefs_service.update_prefs(db_session, {"min_edge": 0.05})
    html = client.get("/").text
    assert "No edges" in html and "5.0%" in html


def test_edges_no_opps_state_when_scan_exists(client: TestClient, db_session: Session):
    scan = Scan(
        started_at=FIXED_NOW, kind="poly", leagues=["nfl"], ok=True, n_markets=4, n_matched=0
    )
    db_session.add(scan)
    db_session.commit()
    html = client.get("/").text
    assert "No edges" in html and "0 of 4 markets matched" in html


def test_scan_bar_shows_quota_and_books_cost(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    seed_slate(db_session)
    monkeypatch.setattr(
        scan_service,
        "quota_status",
        lambda session: {"remaining": 491, "used": 9, "as_of": FIXED_NOW},
    )
    monkeypatch.setattr(scan_service, "estimate_books_cost", lambda prefs: 9)
    html = client.get("/").text
    assert "491 credits" in html
    assert "costs 9 credits, 491 left" in html
    assert "Refresh Books costs 9 Odds API credits (491 left this month)" in html  # hx-confirm


def test_scan_bar_without_key_shows_espn_only(client: TestClient):
    html = client.get("/").text
    assert "ESPN only" in html
    assert "quota unknown" not in html


def test_api_opportunities_json(client: TestClient, db_session: Session):
    slate = seed_slate(db_session)
    data = client.get("/api/opportunities").json()
    assert [row["outcome_name"] for row in data] == ["Yankees", "Celtics", "Chiefs"]
    kc = data[-1]
    assert kc["opportunity_id"] == slate["o_kc"].id
    assert kc["league"] == "nfl"
    assert kc["game_id"] == slate["g_nfl"].id
    assert kc["away"] == "Kansas City Chiefs" and kc["home"] == "Buffalo Bills"
    assert kc["start_time"] == "2026-09-20T20:25:00Z"
    assert kc["market_label"] == "Moneyline"
    assert kc["ask"] == 0.55 and kc["edge"] == 0.0214 and kc["suggested_stake"] == 12.30
    assert kc["polymarket_url"] == "https://polymarket.com/event/nfl-kc-buf-2026-09-20"
    assert kc["books_used"] == ["pinnacle", "betonlineag", "draftkings"]
    assert len(client.get("/api/opportunities?league=nfl").json()) == 1
    assert client.get("/api/opportunities?league=nfl").json()[0]["outcome_name"] == "Chiefs"


def test_post_scan_returns_partial_with_oob_toast(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)
    calls: list[tuple[str, list[str] | None]] = []

    def fake_run(session, kind, leagues=None, now=None):
        calls.append((kind, leagues))
        return ScanResult(
            scan_id=slate["scan"].id,
            kind=kind,
            leagues=list(leagues or []),
            n_markets=6,
            n_matched=5,
            n_opps=3,
            credits_used=None,
            credits_remaining=491,
        )

    monkeypatch.setattr(scan_service, "run_scan_default", fake_run)
    response = client.post("/scan?kind=poly&league=nfl", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert calls == [("poly", ["nfl", "nba", "mlb"])]
    html = response.text
    assert "<html" not in html  # a partial, not a page
    assert html.lstrip().startswith('<div id="edges-list"')
    assert 'id="toast"' in html and 'hx-swap-oob="true"' in html
    assert "Scan done: 3 edges" in html and "5/6 markets matched" in html and "491 left" in html
    assert 'class="toast"' in html  # success, not error
    assert 'id="scan-status"' in html and 'hx-swap-oob="true"' in html
    assert "Chiefs" in html and "Celtics" not in html  # league filter preserved


def test_post_scan_error_returns_error_toast_and_keeps_list(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    seed_slate(db_session)

    def boom(session, kind, leagues=None, now=None):
        raise RuntimeError("gamma timeout")

    monkeypatch.setattr(scan_service, "run_scan_default", boom)
    response = client.post("/scan?kind=books")
    assert response.status_code == 200
    html = response.text
    assert "Scan failed: gamma timeout" in html
    assert 'class="toast error"' in html
    assert 'id="edges-list"' in html and "Chiefs" in html


def test_post_scan_with_result_errors_flags_toast(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)
    result = ScanResult(
        scan_id=slate["scan"].id,
        kind="both",
        leagues=["nfl"],
        n_markets=6,
        n_matched=5,
        n_opps=3,
        credits_used=9,
        credits_remaining=482,
        errors=["espn: 503"],
    )
    monkeypatch.setattr(scan_service, "run_scan_default", lambda *a, **k: result)
    html = client.post("/scan?kind=both").text
    assert 'class="toast error"' in html
    assert "1 error: espn: 503" in html and "9 credits used" in html


def test_post_scan_rejects_unknown_kind(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        scan_service, "run_scan_default", lambda *a, **k: pytest.fail("must not run")
    )
    response = client.post("/scan?kind=magic")
    assert response.status_code == 200
    assert "Unknown scan kind" in response.text and 'class="toast error"' in response.text


# --------------------------------------------------------------------------- game


def test_game_page_renders_markets_books_and_depth(client: TestClient, db_session: Session):
    slate = seed_slate(db_session)
    response = client.get(f"/games/{slate['g_nfl'].id}")
    assert response.status_code == 200
    html = response.text
    assert "Kansas City Chiefs" in html and "Buffalo Bills" in html
    assert "Moneyline" in html and "Spread KC −3.5" in html and "Total 47.5" in html
    assert "Bills +3.5" in html and "Chiefs −3.5" in html
    assert "Over 47.5" in html and "Under 47.5" in html
    # latest Polymarket quotes
    assert "54¢" in html and "55¢" in html and "46¢" in html
    # per-book table: rows = bookmakers, columns = the two outcomes
    assert 'class="book-table"' in html
    assert "pinnacle" in html and "betonlineag" in html and "draftkings" in html
    assert "-150" in html and "+130" in html and "-148" in html and "+135" in html
    assert "-110 (−3.5)" in html and "-110 (+3.5)" in html
    assert "-105 (−3)" in html  # a book at a different line is still shown
    assert "-110 (+47.5)" in html
    # ask depth: top 5 of 6 levels, cumulative dollars
    assert "Ask depth (top 5)" in html
    assert "126" in html and "1234" in html and "65¢" in html and "70¢" not in html
    # dict-shaped depth on another game parses too
    nba = client.get(f"/games/{slate['g_nba'].id}").text
    assert "Ask depth (top 1)" in nba and "66¢" in nba
    # opportunity + limit price + log bet
    assert "edge +2.1%" in html and "58.4%" in html and "56¢" in html
    assert f'hx-get="/bets/new?opportunity_id={slate["o_kc"].id}"' in html
    assert "Limit price —" in html  # outcomes without an edge
    assert 'href="https://polymarket.com/event/nfl-kc-buf-2026-09-20"' in html
    assert "bottom-nav" in html


def test_game_page_404_when_missing(client: TestClient):
    response = client.get("/games/999")
    assert response.status_code == 404
    assert "Not found" in response.text and "Game 999" in response.text
    assert "bottom-nav" in response.text


def test_game_page_without_quotes_or_books(client: TestClient, db_session: Session):
    slate = seed_slate(db_session)
    html = client.get(f"/games/{slate['g_done'].id}").text
    assert "Baltimore Orioles" in html
    assert "closed" in html and "No book quotes stored" in html
    assert "No Polymarket quote stored" in html


# --------------------------------------------------------------------------- bets


def test_bets_page_tiles_and_lists(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)
    summary = {
        "n_open": 1,
        "n_settled": 1,
        "n_won": 0,
        "n_lost": 1,
        "total_staked": 22.30,
        "total_pnl": -10.0,
        "roi": -0.4484,
        "avg_clv": -0.02,
        "n_clv_positive": 0,
        "n_clv_recorded": 1,
    }
    monkeypatch.setattr(bets_service, "ledger_summary", lambda session: summary)
    response = client.get("/bets")
    assert response.status_code == 200
    html = response.text
    assert 'id="ledger-summary"' in html
    assert "-$10.00" in html and "-44.8%" in html and "-2.0%" in html and "0 / 1" in html
    assert "0W · 1L" in html
    # open bet with the current ask from the latest PmQuote and settle buttons
    assert 'id="open-bets"' in html and "Chiefs" in html and "55¢" in html
    assert f'hx-post="/bets/{slate["b_open"].id}/settle"' in html
    assert 'hx-vals=\'{"result": "won"}\'' in html
    assert 'hx-vals=\'{"result": "void"}\'' in html
    assert "hx-confirm=" in html and 'hx-target="#ledger"' in html
    # settled bet with P&L and CLV
    assert 'id="settled-bets"' in html and "Orioles" in html
    assert ">lost<" in html and "-2.0%" in html
    assert 'class="nav-item active" aria-current="page"' in html
    assert "NotImplementedError" not in html


def test_bets_page_survives_unavailable_summary(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    seed_slate(db_session)

    def stub(session):
        raise NotImplementedError

    monkeypatch.setattr(bets_service, "ledger_summary", stub)
    html = client.get("/bets").text
    assert "NotImplementedError" not in html
    assert "Ledger summary unavailable" in html
    assert "Chiefs" in html  # lists still render


def test_bet_form_prefills_from_opportunity(client: TestClient, db_session: Session):
    slate = seed_slate(db_session)
    response = client.get(f"/bets/new?opportunity_id={slate['o_kc'].id}")
    assert response.status_code == 200
    html = response.text
    assert "<html" not in html
    assert 'name="stake_usd"' in html and 'value="12.30"' in html
    assert 'name="price"' in html and 'value="0.55"' in html
    assert 'data-price-maker="0.56"' in html
    assert 'name="mode" value="taker" checked' in html
    assert 'name="mode" value="maker"' in html
    assert "rest a limit at 56¢" in html
    assert 'name="notes"' in html
    assert 'hx-post="/bets"' in html and 'hx-target="#sheet-body"' in html
    assert f'name="opportunity_id" value="{slate["o_kc"].id}"' in html
    assert "bottom-nav" not in html
    assert "NotImplementedError" not in html


def test_bet_form_for_missing_opportunity(client: TestClient):
    response = client.get("/bets/new?opportunity_id=999")
    assert response.status_code == 200
    assert "Opportunity not found" in response.text and "data-close-sheet" in response.text


def test_post_bet_calls_create_bet_and_returns_open_bets(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)
    calls: dict[str, Any] = {}

    def fake_create(session, opportunity_id, stake_usd, price, mode, notes=""):
        calls.update(
            opportunity_id=opportunity_id, stake_usd=stake_usd, price=price, mode=mode, notes=notes
        )
        bet = Bet(
            market_id="500101",
            token=TOK_KC_A,
            outcome_key="KC",
            outcome_name="Chiefs",
            mode=mode,
            price=price,
            shares=round(stake_usd / price, 2),
            stake_usd=stake_usd,
            fee_usd=0.0,
            fair_at_bet=0.5839,
            edge_at_bet=0.0439,
            status="open",
            notes=notes,
        )
        session.add(bet)
        session.commit()
        session.refresh(bet)
        return bet

    monkeypatch.setattr(bets_service, "create_bet", fake_create)
    response = client.post(
        "/bets",
        data={
            "opportunity_id": str(slate["o_kc"].id),
            "stake_usd": "25",
            "price": "0.54",
            "mode": "maker",
            "notes": "test note",
        },
    )
    assert response.status_code == 200
    assert calls == {
        "opportunity_id": slate["o_kc"].id,
        "stake_usd": 25.0,
        "price": 0.54,
        "mode": "maker",
        "notes": "test note",
    }
    html = response.text
    assert 'id="open-bets"' in html and "Open bets" in html
    assert "test note" in html and "$25.00" in html and "54¢" in html
    assert 'id="toast"' in html and "Logged" in html and 'class="toast"' in html
    assert "data-close-sheet" in html and 'href="/bets"' in html
    assert 'hx-post="/bets/' not in html  # compact list: no settle buttons inside the sheet
    assert db_session.query(Bet).filter(Bet.notes == "test note").count() == 1


def test_post_bet_value_error_re_renders_form_with_toast(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)

    def bad(session, opportunity_id, stake_usd, price, mode, notes=""):
        raise ValueError("stake must be at least $1")

    monkeypatch.setattr(bets_service, "create_bet", bad)
    response = client.post(
        "/bets",
        data={
            "opportunity_id": str(slate["o_kc"].id),
            "stake_usd": "0.5",
            "price": "0.55",
            "mode": "taker",
        },
    )
    assert response.status_code == 200
    html = response.text
    assert "stake must be at least $1" in html
    assert 'class="toast error"' in html
    assert 'name="stake_usd"' in html and 'value="0.5"' in html  # edits kept


def test_post_bet_rejects_non_numeric_input_before_service(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)
    monkeypatch.setattr(
        bets_service, "create_bet", lambda *a, **k: pytest.fail("must not be called")
    )
    response = client.post(
        "/bets",
        data={"opportunity_id": str(slate["o_kc"].id), "stake_usd": "lots", "price": "0.55"},
    )
    assert response.status_code == 200
    assert "Stake must be a number" in response.text
    missing = client.post("/bets", data={"opportunity_id": "999", "stake_usd": "5", "price": "0.5"})
    assert missing.status_code == 200 and "Opportunity not found" in missing.text


def test_settle_bet_returns_ledger_and_toast(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)
    seen: list[tuple[int, str]] = []

    def fake_settle(session, bet_id, result):
        seen.append((bet_id, result))
        bet = session.get(Bet, bet_id)
        bet.status = result
        bet.pnl_usd = 9.99 if result == "won" else -bet.stake_usd
        bet.settled_at = datetime.now(UTC)
        session.commit()
        return bet

    monkeypatch.setattr(bets_service, "settle_bet_manual", fake_settle)
    response = client.post(f"/bets/{slate['b_open'].id}/settle", data={"result": "won"})
    assert response.status_code == 200
    assert seen == [(slate["b_open"].id, "won")]
    html = response.text
    assert html.lstrip().startswith('<div id="ledger"')
    assert "settled as won" in html and "P&amp;L +$9.99" in html  # same helper as the tiles
    assert "No open bets" in html and ">won<" in html
    bad = client.post(f"/bets/{slate['b_lost'].id}/settle", data={"result": "maybe"})
    assert bad.status_code == 200
    assert "Result must be won, lost, void or push" in bad.text
    assert 'class="toast error"' in bad.text
    assert seen == [(slate["b_open"].id, "won")]


def test_settle_bet_service_value_error(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    slate = seed_slate(db_session)

    def already(session, bet_id, result):
        raise ValueError("bet is already settled")

    monkeypatch.setattr(bets_service, "settle_bet_manual", already)
    html = client.post(f"/bets/{slate['b_lost'].id}/settle", data={"result": "void"}).text
    assert "bet is already settled" in html and 'id="ledger"' in html


# --------------------------------------------------------------------------- settings


SETTINGS_FORM = {
    "bankroll": "2,500",
    "kelly_fraction": "0.5",
    "max_stake_pct": "5",
    "min_edge": "0.03",
    "taker_fee_rate": "0.04",
    "devig_method": "shin",
    "bookmakers": "Pinnacle, circasports",
    "book_weights": "pinnacle=4\nESPN: 0.25\n\n# comment\n",
    "leagues_enabled": ["mlb", "nfl"],
    "espn_fallback_enabled": "on",
    "match_window_hours": "24",
    "min_liquidity_usd": "0",
    "stale_book_minutes": "60",
}


def test_settings_get_shows_every_pref_and_status(client: TestClient):
    response = client.get("/settings")
    assert response.status_code == 200
    html = response.text
    for name in (
        "bankroll",
        "kelly_fraction",
        "max_stake_pct",
        "min_edge",
        "taker_fee_rate",
        "devig_method",
        "bookmakers",
        "book_weights",
        "leagues_enabled",
        "espn_fallback_enabled",
        "match_window_hours",
        "min_liquidity_usd",
        "stale_book_minutes",
    ):
        assert f'name="{name}"' in html, name
    assert 'name="bankroll" inputmode="decimal" value="1000"' in html
    assert 'value="power" selected' in html
    assert "pinnacle, betonlineag, lowvig, circasports, draftkings, fanduel" in html
    assert "pinnacle=3\ncircasports=2" in html
    assert 'name="leagues_enabled" value="nba" checked' in html
    assert 'name="espn_fallback_enabled" checked' in html
    assert "ODDS_API_KEY" in html and "not set" in html
    assert "DEMO_MODE" in html and ">off<" in html
    assert "NotImplementedError" not in html
    assert "test" not in html.split("ODDS_API_KEY")[1][:200]  # the key value is never shown


def test_settings_post_round_trip_through_update_prefs(client: TestClient, db_session: Session):
    response = client.post("/settings", data=SETTINGS_FORM)
    assert response.status_code == 200
    assert "Saved." in response.text
    db_session.expire_all()
    prefs = prefs_service.get_prefs(db_session)
    assert prefs.bankroll == 2500.0
    assert prefs.kelly_fraction == 0.5
    assert prefs.max_stake_pct == 5.0
    assert prefs.min_edge == 0.03
    assert prefs.taker_fee_rate == 0.04
    assert prefs.devig_method == "shin"
    assert prefs.bookmakers == ["pinnacle", "circasports"]
    assert prefs.book_weights == {"pinnacle": 4.0, "espn": 0.25}
    assert prefs.leagues_enabled == ["nfl", "mlb"]
    assert prefs.espn_fallback_enabled is True
    assert prefs.match_window_hours == 24.0
    assert prefs.min_liquidity_usd == 0.0
    assert prefs.stale_book_minutes == 60
    # the re-rendered form shows the saved values
    assert 'value="2500"' in response.text
    assert 'value="shin" selected' in response.text
    assert "pinnacle=4\nespn=0.25" in response.text
    assert 'name="leagues_enabled" value="nba">' in response.text  # unchecked

    # an unchecked checkbox comes back as False
    without_espn = {k: v for k, v in SETTINGS_FORM.items() if k != "espn_fallback_enabled"}
    client.post("/settings", data=without_espn)
    db_session.expire_all()
    assert prefs_service.get_prefs(db_session).espn_fallback_enabled is False


def test_settings_post_validation_error_keeps_edits(client: TestClient, db_session: Session):
    bad = dict(SETTINGS_FORM, bankroll="-5")
    response = client.post("/settings", data=bad)
    assert response.status_code == 200
    html = response.text
    assert 'class="alert alert-error"' in html and "bankroll" in html
    assert 'value="-5"' in html  # edit preserved for correction
    assert "Saved." not in html
    assert prefs_service.get_prefs(db_session).bankroll == 1000.0

    malformed = dict(SETTINGS_FORM, book_weights="pinnacle")
    html = client.post("/settings", data=malformed).text
    assert "book_weights line 1" in html
    assert prefs_service.get_prefs(db_session).book_weights["pinnacle"] == 3.0


# --------------------------------------------------------------------------- diagnostics


def test_diagnostics_shows_scans_errors_unmatched_and_samples(
    client: TestClient, db_session: Session
):
    seed_slate(db_session)
    failed = Scan(
        started_at=FIXED_NOW - timedelta(minutes=1),
        kind="books",
        leagues=["nfl", "nba"],
        ok=False,
        errors=["oddsapi: 401 Unauthorized"],
        notes={
            "unmatched": [
                {
                    "market_id": "500402",
                    "question": "Spread: Nuggets (-3.5)",
                    "reason": "no book at line",
                }
            ],
            "unparseable": [
                {"market_id": "500999", "question": "Who wins MVP?", "reason": "unknown type"}
            ],
        },
    )
    db_session.add(failed)
    db_session.commit()
    response = client.get("/diagnostics")
    assert response.status_code == 200
    html = response.text
    assert "Last 2 scans" in html
    assert "oddsapi: 401 Unauthorized" in html
    assert "no book at line" in html and "Spread: Nuggets (-3.5)" in html and "500402" in html
    assert "unknown type" in html and "Who wins MVP?" in html
    assert "Unmatched markets (1)" in html and "Unparseable markets (1)" in html
    assert "Errors (1)" in html
    assert ">failed<" in html and ">ok<" in html
    assert "9 / 491" in html  # credits used / remaining
    # raw samples are autoescaped inside <pre>, so quotes arrive as &#34;
    assert "&#34;best_ask&#34;: 0.4" in html
    assert "&#34;price_american&#34;: -110" in html
    assert 'href="/healthz"' in html
    assert "Latest scan #" in html
    assert "NotImplementedError" not in html


def test_diagnostics_empty(client: TestClient):
    html = client.get("/diagnostics").text
    assert "No scans recorded yet" in html
    assert "(none yet)" in html
    assert 'href="/healthz"' in html


# --------------------------------------------------------------------------- every page


def test_every_page_has_nav_sheet_toast_and_no_stub_leak(client: TestClient, db_session: Session):
    slate = seed_slate(db_session)
    paths = (
        "/",
        "/?league=nba",
        f"/games/{slate['g_nfl'].id}",
        "/bets",
        "/settings",
        "/diagnostics",
    )
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.text
        assert "bottom-nav" in html, path
        assert 'id="sheet"' in html and 'id="toast"' in html, path
        assert 'href="/manifest.webmanifest"' in html, path
        assert "NotImplementedError" not in html, path
    partial = client.get(f"/bets/new?opportunity_id={slate['o_kc'].id}").text
    assert "NotImplementedError" not in partial


def test_bottom_nav_marks_active_page(client: TestClient):
    home = client.get("/").text
    assert 'href="/" class="nav-item active" aria-current="page"' in home
    assert 'href="/settings" class="nav-item active"' not in home
    settings = client.get("/settings").text
    assert 'href="/settings" class="nav-item active" aria-current="page"' in settings
    diag = client.get("/diagnostics").text
    assert 'href="/diagnostics" class="nav-item active" aria-current="page"' in diag


# ------------------------------------------------------------------ /games (the list page)
#
# Added because the app had no way to reach a game when there were no edges: the home page
# IS the edge list, and with one low-weight book almost nothing clears the minimum edge, so
# a correctly-working app showed an empty screen and nothing to click.


def _seed_games(db_session) -> tuple[int, int]:
    """Anchored on the real clock, because `/games` splits upcoming from started using
    `get_now()` (the wall clock) and not the tests' FIXED_NOW."""
    from datetime import UTC, datetime, timedelta

    from app.models import Game, Market, Scan

    now = datetime.now(UTC)
    db_session.add(Scan(started_at=now, kind="both", leagues=["mlb"], ok=True))
    upcoming = Game(
        league="mlb",
        home_key="CIN",
        away_key="CHC",
        home_name="Cincinnati Reds",
        away_name="Chicago Cubs",
        start_time=now + timedelta(hours=6),
        book_game_id="espn:1",
    )
    started = Game(
        league="mlb",
        home_key="NYM",
        away_key="PHI",
        home_name="New York Mets",
        away_name="Philadelphia Phillies",
        start_time=now - timedelta(hours=6),
    )
    other_league = Game(
        league="nfl",
        home_key="BUF",
        away_key="KC",
        home_name="Buffalo Bills",
        away_name="Kansas City Chiefs",
        start_time=now + timedelta(days=1),
    )
    db_session.add_all([upcoming, started, other_league])
    db_session.flush()
    db_session.add(Market(id="m1", game_id=upcoming.id, market_type="moneyline", question="q"))
    db_session.commit()
    return upcoming.id, started.id


def test_games_list_shows_upcoming_and_started_separately(client, db_session) -> None:
    upcoming_id, started_id = _seed_games(db_session)
    body = client.get("/games").text

    assert "Chicago Cubs" in body and "Cincinnati Reds" in body
    assert f'href="/games/{upcoming_id}"' in body
    assert f'href="/games/{started_id}"' in body
    assert "Started or finished" in body
    # the one market on the upcoming game is counted
    assert "1 market" in body


def test_games_list_filters_by_league(client, db_session) -> None:
    _seed_games(db_session)
    mlb = client.get("/games?league=mlb").text
    assert "Chicago Cubs" in mlb
    assert "Kansas City Chiefs" not in mlb

    nfl = client.get("/games?league=nfl").text
    assert "Kansas City Chiefs" in nfl
    assert "Chicago Cubs" not in nfl


def test_games_list_flags_a_game_with_no_book_line(client, db_session) -> None:
    _seed_games(db_session)
    body = client.get("/games?league=nfl").text
    assert "no book line" in body


def test_games_list_is_empty_but_helpful_before_any_scan(client) -> None:
    body = client.get("/games").text
    assert "No scans yet" in body


def test_games_list_requires_login(anon_client) -> None:
    response = anon_client.get("/games", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


def test_edges_empty_state_links_to_the_games_list(client, db_session) -> None:
    _seed_games(db_session)
    assert 'href="/games' in client.get("/").text
