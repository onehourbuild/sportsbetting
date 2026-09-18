"""EspnClient against the synthetic ESPN scoreboard fixtures (docs/FIXTURES.md).

A dict-backed team resolver is injected everywhere so nothing here depends on
app.core.matching.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.clients import espn
from app.clients.espn import (
    ESPN_BOOKMAKER,
    ESPN_DEFAULT_TITLE,
    ESPN_SIDE_PRICE,
    EspnClient,
    parse_spread_details,
)
from app.clients.transport import USER_AGENT, FixtureTransport, TransportError
from app.core.types import BookGame, EspnGame
from tests.conftest import FIXTURES_DIR

BASE = "https://site.api.espn.com/apis/site/v2/sports"

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
    "mlb": {"Toronto Blue Jays": "TOR", "Baltimore Orioles": "BAL"},
}


def dict_resolver(name: str, league: str) -> str | None:
    return TEAM_TABLE.get(league, {}).get(name)


def make_transport(routes=None) -> FixtureTransport:
    if routes is None:
        routes = [
            ("GET", f"{BASE}/football/nfl/scoreboard", "espn_scoreboard_nfl.json"),
            ("GET", f"{BASE}/basketball/nba/scoreboard", "espn_scoreboard_nba.json"),
            ("GET", f"{BASE}/baseball/mlb/scoreboard", "espn_scoreboard_mlb.json"),
        ]
    return FixtureTransport(FIXTURES_DIR, routes)


def make_client(transport=None, resolver=dict_resolver) -> EspnClient:
    return EspnClient(transport or make_transport(), base=BASE, team_resolver=resolver)


def by_id(games: list[EspnGame]) -> dict[str, EspnGame]:
    return {g.espn_id: g for g in games}


def market(game: BookGame, key: str):
    (quote,) = game.books
    (mkt,) = [m for m in quote.markets if m.key == key]
    return mkt


def make_game(**overrides: Any) -> EspnGame:
    base: dict[str, Any] = {
        "espn_id": "401772101",
        "league": "nfl",
        "start_time": datetime(2026, 9, 20, 20, 25, tzinfo=UTC),
        "home_team_key": "BUF",
        "away_team_key": "KC",
        "home_name": "Buffalo Bills",
        "away_name": "Kansas City Chiefs",
        "home_score": 0,
        "away_score": 0,
        "completed": False,
        "odds_provider": "ESPN BET",
        "home_moneyline": 130,
        "away_moneyline": -150,
        "spread_details": "KC -3.5",
        "over_under": 47.5,
    }
    base.update(overrides)
    return EspnGame(**base)


# --------------------------------------------------------------------------- request


class RecordingTransport:
    """Minimal Transport that records headers too (FixtureTransport drops them)."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        self.calls.append({"url": url, "params": params, "headers": dict(headers or {})})
        return self.payload, {}

    def post_json(self, url: str, body: Any, headers=None) -> tuple[Any, Mapping[str, str]]:
        raise AssertionError("ESPN client never POSTs")


def test_scoreboard_url_dates_param_and_user_agent() -> None:
    transport = RecordingTransport({"events": []})
    client = EspnClient(transport, base=BASE + "/", team_resolver=dict_resolver)
    assert client.scoreboard("nfl", date(2026, 9, 20)) == []
    assert client.scoreboard("nba") == []
    assert client.scoreboard("mlb", datetime(2026, 9, 17, 23, 7, tzinfo=UTC)) == []
    assert transport.calls[0]["url"] == f"{BASE}/football/nfl/scoreboard"
    assert transport.calls[0]["params"] == {"dates": "20260920"}
    assert transport.calls[1]["url"] == f"{BASE}/basketball/nba/scoreboard"
    assert transport.calls[1]["params"] is None
    assert transport.calls[2]["url"] == f"{BASE}/baseball/mlb/scoreboard"
    assert transport.calls[2]["params"] == {"dates": "20260917"}
    for call in transport.calls:
        ua = call["headers"]["User-Agent"]
        assert ua == USER_AGENT
        assert "Mozilla" not in ua


def test_scoreboard_unknown_league_raises_before_network() -> None:
    transport = make_transport()
    with pytest.raises(ValueError):
        make_client(transport).scoreboard("nhl")  # type: ignore[arg-type]
    assert transport.calls == []


def test_scoreboard_unknown_route_propagates_transport_error() -> None:
    with pytest.raises(TransportError):
        make_client(make_transport(routes=[])).scoreboard("nfl")


# --------------------------------------------------------------------------- nfl fixture


def test_nfl_scoreboard_parses_games_with_odds() -> None:
    games = make_client().scoreboard("nfl", date(2026, 9, 20))
    assert len(games) == 2
    assert all(g.league == "nfl" for g in games)
    assert all(len(g.espn_id) == 9 and g.espn_id.isdigit() for g in games)
    g = by_id(games)
    kc_buf, dal_phi = g["401772101"], g["401772102"]

    assert kc_buf.start_time == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert kc_buf.start_time.tzinfo is not None
    assert (kc_buf.home_team_key, kc_buf.away_team_key) == ("BUF", "KC")
    assert (kc_buf.home_name, kc_buf.away_name) == ("Buffalo Bills", "Kansas City Chiefs")
    assert kc_buf.completed is False
    assert (kc_buf.home_score, kc_buf.away_score) == (0, 0)
    assert kc_buf.odds_provider == "ESPN BET"
    assert (kc_buf.home_moneyline, kc_buf.away_moneyline) == (130, -150)
    assert kc_buf.spread_details == "KC -3.5"
    assert kc_buf.over_under == 47.5

    assert dal_phi.start_time == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    assert (dal_phi.home_team_key, dal_phi.away_team_key) == ("PHI", "DAL")
    assert (dal_phi.home_moneyline, dal_phi.away_moneyline) == (-200, 170)
    assert dal_phi.spread_details == "PHI -4.5"
    assert dal_phi.over_under == 44.5
    assert dal_phi.completed is False


# --------------------------------------------------------------------------- mlb + nba fixtures


def test_mlb_scoreboard_completed_game_with_score_and_no_odds() -> None:
    games = make_client().scoreboard("mlb", date(2026, 9, 17))
    assert len(games) == 1
    (bal_tor,) = games
    assert bal_tor.espn_id == "401695707"
    assert bal_tor.league == "mlb"
    assert bal_tor.start_time == datetime(2026, 9, 17, 23, 7, tzinfo=UTC)
    assert (bal_tor.home_team_key, bal_tor.away_team_key) == ("TOR", "BAL")
    assert (bal_tor.home_name, bal_tor.away_name) == ("Toronto Blue Jays", "Baltimore Orioles")
    assert bal_tor.completed is True
    assert (bal_tor.away_score, bal_tor.home_score) == (2, 5)  # BAL 2 - TOR 5
    assert bal_tor.odds_provider is None
    assert bal_tor.home_moneyline is None and bal_tor.away_moneyline is None
    assert bal_tor.spread_details is None and bal_tor.over_under is None


def test_nba_scoreboard_games_without_odds() -> None:
    games = make_client().scoreboard("nba", date(2026, 10, 22))
    assert len(games) == 2
    g = by_id(games)
    lal_bos, gsw_den = g["401810301"], g["401810302"]
    assert (lal_bos.home_team_key, lal_bos.away_team_key) == ("BOS", "LAL")
    assert lal_bos.start_time == datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
    assert (gsw_den.home_team_key, gsw_den.away_team_key) == ("DEN", "GSW")
    assert gsw_den.start_time == datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
    for game in games:
        assert game.completed is False
        assert game.odds_provider is None
        assert game.home_moneyline is None and game.away_moneyline is None
        assert game.spread_details is None and game.over_under is None


# --------------------------------------------------------------------------- defensive parsing


def test_scoreboard_tolerates_malformed_payloads() -> None:
    def route(payload):
        return [("GET", f"{BASE}/football/nfl/scoreboard", lambda *_: (payload, {}))]

    assert make_client(make_transport(route("nope"))).scoreboard("nfl") == []
    assert make_client(make_transport(route({"events": "nope"}))).scoreboard("nfl") == []

    payload = {
        "events": [
            "junk",
            {"id": "1", "date": "2026-09-20T17:00Z", "competitions": []},  # no competitors
            {
                "id": "2",
                "date": "not a date",
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "Buffalo Bills"}},
                            {"homeAway": "away", "team": {"displayName": "Kansas City Chiefs"}},
                        ]
                    }
                ],
            },
            {
                "id": "3",
                "date": "2026-09-20T17:00Z",
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": "Nowhere FC", "abbreviation": "NWH"},
                                "score": {"value": 3, "displayValue": "3"},
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": "Kansas City Chiefs"},
                                "score": "",
                            },
                        ],
                        "status": {"type": {"completed": "yes"}},
                        "odds": [{"details": "EVEN", "overUnder": "41.5"}],
                    }
                ],
            },
        ]
    }
    games = make_client(make_transport(route(payload))).scoreboard("nfl")
    assert len(games) == 1
    (game,) = games
    assert game.espn_id == "3"
    assert game.home_team_key == ""  # unresolved but still returned
    assert game.away_team_key == "KC"
    assert game.home_name == "Nowhere FC"
    assert (game.home_score, game.away_score) == (3, None)
    assert game.completed is True
    assert game.odds_provider == ESPN_DEFAULT_TITLE  # odds block without provider name
    assert game.spread_details == "EVEN"
    assert game.over_under == 41.5
    assert game.home_moneyline is None and game.away_moneyline is None


def test_team_key_falls_back_to_abbreviation_and_short_name() -> None:
    def abbr_only(name: str, league: str) -> str | None:
        return {"BUF": "BUF", "Chiefs": "KC"}.get(name)

    games = make_client(resolver=abbr_only).scoreboard("nfl")
    kc_buf = by_id(games)["401772101"]
    assert (kc_buf.home_team_key, kc_buf.away_team_key) == ("BUF", "KC")


def test_resolver_exceptions_are_contained() -> None:
    def broken(name: str, league: str) -> str | None:
        raise RuntimeError(name)

    games = make_client(resolver=broken).scoreboard("nfl")
    assert len(games) == 2
    assert all(g.home_team_key == "" and g.away_team_key == "" for g in games)


def test_default_resolver_is_used_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(espn, "_default_team_resolver", dict_resolver)
    games = EspnClient(make_transport(), base=BASE).scoreboard("nfl")
    assert {g.home_team_key for g in games} == {"BUF", "PHI"}


# --------------------------------------------------------------------------- to_book_games


def test_to_book_games_from_nfl_fixture() -> None:
    games = make_client().scoreboard("nfl", date(2026, 9, 20))
    book_games = EspnClient.to_book_games(games)
    assert len(book_games) == 2
    g = {bg.home_team_key: bg for bg in book_games}
    kc_buf, dal_phi = g["BUF"], g["PHI"]

    assert kc_buf.game_id == "espn:401772101"
    assert kc_buf.league == "nfl"
    assert kc_buf.commence_time == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)
    assert (kc_buf.home_team_name, kc_buf.away_team_name) == (
        "Buffalo Bills",
        "Kansas City Chiefs",
    )
    assert kc_buf.away_team_key == "KC"
    (quote,) = kc_buf.books
    assert quote.bookmaker == ESPN_BOOKMAKER == "espn"
    assert quote.title == "ESPN BET"
    assert [m.key for m in quote.markets] == ["h2h", "spreads", "totals"]

    h2h = market(kc_buf, "h2h")
    assert {(o.name, o.team_key, o.price_american, o.point) for o in h2h.outcomes} == {
        ("Buffalo Bills", "BUF", 130, None),
        ("Kansas City Chiefs", "KC", -150, None),
    }
    spreads = market(kc_buf, "spreads")
    assert {(o.team_key, o.point, o.price_american) for o in spreads.outcomes} == {
        ("KC", -3.5, ESPN_SIDE_PRICE),
        ("BUF", 3.5, ESPN_SIDE_PRICE),
    }
    totals = market(kc_buf, "totals")
    assert {(o.name, o.team_key, o.point, o.price_american) for o in totals.outcomes} == {
        ("Over", None, 47.5, -110),
        ("Under", None, 47.5, -110),
    }
    assert all(m.last_update is None for m in quote.markets)

    assert dal_phi.game_id == "espn:401772102"
    assert {(o.team_key, o.price_american) for o in market(dal_phi, "h2h").outcomes} == {
        ("PHI", -200),
        ("DAL", 170),
    }
    assert {(o.team_key, o.point) for o in market(dal_phi, "spreads").outcomes} == {
        ("PHI", -4.5),
        ("DAL", 4.5),
    }
    assert {o.point for o in market(dal_phi, "totals").outcomes} == {44.5}


def test_to_book_games_skips_games_without_odds() -> None:
    client = make_client()
    assert EspnClient.to_book_games(client.scoreboard("mlb")) == []
    assert EspnClient.to_book_games(client.scoreboard("nba")) == []
    assert EspnClient.to_book_games([]) == []


def test_to_book_games_partial_odds() -> None:
    only_ml = make_game(spread_details=None, over_under=None)
    only_total = make_game(home_moneyline=None, spread_details=None)
    one_ml = make_game(away_moneyline=None, spread_details=None, over_under=None)
    pickem = make_game(spread_details="EVEN")
    unknown_team = make_game(spread_details="ZZZ -3.5")
    away_dog = make_game(spread_details="KC +3.5")
    zero = make_game(spread_details="BUF -0")

    (bg,) = EspnClient.to_book_games([only_ml])
    assert [m.key for m in bg.books[0].markets] == ["h2h"]

    (bg,) = EspnClient.to_book_games([only_total])
    assert [m.key for m in bg.books[0].markets] == ["totals"]

    assert EspnClient.to_book_games([one_ml]) == []

    (bg,) = EspnClient.to_book_games([pickem])
    assert [m.key for m in bg.books[0].markets] == ["h2h", "totals"]

    (bg,) = EspnClient.to_book_games([unknown_team])
    assert [m.key for m in bg.books[0].markets] == ["h2h", "totals"]

    (bg,) = EspnClient.to_book_games([away_dog])
    assert {(o.team_key, o.point) for o in market(bg, "spreads").outcomes} == {
        ("KC", 3.5),
        ("BUF", -3.5),
    }

    (bg,) = EspnClient.to_book_games([zero])
    assert {o.point for o in market(bg, "spreads").outcomes} == {0.0}


def test_to_book_games_uses_provider_name_as_title_and_blank_keys_become_none() -> None:
    game = make_game(odds_provider="DraftKings", home_team_key="", spread_details="KC -3.5")
    (bg,) = EspnClient.to_book_games([game])
    assert bg.books[0].title == "DraftKings"
    assert bg.home_team_key == ""
    h2h = market(bg, "h2h")
    assert {(o.name, o.team_key) for o in h2h.outcomes} == {
        ("Buffalo Bills", None),
        ("Kansas City Chiefs", "KC"),
    }
    # the favourite named in details is still placed by its key
    assert {(o.name, o.point) for o in market(bg, "spreads").outcomes} == {
        ("Kansas City Chiefs", -3.5),
        ("Buffalo Bills", 3.5),
    }


def test_to_book_games_resolves_espn_abbreviation_via_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ESPN writes "GS" for Golden State; our key is "GSW". The direct key match fails and
    # the alias fallback (patched here so the test does not load matching.py) places it.
    def alias_table(name: str, league: str) -> str | None:
        return {"GS": "GSW"}.get(name)

    monkeypatch.setattr(espn, "_default_team_resolver", alias_table)
    game = make_game(
        league="nba",
        home_team_key="DEN",
        away_team_key="GSW",
        home_name="Denver Nuggets",
        away_name="Golden State Warriors",
        home_moneyline=-130,
        away_moneyline=110,
        spread_details="GS +2.5",
        over_under=231.5,
    )
    (bg,) = EspnClient.to_book_games([game])
    assert {(o.team_key, o.point) for o in market(bg, "spreads").outcomes} == {
        ("GSW", 2.5),
        ("DEN", -2.5),
    }


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ("KC -3.5", ("KC", -3.5)),
        ("PHI -4.5", ("PHI", -4.5)),
        ("NE +7", ("NE", 7.0)),
        ("  GS -6.5 ", ("GS", -6.5)),
        ("LA Angels -1.5", ("LA Angels", -1.5)),
        ("EVEN", None),
        ("PK", None),
        ("", None),
        (None, None),
        ("KC -3.5 (-110)", None),
        ("-3.5", None),
    ],
)
def test_parse_spread_details(details, expected) -> None:
    assert parse_spread_details(details) == expected
