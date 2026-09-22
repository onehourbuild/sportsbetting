"""Demo mode: fixture transport routes and the idempotent seed.

Everything here is synthetic (docs/FIXTURES.md). The demo clock is fixed at
2026-09-19T15:00Z so the slate's games are always in the future relative to it and the
stored book snapshot never goes stale; `run_scan_default` uses the same clock in demo mode.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.espn import DEFAULT_BASE as ESPN_BASE
from app.clients.espn import ESPN_PATHS, EspnClient
from app.clients.oddsapi import DEFAULT_BASE as ODDS_BASE
from app.clients.oddsapi import SPORT_KEYS, OddsApiClient
from app.clients.polymarket import CLOB, DATA_API, GAMMA, PolymarketClient
from app.clients.transport import FixtureTransport, Route, TransportError, load_fixture
from app.core import clv as clv_math
from app.core.edge import taker_fee_per_share
from app.core.types import LEAGUES
from app.models import Bet, BookQuote, Game, Market, Opportunity, PmQuote, Scan
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)

DEMO_NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
DEMO_ODDS_HEADERS: dict[str, str] = {
    "x-requests-remaining": "497",
    "x-requests-used": "3",
    "x-requests-last": "3",
}
CHIEFS_MARKET_ID = "500101"
ORIOLES_MARKET_ID = "500701"
DEMO_NOTE = "demo"  # Bet.notes marker: every synthetic bet carries it
DEMO_API_KEY = "demo"  # never sent anywhere: the fixture transport serves every route
ORIOLES_PRICE = 0.42
ORIOLES_STAKE = 10.0
ORIOLES_FAIR_AT_BET = 0.45
ORIOLES_CLOSING_FAIR = 0.40
ORIOLES_CLOSING_PM_PRICE = 0.41
ORIOLES_PLACED_AT = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)


def _gamma_market_route(fixtures_dir: Any):
    """GET /markets/{id}: the market dict from the event fixtures plus its event (without
    markets) under `events`, exactly as tests/test_polymarket_client.py serves it."""

    def handler(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        market_id = url.rstrip("/").rsplit("/", 1)[-1]
        for league in LEAGUES:
            for event in load_fixture(f"gamma_events_{league}.json", fixtures_dir):
                for market in event.get("markets", []):
                    if str(market.get("id")) == market_id:
                        payload = dict(market)
                        payload["events"] = [{k: v for k, v in event.items() if k != "markets"}]
                        return payload, {}
        raise TransportError(f"no fixture market {market_id}", status=404, url=url)

    return handler


def demo_routes(fixtures_dir: Any) -> list[Route]:
    routes: list[Route] = []
    for league in LEAGUES:
        routes.append(("GET", f"{GAMMA}/events?tag_slug={league}", f"gamma_events_{league}.json"))
        routes.append(("GET", f"{GAMMA}/teams?league={league}", f"gamma_teams_{league}.json"))
    routes.append(("POST", f"{CLOB}/books", "clob_books.json"))
    routes.append(("GET", f"{DATA_API}/trades", "data_trades_wallet.json"))
    routes.append(("GET", f"{GAMMA}/markets/", _gamma_market_route(fixtures_dir)))
    for league in LEAGUES:
        routes.append(
            ("GET", f"{ODDS_BASE}/sports/{SPORT_KEYS[league]}/odds", f"oddsapi_{league}.json")
        )
        routes.append(
            (
                "GET",
                f"{ESPN_BASE}/{ESPN_PATHS[league]}/scoreboard",
                f"espn_scoreboard_{league}.json",
            )
        )
    return routes


def build_demo_transport(settings: Settings) -> FixtureTransport:
    """A FixtureTransport serving every API the scan touches from `settings.fixtures_dir`."""
    return FixtureTransport(
        settings.fixtures_dir, demo_routes(settings.fixtures_dir), default_headers=DEMO_ODDS_HEADERS
    )


def _seed_orioles_bet(session: Session, polymarket: PolymarketClient) -> Bet | None:
    """An open taker bet on the Orioles, placed before the (now closed) BAL@TOR game, so the
    demo scan's settlement step settles it as lost. Closing data is pre-filled because the
    finished game has no books or asks left to compute it from."""
    from app.services.scan import upsert_game, upsert_market

    market = polymarket.market(ORIOLES_MARKET_ID)
    if market is None:
        log.warning("demo: market %s missing from fixtures", ORIOLES_MARKET_ID)
        return None
    game = upsert_game(session, market, DEMO_NOW)
    upsert_market(session, market, game, DEMO_NOW)
    orioles = market.outcomes[0]
    fee_per_share = taker_fee_per_share(ORIOLES_PRICE, 0.05)
    cost = ORIOLES_PRICE + fee_per_share
    shares = ORIOLES_STAKE / cost
    bet = Bet(
        market_id=market.market_id,
        token=orioles.token_id,
        outcome_key=orioles.team_key,
        outcome_name=orioles.name,
        mode="taker",
        price=ORIOLES_PRICE,
        shares=round(shares, 6),
        stake_usd=ORIOLES_STAKE,
        fee_usd=round(ORIOLES_STAKE - shares * ORIOLES_PRICE, 4),
        fair_at_bet=ORIOLES_FAIR_AT_BET,
        edge_at_bet=ORIOLES_FAIR_AT_BET - cost,
        placed_at=ORIOLES_PLACED_AT,
        status="open",
        closing_fair=ORIOLES_CLOSING_FAIR,
        closing_pm_price=ORIOLES_CLOSING_PM_PRICE,
        clv=clv_math.clv(ORIOLES_CLOSING_FAIR, cost),
        notes=DEMO_NOTE,
    )
    session.add(bet)
    session.commit()
    return bet


def demo_rows_present(session: Session) -> bool:
    """True when the synthetic demo bets are in the ledger."""
    return bool(session.scalar(select(func.count()).select_from(Bet).where(Bet.notes == DEMO_NOTE)))


def _real_bet_count(session: Session) -> int:
    return int(
        session.scalar(select(func.count()).select_from(Bet).where(Bet.notes != DEMO_NOTE)) or 0
    )


def seed_demo(session: Session, settings: Settings | None = None) -> None:
    """Fixture-backed scan of all three leagues plus two demo bets.

    Self-contained: the clients are built on the fixture transport and `run_scan` is
    called directly, so seeding never touches the network whatever DEMO_MODE says.
    Idempotent: a no-op once the demo bets exist, and it refuses (with a warning) to seed
    a database that already holds real bets.
    """
    from app.services import bets as bets_service
    from app.services.prefs import get_prefs
    from app.services.scan import run_scan

    if demo_rows_present(session):
        return
    real = _real_bet_count(session)
    if real:
        log.warning("demo seed skipped: %d real bet(s) in the ledger", real)
        return
    settings = settings or get_settings()
    transport = build_demo_transport(settings)
    polymarket = PolymarketClient(transport)
    _seed_orioles_bet(session, polymarket)

    result = run_scan(
        session,
        polymarket=polymarket,
        oddsapi=OddsApiClient(transport, DEMO_API_KEY),
        espn=EspnClient(transport),
        prefs=get_prefs(session),
        kind="both",
        leagues=list(LEAGUES),
        now=DEMO_NOW,
    )
    log.info(
        "demo scan #%s: %d markets, %d matched, %d opps",
        result.scan_id,
        result.n_markets,
        result.n_matched,
        result.n_opps,
    )

    chiefs = session.scalars(
        select(Opportunity)
        .where(
            Opportunity.scan_id == result.scan_id,
            Opportunity.market_id == CHIEFS_MARKET_ID,
            Opportunity.outcome_key == "KC",
        )
        .order_by(Opportunity.id.asc())
    ).first()
    if chiefs is None:
        log.warning("demo: no Chiefs moneyline opportunity in scan #%s", result.scan_id)
        return
    bet = bets_service.create_bet(
        session, chiefs.id, chiefs.suggested_stake, chiefs.ask, "taker", notes=DEMO_NOTE
    )
    bet.placed_at = DEMO_NOW - timedelta(hours=1)
    session.commit()


def clear_demo(session: Session) -> dict[str, int]:
    """Delete the synthetic slate and demo bets; returns rows removed per table.

    Refuses (ValueError) when any non-demo bet exists, because real bets reference
    markets and scans that would otherwise be deleted. Preferences are kept.
    """
    real = _real_bet_count(session)
    if real:
        raise ValueError(
            f"refusing to clear: {real} real bet(s) in the ledger; delete them by hand first"
        )
    removed: dict[str, int] = {}
    for model in (Bet, Opportunity, PmQuote, BookQuote, Market, Game, Scan):
        rows = list(session.scalars(select(model)))
        for row in rows:
            session.delete(row)
        removed[model.__tablename__] = len(rows)
        session.flush()
    session.commit()
    return removed


__all__ = [
    "DEMO_API_KEY",
    "DEMO_NOTE",
    "DEMO_NOW",
    "DEMO_ODDS_HEADERS",
    "build_demo_transport",
    "clear_demo",
    "demo_routes",
    "demo_rows_present",
    "seed_demo",
]
