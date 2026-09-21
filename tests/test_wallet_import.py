"""Wallet import: the owner's Polymarket fills become ledger rows, read-only.

Fixture `fixtures/data_trades_wallet.json` (newest first, like the live feed):
Cowboys SELL; Chiefs BUY as two fills in one transaction; Orioles BUY after kickoff;
Orioles BUY pre-game on a market that has since resolved; a market the app never saw;
an asset that is not one of the market's tokens; a row with no price.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import cli
from app.clients.espn import EspnClient
from app.clients.oddsapi import OddsApiClient
from app.clients.polymarket import DATA_API, WALLET_TRADES_PAGE, PolymarketClient, PolymarketError
from app.clients.transport import FixtureTransport, load_fixture
from app.core.edge import taker_fee_per_share
from app.models import Bet, ForwardSample, Scan
from app.services import wallet_import as wi
from app.services.demo import ORIOLES_MARKET_ID, build_demo_transport
from app.services.prefs import get_prefs, update_prefs, validate_prefs
from app.services.scan import run_scan, upsert_game, upsert_market
from app.settings import Settings, set_settings
from tests.conftest import FIXED_NOW
from tests.test_routes_ui import SETTINGS_FORM

WALLET = "0x" + "ab" * 20
KC_TOKEN = "101666138886874663788676141866982203464022852282780921572169343696303342181974"
BAL_TOKEN = "54277772809694200783696962900215684549355838417132312744347879690503482462638"
TX_CHIEFS = "0x" + "a1" * 32
TX_ORIOLES = "0x" + "b1" * 32


def _scan(session: Session, transport, *, kind: str = "both"):
    return run_scan(
        session,
        polymarket=PolymarketClient(transport),
        oddsapi=OddsApiClient(transport, "fixture-key"),
        espn=EspnClient(transport),
        prefs=get_prefs(session),
        kind=kind,
        leagues=["nfl", "nba", "mlb"],
        now=FIXED_NOW,
    )


def _seed_orioles(session: Session, transport) -> None:
    """The resolved BAL@TOR market is not in the active slate; give it a row the way the
    demo seed does, so a fill on it can be matched by conditionId."""
    market = PolymarketClient(transport).market(ORIOLES_MARKET_ID)
    assert market is not None
    game = upsert_game(session, market, FIXED_NOW)
    upsert_market(session, market, game, FIXED_NOW)
    session.commit()


@pytest.fixture
def slate(db_session: Session, settings: Settings):
    transport = build_demo_transport(settings)
    _scan(db_session, transport)
    _seed_orioles(db_session, transport)
    return transport


def _rows(settings: Settings) -> list[dict]:
    return load_fixture("data_trades_wallet.json", settings.fixtures_dir)


# --------------------------------------------------------------------------- parsing


def test_parse_fill_requires_every_field_and_sane_values():
    good = {
        "transactionHash": "0xABC",
        "asset": "1",
        "conditionId": "0xDEF",
        "side": "buy",
        "price": "0.55",
        "size": 4,
        "timestamp": 1789826400,
        "title": "Chiefs vs. Bills",
    }
    fill = wi.parse_fill(good)
    assert fill is not None
    assert (fill.tx, fill.condition_id, fill.side) == ("0xabc", "0xdef", "BUY")
    assert fill.when == datetime(2026, 9, 19, 14, 0, tzinfo=UTC)
    assert fill.when.tzinfo is not None

    for bad in (
        {**good, "price": None},
        {**good, "price": 1.0},
        {**good, "price": "nan"},
        {**good, "size": 0},
        {**good, "side": "HOLD"},
        {**good, "timestamp": "soon"},
        {**good, "transactionHash": ""},
        {k: v for k, v in good.items() if k != "conditionId"},
    ):
        assert wi.parse_fill(bad) is None, bad


def test_group_fills_by_transaction_asset_and_side_oldest_first():
    t0 = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)
    t1 = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
    a = wi.Fill("0x1", "tok", "0xc", "BUY", 0.5, 1, t1, "")
    b = wi.Fill("0x1", "tok", "0xc", "BUY", 0.51, 2, t1, "")
    c = wi.Fill("0x1", "tok", "0xc", "SELL", 0.6, 1, t1, "")
    d = wi.Fill("0x0", "tok", "0xc", "BUY", 0.4, 1, t0, "")
    groups = wi.group_fills([a, b, c, d])
    assert groups == [[d], [a, b], [c]]
    assert wi.import_key(a) == "0x1:tok"


# --------------------------------------------------------------------------- import


def test_import_fills_from_the_fixture(db_session: Session, settings: Settings, slate):
    result = wi.import_fills(
        db_session, _rows(settings), wallet=WALLET, prefs=get_prefs(db_session)
    )

    assert (result.fetched, result.imported, result.already) == (8, 2, 0)
    assert dict(result.skipped_counts) == {
        wi.SKIP_SELL: 1,
        wi.SKIP_AFTER_KICKOFF: 1,
        wi.SKIP_UNKNOWN_MARKET: 1,
        wi.SKIP_NOT_AN_OUTCOME: 1,
        wi.SKIP_UNPARSEABLE: 1,
    }
    reasons = {e["reason"] for e in result.skipped}
    assert reasons == set(result.skipped_counts)
    sell = next(e for e in result.skipped if e["reason"] == wi.SKIP_SELL)
    assert sell["title"] == "Cowboys vs. Eagles" and sell["side"] == "SELL"

    bets = list(db_session.scalars(select(Bet).order_by(Bet.id)))
    assert [b.id for b in bets] == result.bet_ids
    orioles, chiefs = bets  # oldest fill first

    # Two fills in one transaction are one bet at the volume-weighted price.
    price = (0.56 * 2 + 0.55 * 4) / 6
    fee_per_share = taker_fee_per_share(price, 0.05)
    assert chiefs.market_id == "500101" and chiefs.token == KC_TOKEN
    assert (chiefs.outcome_name, chiefs.outcome_key, chiefs.mode) == ("Chiefs", "KC", "taker")
    assert chiefs.shares == pytest.approx(6.0)
    assert chiefs.price == pytest.approx(price, abs=1e-6)
    assert chiefs.fee_usd == pytest.approx(6 * fee_per_share, abs=1e-4)
    assert chiefs.stake_usd == pytest.approx(round(3.32 + 6 * fee_per_share, 2))
    assert chiefs.placed_at == datetime(2026, 9, 19, 15, 30, tzinfo=UTC)
    assert chiefs.status == "open"
    assert (chiefs.source, chiefs.import_key) == ("wallet", f"{TX_CHIEFS}:{KC_TOKEN}")
    assert chiefs.notes == f"2 fill(s), tx {TX_CHIEFS[:10]}"
    # The scan an hour and a half earlier priced this outcome; that is the fair at bet.
    sample = db_session.scalars(
        select(ForwardSample).where(ForwardSample.token == KC_TOKEN)
    ).first()
    assert sample is not None and chiefs.fair_at_bet == pytest.approx(sample.fair_prob)
    assert chiefs.edge_at_bet == pytest.approx(sample.fair_prob - (price + fee_per_share))

    # A pre-game fill on a market nothing had priced: imported, no fair, open until the
    # scan settles it.
    assert orioles.market_id == ORIOLES_MARKET_ID and orioles.token == BAL_TOKEN
    assert orioles.shares == 10 and orioles.price == 0.44
    assert orioles.placed_at == datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
    assert orioles.fair_at_bet is None and orioles.edge_at_bet is None
    assert (orioles.status, orioles.source, orioles.import_key) == (
        "open",
        "wallet",
        f"{TX_ORIOLES}:{BAL_TOKEN}",
    )


def test_import_is_idempotent(db_session: Session, settings: Settings, slate):
    prefs = get_prefs(db_session)
    first = wi.import_fills(db_session, _rows(settings), wallet=WALLET, prefs=prefs)
    second = wi.import_fills(db_session, _rows(settings), wallet=WALLET, prefs=prefs)
    assert (first.imported, second.imported, second.already) == (2, 0, 2)
    assert second.bet_ids == []
    assert db_session.scalar(select(func.count()).select_from(Bet)) == 2


def test_fair_at_uses_the_latest_pricing_at_or_before_the_fill(
    db_session: Session, settings: Settings, slate
):
    before_scan = FIXED_NOW.replace(hour=14)
    assert wi.fair_at(db_session, KC_TOKEN, before_scan) is None
    at_scan = wi.fair_at(db_session, KC_TOKEN, FIXED_NOW)
    sample = db_session.scalars(
        select(ForwardSample).where(ForwardSample.token == KC_TOKEN)
    ).first()
    assert at_scan == pytest.approx(sample.fair_prob)
    assert wi.fair_at(db_session, "no-such-token", FIXED_NOW) is None


# --------------------------------------------------------------------------- scan hook


def test_scan_imports_then_settles_in_the_same_run(db_session: Session, settings: Settings):
    transport = build_demo_transport(settings)
    _scan(db_session, transport)
    _seed_orioles(db_session, transport)
    update_prefs(db_session, {"pm_wallet": WALLET})

    result = _scan(db_session, transport, kind="poly")
    assert result.errors == []
    scan = db_session.get(Scan, result.scan_id)
    note = scan.notes["wallet_import"]
    assert note["wallet"] == WALLET
    assert (note["fetched"], note["imported"], note["already"]) == (8, 2, 0)
    assert note["skipped_counts"][wi.SKIP_SELL] == 1

    bets = {b.market_id: b for b in db_session.scalars(select(Bet))}
    # The Orioles market resolved for the Blue Jays: imported and settled by this scan.
    assert bets[ORIOLES_MARKET_ID].status == "lost"
    assert bets[ORIOLES_MARKET_ID].pnl_usd == pytest.approx(-bets[ORIOLES_MARKET_ID].stake_usd)
    assert scan.notes["settled"] == 1
    assert bets["500101"].status == "open"

    again = _scan(db_session, transport, kind="poly")
    note = db_session.get(Scan, again.scan_id).notes["wallet_import"]
    assert (note["imported"], note["already"]) == (0, 2)
    assert db_session.scalar(select(func.count()).select_from(Bet)) == 2


def test_scan_without_a_wallet_never_touches_the_feed(db_session: Session, settings: Settings):
    transport = build_demo_transport(settings)
    result = _scan(db_session, transport)
    scan = db_session.get(Scan, result.scan_id)
    assert "wallet_import" not in scan.notes
    assert not any(c["url"].startswith(DATA_API) for c in transport.calls)


def test_scan_survives_a_failing_feed(
    db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    transport = build_demo_transport(settings)
    update_prefs(db_session, {"pm_wallet": WALLET})

    def boom(self, wallet, *, since=None):
        raise RuntimeError("data-api 429")

    monkeypatch.setattr(PolymarketClient, "wallet_trades", boom)
    result = _scan(db_session, transport)
    scan = db_session.get(Scan, result.scan_id)
    assert scan.ok and scan.n_markets > 0
    assert any("wallet" in e and "data-api 429" in e for e in scan.errors)
    assert "wallet_import" not in scan.notes
    assert db_session.scalar(select(func.count()).select_from(Bet)) == 0


# --------------------------------------------------------------------------- client


def _feed(pages: list[list[dict]]):
    """A callable fixture route serving `pages` by offset."""

    def route(url, params, body):
        index = int(params["offset"]) // WALLET_TRADES_PAGE
        return (pages[index] if index < len(pages) else []), {}

    return route


def test_wallet_trades_pages_by_offset_until_a_short_page(settings: Settings):
    full = [{"timestamp": 1789900000 + i} for i in range(WALLET_TRADES_PAGE)]
    short = [{"timestamp": 1789800000}, "not-a-row"]
    transport = FixtureTransport(
        settings.fixtures_dir, [("GET", f"{DATA_API}/trades", _feed([full, short]))]
    )
    rows = PolymarketClient(transport).wallet_trades(" " + WALLET.upper() + " ")
    assert len(rows) == WALLET_TRADES_PAGE + 1  # the non-object row is dropped
    assert [c["params"]["offset"] for c in transport.calls] == [0, WALLET_TRADES_PAGE]
    assert transport.calls[0]["params"] == {
        "user": WALLET,
        "limit": WALLET_TRADES_PAGE,
        "offset": 0,
        "takerOnly": "false",
    }


def test_wallet_trades_stops_at_rows_older_than_since(settings: Settings):
    full = [{"timestamp": 1789900000} for _ in range(WALLET_TRADES_PAGE - 1)] + [{"timestamp": 1}]
    transport = FixtureTransport(
        settings.fixtures_dir, [("GET", f"{DATA_API}/trades", _feed([full, full]))]
    )
    rows = PolymarketClient(transport).wallet_trades(WALLET, since=datetime(2026, 8, 1, tzinfo=UTC))
    assert len(rows) == WALLET_TRADES_PAGE and len(transport.calls) == 1


def test_wallet_trades_rejects_a_non_list_payload_and_an_empty_wallet(settings: Settings):
    transport = FixtureTransport(
        settings.fixtures_dir, [("GET", f"{DATA_API}/trades", lambda u, p, b: ({"error": "x"}, {}))]
    )
    with pytest.raises(PolymarketError, match="expected a list"):
        PolymarketClient(transport).wallet_trades(WALLET)
    with pytest.raises(ValueError, match="wallet"):
        PolymarketClient(transport).wallet_trades("  ")


# --------------------------------------------------------------------------- prefs + UI


def test_pm_wallet_preference_validation(db_session: Session):
    assert validate_prefs({"pm_wallet": " " + WALLET.upper() + " "}) == {"pm_wallet": WALLET}
    assert validate_prefs({"pm_wallet": ""}) == {"pm_wallet": ""}
    assert validate_prefs({"pm_wallet": None}) == {"pm_wallet": ""}
    for bad in ("0x12", WALLET[2:], "0x" + "zz" * 20, 42):
        with pytest.raises(ValueError, match="pm_wallet"):
            validate_prefs({"pm_wallet": bad})
    assert get_prefs(db_session).pm_wallet == ""
    assert update_prefs(db_session, {"pm_wallet": WALLET}).pm_wallet == WALLET
    assert update_prefs(db_session, {"pm_wallet": ""}).pm_wallet == ""


def test_settings_page_shows_and_saves_the_wallet(client: TestClient, db_session: Session):
    html = client.get("/settings").text
    assert 'name="pm_wallet"' in html and "Polymarket wallet" in html

    response = client.post("/settings", data={**SETTINGS_FORM, "pm_wallet": WALLET.upper()})
    assert response.status_code == 200 and "Saved." in response.text
    db_session.expire_all()
    assert get_prefs(db_session).pm_wallet == WALLET
    assert f'value="{WALLET}"' in response.text

    response = client.post("/settings", data={**SETTINGS_FORM, "pm_wallet": "0xnope"})
    assert response.status_code == 200 and "pm_wallet must be a 0x address" in response.text
    assert 'value="0xnope"' in response.text  # the edit survives the error
    db_session.expire_all()
    assert get_prefs(db_session).pm_wallet == WALLET


def test_ledger_marks_imported_bets(
    client: TestClient, db_session: Session, settings: Settings, slate
):
    wi.import_fills(db_session, _rows(settings), wallet=WALLET, prefs=get_prefs(db_session))
    html = client.get("/bets").text
    assert html.count(">imported<") == 2
    assert f"tx {TX_CHIEFS[:10]}" in html


# --------------------------------------------------------------------------- cli


@pytest.fixture
def demo_cli(settings: Settings, engine, db_session: Session):
    demo = settings.model_copy(update={"demo_mode": True})
    set_settings(demo)
    try:
        yield db_session
    finally:
        set_settings(None)


def test_cli_import_wallet(demo_cli: Session, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["import-wallet"]) == 2
    assert "no wallet" in capsys.readouterr().err

    assert cli.main(["demo-seed"]) == 0
    capsys.readouterr()
    assert cli.main(["import-wallet", "--wallet", WALLET.upper()]) == 0
    out = capsys.readouterr().out
    assert (
        out.splitlines()[0]
        == f"wallet {WALLET[:6]}...{WALLET[-4:]}: fetched=8 imported=2 already=0 skipped=5"
    )
    assert f"  skipped 1: {wi.SKIP_SELL}" in out
    # The demo seed's two synthetic bets plus the two imported ones.
    assert demo_cli.scalar(select(func.count()).select_from(Bet)) == 4
    assert demo_cli.scalar(select(func.count()).select_from(Bet).where(Bet.source == "wallet")) == 2

    assert cli.main(["import-wallet", "--wallet", WALLET]) == 0
    assert "imported=0 already=2" in capsys.readouterr().out


def test_parser_accepts_import_wallet():
    args = cli.build_parser().parse_args(["import-wallet", "--wallet", WALLET])
    assert args.command == "import-wallet" and args.wallet == WALLET
