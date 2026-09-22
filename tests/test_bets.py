"""Bet ledger service: create (taker/maker), manual settle P&L, settlement from a closed
PmMarket, closing-line capture with CLV, and the ledger summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.espn import EspnClient
from app.clients.oddsapi import OddsApiClient
from app.clients.polymarket import CLOB, GAMMA, PolymarketClient
from app.clients.transport import FixtureTransport, TransportError, load_fixture
from app.core.edge import effective_price
from app.models import Bet, Market, Opportunity, Scan
from app.services.bets import (
    capture_closing,
    cost_per_share,
    create_bet,
    fee_rate_for,
    latest_best_ask,
    ledger_summary,
    settle_bet_manual,
    settle_open_bets,
)
from app.services.demo import build_demo_transport, demo_routes
from app.services.prefs import get_prefs, update_prefs
from app.services.scan import run_scan, upsert_game, upsert_market
from app.settings import Settings
from tests.conftest import FIXED_NOW

CHIEFS_START = datetime(2026, 9, 20, 20, 25, tzinfo=UTC)


@pytest.fixture
def slate(db_session: Session, settings: Settings):
    """A real fixture-backed scan; returns (transport, scan result)."""
    transport = build_demo_transport(settings)
    result = run_scan(
        db_session,
        polymarket=PolymarketClient(transport),
        oddsapi=OddsApiClient(transport, "fixture-key"),
        espn=EspnClient(transport),
        prefs=get_prefs(db_session),
        kind="both",
        leagues=["nfl", "nba", "mlb"],
        now=FIXED_NOW,
    )
    return transport, result


def _opp(session: Session, market_id: str, outcome_name: str) -> Opportunity:
    return session.scalars(
        select(Opportunity)
        .where(Opportunity.market_id == market_id, Opportunity.outcome_name == outcome_name)
        .order_by(Opportunity.id.desc())
    ).first()


def _rescan(session: Session, transport, now: datetime, *, oddsapi: bool = False):
    return run_scan(
        session,
        polymarket=PolymarketClient(transport),
        oddsapi=OddsApiClient(transport, "fixture-key") if oddsapi else None,
        espn=None,
        prefs=get_prefs(session),
        kind="poly",
        leagues=["nfl", "nba", "mlb"],
        now=now,
    )


# --------------------------------------------------------------------------- create


def test_create_taker_bet_from_opportunity(db_session: Session, slate):
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, opp.suggested_stake, opp.ask, "taker", notes=" first ")

    cost = effective_price(0.55, 0.05)  # 0.562375
    assert bet.id and bet.status == "open"
    assert (bet.market_id, bet.token) == ("500101", opp.token)
    assert (bet.outcome_key, bet.outcome_name, bet.mode) == ("KC", "Chiefs", "taker")
    assert bet.price == 0.55 and bet.stake_usd == pytest.approx(opp.suggested_stake)
    assert bet.shares == pytest.approx(opp.suggested_stake / cost, rel=1e-6)
    assert bet.fee_usd == pytest.approx(bet.stake_usd - bet.shares * 0.55, abs=1e-3)
    assert bet.fee_usd == pytest.approx(bet.shares * 0.05 * 0.55 * 0.45, abs=1e-3)
    assert cost_per_share(bet) == pytest.approx(cost, rel=1e-6)
    assert bet.fair_at_bet == opp.fair_prob
    assert bet.edge_at_bet == pytest.approx(opp.fair_prob - cost, abs=1e-9)
    assert bet.edge_at_bet == pytest.approx(opp.edge, abs=1e-9)
    assert bet.notes == "first" and bet.placed_at.tzinfo is not None
    assert bet.pnl_usd is None and bet.settled_at is None and bet.clv is None


def test_create_maker_bet_at_the_limit_price(db_session: Session, slate):
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 20.0, opp.limit_price, "maker")
    assert bet.mode == "maker" and bet.price == opp.limit_price
    assert bet.fee_usd == 0.0
    assert bet.shares == pytest.approx(20.0 / opp.limit_price, rel=1e-6)
    assert bet.stake_usd == 20.0
    assert bet.edge_at_bet == pytest.approx(opp.fair_prob - opp.limit_price, abs=1e-9)
    assert bet.edge_at_bet > bet.fair_at_bet - effective_price(opp.ask, 0.05)  # cheaper than taking


def test_create_bet_at_an_edited_price_reprices_the_edge(db_session: Session, slate):
    opp = _opp(db_session, "500501", "Yankees")
    bet = create_bet(db_session, opp.id, 10.0, 0.45, "taker")
    assert bet.price == 0.45
    assert bet.edge_at_bet == pytest.approx(opp.fair_prob - effective_price(0.45, 0.05), abs=1e-9)
    assert bet.edge_at_bet < opp.edge


def test_create_bet_uses_the_per_market_fee_rate(db_session: Session, slate):
    opp = _opp(db_session, "500101", "Chiefs")
    assert fee_rate_for(opp, 0.05) == 0.05
    update_prefs(db_session, {"taker_fee_rate": 0.02})
    # the opportunity was priced at 5% (effective 0.562375 on a 0.55 ask): that rate wins
    assert fee_rate_for(opp, 0.02) == pytest.approx(0.05)
    bet = create_bet(db_session, opp.id, 10.0, 0.55, "taker")
    assert cost_per_share(bet) == pytest.approx(effective_price(0.55, 0.05), rel=1e-6)


@pytest.mark.parametrize(
    ("stake", "price", "mode", "message"),
    [
        (0.0, 0.55, "taker", "stake must be greater than 0"),
        (-5.0, 0.55, "taker", "stake must be greater than 0"),
        (10.0, 1.0, "taker", "price must be between 0 and 1"),
        (10.0, 0.0, "maker", "price must be between 0 and 1"),
        (10.0, 0.55, "limit", "mode must be taker or maker"),
        (float("nan"), 0.55, "taker", "finite"),
    ],
)
def test_create_bet_validation(db_session: Session, slate, stake, price, mode, message):
    opp = _opp(db_session, "500101", "Chiefs")
    with pytest.raises(ValueError, match=message):
        create_bet(db_session, opp.id, stake, price, mode)
    assert db_session.scalars(select(Bet)).first() is None


def test_create_bet_rejects_missing_opportunity_and_closed_market(db_session: Session, slate):
    with pytest.raises(ValueError, match="opportunity 999999 not found"):
        create_bet(db_session, 999999, 10.0, 0.5, "taker")
    opp = _opp(db_session, "500101", "Chiefs")
    market = db_session.get(Market, "500101")
    market.closed = True
    db_session.commit()
    with pytest.raises(ValueError, match="market is closed"):
        create_bet(db_session, opp.id, 10.0, 0.5, "taker")


# --------------------------------------------------------------------------- manual settle


@pytest.mark.parametrize(
    ("result", "expected_pnl"),
    [("won", None), ("lost", None), ("void", 0.0)],
)
def test_settle_bet_manual_pnl(db_session: Session, slate, result, expected_pnl):
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 13.27, 0.55, "taker")
    settled = settle_bet_manual(db_session, bet.id, result)
    assert settled.status == result and settled.settled_at is not None
    if result == "won":
        expected_pnl = round(bet.shares - 13.27, 2)  # every share pays $1
        assert expected_pnl > 0
    elif result == "lost":
        expected_pnl = -13.27
    assert settled.pnl_usd == pytest.approx(expected_pnl, abs=1e-9)


def test_settle_bet_manual_errors(db_session: Session, slate):
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 10.0, 0.55, "taker")
    # review round: "push" joined the accepted results, so the message names four
    with pytest.raises(ValueError, match="result must be won, lost, void or push"):
        settle_bet_manual(db_session, bet.id, "maybe")
    with pytest.raises(ValueError, match="bet 999 not found"):
        settle_bet_manual(db_session, 999, "won")
    settle_bet_manual(db_session, bet.id, "void")
    with pytest.raises(ValueError, match="already settled as void"):
        settle_bet_manual(db_session, bet.id, "won")
    assert db_session.get(Bet, bet.id).pnl_usd == 0.0


# --------------------------------------------------------------------------- settle_open_bets


def test_settle_open_bets_from_a_closed_pm_market(db_session: Session, slate):
    transport, _ = slate
    bal = PolymarketClient(transport).market("500701")
    assert bal.closed and bal.resolved_outcome_index == 1
    game = upsert_game(db_session, bal, FIXED_NOW)
    upsert_market(db_session, bal, game, FIXED_NOW)
    orioles_token, jays_token = (o.token_id for o in bal.outcomes)

    def bet(token: str, name: str, stake: float, shares: float) -> Bet:
        row = Bet(
            market_id="500701",
            token=token,
            outcome_name=name,
            mode="taker",
            price=stake / shares,
            shares=shares,
            stake_usd=stake,
            fee_usd=0.0,
            status="open",
        )
        db_session.add(row)
        return row

    lost = bet(orioles_token, "Orioles", 10.0, 23.0)
    won = bet(jays_token, "Blue Jays", 15.0, 25.0)
    stray = bet("9" * 70, "Nobody", 5.0, 10.0)
    db_session.commit()

    when = FIXED_NOW + timedelta(minutes=1)
    assert settle_open_bets(db_session, {"500701": bal}, when) == 2
    assert (lost.status, lost.pnl_usd, lost.settled_at) == ("lost", -10.0, when)
    assert (won.status, won.pnl_usd, won.settled_at) == ("won", 10.0, when)
    assert stray.status == "open"  # not an outcome of the market: left alone, logged
    assert db_session.get(Market, "500701").resolved_outcome == "b"

    # open markets and unknown markets settle nothing
    chiefs = _opp(db_session, "500101", "Chiefs")
    open_bet = create_bet(db_session, chiefs.id, 10.0, 0.55, "taker")
    assert settle_open_bets(db_session, {}) == 0
    assert open_bet.status == "open"


# --------------------------------------------------------------------------- closing line


def test_capture_closing_sets_clv_from_the_opportunity(db_session: Session, slate):
    transport, first = slate
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 13.27, 0.55, "taker")

    # before kickoff nothing is captured
    assert capture_closing(db_session, first.scan_id, CHIEFS_START - timedelta(minutes=1)) == 0
    assert bet.closing_fair is None

    after = _rescan(db_session, transport, CHIEFS_START + timedelta(minutes=5))
    db_session.refresh(bet)
    assert bet.closing_fair == pytest.approx(opp.fair_prob, abs=1e-9)
    assert bet.closing_pm_price == 0.55
    assert bet.clv == pytest.approx(opp.fair_prob - cost_per_share(bet), abs=1e-9)
    assert bet.clv > 0 and bet.status == "open"
    assert db_session.get(Bet, bet.id).clv == bet.clv and after.errors == []

    # captured once: a later scan does not overwrite it
    _rescan(db_session, transport, CHIEFS_START + timedelta(hours=1))
    db_session.refresh(bet)
    assert bet.closing_pm_price == 0.55 and bet.clv == pytest.approx(
        opp.fair_prob - cost_per_share(bet)
    )


def test_capture_closing_recomputes_fair_when_no_opportunity_row_exists(db_session: Session, slate):
    """A Bills bet has no Opportunity (negative edge): the closing fair comes from the stored
    book snapshot via matching.fair_for_outcome."""
    transport, first = slate
    market = db_session.get(Market, "500101")
    bills = Bet(
        market_id="500101",
        token=market.outcome_b_token,
        outcome_key="BUF",
        outcome_name="Bills",
        mode="maker",
        price=0.40,
        shares=25.0,
        stake_usd=10.0,
        fee_usd=0.0,
        status="open",
    )
    db_session.add(bills)
    db_session.commit()
    result = _rescan(db_session, transport, CHIEFS_START + timedelta(minutes=5))
    db_session.refresh(bills)
    # power de-vig, default weights: 1 - Chiefs fair 0.585612 (additive would give 0.415918)
    assert bills.closing_fair == pytest.approx(0.414388, abs=1e-5)
    assert bills.closing_pm_price == 0.46
    assert bills.clv == pytest.approx(bills.closing_fair - 0.40, abs=1e-9)
    assert result.errors == []

    # the configured method is honoured for the closing line: additive gives 1 - 0.584082
    update_prefs(db_session, {"devig_method": "additive"})
    bills_additive = Bet(
        market_id="500101",
        token=market.outcome_b_token,
        outcome_key="BUF",
        outcome_name="Bills",
        mode="maker",
        price=0.41,
        shares=20.0,
        stake_usd=8.2,
        fee_usd=0.0,
        status="open",
    )
    db_session.add(bills_additive)
    db_session.commit()
    _rescan(db_session, transport, CHIEFS_START + timedelta(minutes=10))
    db_session.refresh(bills_additive)
    db_session.refresh(bills)
    assert bills_additive.closing_fair == pytest.approx(0.415918, abs=1e-5)
    assert bills.closing_fair == pytest.approx(0.414388, abs=1e-5)  # the first capture wins


def test_capture_closing_skips_games_without_a_start_or_snapshot(db_session: Session, slate):
    transport, first = slate
    market = db_session.get(Market, "500301")  # LAL@BOS, tips off 2026-10-22
    bet = Bet(
        market_id="500301",
        token=market.outcome_b_token,
        outcome_name="Celtics",
        mode="taker",
        price=0.64,
        shares=30.0,
        stake_usd=20.0,
        fee_usd=0.0,
        status="open",
    )
    db_session.add(bet)
    db_session.commit()
    assert capture_closing(db_session, first.scan_id, FIXED_NOW) == 0  # not started yet
    market.game_id = None
    db_session.commit()
    assert capture_closing(db_session, first.scan_id, FIXED_NOW + timedelta(days=60)) == 0


# --------------------------------------------------------------------------- summary


def test_ledger_summary_keys_and_maths(db_session: Session, slate):
    empty = ledger_summary(db_session)
    assert empty == {
        "n_open": 0,
        "n_settled": 0,
        "n_won": 0,
        "n_lost": 0,
        "n_push": 0,  # review round: a 50/50 resolution is its own outcome
        "total_staked": 0.0,
        "total_pnl": 0.0,
        "roi": 0.0,
        "avg_clv": None,
        "n_clv_positive": 0,
        "n_clv_recorded": 0,
    }

    chiefs = _opp(db_session, "500101", "Chiefs")
    yankees = _opp(db_session, "500501", "Yankees")
    under = _opp(db_session, "500602", "Under")
    open_bet = create_bet(db_session, chiefs.id, 10.0, 0.55, "taker")
    # maker orders rest one tick below the asks (0.40 / 0.45): 25.641 and 18.18 shares
    won = create_bet(db_session, yankees.id, 10.0, 0.39, "maker")
    lost = create_bet(db_session, under.id, 8.0, 0.44, "maker")
    void = create_bet(db_session, under.id, 3.0, 0.45, "taker")
    settle_bet_manual(db_session, won.id, "won")
    settle_bet_manual(db_session, lost.id, "lost")
    settle_bet_manual(db_session, void.id, "void")
    open_bet.clv = 0.02
    won.clv = -0.01
    db_session.commit()

    summary = ledger_summary(db_session)
    assert summary["n_open"] == 1 and summary["n_settled"] == 3
    assert summary["n_won"] == 1 and summary["n_lost"] == 1
    assert summary["total_staked"] == 18.0  # voids return the stake
    assert won.shares == pytest.approx(10.0 / 0.39, rel=1e-6)
    expected_pnl = round(won.shares - 10.0, 2) - 8.0  # 15.64 - 8.00
    assert summary["total_pnl"] == pytest.approx(expected_pnl)
    assert summary["roi"] == pytest.approx(expected_pnl / 18.0)
    assert summary["avg_clv"] == pytest.approx(0.005)
    assert summary["n_clv_positive"] == 1 and summary["n_clv_recorded"] == 2
    assert set(summary) == set(empty)


# --------------------------------------------------------------------------- review fixes


def _static(payload):
    def handler(url, params, body):
        return payload, {}

    return handler


def test_create_maker_bet_at_or_above_the_ask_is_rejected(db_session: Session, slate):
    """A bid at or above the best ask crosses the book and fills as a taker, so the fee-free
    maker maths would record a fictitious price."""
    opp = _opp(db_session, "500101", "Chiefs")  # ask 0.55, resting limit 0.54
    assert opp.limit_price == pytest.approx(0.54)
    assert latest_best_ask(db_session, opp.token) == 0.55
    with pytest.raises(ValueError, match="would cross the current 55¢ ask"):
        create_bet(db_session, opp.id, 20.0, 0.55, "maker")
    with pytest.raises(ValueError, match="cross"):
        create_bet(db_session, opp.id, 20.0, 0.56, "maker")
    assert db_session.scalars(select(Bet)).first() is None
    resting = create_bet(db_session, opp.id, 20.0, 0.54, "maker")
    assert resting.fee_usd == 0.0 and resting.shares == pytest.approx(20.0 / 0.54, rel=1e-6)
    taker = create_bet(db_session, opp.id, 20.0, 0.56, "taker")  # a taker may pay up
    assert taker.fee_usd > 0


def test_closing_line_comes_from_the_last_pre_kickoff_snapshot_not_in_play_odds(
    db_session: Session, slate, settings: Settings
):
    """A scan run after kickoff sees in-play books (KC -400) and an in-game ask (0.80). CLV
    must still be measured against the last pre-game snapshot."""
    transport, first = slate
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 13.27, 0.55, "taker")
    kc_token = opp.token

    odds = load_fixture("oddsapi_nfl.json", settings.fixtures_dir)
    kc_buf = next(g for g in odds if g["home_team"] == "Buffalo Bills")
    for book in kc_buf["bookmakers"]:
        for market in book["markets"]:
            if market["key"] == "h2h":
                for outcome in market["outcomes"]:
                    outcome["price"] = -400 if outcome["name"] == "Kansas City Chiefs" else 300
    books = load_fixture("clob_books.json", settings.fixtures_dir)
    books[kc_token]["asks"] = [{"price": "0.80", "size": "500"}]
    books[kc_token]["bids"] = [{"price": "0.79", "size": "500"}]
    routes = []
    for method, prefix, target in demo_routes(settings.fixtures_dir):
        if "americanfootball_nfl" in prefix:
            target = _static(odds)
        elif prefix == f"{CLOB}/books":
            target = _static(books)
        routes.append((method, prefix, target))
    in_play = FixtureTransport(
        settings.fixtures_dir, routes, default_headers={"x-requests-remaining": "497"}
    )
    live = run_scan(
        db_session,
        polymarket=PolymarketClient(in_play),
        oddsapi=OddsApiClient(in_play, "fixture-key"),
        espn=None,
        prefs=get_prefs(db_session),
        kind="both",
        leagues=["nfl"],
        now=CHIEFS_START + timedelta(hours=1),
    )
    assert live.errors == []
    assert ("500101", "Chiefs") not in {
        (o.market_id, o.outcome_name)
        for o in db_session.scalars(select(Opportunity).where(Opportunity.scan_id == live.scan_id))
    }
    db_session.refresh(bet)
    assert bet.closing_fair == pytest.approx(opp.fair_prob, abs=1e-9)  # 0.5856, not 0.78
    assert bet.closing_pm_price == 0.55  # not the 0.80 in-game ask
    assert bet.clv == pytest.approx(opp.fair_prob - cost_per_share(bet), abs=1e-9)
    assert 0.0 < bet.clv < 0.05
    assert db_session.get(Scan, live.scan_id).notes["closing_captured"] == 1


def test_a_bet_settled_by_the_same_scan_still_gets_its_closing_line(
    db_session: Session, slate, settings: Settings
):
    """Morning scan, evening game, next-morning scan: the market resolves between two scans and
    the bet is settled and CLV'd by the same scan (closing is captured before settlement)."""
    transport, first = slate
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 13.27, 0.55, "taker")

    events = load_fixture("gamma_events_nfl.json", settings.fixtures_dir)
    chiefs_ml = next(m for e in events for m in e["markets"] if m["id"] == "500101")
    chiefs_ml.update(closed=True, acceptingOrders=False, outcomePrices='["1", "0"]')
    books = load_fixture("clob_books.json", settings.fixtures_dir)
    books[opp.token]["asks"] = [{"price": "0.99", "size": "100"}]  # the settlement print
    routes = []
    for method, prefix, target in demo_routes(settings.fixtures_dir):
        if prefix == f"{GAMMA}/events?tag_slug=nfl":
            target = _static(events)
        elif prefix == f"{CLOB}/books":
            target = _static(books)
        routes.append((method, prefix, target))
    morning_after = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    result = run_scan(
        db_session,
        polymarket=PolymarketClient(FixtureTransport(settings.fixtures_dir, routes)),
        oddsapi=None,
        espn=None,
        prefs=get_prefs(db_session),
        kind="poly",
        leagues=["nfl"],
        now=morning_after,
    )
    assert result.errors == []
    db_session.refresh(bet)
    assert bet.status == "won" and bet.settled_at == morning_after
    assert bet.pnl_usd == pytest.approx(round(bet.shares - 13.27, 2))
    assert bet.closing_fair == pytest.approx(opp.fair_prob, abs=1e-9)
    assert bet.closing_pm_price == 0.55  # the last pre-kickoff ask, not the 0.99 print
    assert bet.clv == pytest.approx(opp.fair_prob - cost_per_share(bet), abs=1e-9)
    notes = db_session.get(Scan, result.scan_id).notes
    assert (notes["settled"], notes["closing_captured"]) == (1, 1)


def test_capture_closing_without_a_pre_kickoff_book_snapshot_records_nothing(
    db_session: Session, settings: Settings
):
    transport = build_demo_transport(settings)
    poly = run_scan(  # markets and Polymarket quotes, no books at all
        db_session,
        polymarket=PolymarketClient(transport),
        oddsapi=None,
        espn=None,
        prefs=get_prefs(db_session),
        kind="poly",
        leagues=["nba"],
        now=FIXED_NOW,
    )
    market = db_session.get(Market, "500301")
    bet = Bet(
        market_id="500301",
        token=market.outcome_b_token,
        outcome_key="BOS",
        outcome_name="Celtics",
        mode="taker",
        price=0.64,
        shares=30.0,
        stake_usd=20.0,
        fee_usd=0.0,
        status="open",
    )
    db_session.add(bet)
    db_session.commit()
    assert capture_closing(db_session, poly.scan_id, FIXED_NOW + timedelta(days=60)) == 0
    assert bet.closing_fair is None and bet.closing_pm_price is None and bet.clv is None


def test_capture_closing_without_a_pre_kickoff_ask_keeps_the_price_blank(
    db_session: Session, settings: Settings
):
    def boom(url, params, body):
        raise TransportError("clob down", status=502, url=url)

    routes = [
        (r[0], r[1], boom) if r[1].endswith("/books") else r
        for r in demo_routes(settings.fixtures_dir)
    ]
    transport = FixtureTransport(
        settings.fixtures_dir, routes, default_headers={"x-requests-remaining": "497"}
    )
    first = run_scan(
        db_session,
        polymarket=PolymarketClient(transport),
        oddsapi=OddsApiClient(transport, "fixture-key"),
        espn=None,
        prefs=get_prefs(db_session),
        kind="both",
        leagues=["nfl"],
        now=FIXED_NOW,
    )
    assert first.n_opps == 0  # no asks, so nothing to buy; the book snapshot is stored anyway
    market = db_session.get(Market, "500101")
    bet = Bet(
        market_id="500101",
        token=market.outcome_a_token,
        outcome_key="KC",
        outcome_name="Chiefs",
        mode="maker",
        price=0.54,
        shares=20.0,
        stake_usd=10.8,
        fee_usd=0.0,
        status="open",
    )
    db_session.add(bet)
    db_session.commit()
    assert capture_closing(db_session, first.scan_id, CHIEFS_START + timedelta(minutes=5)) == 1
    assert bet.closing_fair == pytest.approx(0.585612, abs=1e-5)
    assert bet.closing_pm_price is None
    assert bet.clv == pytest.approx(0.585612 - 0.54, abs=1e-5)


def _push_market(market):
    """The same PmMarket, closed with Polymarket's 50/50 (cancelled game) resolution."""
    from dataclasses import replace

    return replace(
        market,
        closed=True,
        accepting_orders=False,
        resolved_outcome_index=None,  # ["0.5","0.5"] resolves to neither side
        outcomes=tuple(replace(o, last_price=0.5) for o in market.outcomes),
    )


def test_a_fifty_fifty_resolution_settles_as_a_push_paying_half_a_dollar_a_share(
    db_session: Session, slate
):
    """A postponed/cancelled game resolves ["0.5","0.5"], which is no `resolved_outcome_index`
    at all: without the push branch the bet stays open for ever, and voiding it by hand
    reports P&L 0 when the truth is 0.5*shares - stake."""
    transport, _ = slate
    opp = _opp(db_session, "500101", "Chiefs")
    bet = create_bet(db_session, opp.id, 10.0, 0.57, "taker")
    assert bet.shares == pytest.approx(10.0 / effective_price(0.57, 0.05), abs=1e-6)
    assert bet.shares == pytest.approx(17.174606, abs=1e-5)

    live = PolymarketClient(transport).market("500101")
    assert settle_open_bets(db_session, {"500101": live}) == 0  # still open, nothing settles

    when = FIXED_NOW + timedelta(days=1)
    assert settle_open_bets(db_session, {"500101": _push_market(live)}, when) == 1
    db_session.refresh(bet)
    assert bet.status == "push" and bet.settled_at == when
    assert bet.pnl_usd == pytest.approx(round(0.5 * bet.shares - 10.0, 2), abs=1e-9)
    assert bet.pnl_usd == pytest.approx(-1.41, abs=1e-9)  # not 0.00
    # a push has no winning outcome, so the market row records none
    assert db_session.get(Market, "500101").closed is True
    assert db_session.get(Market, "500101").resolved_outcome is None


def test_manual_push_and_void_are_different_results(db_session: Session, slate):
    opp = _opp(db_session, "500101", "Chiefs")
    pushed = create_bet(db_session, opp.id, 10.0, 0.57, "taker")
    voided = create_bet(db_session, opp.id, 10.0, 0.57, "taker")
    settle_bet_manual(db_session, pushed.id, "push")
    settle_bet_manual(db_session, voided.id, "void")
    assert pushed.status == "push"
    assert pushed.pnl_usd == pytest.approx(round(0.5 * pushed.shares - 10.0, 2), abs=1e-9)
    assert pushed.pnl_usd < 0
    assert voided.status == "void" and voided.pnl_usd == 0.0  # an order that never filled

    summary = ledger_summary(db_session)
    assert summary["n_push"] == 1
    assert summary["n_settled"] == 2 and summary["n_won"] == 0 and summary["n_lost"] == 0
    # a push risked the money: it counts as decided, a void does not
    assert summary["total_staked"] == 10.0
    assert summary["total_pnl"] == pytest.approx(pushed.pnl_usd, abs=1e-9)
    assert summary["roi"] == pytest.approx(pushed.pnl_usd / 10.0, abs=1e-9)


def test_create_and_settle_accept_an_injected_clock(db_session: Session, slate):
    """`now` pins placed_at / settled_at so tests (and a replayed scan) are deterministic."""
    opp = _opp(db_session, "500101", "Chiefs")
    placed = FIXED_NOW - timedelta(hours=3)
    bet = create_bet(db_session, opp.id, 10.0, 0.55, "taker", notes="pinned", now=placed)
    assert bet.placed_at == placed

    settled_at = FIXED_NOW + timedelta(days=2)
    settled = settle_bet_manual(db_session, bet.id, "won", now=settled_at)
    assert settled.settled_at == settled_at
    assert settled.placed_at == placed

    # the default is still the wall clock, so every existing call site is unchanged
    wall_clock = datetime.now(UTC)
    default_bet = create_bet(db_session, opp.id, 10.0, 0.55, "taker")
    assert abs(default_bet.placed_at - wall_clock) < timedelta(minutes=1)
    default_settled = settle_bet_manual(db_session, default_bet.id, "lost")
    assert abs(default_settled.settled_at - wall_clock) < timedelta(minutes=1)


def test_capture_closing_covers_settled_bets_and_skips_voids(db_session: Session, slate):
    """A bet settled by hand before kickoff still gets its CLV from the pre-kickoff snapshot;
    a void never does, and the capture count reflects only the eligible bet."""
    transport, first = slate
    opp = _opp(db_session, "500101", "Chiefs")
    settled = create_bet(db_session, opp.id, 13.27, 0.55, "taker")
    voided = create_bet(db_session, opp.id, 5.00, 0.55, "taker")
    settle_bet_manual(db_session, settled.id, "won", now=CHIEFS_START - timedelta(hours=2))
    settle_bet_manual(db_session, voided.id, "void", now=CHIEFS_START - timedelta(hours=2))
    assert (settled.status, voided.status) == ("won", "void")

    after = _rescan(db_session, transport, CHIEFS_START + timedelta(minutes=5))
    assert after.errors == []
    assert db_session.get(Scan, after.scan_id).notes["closing_captured"] == 1

    db_session.refresh(settled)
    db_session.refresh(voided)
    assert settled.closing_fair == pytest.approx(opp.fair_prob, abs=1e-9)
    assert settled.closing_pm_price == 0.55  # the last pre-kickoff ask
    assert settled.clv == pytest.approx(opp.fair_prob - cost_per_share(settled), abs=1e-9)
    assert settled.status == "won"  # capture never reopens a settled bet
    assert voided.closing_fair is None
    assert voided.closing_pm_price is None
    assert voided.clv is None

    # and a direct call agrees: everything eligible is already captured
    assert capture_closing(db_session, after.scan_id, CHIEFS_START + timedelta(hours=1)) == 0


def test_capture_closing_skips_a_bet_whose_token_is_not_an_outcome(db_session: Session, slate):
    transport, first = slate
    stray = Bet(
        market_id="500101",
        token="9" * 70,
        outcome_name="Nobody",
        mode="taker",
        price=0.5,
        shares=10.0,
        stake_usd=5.0,
        fee_usd=0.0,
        status="open",
    )
    db_session.add(stray)
    db_session.commit()
    assert capture_closing(db_session, first.scan_id, CHIEFS_START + timedelta(minutes=5)) == 0
    assert stray.closing_fair is None and stray.clv is None
