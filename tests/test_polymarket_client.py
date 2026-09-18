"""PolymarketClient against FixtureTransport: slate parsing, paging, books, settlement lookups.

Team keys come from a dict-backed resolver so nothing here depends on `app.core.matching`
(the shared `parse_spread` resolves the spread team itself; the client reconciles its answer
with the injected resolver's outcome keys).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.clients.polymarket import (
    BOOKS_CHUNK_SIZE,
    CLOB,
    EVENTS_PAGE_SIZE,
    GAMMA,
    TEAMS_PAGE_SIZE,
    PolymarketClient,
    PolymarketError,
    parse_iso_utc,
)
from app.clients.transport import FixtureTransport, Route, TransportError, load_fixture
from app.core.types import LEAGUES, PmMarket
from tests.conftest import FIXED_NOW, FIXTURES_DIR

# --------------------------------------------------------------------------- resolver

_TEAM_KEYS: dict[str, dict[str, str]] = {
    "nfl": {
        "chiefs": "KC",
        "kansas city chiefs": "KC",
        "bills": "BUF",
        "buffalo bills": "BUF",
        "cowboys": "DAL",
        "dallas cowboys": "DAL",
        "eagles": "PHI",
        "philadelphia eagles": "PHI",
    },
    "nba": {
        "lakers": "LAL",
        "los angeles lakers": "LAL",
        "celtics": "BOS",
        "boston celtics": "BOS",
        "warriors": "GSW",
        "golden state warriors": "GSW",
        "nuggets": "DEN",
        "denver nuggets": "DEN",
    },
    "mlb": {
        "yankees": "NYY",
        "new york yankees": "NYY",
        "dodgers": "LAD",
        "los angeles dodgers": "LAD",
        "athletics": "ATH",
        "mariners": "SEA",
        "seattle mariners": "SEA",
        "orioles": "BAL",
        "baltimore orioles": "BAL",
        "blue jays": "TOR",
        "toronto blue jays": "TOR",
    },
}


def resolve(name: str, league: str) -> str | None:
    return _TEAM_KEYS.get(league, {}).get(name.strip().lower())


# --------------------------------------------------------------------------- routes


def _books_route(url: str, params: Any, body: Any) -> tuple[Any, dict]:
    """Serve clob_books.json (keyed by token id) as the list shape the live POST /books returns."""
    books = load_fixture("clob_books.json", FIXTURES_DIR)
    return [books[item["token_id"]] for item in body if item["token_id"] in books], {}


def _market_route(url: str, params: Any, body: Any) -> tuple[Any, dict]:
    """GET /markets/{id}: the market dict from the events fixtures plus its event under `events`."""
    market_id = url.rsplit("/", 1)[1]
    for league in LEAGUES:
        for event in load_fixture(f"gamma_events_{league}.json", FIXTURES_DIR):
            for market in event["markets"]:
                if market["id"] == market_id:
                    payload = dict(market)
                    payload["events"] = [{k: v for k, v in event.items() if k != "markets"}]
                    return payload, {}
    raise TransportError(f"no market {market_id}", status=404, url=url)


def _routes(*, teams: bool = True) -> list[Route]:
    routes: list[Route] = [
        ("GET", f"{GAMMA}/events?tag_slug={lg}", f"gamma_events_{lg}.json") for lg in LEAGUES
    ]
    if teams:
        routes += [
            ("GET", f"{GAMMA}/teams?league={lg}", f"gamma_teams_{lg}.json") for lg in LEAGUES
        ]
    routes.append(("POST", f"{CLOB}/books", _books_route))
    routes.append(("GET", f"{GAMMA}/markets/", _market_route))
    return routes


def _client(routes: Sequence[Route] | None = None) -> tuple[PolymarketClient, FixtureTransport]:
    transport = FixtureTransport(FIXTURES_DIR, list(_routes() if routes is None else routes))
    return PolymarketClient(transport, team_resolver=resolve), transport


def _by_id(markets: Sequence[PmMarket]) -> dict[str, PmMarket]:
    return {m.market_id: m for m in markets}


def _fixture_market(league: str, market_id: str) -> dict:
    for event in load_fixture(f"gamma_events_{league}.json", FIXTURES_DIR):
        for market in event["markets"]:
            if market["id"] == market_id:
                return market
    raise KeyError(market_id)


def _tokens(league: str, market_id: str) -> list[str]:
    import json

    return json.loads(_fixture_market(league, market_id)["clobTokenIds"])


def _raw_market(market_id: str, question: str, smt: str | None, outcomes: Sequence[str], **kw):
    import json

    raw: dict[str, Any] = {
        "id": market_id,
        "question": question,
        "conditionId": "0x" + "ab" * 32,
        "slug": f"synthetic-{market_id}",
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([f"{market_id}{'1' * 70}", f"{market_id}{'2' * 70}"]),
        "sportsMarketType": smt,
        "line": None,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "liquidityNum": 1000.0,
        "volumeNum": 2000.0,
        "bestBid": 0.49,
        "bestAsk": 0.51,
        "gameStartTime": "2026-09-20T20:25:00Z",
        "takerBaseFee": 0,
    }
    raw.update(kw)
    return raw


def _event(event_id: str, slug: str, markets: Sequence[dict], **kw) -> dict:
    event: dict[str, Any] = {
        "id": event_id,
        "slug": slug,
        "title": slug,
        "startDate": "2026-09-20T20:25:00Z",
        "active": True,
        "closed": False,
        "tags": [{"id": "1", "slug": "sports"}],
        "markets": list(markets),
    }
    event.update(kw)
    return event


def _static(payload: Any):
    def handler(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        return payload, {}

    return handler


# --------------------------------------------------------------------------- the slate

# market id -> (best ask outcome 0, best ask outcome 1) from docs/FIXTURES.md
EXPECTED_ASKS: dict[str, dict[str, tuple[float, float]]] = {
    "nfl": {
        "500101": (0.55, 0.46),
        "500102": (0.50, 0.52),
        "500103": (0.49, 0.53),
        "500201": (0.34, 0.66),
        "500202": (0.51, 0.51),
        "500203": (0.50, 0.50),
    },
    "nba": {
        "500301": (0.31, 0.66),
        "500302": (0.50, 0.52),
        "500303": (0.50, 0.51),
        "500401": (0.48, 0.55),
        "500402": (0.48, 0.54),
        "500403": (0.47, 0.49),
    },
    "mlb": {
        "500501": (0.40, 0.58),
        "500502": (0.44, 0.58),
        "500503": (0.50, 0.52),
        "500601": (0.41, 0.62),
        "500602": (0.55, 0.45),
    },
}


def test_nfl_slate_markets_and_unparseable() -> None:
    client, transport = _client()
    markets, unparseable = client.events("nfl")

    assert [m.market_id for m in markets] == [
        "500101",
        "500102",
        "500103",
        "500201",
        "500202",
        "500203",
    ]
    assert unparseable == [
        {
            "market_id": "500104",
            "question": "Chiefs starting QB 250+ passing yards?",
            "reason": "unsupported market type 'player_props'",
        },
        {
            "market_id": "500105",
            "question": "Will the game go to overtime?",
            "reason": "unsupported market type None",
        },
    ]

    ml = _by_id(markets)["500101"]
    assert ml.market_type == "moneyline"
    assert ml.league == "nfl"
    assert ml.event_id == "10001"
    assert ml.event_slug == "nfl-kc-buf-2026-09-20"
    assert ml.event_title == "Chiefs vs. Bills"
    assert ml.slug == "nfl-kc-buf-2026-09-20-moneyline"
    assert ml.condition_id.startswith("0x") and len(ml.condition_id) == 66
    assert ml.game_start == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert ml.game_start.tzinfo is not None
    assert (ml.away_team_key, ml.home_team_key) == ("KC", "BUF")
    assert [o.name for o in ml.outcomes] == ["Chiefs", "Bills"]
    assert [o.team_key for o in ml.outcomes] == ["KC", "BUF"]
    assert [o.token_id for o in ml.outcomes] == _tokens("nfl", "500101")
    assert all(len(o.token_id) >= 70 and o.token_id.isdigit() for o in ml.outcomes)
    assert [o.last_price for o in ml.outcomes] == [0.545, 0.455]
    # Gamma bestBid/bestAsk are outcome 0's; outcome 1 is the mirrored book.
    assert (ml.outcomes[0].best_bid, ml.outcomes[0].best_ask) == (0.54, 0.55)
    assert (ml.outcomes[1].best_bid, ml.outcomes[1].best_ask) == (0.45, 0.46)
    assert ml.line is None and ml.line_team_key is None
    assert ml.accepting_orders is True and ml.closed is False
    assert ml.resolved_outcome_index is None
    assert ml.tick_size == 0.01 and ml.min_order_size == 5.0
    assert ml.liquidity == 32718.0 and ml.volume is not None and ml.volume > 0
    assert ml.taker_fee_rate is None  # takerBaseFee 0 -> no per-market override

    spread = _by_id(markets)["500102"]
    assert spread.market_type == "spread"
    assert (spread.line, spread.line_team_key) == (-3.5, "KC")
    assert [o.team_key for o in spread.outcomes] == ["KC", "BUF"]
    assert (spread.away_team_key, spread.home_team_key) == ("KC", "BUF")

    total = _by_id(markets)["500103"]
    assert total.market_type == "total"
    assert total.line == 47.5 and total.line_team_key is None
    assert [o.name for o in total.outcomes] == ["Over", "Under"]
    assert [o.team_key for o in total.outcomes] == [None, None]
    assert (total.away_team_key, total.home_team_key) == ("KC", "BUF")  # from the event
    assert total.taker_fee_rate == 0.05  # takerBaseFee 500 bps

    phi = _by_id(markets)["500202"]
    assert (phi.line, phi.line_team_key) == (-4.5, "PHI")
    assert (phi.away_team_key, phi.home_team_key) == ("DAL", "PHI")
    assert phi.game_start == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    assert _by_id(markets)["500203"].line == 44.5

    # one events page and one /teams lookup for the league, nothing else
    urls = [c["url"] for c in transport.calls]
    assert urls.count(f"{GAMMA}/events?tag_slug=nfl") == 1
    assert urls.count(f"{GAMMA}/teams?league=nfl") == 1
    assert len(urls) == 2


def test_nba_slate_including_den_spread_at_minus_3_5() -> None:
    client, _ = _client()
    markets, unparseable = client.events("nba")
    assert [m.market_id for m in markets] == [
        "500301",
        "500302",
        "500303",
        "500401",
        "500402",
        "500403",
    ]
    assert [(u["market_id"], u["reason"]) for u in unparseable] == [
        ("500304", "unsupported market type None"),
        ("500404", "unsupported market type 'player_props'"),
    ]
    by_id = _by_id(markets)
    lal = by_id["500301"]
    assert (lal.away_team_key, lal.home_team_key) == ("LAL", "BOS")
    assert lal.game_start == datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
    assert (by_id["500302"].line, by_id["500302"].line_team_key) == (-6.5, "BOS")
    assert by_id["500303"].line == 224.5
    den = by_id["500402"]
    assert (den.line, den.line_team_key) == (-3.5, "DEN")  # deliberate mismatch with books' -2.5
    assert (den.away_team_key, den.home_team_key) == ("GSW", "DEN")
    assert den.game_start == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert by_id["500403"].line == 231.5
    assert all(m.league == "nba" for m in markets)


def test_mlb_slate_excludes_closed_event_unless_asked() -> None:
    client, _ = _client()
    markets, unparseable = client.events("mlb")
    assert [m.market_id for m in markets] == ["500501", "500502", "500503", "500601", "500602"]
    assert [(u["market_id"], u["reason"]) for u in unparseable] == [
        ("500504", "unsupported market type 'player_props'"),
        ("500603", "unsupported market type None"),
    ]
    by_id = _by_id(markets)
    assert (by_id["500501"].away_team_key, by_id["500501"].home_team_key) == ("NYY", "LAD")
    assert by_id["500501"].game_start == datetime(2026, 9, 20, 2, 10, tzinfo=UTC)
    assert (by_id["500502"].line, by_id["500502"].line_team_key) == (-1.5, "LAD")
    assert by_id["500503"].line == 8.5
    ath = by_id["500601"]
    assert (ath.away_team_key, ath.home_team_key) == ("ATH", "SEA")
    assert ath.game_start == datetime(2026, 9, 20, 1, 40, tzinfo=UTC)
    assert by_id["500602"].line == 7.5
    assert (by_id["500602"].away_team_key, by_id["500602"].home_team_key) == ("ATH", "SEA")

    markets_all, unparseable_all = client.events("mlb", include_closed=True)
    assert [m.market_id for m in markets_all] == [
        "500501",
        "500502",
        "500503",
        "500601",
        "500602",
        "500701",
    ]
    assert len(unparseable_all) == 2
    bal = _by_id(markets_all)["500701"]
    assert bal.closed is True and bal.accepting_orders is False
    assert bal.resolved_outcome_index == 1  # Blue Jays won; the Orioles bet loses
    assert [o.last_price for o in bal.outcomes] == [0.0, 1.0]
    assert (bal.away_team_key, bal.home_team_key) == ("BAL", "TOR")
    assert bal.game_start == datetime(2026, 9, 17, 23, 7, tzinfo=UTC)


@pytest.mark.parametrize("league", LEAGUES)
def test_order_books_best_asks_match_the_slate(league: str) -> None:
    client, _ = _client()
    markets, _ = client.events(league)
    tokens = [o.token_id for m in markets for o in m.outcomes]
    books = client.order_books(tokens)
    assert set(books) == set(tokens)
    for market in markets:
        expected = EXPECTED_ASKS[league][market.market_id]
        got = tuple(books[o.token_id].best_ask for o in market.outcomes)
        assert got == expected, (market.market_id, got, expected)
        for outcome in market.outcomes:
            book = books[outcome.token_id]
            assert book.best_bid is not None and book.best_bid < book.best_ask
            assert len(book.asks) == 3 and len(book.bids) == 3
            assert all(100 <= lvl.size <= 2000 for lvl in book.asks + book.bids)
    assert set(EXPECTED_ASKS[league]) == {m.market_id for m in markets}


# --------------------------------------------------------------------------- events: requests


def test_events_params_and_offset_pagination() -> None:
    def page(offset: int, count: int) -> list[dict]:
        return [
            _event(
                f"1{offset + i:04d}",
                f"nfl-kc-buf-2026-09-{(i % 28) + 1:02d}",
                [
                    _raw_market(
                        f"5{offset + i:05d}", "Chiefs vs. Bills", "moneyline", ["Chiefs", "Bills"]
                    )
                ],
            )
            for i in range(count)
        ]

    def handler(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        assert url == f"{GAMMA}/events?tag_slug=nfl"
        assert params["tag_slug"] == "nfl"
        assert params["active"] == "true"
        assert params["closed"] == "false"
        assert params["limit"] == EVENTS_PAGE_SIZE == 100
        return (page(0, 100) if params["offset"] == 0 else page(100, 3)), {}

    client, transport = _client([("GET", f"{GAMMA}/events", handler)])
    markets, unparseable = client.events("nfl")
    assert len(markets) == 103 and unparseable == []
    assert len({m.market_id for m in markets}) == 103
    assert [c["params"]["offset"] for c in transport.calls] == [0, 100]
    assert all(m.home_team_key == "BUF" and m.away_team_key == "KC" for m in markets)


def test_events_include_closed_drops_the_closed_filter() -> None:
    seen: list[dict] = []

    def handler(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        seen.append(dict(params))
        return [], {}

    client, _ = _client([("GET", f"{GAMMA}/events", handler)])
    assert client.events("nba", include_closed=True) == ([], [])
    assert client.events("nba") == ([], [])
    assert "closed" not in seen[0] and seen[0]["active"] == "true"
    assert seen[1]["closed"] == "false"


def test_events_wraps_transport_error_with_url() -> None:
    client, _ = _client([])
    with pytest.raises(PolymarketError) as excinfo:
        client.events("nfl")
    assert "events fetch failed" in str(excinfo.value)
    assert f"{GAMMA}/events?tag_slug=nfl" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, TransportError)


def test_events_rejects_unknown_league_and_bad_payload() -> None:
    client, _ = _client()
    with pytest.raises(PolymarketError, match="unknown league"):
        client.events("nhl")  # type: ignore[arg-type]
    client, _ = _client([("GET", f"{GAMMA}/events", _static("not a list"))])
    with pytest.raises(PolymarketError, match="unexpected nfl events payload"):
        client.events("nfl")


def test_events_skips_malformed_markets_with_reasons() -> None:
    three = _raw_market("600001", "Chiefs vs. Bills", "moneyline", ["Chiefs", "Bills", "Tie"])
    no_tokens = _raw_market("600002", "Chiefs vs. Bills", "moneyline", ["Chiefs", "Bills"])
    no_tokens["clobTokenIds"] = "[]"
    bad_spread = _raw_market("600003", "Spread", "spreads", ["Chiefs", "Bills"], line=None)
    bad_total = _raw_market("600004", "Total", "totals", ["Over", "Under"], line=None)
    other_team = _raw_market("600005", "Spread: Eagles (-4.5)", "spreads", ["Chiefs", "Bills"])
    good = _raw_market("600006", "Chiefs vs. Bills", "moneyline", ["Chiefs", "Bills"])
    events = [
        _event(
            "10009",
            "nfl-kc-buf-2026-09-20",
            [three, no_tokens, bad_spread, bad_total, other_team, good],
        )
    ]
    client, _ = _client([("GET", f"{GAMMA}/events", _static(events))])
    markets, unparseable = client.events("nfl")
    assert [m.market_id for m in markets] == ["600006"]
    assert [(u["market_id"], u["reason"]) for u in unparseable] == [
        ("600001", "expected 2 outcomes, got 3"),
        ("600002", "expected 2 clobTokenIds, got 0"),
        ("600003", "spread line/team not parseable"),
        ("600004", "total line not parseable"),
        ("600005", "spread line/team not parseable"),
    ]
    assert all(set(u) == {"market_id", "question", "reason"} for u in unparseable)


# --------------------------------------------------------------------------- home / away


def test_home_away_prefers_team_ids_and_falls_back_to_outcomes(caplog) -> None:
    # with /teams routed the ids resolve: teamA (first) = away, teamB (second) = home
    client, transport = _client()
    markets, _ = client.events("nfl")
    assert {(m.away_team_key, m.home_team_key) for m in markets} == {("KC", "BUF"), ("DAL", "PHI")}
    assert [c["url"] for c in transport.calls].count(f"{GAMMA}/teams?league=nfl") == 1
    client.events("nfl")  # the id map is cached per client instance
    assert [c["url"] for c in transport.calls].count(f"{GAMMA}/teams?league=nfl") == 1

    # without /teams the lookup is logged (not swallowed) and the moneyline outcomes decide
    client, transport = _client(_routes(teams=False))
    with caplog.at_level(logging.WARNING, logger="app.clients.polymarket"):
        markets, _ = client.events("nfl")
    assert {(m.away_team_key, m.home_team_key) for m in markets} == {("KC", "BUF"), ("DAL", "PHI")}
    assert any("/teams unavailable" in rec.message for rec in caplog.records)
    assert [c["url"] for c in transport.calls].count(f"{GAMMA}/teams?league=nfl") == 1

    # an event without a moneyline market uses a spread market's outcomes; totals-only -> None
    spread_only = _event(
        "10010",
        "nfl-dal-phi-2026-09-20",
        [_raw_market("610001", "Spread: Eagles (-4.5)", "spreads", ["Cowboys", "Eagles"])],
    )
    totals_only = _event(
        "10011",
        "nfl-dal-phi-2026-09-21",
        [_raw_market("610002", "Cowboys vs. Eagles: O/U 44.5", "totals", ["Over", "Under"])],
    )
    client, _ = _client([("GET", f"{GAMMA}/events", _static([spread_only, totals_only]))])
    markets, unparseable = client.events("nfl")
    assert unparseable == []
    by_id = _by_id(markets)
    assert (by_id["610001"].away_team_key, by_id["610001"].home_team_key) == ("DAL", "PHI")
    assert (by_id["610002"].away_team_key, by_id["610002"].home_team_key) == (None, None)
    assert by_id["610002"].line == 44.5


def test_unknown_team_names_keep_the_market_with_none_keys() -> None:
    event = _event(
        "10012",
        "nfl-xxx-yyy-2026-09-20",
        [_raw_market("620001", "Gophers vs. Badgers", "moneyline", ["Gophers", "Badgers"])],
    )
    client, _ = _client([("GET", f"{GAMMA}/events", _static([event]))])
    markets, unparseable = client.events("nfl")
    assert unparseable == []
    (market,) = markets
    assert [o.team_key for o in market.outcomes] == [None, None]
    assert (market.home_team_key, market.away_team_key) == (None, None)


# --------------------------------------------------------------------------- field coercions


def test_numeric_fields_fee_guard_and_start_time_fallbacks() -> None:
    m1 = _raw_market(
        "630001",
        "Chiefs vs. Bills",
        "moneyline",
        ["Chiefs", "Bills"],
        takerBaseFee=5000,  # 50%: implausible -> ignored
        liquidityNum=None,
        liquidity="1234.5",
        volumeNum=None,
        volume="99.25",
        gameStartTime=None,  # -> event startDate
        orderPriceMinTickSize="0.001",
        orderMinSize="15",
        bestBid=None,
        bestAsk=None,
    )
    m2 = _raw_market(
        "630002",
        "Chiefs vs. Bills",
        "moneyline",
        ["Chiefs", "Bills"],
        takerBaseFee=200,
        gameStartTime="2026-09-21 01:05:00+00",  # Postgres-style timestamp
        outcomePrices='"[\\"0.7\\", \\"0.3\\"]"',  # double-encoded
        closed="true",
        acceptingOrders="false",
    )
    m3 = _raw_market(
        "630003",
        "Chiefs vs. Bills",
        "moneyline",
        ["Chiefs", "Bills"],
        outcomePrices='["1", "0"]',
        closed=True,
        acceptingOrders=False,
    )
    event = _event("10013", "nfl-kc-buf-2026-09-20", [m1, m2, m3], startDate="2026-09-20T20:25:00Z")
    client, _ = _client([("GET", f"{GAMMA}/events", _static([event]))])
    markets, unparseable = client.events("nfl", include_closed=True)
    assert unparseable == []
    by_id = _by_id(markets)

    a = by_id["630001"]
    assert a.taker_fee_rate is None
    assert a.liquidity == 1234.5 and a.volume == 99.25
    assert a.game_start == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert a.tick_size == 0.001 and a.min_order_size == 15.0
    assert a.outcomes[0].best_bid is None and a.outcomes[1].best_ask is None
    assert a.resolved_outcome_index is None

    b = by_id["630002"]
    assert b.taker_fee_rate == 0.02
    assert b.game_start == datetime(2026, 9, 21, 1, 5, tzinfo=UTC)
    assert [o.last_price for o in b.outcomes] == [0.7, 0.3]
    assert b.closed is True and b.accepting_orders is False
    assert b.resolved_outcome_index is None  # closed but prices not settled to 1/0

    assert by_id["630003"].resolved_outcome_index == 0


def test_parse_iso_utc_variants() -> None:
    expected = datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert parse_iso_utc("2026-09-20T20:25:00Z") == expected
    assert parse_iso_utc("2026-09-20T20:25:00.000Z") == expected
    assert parse_iso_utc("2026-09-20 20:25:00+00") == expected
    assert parse_iso_utc("2026-09-20T16:25:00-04:00") == expected
    assert parse_iso_utc("2026-09-20T20:25:00") == expected  # naive -> UTC
    assert parse_iso_utc("") is None
    assert parse_iso_utc(None) is None
    assert parse_iso_utc("not a date") is None
    assert parse_iso_utc(1758400000) is None


# --------------------------------------------------------------------------- market()


def test_market_closed_bal_tor_is_resolved_for_settlement() -> None:
    client, transport = _client()
    market = client.market("500701")
    assert market is not None
    assert market.market_id == "500701"
    assert market.league == "mlb"
    assert market.closed is True and market.accepting_orders is False
    assert market.resolved_outcome_index == 1
    assert [o.name for o in market.outcomes] == ["Orioles", "Blue Jays"]
    assert [o.team_key for o in market.outcomes] == ["BAL", "TOR"]
    assert [o.last_price for o in market.outcomes] == [0.0, 1.0]
    assert market.event_id == "10007"
    assert market.event_slug == "mlb-bal-tor-2026-09-17"
    assert market.event_title == "Orioles vs. Blue Jays"
    assert (market.away_team_key, market.home_team_key) == ("BAL", "TOR")
    assert market.game_start == datetime(2026, 9, 17, 23, 7, tzinfo=UTC)
    assert transport.calls[0]["url"] == f"{GAMMA}/markets/500701"

    open_market = client.market("500402")
    assert open_market is not None
    assert open_market.resolved_outcome_index is None
    assert (open_market.line, open_market.line_team_key) == (-3.5, "DEN")
    assert (open_market.away_team_key, open_market.home_team_key) == ("GSW", "DEN")


def test_market_without_event_leaves_event_fields_blank() -> None:
    raw = _fixture_market("mlb", "500701")  # no `events` key: league from the slug prefix
    client, _ = _client([("GET", f"{GAMMA}/markets/", _static(raw))])
    market = client.market("500701")
    assert market is not None
    assert market.league == "mlb"
    assert (market.event_id, market.event_slug, market.event_title) == ("", "", "")
    assert market.resolved_outcome_index == 1
    assert (market.away_team_key, market.home_team_key) == ("BAL", "TOR")  # from the outcomes

    # no slug prefix and no tags: the league remembered from events() is used
    bare = dict(raw, slug="orioles-blue-jays-moneyline")
    # routes match in order, so the override must precede the default /markets/ route
    client, _ = _client([("GET", f"{GAMMA}/markets/", _static(bare)), *_routes()])
    with pytest.raises(PolymarketError, match="cannot determine league"):
        client.market("500701")
    client.events("mlb", include_closed=True)
    remembered = client.market("500701")
    assert remembered is not None and remembered.league == "mlb"


def test_market_not_found_returns_none_other_errors_raise() -> None:
    client, _ = _client()
    assert client.market("599999") is None

    def boom(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        raise TransportError("upstream down", status=503, url=url)

    client, _ = _client([("GET", f"{GAMMA}/markets/", boom)])
    with pytest.raises(PolymarketError, match="market 500701 fetch failed"):
        client.market("500701")

    client, _ = _client([("GET", f"{GAMMA}/markets/", _static([1, 2]))])
    with pytest.raises(PolymarketError, match="unexpected payload"):
        client.market("500701")


def test_market_unsupported_type_returns_none(caplog) -> None:
    client, _ = _client()
    with caplog.at_level(logging.WARNING, logger="app.clients.polymarket"):
        assert client.market("500104") is None  # player prop
    assert any("500104" in rec.message and "skipped" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- order books


def test_order_books_parse_sort_key_and_timestamp(monkeypatch) -> None:
    monkeypatch.setattr("app.clients.polymarket._utcnow", lambda: FIXED_NOW)
    client, transport = _client()
    tokens = _tokens("nfl", "500101")
    books = client.order_books(tokens)
    assert list(books) == tokens
    chiefs, bills = (books[t] for t in tokens)
    assert chiefs.token_id == tokens[0]
    assert chiefs.best_ask == 0.55 and chiefs.best_bid == 0.54
    assert bills.best_ask == 0.46 and bills.best_bid == 0.45
    assert [lvl.price for lvl in chiefs.asks] == [0.55, 0.57, 0.60]  # ascending, best first
    assert [lvl.price for lvl in chiefs.bids] == [0.54, 0.52, 0.49]  # descending, best first
    assert all(isinstance(lvl.price, float) and isinstance(lvl.size, float) for lvl in chiefs.asks)
    assert chiefs.tick_size == 0.01
    assert chiefs.fetched_at == FIXED_NOW and chiefs.fetched_at.tzinfo is not None
    (call,) = transport.calls
    assert call["method"] == "POST" and call["url"] == f"{CLOB}/books"
    assert call["body"] == [{"token_id": tokens[0]}, {"token_id": tokens[1]}]

    # a closed market has an empty book on both sides
    (bal_token, _) = _tokens("mlb", "500701")
    empty = client.order_books([bal_token])[bal_token]
    assert empty.bids == () and empty.asks == ()
    assert empty.best_bid is None and empty.best_ask is None


def test_order_books_chunks_at_200_and_uses_positional_fallback() -> None:
    tokens = [f"{i:03d}{'7' * 74}" for i in range(250)]

    def handler(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        # no asset_id in the reply: the client keys by request position
        return [
            {"bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.42", "size": "5"}]}
            for _ in body
        ], {}

    client, transport = _client([("POST", f"{CLOB}/books", handler)])
    books = client.order_books(tokens)
    assert len(books) == 250 and list(books) == tokens
    assert [len(c["body"]) for c in transport.calls] == [BOOKS_CHUNK_SIZE, 50] == [200, 50]
    assert transport.calls[1]["body"][0] == {"token_id": tokens[200]}
    assert books[tokens[249]].best_ask == 0.42 and books[tokens[249]].tick_size is None


def test_order_books_dedupes_skips_empty_and_accepts_dict_payload() -> None:
    client, transport = _client()
    assert client.order_books([]) == {}
    assert client.order_books(["", ""]) == {}
    assert transport.calls == []
    tokens = _tokens("nba", "500301")
    books = client.order_books([tokens[0], tokens[0], tokens[1]])
    assert list(books) == tokens
    assert transport.calls[-1]["body"] == [{"token_id": tokens[0]}, {"token_id": tokens[1]}]

    # the fixture file itself (an object keyed by token id) is also accepted and filtered
    client, _ = _client([("POST", f"{CLOB}/books", "clob_books.json")])
    books = client.order_books(tokens)
    assert set(books) == set(tokens)
    assert (books[tokens[0]].best_ask, books[tokens[1]].best_ask) == (0.31, 0.66)


def test_order_books_errors_are_wrapped() -> None:
    client, _ = _client([])
    with pytest.raises(PolymarketError, match="order books fetch failed"):
        client.order_books(["1" * 70])
    client, _ = _client([("POST", f"{CLOB}/books", _static("nope"))])
    with pytest.raises(PolymarketError, match="unexpected CLOB /books payload"):
        client.order_books(["1" * 70])


# --------------------------------------------------------------------------- teams


@pytest.mark.parametrize("league", LEAGUES)
def test_teams_fixture_shape(league: str) -> None:
    client, transport = _client()
    teams = client.teams(league)
    assert 6 <= len(teams) <= 8
    for team in teams:
        assert {"id", "name", "league", "abbreviation", "alias", "record"} <= set(team)
        assert team["league"] == league
    (call,) = transport.calls
    assert call["url"] == f"{GAMMA}/teams?league={league}"
    assert call["params"] == {"league": league, "limit": TEAMS_PAGE_SIZE, "offset": 0}


def test_teams_paginates_and_wraps_errors() -> None:
    def handler(url: str, params: Any, body: Any) -> tuple[Any, dict]:
        offset = params["offset"]
        count = TEAMS_PAGE_SIZE if offset == 0 else 2
        return [
            {"id": offset + i, "name": f"Team {offset + i}", "league": "nfl"} for i in range(count)
        ], {}

    client, transport = _client([("GET", f"{GAMMA}/teams", handler)])
    teams = client.teams("nfl")
    assert len(teams) == TEAMS_PAGE_SIZE + 2
    assert [c["params"]["offset"] for c in transport.calls] == [0, TEAMS_PAGE_SIZE]

    client, _ = _client([])
    with pytest.raises(PolymarketError, match="teams fetch failed"):
        client.teams("nfl")
    with pytest.raises(PolymarketError, match="unknown league"):
        client.teams("xfl")  # type: ignore[arg-type]
