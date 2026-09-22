"""End to end in demo mode: the app seeds the fixture slate on startup and every screen,
the scan button, the bet form and manual settlement work against the real services."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import create_session_factory, get_engine
from app.main import create_app
from app.models import Bet, Opportunity, Scan
from app.services.demo import DEMO_NOW, seed_demo
from app.settings import Settings
from tests.conftest import FIXTURES_DIR, TEST_PASSWORD


@pytest.fixture
def demo_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="dev",
        app_password=TEST_PASSWORD,
        secret_key="test",
        demo_mode=True,
        database_url=f"sqlite:///{tmp_path / 'demo.db'}",
        fixtures_dir=FIXTURES_DIR,
    )


@pytest.fixture
def demo(demo_settings: Settings) -> Iterator[TestClient]:
    app = create_app(demo_settings)
    with TestClient(app) as client:
        response = client.post("/login", data={"password": TEST_PASSWORD})
        assert response.status_code == 200
        yield client


def _session():
    return create_session_factory(get_engine())()


def test_startup_seeds_the_fixture_slate_once(demo: TestClient):
    with _session() as session:
        scans = list(session.scalars(select(Scan)))
        assert len(scans) == 1
        scan = scans[0]
        assert scan.kind == "both" and scan.ok and scan.started_at == DEMO_NOW
        assert scan.n_opps == 4 and scan.errors == []
        bets = list(session.scalars(select(Bet).order_by(Bet.id)))
        assert [(b.outcome_name, b.status) for b in bets] == [
            ("Orioles", "lost"),
            ("Chiefs", "open"),
        ]
        orioles, chiefs = bets
        assert orioles.pnl_usd == -10.0 and orioles.settled_at == DEMO_NOW and orioles.clv < 0
        assert chiefs.placed_at < DEMO_NOW and chiefs.stake_usd > 0 and chiefs.fee_usd > 0
        # idempotent: a second seed (as on every restart) changes nothing
        seed_demo(session)
        assert session.scalar(select(func.count()).select_from(Scan)) == 1
        assert session.scalar(select(func.count()).select_from(Bet)) == 2
    assert demo.get("/healthz").json()["demo"] is True


def test_every_screen_renders_real_content(demo: TestClient):
    home = demo.get("/")
    assert home.status_code == 200
    html = home.text
    assert "4 edges" in html
    for text in ("Kansas City Chiefs @ Buffalo Bills", "Chiefs", "Celtics", "Yankees", "Under 7.5"):
        assert text in html, text
    assert "Warriors" not in html and "Eagles" not in html
    assert "497 credits" in html and "demo" in html
    assert 'href="https://polymarket.com/event/nfl-kc-buf-2026-09-20"' in html
    assert "NotImplementedError" not in html

    rows = demo.get("/api/opportunities").json()
    assert [r["outcome_name"] for r in rows] == ["Under", "Celtics", "Yankees", "Chiefs"]
    game_id = rows[-1]["game_id"]
    game = demo.get(f"/games/{game_id}")
    assert game.status_code == 200
    assert "Kansas City Chiefs" in game.text and "Spread KC −3.5" in game.text
    assert "pinnacle" in game.text and "fanduel" in game.text and "-150" in game.text
    assert "Ask depth (top 3)" in game.text and "Log bet" in game.text
    assert "Open on Polymarket" in game.text

    bets = demo.get("/bets")
    assert bets.status_code == 200
    assert "Orioles" in bets.text and ">lost<" in bets.text and "-$10.00" in bets.text
    assert "Chiefs" in bets.text and "Won" in bets.text and "Void" in bets.text
    assert "Ledger summary unavailable" not in bets.text

    settings_page = demo.get("/settings")
    assert settings_page.status_code == 200
    assert "497 left" in settings_page.text and "9 credits" in settings_page.text
    assert 'name="bankroll"' in settings_page.text

    diagnostics = demo.get("/diagnostics")
    assert diagnostics.status_code == 200
    assert "no book at line" in diagnostics.text and "Spread: Nuggets (-3.5)" in diagnostics.text
    assert "unsupported market type" in diagnostics.text
    assert "Errors (0)" in diagnostics.text and "9 / 497" in diagnostics.text
    assert "book_source" in diagnostics.text


def test_scan_buttons_rerun_the_fixture_scan(demo: TestClient):
    response = demo.post("/scan?kind=poly&league=nfl", headers={"HX-Request": "true"})
    assert response.status_code == 200
    html = response.text
    assert html.lstrip().startswith('<div id="edges-list"')
    assert "Chiefs" in html and "Celtics" not in html  # league filter kept
    assert "Scan done: 4 edges" in html and "17/17 markets matched" in html
    assert 'class="toast"' in html and 'class="toast error"' not in html
    with _session() as session:
        assert session.scalar(select(func.count()).select_from(Scan)) == 2
        latest = session.scalars(select(Scan).order_by(Scan.id.desc())).first()
        assert latest.kind == "poly" and latest.credits_used is None
        assert latest.notes["book_source"]["nfl"].startswith("stored:")

    books = demo.post("/scan?kind=books&league=all")
    assert books.status_code == 200
    assert "Scan done: 4 edges" in books.text and "9 credits used" in books.text
    assert "497 left" in books.text


def test_log_a_bet_then_settle_it(demo: TestClient):
    with _session() as session:
        opp = session.scalars(
            select(Opportunity)
            .where(Opportunity.outcome_name == "Yankees")
            .order_by(Opportunity.id)
        ).first()
        opportunity_id, suggested = opp.id, opp.suggested_stake

    form = demo.get(f"/bets/new?opportunity_id={opportunity_id}")
    assert form.status_code == 200
    assert f'value="{suggested:.2f}"' in form.text and 'value="0.40"' in form.text

    logged = demo.post(
        "/bets",
        data={
            "opportunity_id": str(opportunity_id),
            "stake_usd": "12.5",
            "price": "0.40",
            "mode": "taker",
            "notes": "e2e",
        },
    )
    assert logged.status_code == 200
    assert "Logged" in logged.text and 'class="toast"' in logged.text
    assert "Yankees" in logged.text and "$12.50" in logged.text and "e2e" in logged.text

    with _session() as session:
        bet = session.scalars(select(Bet).where(Bet.notes == "e2e")).one()
        assert bet.status == "open" and bet.mode == "taker" and bet.price == 0.40
        assert bet.fee_usd > 0 and bet.shares == pytest.approx(12.5 / (0.40 + 0.05 * 0.40 * 0.60))
        bet_id = bet.id

    ledger = demo.get("/bets").text
    assert 'Open bets <span class="muted">(2)</span>' in ledger and "e2e" in ledger

    settled = demo.post(f"/bets/{bet_id}/settle", data={"result": "won"})
    assert settled.status_code == 200
    assert f"Bet #{bet_id} settled as won" in settled.text
    assert 'Open bets <span class="muted">(1)</span>' in settled.text
    assert ">won<" in settled.text
    with _session() as session:
        bet = session.get(Bet, bet_id)
        assert bet.status == "won" and bet.pnl_usd == pytest.approx(round(bet.shares - 12.5, 2))
        expected_pnl = bet.pnl_usd
    assert f"P&amp;L +${expected_pnl:,.2f}" in settled.text  # toast uses the ledger's $ format
    # summary tiles reflect it: 1 won, 1 lost
    assert "1W · 1L" in settled.text

    again = demo.post(f"/bets/{bet_id}/settle", data={"result": "void"})
    assert "already settled" in again.text and 'class="toast error"' in again.text
