"""OddsApiClient against the synthetic Odds API fixtures (docs/FIXTURES.md).

A dict-backed team resolver is injected everywhere so nothing here depends on
app.core.matching.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import pytest

from app.clients import oddsapi
from app.clients.oddsapi import (
    SPORT_KEYS,
    OddsApiClient,
    OddsApiError,
    parse_iso_utc,
    parse_quota,
    to_american,
)
from app.clients.transport import FixtureTransport, TransportError
from app.core.types import BookGame, QuotaInfo
from tests.conftest import FIXTURES_DIR

BASE = "https://api.the-odds-api.com/v4"
API_KEY = "sekrit-key-0123456789abcdef"
BOOKS = ("pinnacle", "betonlineag", "lowvig", "draftkings", "fanduel")
QUOTA_HEADERS = {"x-requests-remaining": "497", "x-requests-used": "3", "x-requests-last": "3"}

TEAM_TABLE: dict[str, dict[str, str]] = {
    "nfl": {
        "Buffalo Bills": "BUF",
        "Kansas City Chiefs": "KC",
        "Philadelphia Eagles": "PHI",
        "Dallas Cowboys": "DAL",
    },
    "nba": {
        "Boston Celtics": "BOS",
        "Los Angeles Lakers": "LAL",
        "Denver Nuggets": "DEN",
        "Golden State Warriors": "GSW",
    },
    "mlb": {
        "Los Angeles Dodgers": "LAD",
        "New York Yankees": "NYY",
        "Seattle Mariners": "SEA",
        "Athletics": "ATH",
        "Toronto Blue Jays": "TOR",
        "Baltimore Orioles": "BAL",
    },
}


def dict_resolver(name: str, league: str) -> str | None:
    return TEAM_TABLE.get(league, {}).get(name)


def make_transport(routes=None, headers=None) -> FixtureTransport:
    if routes is None:
        routes = [
            ("GET", f"{BASE}/sports/americanfootball_nfl/odds", "oddsapi_nfl.json"),
            ("GET", f"{BASE}/sports/basketball_nba/odds", "oddsapi_nba.json"),
            ("GET", f"{BASE}/sports/baseball_mlb/odds", "oddsapi_mlb.json"),
        ]
    if headers is None:
        headers = QUOTA_HEADERS
    return FixtureTransport(FIXTURES_DIR, routes, default_headers=headers)


def make_client(transport=None, resolver=dict_resolver, api_key: str = API_KEY) -> OddsApiClient:
    return OddsApiClient(transport or make_transport(), api_key, base=BASE, team_resolver=resolver)


def by_home(games: list[BookGame]) -> dict[str, BookGame]:
    return {g.home_team_key: g for g in games}


def market(game: BookGame, bookmaker: str, key: str):
    (quote,) = [b for b in game.books if b.bookmaker == bookmaker]
    (mkt,) = [m for m in quote.markets if m.key == key]
    return mkt


def outcome(game: BookGame, bookmaker: str, key: str, name: str):
    (out,) = [o for o in market(game, bookmaker, key).outcomes if o.name == name]
    return out


# --------------------------------------------------------------------------- request


def test_odds_builds_url_and_params() -> None:
    transport = make_transport()
    client = make_client(transport)
    client.odds("nfl", BOOKS)
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{BASE}/sports/americanfootball_nfl/odds"
    assert call["params"] == {
        "apiKey": API_KEY,
        "bookmakers": "pinnacle,betonlineag,lowvig,draftkings,fanduel",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }


def test_odds_custom_markets_and_sport_keys() -> None:
    transport = make_transport()
    client = make_client(transport)
    client.odds("nba", ["pinnacle"], markets=("h2h",))
    client.odds("mlb", ["pinnacle", " draftkings "], markets=["totals", "spreads"])
    assert transport.calls[0]["url"] == f"{BASE}/sports/{SPORT_KEYS['nba']}/odds"
    assert transport.calls[0]["params"]["markets"] == "h2h"
    assert transport.calls[1]["url"] == f"{BASE}/sports/{SPORT_KEYS['mlb']}/odds"
    assert transport.calls[1]["params"]["markets"] == "totals,spreads"
    assert transport.calls[1]["params"]["bookmakers"] == "pinnacle,draftkings"


def test_odds_rejects_bad_inputs_without_calling_the_network() -> None:
    transport = make_transport()
    client = make_client(transport)
    with pytest.raises(ValueError):
        client.odds("nhl", BOOKS)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        client.odds("nfl", [])
    with pytest.raises(ValueError):
        client.odds("nfl", BOOKS, markets=())
    assert transport.calls == []


def test_base_trailing_slash_is_normalized() -> None:
    transport = make_transport()
    client = OddsApiClient(transport, API_KEY, base=BASE + "/", team_resolver=dict_resolver)
    client.odds("nfl", BOOKS)
    assert transport.calls[0]["url"] == f"{BASE}/sports/americanfootball_nfl/odds"


# --------------------------------------------------------------------------- nfl fixture


def test_nfl_fixture_games_and_teams() -> None:
    games, quota = make_client().odds("nfl", BOOKS)
    assert quota == QuotaInfo(remaining=497, used=3, last_cost=3)
    assert len(games) == 2
    assert all(re.fullmatch(r"[0-9a-f]{32}", g.game_id) for g in games)
    assert len({g.game_id for g in games}) == 2
    g = by_home(games)
    kc_buf, dal_phi = g["BUF"], g["PHI"]

    assert kc_buf.league == "nfl"
    assert kc_buf.away_team_key == "KC"
    assert kc_buf.home_team_name == "Buffalo Bills"
    assert kc_buf.away_team_name == "Kansas City Chiefs"
    assert kc_buf.commence_time == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert kc_buf.commence_time.tzinfo is not None

    assert dal_phi.away_team_key == "DAL"
    assert dal_phi.commence_time == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


def test_nfl_fixture_pinnacle_prices_and_points() -> None:
    games, _ = make_client().odds("nfl", BOOKS)
    kc_buf = by_home(games)["BUF"]

    kc = outcome(kc_buf, "pinnacle", "h2h", "Kansas City Chiefs")
    buf = outcome(kc_buf, "pinnacle", "h2h", "Buffalo Bills")
    assert (kc.price_american, kc.point, kc.team_key) == (-150, None, "KC")
    assert (buf.price_american, buf.point, buf.team_key) == (130, None, "BUF")

    kc_sp = outcome(kc_buf, "pinnacle", "spreads", "Kansas City Chiefs")
    buf_sp = outcome(kc_buf, "pinnacle", "spreads", "Buffalo Bills")
    assert (kc_sp.price_american, kc_sp.point) == (-110, -3.5)
    assert (buf_sp.price_american, buf_sp.point) == (-110, 3.5)

    over = outcome(kc_buf, "pinnacle", "totals", "Over")
    under = outcome(kc_buf, "pinnacle", "totals", "Under")
    assert (over.price_american, over.point, over.team_key) == (-110, 47.5, None)
    assert (under.price_american, under.point, under.team_key) == (-110, 47.5, None)

    dal_phi = by_home(games)["PHI"]
    assert outcome(dal_phi, "pinnacle", "h2h", "Philadelphia Eagles").price_american == -200
    assert outcome(dal_phi, "pinnacle", "h2h", "Dallas Cowboys").price_american == 170
    assert outcome(dal_phi, "pinnacle", "spreads", "Philadelphia Eagles").point == -4.5
    assert outcome(dal_phi, "pinnacle", "spreads", "Dallas Cowboys").point == 4.5
    assert outcome(dal_phi, "pinnacle", "totals", "Over").point == 44.5


def test_nfl_fixture_last_update_is_tz_aware() -> None:
    games, _ = make_client().odds("nfl", BOOKS)
    mkt = market(games[0], "pinnacle", "h2h")
    assert mkt.last_update is not None
    assert mkt.last_update.tzinfo is not None
    assert mkt.last_update == datetime(2026, 9, 19, 14, 55, tzinfo=UTC)


# --------------------------------------------------------------------------- nba fixture


def test_nba_fixture_prices_and_lines() -> None:
    games, _ = make_client().odds("nba", BOOKS)
    assert len(games) == 2
    g = by_home(games)
    lal_bos, gsw_den = g["BOS"], g["DEN"]

    assert lal_bos.away_team_key == "LAL"
    assert lal_bos.commence_time == datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
    assert outcome(lal_bos, "pinnacle", "h2h", "Boston Celtics").price_american == -240
    assert outcome(lal_bos, "pinnacle", "h2h", "Los Angeles Lakers").price_american == 195
    assert outcome(lal_bos, "pinnacle", "spreads", "Boston Celtics").point == -6.5
    assert outcome(lal_bos, "pinnacle", "spreads", "Los Angeles Lakers").point == 6.5
    assert outcome(lal_bos, "pinnacle", "totals", "Under").point == 224.5

    assert gsw_den.away_team_key == "GSW"
    assert gsw_den.commence_time == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    assert outcome(gsw_den, "pinnacle", "h2h", "Denver Nuggets").price_american == -130
    assert outcome(gsw_den, "pinnacle", "h2h", "Golden State Warriors").price_american == 110
    assert outcome(gsw_den, "pinnacle", "spreads", "Denver Nuggets").point == -2.5
    over = outcome(gsw_den, "pinnacle", "totals", "Over")
    under = outcome(gsw_den, "pinnacle", "totals", "Under")
    assert (over.price_american, over.point) == (-105, 231.5)
    assert (under.price_american, under.point) == (-115, 231.5)


# --------------------------------------------------------------------------- mlb fixture


def test_mlb_fixture_prices_lines_and_no_finished_game() -> None:
    games, _ = make_client().odds("mlb", BOOKS)
    assert len(games) == 2
    keys = {(g.away_team_key, g.home_team_key) for g in games}
    assert keys == {("NYY", "LAD"), ("ATH", "SEA")}
    assert not any("TOR" in (g.home_team_key, g.away_team_key) for g in games)
    assert not any("Blue Jays" in g.home_team_name or "Orioles" in g.away_team_name for g in games)

    g = by_home(games)
    nyy_lad, ath_sea = g["LAD"], g["SEA"]
    assert nyy_lad.commence_time == datetime(2026, 9, 20, 2, 10, tzinfo=UTC)
    assert outcome(nyy_lad, "pinnacle", "h2h", "Los Angeles Dodgers").price_american == -140
    assert outcome(nyy_lad, "pinnacle", "h2h", "New York Yankees").price_american == 120
    lad_rl = outcome(nyy_lad, "pinnacle", "spreads", "Los Angeles Dodgers")
    nyy_rl = outcome(nyy_lad, "pinnacle", "spreads", "New York Yankees")
    assert (lad_rl.price_american, lad_rl.point) == (120, -1.5)
    assert (nyy_rl.price_american, nyy_rl.point) == (-140, 1.5)
    assert outcome(nyy_lad, "pinnacle", "totals", "Over").point == 8.5

    assert ath_sea.away_team_name == "Athletics"
    assert ath_sea.commence_time == datetime(2026, 9, 20, 1, 40, tzinfo=UTC)
    assert outcome(ath_sea, "pinnacle", "h2h", "Seattle Mariners").price_american == -165
    assert outcome(ath_sea, "pinnacle", "h2h", "Athletics").price_american == 140
    over = outcome(ath_sea, "pinnacle", "totals", "Over")
    assert (over.price_american, over.point) == (-110, 7.5)
    assert outcome(ath_sea, "pinnacle", "totals", "Under").price_american == -110
    # the FIXTURES table lists no run line for game 6
    assert [m.key for m in ath_sea.books[0].markets] == ["h2h", "totals"]


# --------------------------------------------------------------------------- fixture invariants


@pytest.mark.parametrize("league", ["nfl", "nba", "mlb"])
def test_every_game_has_all_five_books_with_consistent_lines(league: str) -> None:
    games, _ = make_client().odds(league, BOOKS)
    assert games, league
    for game in games:
        assert [b.bookmaker for b in game.books] == list(BOOKS)
        assert game.home_team_key and game.away_team_key
        pinnacle = game.books[0]
        pinnacle_markets = {m.key: m for m in pinnacle.markets}
        for quote in game.books:
            assert quote.title
            assert {m.key for m in quote.markets} == set(pinnacle_markets)
            for mkt in quote.markets:
                assert len(mkt.outcomes) == 2
                ref = {o.name: o for o in pinnacle_markets[mkt.key].outcomes}
                for out in mkt.outcomes:
                    # same line as pinnacle; price within +-5 cents of pinnacle
                    assert out.point == ref[out.name].point, (game.game_id, mkt.key, out.name)
                    assert abs(out.price_american - ref[out.name].price_american) <= 5
                    assert abs(out.price_american) >= 100
                    if mkt.key == "h2h":
                        assert out.point is None
                        assert out.team_key in (game.home_team_key, game.away_team_key)
                    elif mkt.key == "spreads":
                        assert out.point is not None
                        assert out.team_key in (game.home_team_key, game.away_team_key)
                    else:
                        assert out.name in ("Over", "Under")
                        assert out.point is not None and out.point > 0
                        assert out.team_key is None
                if mkt.key == "spreads":
                    a, b = mkt.outcomes
                    assert a.point == -b.point
                if mkt.key == "totals":
                    a, b = mkt.outcomes
                    assert a.point == b.point


@pytest.mark.parametrize("league", ["nfl", "nba", "mlb"])
def test_fixture_files_contain_no_secrets_and_use_iso_dates(league: str) -> None:
    raw = (FIXTURES_DIR / f"oddsapi_{league}.json").read_text(encoding="utf-8")
    assert "apiKey" not in raw and "api_key" not in raw
    payload = json.loads(raw)
    assert isinstance(payload, list)
    for game in payload:
        assert game["sport_key"] == SPORT_KEYS[league]
        assert parse_iso_utc(game["commence_time"]) is not None
        assert len(game["bookmakers"]) == 5


# --------------------------------------------------------------------------- quota + cost


def test_quota_headers_missing_or_junk_become_none() -> None:
    games, quota = make_client(make_transport(headers={})).odds("nfl", BOOKS)
    assert games
    assert quota == QuotaInfo(None, None, None)

    junk = {"x-requests-remaining": "lots", "X-Requests-Used": "12", "x-requests-last": ""}
    _, quota = make_client(make_transport(headers=junk)).odds("nfl", BOOKS)
    assert quota == QuotaInfo(remaining=None, used=12, last_cost=None)


def test_parse_quota_is_case_insensitive() -> None:
    assert parse_quota({"X-Requests-Remaining": "10", "X-REQUESTS-USED": "490"}) == QuotaInfo(
        10, 490, None
    )
    assert parse_quota(None) == QuotaInfo(None, None, None)


@pytest.mark.parametrize(
    ("n_markets", "n_bookmakers", "expected"),
    [
        (3, 6, 3),
        (3, 11, 6),
        (1, 1, 1),
        (3, 10, 3),
        (3, 20, 6),
        (3, 21, 9),
        (3, 0, 3),  # at least one region
        (1, 0, 1),
        (0, 6, 0),
    ],
)
def test_estimate_cost_table(n_markets: int, n_bookmakers: int, expected: int) -> None:
    assert OddsApiClient.estimate_cost(n_markets, n_bookmakers) == expected


# --------------------------------------------------------------------------- secrecy


def test_api_key_never_appears_in_errors_or_repr() -> None:
    url = f"{BASE}/sports/americanfootball_nfl/odds"

    def leaky_route(url_, params, body):
        # a transport that (wrongly) echoes the full query string, like a naive HTTP lib
        raise TransportError(
            f"HTTP 401 for GET {url_}?apiKey={params['apiKey']}: bad key",
            status=401,
            url=f"{url_}?apiKey={params['apiKey']}",
        )

    client = make_client(make_transport([("GET", url, leaky_route)]))
    with pytest.raises(OddsApiError) as excinfo:
        client.odds("nfl", BOOKS)
    err = excinfo.value
    assert isinstance(err, TransportError)
    assert err.status == 401
    assert API_KEY not in str(err)
    assert API_KEY not in repr(err)
    assert API_KEY not in (err.url or "")
    assert "bad key" in str(err)
    assert "***" in str(err)
    assert API_KEY not in repr(client)
    assert "api_key=set" in repr(client)


def test_generic_transport_exception_is_wrapped_and_redacted() -> None:
    url = f"{BASE}/sports/americanfootball_nfl/odds"

    def exploding_route(url_, params, body):
        raise RuntimeError(f"socket blew up while sending apiKey={params['apiKey']}")

    client = make_client(make_transport([("GET", url, exploding_route)]))
    with pytest.raises(OddsApiError) as excinfo:
        client.odds("nfl", BOOKS)
    assert API_KEY not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_unknown_route_surfaces_as_odds_api_error() -> None:
    client = make_client(make_transport(routes=[]))
    with pytest.raises(OddsApiError) as excinfo:
        client.odds("mlb", BOOKS)
    assert excinfo.value.status == 404
    assert API_KEY not in str(excinfo.value)


def test_api_error_body_is_raised_not_parsed() -> None:
    url = f"{BASE}/sports/americanfootball_nfl/odds"

    def message_route(url_, params, body):
        return {"message": "Usage quota has been reached"}, {}

    client = make_client(make_transport([("GET", url, message_route)]))
    with pytest.raises(OddsApiError, match="quota"):
        client.odds("nfl", BOOKS)


def test_non_list_payload_raises() -> None:
    url = f"{BASE}/sports/americanfootball_nfl/odds"
    client = make_client(make_transport([("GET", url, lambda *_: ("nope", {}))]))
    with pytest.raises(OddsApiError, match="shape"):
        client.odds("nfl", BOOKS)


# --------------------------------------------------------------------------- resolver + defensive


def test_unresolvable_team_keeps_game_with_empty_keys() -> None:
    def partial(name: str, league: str) -> str | None:
        return None if name == "Kansas City Chiefs" else dict_resolver(name, league)

    games, _ = make_client(resolver=partial).odds("nfl", BOOKS)
    assert len(games) == 2
    kc_buf = next(g for g in games if g.home_team_key == "BUF")
    assert kc_buf.away_team_key == ""
    assert kc_buf.away_team_name == "Kansas City Chiefs"
    kc = outcome(kc_buf, "pinnacle", "h2h", "Kansas City Chiefs")
    assert kc.team_key is None and kc.price_american == -150


def test_default_resolver_is_used_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oddsapi, "_default_team_resolver", dict_resolver)
    client = OddsApiClient(make_transport(), API_KEY, base=BASE)
    games, _ = client.odds("nfl", BOOKS)
    assert {g.home_team_key for g in games} == {"BUF", "PHI"}


def test_resolver_exceptions_are_contained() -> None:
    def broken(name: str, league: str) -> str | None:
        raise KeyError(name)

    games, _ = make_client(resolver=broken).odds("nfl", BOOKS)
    assert len(games) == 2
    assert all(g.home_team_key == "" and g.away_team_key == "" for g in games)


def test_malformed_entries_are_skipped_not_fatal() -> None:
    url = f"{BASE}/sports/americanfootball_nfl/odds"
    payload = [
        "not a game",
        {"id": "x", "home_team": "Buffalo Bills", "away_team": "Kansas City Chiefs"},
        {
            "id": "abc",
            "commence_time": "2026-09-20T20:25:00Z",
            "home_team": "Buffalo Bills",
            "away_team": "Kansas City Chiefs",
            "bookmakers": [
                {"title": "no key", "markets": []},
                {"key": "empty", "title": "Empty", "markets": [{"key": "h2h", "outcomes": []}]},
                {
                    "key": "pinnacle",
                    "title": "Pinnacle",
                    "last_update": "2026-09-19T14:55:00Z",
                    "markets": [
                        {"key": "outrights", "outcomes": [{"name": "Buffalo Bills", "price": 900}]},
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Buffalo Bills", "price": 130},
                                {"name": "Kansas City Chiefs", "price": 1.67},  # decimal: rejected
                                {"price": -150},  # no name
                                "junk",
                            ],
                        },
                        {
                            "key": "totals",
                            "last_update": "not a date",
                            "outcomes": [
                                {"name": "Over", "price": "-110", "point": "47.5"},
                                {"name": "Under", "price": -110.0, "point": 47.5},
                            ],
                        },
                    ],
                },
            ],
        },
    ]
    client = make_client(make_transport([("GET", url, lambda *_: (payload, {}))]))
    games, _ = client.odds("nfl", BOOKS)
    assert len(games) == 1
    (game,) = games
    assert game.game_id == "abc"
    assert [b.bookmaker for b in game.books] == ["pinnacle"]
    (pinnacle,) = game.books
    assert [m.key for m in pinnacle.markets] == ["h2h", "totals"]
    h2h, totals = pinnacle.markets
    assert [o.name for o in h2h.outcomes] == ["Buffalo Bills"]
    assert h2h.last_update == datetime(2026, 9, 19, 14, 55, tzinfo=UTC)  # bookmaker fallback
    assert totals.last_update == datetime(2026, 9, 19, 14, 55, tzinfo=UTC)
    assert [(o.price_american, o.point) for o in totals.outcomes] == [(-110, 47.5), (-110, 47.5)]


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-20T20:25:00Z", datetime(2026, 9, 20, 20, 25, tzinfo=UTC)),
        ("2026-09-20T20:25Z", datetime(2026, 9, 20, 20, 25, tzinfo=UTC)),
        ("2026-09-20T16:25:00-04:00", datetime(2026, 9, 20, 20, 25, tzinfo=UTC)),
        ("2026-09-20T20:25:00", datetime(2026, 9, 20, 20, 25, tzinfo=UTC)),
        ("", None),
        ("yesterday", None),
        (None, None),
        (1726859100, None),
    ],
)
def test_parse_iso_utc(value, expected) -> None:
    parsed = parse_iso_utc(value)
    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-150, -150),
        (130, 130),
        (-110.0, -110),
        ("+120", 120),
        ("EVEN", 100),
        (1.91, None),
        (True, None),
        ("n/a", None),
        (None, None),
        (-99, None),
    ],
)
def test_to_american(value, expected) -> None:
    assert to_american(value) == expected


# --------------------------------------------------------------------------- review fixes


def test_unresolved_team_labels_are_recorded_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A renamed team must be visible on Diagnostics, not a silent 'no book game'."""

    def partial(name: str, league: str) -> str | None:
        return None if name == "Kansas City Chiefs" else dict_resolver(name, league)

    client = make_client(resolver=partial)
    with caplog.at_level(logging.WARNING, logger="app.clients.oddsapi"):
        client.odds("nfl", BOOKS)
    assert client.unresolved_teams == [
        {"league": "nfl", "name": "Kansas City Chiefs", "source": "oddsapi"}
    ]
    assert any("unresolved team 'Kansas City Chiefs'" in r.getMessage() for r in caplog.records)
    client.odds("nfl", BOOKS)  # the same label is recorded once per client, not per call
    assert len(client.unresolved_teams) == 1
    assert make_client().unresolved_teams == []
