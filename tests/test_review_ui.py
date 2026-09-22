"""UI review fixes: the toast live region, tap targets and tokens in the stylesheet, the
topbar refresh, diagnostics conventions, exit-fee-aware marks, number formatting, and the
schema column backfill."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.types import ScanResult
from app.db import create_db_engine, init_db
from app.models import Bet, Market, Opportunity, PmQuote, Scan
from app.routes.bets import BetView, exit_fee_rate, fmt_price, price_decimals
from app.routes.edges import get_now
from app.services import scan as scan_service
from app.services.scan import HOME_AWAY_CONVENTION
from app.templating import ago, usd_signed
from tests.conftest import FIXED_NOW
from tests.test_routes_ui import TOK_KC_A, seed_slate

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.css"
JS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"
BASE_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"
SCAN_STUB_TIMEOUT_S = 5.0  # upper bound on the stubbed "slow scan"; never reached when green


# --------------------------------------------------------------------------- toast


def test_toasts_swap_into_the_persistent_live_region(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    slate = seed_slate(db_session)
    home = client.get("/").text
    assert '<div id="toast" class="toast" role="status" aria-live="polite" hidden></div>' in home

    ok = ScanResult(slate["scan"].id, "poly", ["nfl"], 6, 5, 3, None, 491)
    monkeypatch.setattr(scan_service, "run_scan_default", lambda *a, **k: ok)
    html = client.post("/scan?kind=poly", headers={"HX-Request": "true"}).text
    assert (
        'id="toast" class="toast" role="status" aria-live="polite" hx-swap-oob="innerHTML"' in html
    )
    assert '<span class="toast-msg" data-error="0">Scan done: 3 edges' in html
    assert 'id="toast"' in html and 'hx-swap-oob="true"' not in html.split('id="toast"')[1][:80]

    def boom(*a, **k):
        raise RuntimeError("gamma timeout")

    monkeypatch.setattr(scan_service, "run_scan_default", boom)
    html = client.post("/scan?kind=poly", headers={"HX-Request": "true"}).text
    assert 'class="toast error"' in html and '<span class="toast-msg" data-error="1">' in html

    js = JS.read_text(encoding="utf-8")
    assert "htmx:oobBeforeSwap" in js and 'target.id === "toast"' in js
    assert 'getAttribute("data-error") === "1"' in js


# --------------------------------------------------------------------------- topbar


def test_scan_result_refreshes_the_topbar_out_of_band(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    slate = seed_slate(db_session)
    home = client.get("/").text
    assert '<div id="topbar-right" class="topbar-right">' in home
    assert home.count('id="topbar-right"') == 1
    result = ScanResult(slate["scan"].id, "books", ["nfl"], 6, 5, 3, 9, 482)
    monkeypatch.setattr(scan_service, "run_scan_default", lambda *a, **k: result)
    html = client.post("/scan?kind=books", headers={"HX-Request": "true"}).text
    assert '<div id="topbar-right" class="topbar-right" hx-swap-oob="true">' in html
    assert html.count('id="topbar-right"') == 1
    assert "491 credits" in html.split('id="topbar-right"')[1]  # the badge, refreshed


# --------------------------------------------------------------------------- diagnostics


def test_diagnostics_shows_conventions_and_unresolved_book_teams(
    client: TestClient, db_session: Session
) -> None:
    db_session.add(
        Scan(
            started_at=FIXED_NOW,
            kind="both",
            leagues=["nfl"],
            ok=True,
            errors=[],
            notes={
                "unmatched": [],
                "unparseable": [],
                "book_source": {"nfl": "oddsapi"},
                "conventions": {
                    "home_away": HOME_AWAY_CONVENTION,
                    "match_window_hours": 36.0,
                    "stale_book_minutes": 720,
                },
                "unresolved_book_teams": [
                    {"league": "nfl", "name": "Buffalo Bisons", "source": "oddsapi"}
                ],
                "duplicates_dropped": ["nfl:event:10001"],
            },
        )
    )
    db_session.commit()
    html = client.get("/diagnostics").text
    assert "Conventions in force" in html and HOME_AWAY_CONVENTION in html
    assert "match_window_hours" in html and ">36.0<" in html and ">720<" in html
    assert "Unresolved book teams (1)" in html and "Buffalo Bisons" in html
    assert "duplicates_dropped" in html  # still visible under other notes
    empty = client.get("/diagnostics").text
    assert "Conventions in force" in empty


def test_game_page_marks_the_home_away_guess_and_prints_the_tick_in_cents(
    client: TestClient, db_session: Session
) -> None:
    slate = seed_slate(db_session)
    html = client.get(f"/games/{slate['g_nfl'].id}").text
    assert "home/away assumed from Polymarket's outcome order" in html
    assert "tick 1¢" in html and "tick 0.01" not in html


# --------------------------------------------------------------------------- stylesheet


def test_stylesheet_tap_targets_tokens_and_layout_rules() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert (
        ".opp-title, .bet-title { display: flex; align-items: center; min-height: var(--tap)" in css
    )
    assert ".back-link { display: inline-flex; align-items: center; min-height: var(--tap)" in css
    assert "details.raw-sample summary { cursor: pointer; min-height: var(--tap)" in css
    assert css.count("--nba:") == 2 and "--nba: #b45309" in css  # dark and light values
    assert ".league-nba { background: var(--nba-tint); color: var(--nba); }" in css
    assert "#ff9a57;" not in css.split(".league-nba")[1][:80]
    assert (
        "@media (max-width: 419px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }"
        in css
    )
    assert "white-space: nowrap" in css.split(".tile-value")[1][:200]
    assert "body.sheet-open { overflow: hidden; }" in css
    assert "overscroll-behavior: contain; touch-action: pan-y;" in css
    assert "touch-action: none" in css.split(".sheet-backdrop")[1][:120]
    js = JS.read_text(encoding="utf-8")
    assert 'classList.toggle("sheet-open"' in js


# --------------------------------------------------------------------------- marks and money


def test_mark_pnl_is_net_of_the_exit_taker_fee(client: TestClient, db_session: Session) -> None:
    slate = seed_slate(db_session)
    bet = slate["b_open"]  # 22.36 shares @ 0.55, fee 0.28, current bid 0.54
    quote = db_session.scalars(
        select(PmQuote).where(PmQuote.token == TOK_KC_A).order_by(PmQuote.id.desc())
    ).first()
    assert quote.best_bid == 0.54
    view = BetView(bet=bet, market=None, game=None, quote=quote, exit_fee_rate=0.05)
    gross = (0.54 - 0.55) * 22.36 - 0.28
    exit_fee = 22.36 * 0.05 * 0.54 * 0.46
    assert view.mark_pnl == pytest.approx(gross - exit_fee, abs=1e-9)
    assert view.mark_pnl < gross
    # the rate comes from the bet's own fee when it has one, else the preference
    assert exit_fee_rate(bet, 0.05) == pytest.approx(0.28 / (22.36 * 0.55 * 0.45))
    maker = Bet(
        market_id=bet.market_id,
        token=TOK_KC_A,
        outcome_name="Chiefs",
        mode="maker",
        price=0.5,
        shares=10.0,
        stake_usd=5.0,
        fee_usd=0.0,
        status="open",
    )
    assert exit_fee_rate(maker, 0.03) == 0.03
    html = client.get("/bets").text
    assert "Mark net" in html and "net of the exit taker fee" in html
    rate = exit_fee_rate(bet, 0.05)
    rendered = usd_signed(gross - 22.36 * rate * 0.54 * 0.46)
    assert rendered in html


def test_money_and_shares_use_one_format_everywhere(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import bets as bets_service

    slate = seed_slate(db_session)
    assert usd_signed(4.5) == "+$4.50" and usd_signed(-4.5) == "-$4.50" and usd_signed(0) == "$0.00"
    assert usd_signed(None) == "—"

    def fake_create(session, opportunity_id, stake_usd, price, mode, notes=""):
        bet = Bet(
            market_id="500101",
            token=TOK_KC_A,
            outcome_key="KC",
            outcome_name="Chiefs",
            mode=mode,
            price=price,
            shares=round(stake_usd / price, 6),
            stake_usd=stake_usd,
            fee_usd=0.0,
            status="open",
            notes=notes,
        )
        session.add(bet)
        session.commit()
        session.refresh(bet)
        return bet

    monkeypatch.setattr(bets_service, "create_bet", fake_create)
    html = client.post(
        "/bets",
        data={
            "opportunity_id": str(slate["o_kc"].id),
            "stake_usd": "19.23",
            "price": "0.54",
            "mode": "maker",
        },
    ).text
    assert "Logged $19.23 on Chiefs @ 54¢ (maker)" in html  # toast: same helpers as the alert
    assert "USD" not in html
    assert "35.6 shares" in html  # one decimal, as on the open-bets card

    def fake_settle(session, bet_id, result):
        bet = session.get(Bet, bet_id)
        bet.status = result
        bet.pnl_usd = -bet.stake_usd
        bet.settled_at = FIXED_NOW + timedelta(days=1)
        session.commit()
        return bet

    monkeypatch.setattr(bets_service, "settle_bet_manual", fake_settle)
    settled = client.post(f"/bets/{slate['b_open'].id}/settle", data={"result": "lost"}).text
    assert "P&amp;L -$12.30" in settled and "USD" not in settled


def test_bet_form_prefills_only_the_fillable_stake_for_a_thin_ladder(
    client: TestClient, db_session: Session
) -> None:
    slate = seed_slate(db_session)
    opp = db_session.get(Opportunity, slate["o_kc"].id)
    assert opp.fill_complete is True  # the column default
    opp.fill_complete, opp.fill_usd = False, 3.26
    db_session.commit()
    form = client.get(f"/bets/new?opportunity_id={opp.id}").text
    assert 'name="stake_usd" inputmode="decimal" value="3.26"' in form
    assert "partial fill" in form and "only $3.26 fillable" in form
    home = client.get("/").text
    assert "partial fill" in home and "absorbs only $3.26" in home
    game = client.get(f"/games/{slate['g_nfl'].id}").text
    assert "partial fill" in game


# --------------------------------------------------------------------------- schema backfill


def test_init_db_adds_columns_missing_from_an_older_database(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'old.db'}")
    init_db(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE opportunities DROP COLUMN fill_complete")
        connection.exec_driver_sql("ALTER TABLE opportunities DROP COLUMN fill_usd")
    columns = {c["name"] for c in inspect(engine).get_columns("opportunities")}
    assert "fill_complete" not in columns
    init_db(engine)  # a restart on the older file
    columns = {c["name"]: c for c in inspect(engine).get_columns("opportunities")}
    assert "fill_complete" in columns and "fill_usd" in columns
    assert columns["fill_complete"]["nullable"] is False
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO opportunities (scan_id, market_id, token, outcome_name, ask, "
            "effective_price, fair_prob, fair_method, n_books, books_used, edge, ev_per_dollar, "
            "kelly, suggested_stake, computed_at) VALUES (1, 'm', 't', 'x', 0.5, 0.51, 0.55, "
            "'power', 1, '[]', 0.04, 0.07, 0.05, 10.0, '2026-09-19 15:00:00')"
        )
        row = connection.exec_driver_sql("SELECT fill_complete, fill_usd FROM opportunities").one()
    assert (bool(row[0]), row[1]) == (True, None)
    engine.dispose()


# =========================================================================== review round 2
#
# Confirmed findings from the second UI review: the scan blocked the event loop, the bet
# form rounded prices to whole cents, metric values broke mid-number on a phone, the iOS
# status bar was unreadable in light mode, the toast swallowed taps over the sheet, a
# vanished opportunity nested a second dialog, P&L was unsigned in two places, diagnostics
# text fell below the 15px floor, the layout ignored the landscape safe-area insets, one
# refresh button stayed live while the other scanned, and nothing time-derived was asserted.


# --------------------------------------------------------------------------- 1. the scan


def test_scan_does_not_block_the_rest_of_the_app(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /scan runs the blocking scan in the threadpool, not on the event loop.

    `run_scan_default` is synchronous (sync httpx) and takes seconds; as an `async def`
    handler it froze every other request for the whole scan, so the phone looked hung.
    """
    seed_slate(db_session)
    started, release = threading.Event(), threading.Event()

    def slow_scan(session, kind, leagues=None, now=None):  # noqa: ANN001, ANN202
        started.set()
        release.wait(SCAN_STUB_TIMEOUT_S)  # "in flight" until the other request has answered
        return ScanResult(1, kind, list(leagues or []), 6, 5, 3, None, 491)

    monkeypatch.setattr(scan_service, "run_scan_default", slow_scan)
    posted: dict[str, object] = {}

    def post_scan() -> None:
        posted["response"] = client.post("/scan?kind=poly", headers={"HX-Request": "true"})

    worker = threading.Thread(target=post_scan, name="scan-post")
    worker.start()
    try:
        assert started.wait(SCAN_STUB_TIMEOUT_S), "the scan handler never ran"
        begin = time.perf_counter()
        health = client.get("/healthz")
        elapsed = time.perf_counter() - begin
    finally:
        release.set()
        worker.join(SCAN_STUB_TIMEOUT_S + 5.0)

    assert health.status_code == 200
    assert elapsed < 1.0, f"a concurrent request waited {elapsed:.2f}s for the scan"
    response = posted["response"]
    assert response.status_code == 200  # type: ignore[union-attr]
    assert "Scan done: 3 edges" in response.text  # type: ignore[union-attr]


def test_scan_already_running_is_a_plain_toast_not_a_failure(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scan.ScanBusy` (the services contract) renders as a normal toast, never a 500."""
    seed_slate(db_session)
    busy_cls = getattr(scan_service, "ScanBusy", None)
    if busy_cls is None:  # the symbol may land after this file is written

        class ScanBusy(RuntimeError):
            pass

        busy_cls = ScanBusy
        monkeypatch.setattr(scan_service, "ScanBusy", busy_cls, raising=False)

    def busy(session, kind, leagues=None, now=None):  # noqa: ANN001, ANN202
        raise busy_cls("a scan is already running")

    monkeypatch.setattr(scan_service, "run_scan_default", busy)
    response = client.post("/scan?kind=books", headers={"HX-Request": "true"})
    assert response.status_code == 200
    html = response.text
    assert "a scan is already running" in html
    assert 'class="toast error"' not in html  # a normal toast, not the red one
    assert '<span class="toast-msg" data-error="0">' in html
    assert "Scan failed" not in html
    assert 'id="edges-list"' in html and "Chiefs" in html  # the list survives


def test_either_refresh_button_disables_the_pair(client: TestClient, db_session: Session) -> None:
    """One tap disables both buttons: a queued second scan spends Odds API credits twice."""
    seed_slate(db_session)
    html = client.get("/").text
    assert html.count('hx-disabled-elt="#scan-status button"') == 2
    assert 'hx-disabled-elt="this"' not in html


# --------------------------------------------------------------------------- 2. tick precision


def test_price_prefill_helpers_follow_the_market_tick() -> None:
    assert price_decimals(0.01) == 2 and price_decimals(0.001) == 3
    assert price_decimals(None) == 2 and price_decimals("bogus") == 2
    assert fmt_price(0.55, 0.01) == "0.55"
    assert fmt_price(0.035, 0.001) == "0.035"
    assert fmt_price(0.5, 0.001) == "0.500"
    assert fmt_price(0.499, 0.001) == "0.499"
    assert fmt_price(None, 0.01) == ""
    # an unknown or too-coarse tick must never round the stored price away
    assert fmt_price(0.035, None) == "0.035"
    assert fmt_price(0.035, 0.01) == "0.035"


def test_bet_form_prefills_a_thousandth_tick_market_exactly(
    client: TestClient, db_session: Session
) -> None:
    slate = seed_slate(db_session)
    opp = db_session.get(Opportunity, slate["o_kc"].id)
    market = db_session.get(Market, opp.market_id)
    market.tick_size = 0.001
    opp.ask, opp.limit_price = 0.035, 0.034
    db_session.commit()
    html = client.get(f"/bets/new?opportunity_id={opp.id}").text
    assert 'name="price" inputmode="decimal" value="0.035"' in html  # not the old "0.04"
    assert 'data-price-taker="0.035"' in html
    assert 'data-price-maker="0.034"' in html

    # the maker toggle must not round a resting 0.499 up to the 0.50 ask (create_bet
    # refuses a maker order at or above the ask, which made the toggle unusable)
    opp.ask, opp.limit_price = 0.500, 0.499
    db_session.commit()
    html = client.get(f"/bets/new?opportunity_id={opp.id}").text
    assert 'data-price-maker="0.499"' in html
    assert 'data-price-maker="0.50"' not in html
    assert 'value="0.500"' in html


def test_bet_form_error_keeps_the_tick_precise_price_attributes(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import bets as bets_service

    slate = seed_slate(db_session)
    opp = db_session.get(Opportunity, slate["o_kc"].id)
    market = db_session.get(Market, opp.market_id)
    market.tick_size = 0.001
    opp.ask, opp.limit_price = 0.035, 0.034
    db_session.commit()

    def bad(session, opportunity_id, stake_usd, price, mode, notes=""):  # noqa: ANN001, ANN202
        raise ValueError("stake must be at least $1")

    monkeypatch.setattr(bets_service, "create_bet", bad)
    html = client.post(
        "/bets",
        data={"opportunity_id": str(opp.id), "stake_usd": "0.5", "price": "0.035"},
    ).text
    assert 'data-price-taker="0.035"' in html and 'data-price-maker="0.034"' in html


# --------------------------------------------------------------------------- 13. cents typing


def test_a_price_typed_in_cents_is_converted_before_the_service(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import bets as bets_service

    slate = seed_slate(db_session)
    seen: dict[str, float] = {}

    def fake_create(session, opportunity_id, stake_usd, price, mode, notes=""):  # noqa: ANN001
        seen["price"] = price
        bet = Bet(
            market_id="500101",
            token=TOK_KC_A,
            outcome_name="Chiefs",
            mode=mode,
            price=price,
            shares=round(stake_usd / price, 6),
            stake_usd=stake_usd,
            fee_usd=0.0,
            status="open",
        )
        session.add(bet)
        session.commit()
        session.refresh(bet)
        return bet

    monkeypatch.setattr(bets_service, "create_bet", fake_create)
    response = client.post(
        "/bets", data={"opportunity_id": str(slate["o_kc"].id), "stake_usd": "10", "price": "55"}
    )
    assert response.status_code == 200
    assert seen["price"] == pytest.approx(0.55)  # "55" on the phone keypad means 55¢
    assert "Logged $10.00 on Chiefs @ 55¢" in response.text


def test_a_price_above_the_cents_range_reaches_the_service_and_re_renders(
    client: TestClient, db_session: Session
) -> None:
    """101 and up is neither a probability nor cents: the service's range error is shown."""
    slate = seed_slate(db_session)
    response = client.post(
        "/bets",
        data={"opportunity_id": str(slate["o_kc"].id), "stake_usd": "5", "price": "100.5"},
    )
    assert response.status_code == 200
    html = response.text
    assert 'class="alert alert-error"' in html
    assert "price" in html.lower() and "between 0 and 1" in html
    assert 'value="100.5"' in html  # the edit is kept
    assert db_session.query(Bet).filter(Bet.status == "open").count() == 1  # only the seeded one


# --------------------------------------------------------------------------- 6. sheet nesting


def test_vanished_opportunity_replaces_the_sheet_instead_of_nesting_a_dialog(
    client: TestClient, db_session: Session
) -> None:
    """POST /bets renders a whole sheet, so it must retarget #sheet (the form posts into
    #sheet-body, which would leave two role=dialog elements and a duplicate #sheet-title)."""
    seed_slate(db_session)
    response = client.post(
        "/bets", data={"opportunity_id": "999999", "stake_usd": "5", "price": "0.5"}
    )
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#sheet"
    assert response.headers["HX-Reswap"] == "innerHTML"
    html = response.text
    assert "Opportunity not found" in html
    assert html.count('role="dialog"') == 1
    assert html.count('id="sheet-title"') == 1
    assert html.count("sheet-backdrop") == 1
    # the GET form already targets #sheet, so it must NOT be retargeted
    get_response = client.get("/bets/new?opportunity_id=999999")
    assert "HX-Retarget" not in get_response.headers


# --------------------------------------------------------------------------- 7. signed P&L


def test_pnl_is_signed_on_the_settled_card_and_the_summary_tile(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import bets as bets_service

    slate = seed_slate(db_session)
    won = slate["b_lost"]
    won.status, won.pnl_usd = "won", 4.5
    db_session.commit()
    summary = {
        "n_open": 1,
        "n_settled": 1,
        "n_won": 1,
        "n_lost": 0,
        "total_staked": 10.0,
        "total_pnl": 4.5,
        "roi": 0.45,
        "avg_clv": -0.02,
        "n_clv_positive": 0,
        "n_clv_recorded": 1,
    }
    monkeypatch.setattr(bets_service, "ledger_summary", lambda session: summary)
    html = client.get("/bets").text
    settled = html.split('id="settled-bets"')[1]
    assert "+$4.50" in settled, "a $4.50 profit must not read like a $4.50 stake"
    assert usd_signed(4.5) == "+$4.50"
    tiles = html.split('id="ledger-summary"')[1].split("</section>")[0]
    assert "+$4.50" in tiles
    assert ">$4.50<" not in tiles  # never the unsigned form


# --------------------------------------------------------------------------- 12. the clock


def test_last_scan_age_and_stale_badge_follow_the_injected_clock(
    client: TestClient, app: FastAPI, db_session: Session
) -> None:
    """Both sides of the 720-minute staleness boundary, with the clock frozen per request."""
    slate = seed_slate(db_session)  # scan 5m before FIXED_NOW, book last_update 8m before

    def freeze(moment: datetime) -> None:
        app.dependency_overrides[get_now] = lambda: moment

    try:
        freeze(FIXED_NOW)
        html = client.get("/").text
        assert "Last scan" in html and "5m ago" in html
        assert "in 1d" in client.get(f"/games/{slate['g_nfl'].id}").text  # kickoff is ahead

        freeze(FIXED_NOW + timedelta(hours=2))
        assert "2h ago" in client.get("/").text

        freeze(FIXED_NOW + timedelta(days=3))
        assert "3d ago" in client.get("/").text

        # stale badge: elapsed since last_update is 719 minutes, one short of the limit
        freeze(FIXED_NOW + timedelta(minutes=711))
        fresh = client.get(f"/games/{slate['g_nfl'].id}").text
        assert "pinnacle" in fresh and 'class="badge badge-warn">stale<' not in fresh

        # ... and 721 minutes, one past it
        freeze(FIXED_NOW + timedelta(minutes=713))
        stale = client.get(f"/games/{slate['g_nfl'].id}").text
        assert 'class="badge badge-warn">stale<' in stale
    finally:
        app.dependency_overrides.pop(get_now, None)

    assert isinstance(get_now(), datetime) and get_now().tzinfo is UTC  # the real default


def test_ago_renders_every_unit_against_an_explicit_clock() -> None:
    """`ago(value, now=...)` is what the pages call with the injected clock."""
    now = FIXED_NOW
    assert ago(now, now=now) == "just now"
    assert ago(now - timedelta(seconds=44), now=now) == "just now"
    assert ago(now - timedelta(minutes=1), now=now) == "1m ago"
    assert ago(now - timedelta(minutes=59), now=now) == "59m ago"
    assert ago(now - timedelta(minutes=60), now=now) == "1h ago"
    assert ago(now - timedelta(hours=23), now=now) == "23h ago"
    assert ago(now - timedelta(days=1), now=now) == "1d ago"
    assert ago(now - timedelta(days=9, hours=6), now=now) == "9d ago"
    # future values (the demo clock runs ahead of the wall clock)
    assert ago(now + timedelta(seconds=10), now=now) == "in a moment"
    assert ago(now + timedelta(minutes=5), now=now) == "in 5m"
    assert ago(now + timedelta(hours=5), now=now) == "in 5h"
    assert ago(now + timedelta(days=2), now=now) == "in 2d"
    # naive datetimes on either side are read as UTC, never crash
    assert ago(now.replace(tzinfo=None) - timedelta(minutes=3), now=now) == "3m ago"
    assert ago(now - timedelta(minutes=3), now=now.replace(tzinfo=None)) == "3m ago"
    # non-datetimes render the dash, never an exception
    assert ago(None, now=now) == "—" and ago("2026-09-19", now=now) == "—"


# --------------------------------------------------------------------------- 3,4,5,8,9,10


def test_metric_values_never_break_inside_a_number_on_a_phone() -> None:
    css = CSS.read_text(encoding="utf-8")
    # the 4-column grid drops to 2 on every phone, not only below 360px
    metrics_media = css.split(".metrics, .metrics-4 {")[1].split("\n}")[0]
    assert css.count("@media (max-width: 419px) {") == 2  # tiles (existing) and metrics
    assert ".metrics, .metrics-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in css
    assert "@media (max-width: 419px) {" in css.split(".metrics, .metrics-4 {")[0][-200:]
    assert ".metrics-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }" in metrics_media
    assert "@media (max-width: 359px)" not in css  # the old, too-narrow breakpoint
    dd = css.split(".metrics dd {")[1].split("}")[0]
    assert "white-space: nowrap" in dd and "overflow-wrap: normal" in dd
    assert "overflow-wrap: anywhere" not in dd


def test_ios_status_bar_is_readable_in_light_mode() -> None:
    """black-translucent painted white status-bar text over the near-white light topbar."""
    base = BASE_HTML.read_text(encoding="utf-8")
    assert '<meta name="apple-mobile-web-app-status-bar-style" content="default">' in base
    assert 'content="black-translucent"' not in base
    # "default" delegates the color to these, which already have both themes
    assert 'name="theme-color" media="(prefers-color-scheme: light)"' in base
    assert 'name="theme-color" media="(prefers-color-scheme: dark)"' in base


def test_toast_never_swallows_a_tap_and_clears_an_open_sheet() -> None:
    css = CSS.read_text(encoding="utf-8")
    toast = css.split(".toast {")[1].split("}")[0]
    assert "pointer-events: none" in toast
    assert (
        "body.sheet-open .toast { bottom: auto; top: calc(env(safe-area-inset-top) + 12px); }"
        in css
    )


def test_diagnostics_text_meets_the_body_text_floor() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ".depth-table { font-size: 15px; }" in css
    assert ".diag-list.errors li { color: var(--danger); font-size: 15px; }" in css
    assert "font-size: 15px" in css.split(".kv {")[1].split("}")[0]
    assert "font-size: 14px" in css.split("pre.raw {")[1].split("}")[0]
    # 13px survives only on table headers (short uppercase labels), never on content
    assert css.count("font-size: 13px") == 1
    assert "font-size: 13px" in css.split("\nth {")[1].split("}")[0]


def test_healthz_link_is_a_tap_target(client: TestClient) -> None:
    html = client.get("/diagnostics").text
    assert '<a class="btn btn-sm" href="/healthz">/healthz</a>' in html


def test_layout_clears_the_landscape_safe_area_insets() -> None:
    """viewport-fit=cover puts the notch over the page in landscape unless we pad for it."""
    css = CSS.read_text(encoding="utf-8")
    for selector in (".page {", ".topbar {", ".sheet-panel {", ".login-body {"):
        block = css.split(selector)[1].split("}")[0]
        assert "env(safe-area-inset-left)" in block, selector
        assert "env(safe-area-inset-right)" in block, selector
    nav = css.split(".bottom-nav {")[1].split("}")[0]
    assert "env(safe-area-inset-left)" in nav and "env(safe-area-inset-right)" in nav
    assert "env(safe-area-inset-bottom)" in nav  # the existing inset is still there
