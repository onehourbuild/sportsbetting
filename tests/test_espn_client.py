"""EspnClient against the synthetic ESPN scoreboard fixtures (docs/FIXTURES.md).

A dict-backed team resolver is injected everywhere so nothing here depends on
app.core.matching.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.clients import espn
from app.clients.espn import (
    ESPN_BOOKMAKER,
    ESPN_DEFAULT_TITLE,
    EspnClient,
    EspnOddsGame,
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


def make_game(**overrides: Any) -> EspnOddsGame:
    """A game with per-side prices for every market (the shape `_parse_event` builds)."""
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
        "home_spread_odds": -110,
        "away_spread_odds": -110,
        "over_odds": -110,
        "under_odds": -110,
    }
    base.update(overrides)
    return EspnOddsGame(**base)


def make_bare_game(**overrides: Any) -> EspnGame:
    """A plain `EspnGame` (the contract type): a line and a total but no per-side prices."""
    # Derive the EspnOddsGame-only fields rather than listing them: a new one added to the
    # subclass would otherwise leak into EspnGame(**fields) and fail here as a TypeError.
    extras = set(EspnOddsGame.__dataclass_fields__) - set(EspnGame.__dataclass_fields__)
    fields = {f: v for f, v in vars(make_game(**overrides)).items() if f not in extras}
    return EspnGame(**fields)


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
    # per-side prices come from the payload, never from an assumed -110
    assert (kc_buf.home_spread_odds, kc_buf.away_spread_odds) == (-110, -110)
    assert (kc_buf.over_odds, kc_buf.under_odds) == (-110, -110)

    assert dal_phi.start_time == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    assert (dal_phi.home_team_key, dal_phi.away_team_key) == ("PHI", "DAL")
    assert (dal_phi.home_moneyline, dal_phi.away_moneyline) == (-200, 170)
    assert dal_phi.spread_details == "PHI -4.5"
    assert dal_phi.over_under == 44.5
    assert dal_phi.completed is False
    assert (dal_phi.home_spread_odds, dal_phi.away_spread_odds) == (-110, -110)


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
    # Every side of every market in the fixture carries its own price, so all three
    # markets survive. The unpriced case — where the total is dropped rather than invented
    # at -110/-110 — is covered by make_bare_game() below.
    assert [m.key for m in quote.markets] == ["h2h", "spreads", "totals"]

    h2h = market(kc_buf, "h2h")
    assert {(o.name, o.team_key, o.price_american, o.point) for o in h2h.outcomes} == {
        ("Buffalo Bills", "BUF", 130, None),
        ("Kansas City Chiefs", "KC", -150, None),
    }
    spreads = market(kc_buf, "spreads")
    assert {(o.team_key, o.point, o.price_american) for o in spreads.outcomes} == {
        ("KC", -3.5, -110),
        ("BUF", 3.5, -110),
    }
    totals = market(kc_buf, "totals")
    assert {(o.name, o.point, o.price_american) for o in totals.outcomes} == {
        ("Over", 47.5, -110),
        ("Under", 47.5, -110),
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
    assert {(o.name, o.point) for o in market(dal_phi, "totals").outcomes} == {
        ("Over", 44.5),
        ("Under", 44.5),
    }
    assert [m.key for m in dal_phi.books[0].markets] == ["h2h", "spreads", "totals"]


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
    half_spread = make_game(away_spread_odds=None, over_under=None)
    half_total = make_game(spread_details=None, under_odds=None)

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

    # one side priced is not a market: half a price cannot be de-vigged
    (bg,) = EspnClient.to_book_games([half_spread])
    assert [m.key for m in bg.books[0].markets] == ["h2h"]
    (bg,) = EspnClient.to_book_games([half_total])
    assert [m.key for m in bg.books[0].markets] == ["h2h"]


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


# --------------------------------------------------------------------------- review fixes


def test_spreads_and_totals_use_the_payloads_per_side_prices() -> None:
    """The real prices, not -110/-110: a de-vig of -110/-110 is exactly 0.5 whatever the
    book actually charges, and ESPN is the only book when there is no Odds API key."""
    payload = {
        "events": [
            {
                "id": "401772102",
                "competitions": [
                    {
                        "date": "2026-09-20T17:00Z",
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": "Philadelphia Eagles"},
                                "score": "0",
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": "Dallas Cowboys"},
                                "score": "0",
                            },
                        ],
                        "status": {"type": {"completed": False}},
                        "odds": [
                            {
                                "provider": {"name": "ESPN BET"},
                                "details": "PHI -4.5",
                                "overUnder": 44.5,
                                "homeTeamOdds": {"moneyLine": -200, "spreadOdds": -125},
                                "awayTeamOdds": {"moneyLine": 170, "spreadOdds": 105},
                                "overOdds": -105,
                                "underOdds": -115,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    (game,) = make_client(RecordingTransport(payload)).scoreboard("nfl")
    assert (game.home_spread_odds, game.away_spread_odds) == (-125, 105)
    assert (game.over_odds, game.under_odds) == (-105, -115)

    (bg,) = EspnClient.to_book_games([game])
    assert {(o.team_key, o.point, o.price_american) for o in market(bg, "spreads").outcomes} == {
        ("PHI", -4.5, -125),
        ("DAL", 4.5, 105),
    }
    assert {(o.name, o.price_american) for o in market(bg, "totals").outcomes} == {
        ("Over", -105),
        ("Under", -115),
    }


def test_spreads_and_totals_are_dropped_when_the_payload_has_no_per_side_prices() -> None:
    """A plain EspnGame (or an odds block with only `details`/`overUnder`) contributes h2h
    only; inventing -110/-110 would report any side asking below ~0.475 as an edge."""
    (bg,) = EspnClient.to_book_games([make_bare_game()])
    assert [m.key for m in bg.books[0].markets] == ["h2h"]

    payload = {
        "events": [
            {
                "id": "401772101",
                "competitions": [
                    {
                        "date": "2026-09-20T20:25Z",
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": "Buffalo Bills"},
                                "score": "0",
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": "Kansas City Chiefs"},
                                "score": "0",
                            },
                        ],
                        "status": {"type": {"completed": False}},
                        "odds": [
                            {
                                "details": "KC -3.5",
                                "overUnder": 47.5,
                                "homeTeamOdds": {"moneyLine": 130},
                                "awayTeamOdds": {"moneyLine": -150},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    (game,) = make_client(RecordingTransport(payload)).scoreboard("nfl")
    assert game.spread_details == "KC -3.5" and game.over_under == 47.5
    assert (game.home_spread_odds, game.away_spread_odds) == (None, None)
    (bg,) = EspnClient.to_book_games([game])
    assert [m.key for m in bg.books[0].markets] == ["h2h"]
    assert {(o.team_key, o.price_american) for o in market(bg, "h2h").outcomes} == {
        ("BUF", 130),
        ("KC", -150),
    }

    # a game with nothing left to contribute is skipped entirely
    no_ml = make_bare_game(home_moneyline=None, away_moneyline=None)
    assert EspnClient.to_book_games([no_ml]) == []


def test_unresolved_team_labels_are_recorded_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "events": [
            {
                "id": "7",
                "competitions": [
                    {
                        "date": "2026-09-20T20:25Z",
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": "Nowhere FC", "abbreviation": "NWH"},
                                "score": "0",
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": "Kansas City Chiefs"},
                                "score": "0",
                            },
                        ],
                        "status": {"type": {"completed": False}},
                    }
                ],
            }
        ]
    }
    client = make_client(RecordingTransport(payload))
    with caplog.at_level(logging.WARNING, logger="app.clients.espn"):
        games = client.scoreboard("nfl")
    assert len(games) == 1 and games[0].home_team_key == ""
    assert client.unresolved_teams == [{"league": "nfl", "name": "Nowhere FC", "source": "espn"}]
    assert any("unresolved team 'Nowhere FC'" in r.getMessage() for r in caplog.records)
    client.scoreboard("nfl")
    assert len(client.unresolved_teams) == 1


# ------------------------------------------------- current ("nested block") odds payload
#
# Live regression, 2026-09-18: ESPN stopped sending the flat `homeTeamOdds.moneyLine` /
# `spreadOdds` / `overOdds` fields and moved every price into `moneyline`, `pointSpread`
# and `total` blocks, each with `{open, close}` phases holding STRING values. The old
# parser read nothing from that payload, so `to_book_games` returned an empty list, no
# market matched a book and the app reported zero opportunities on a full live slate.


def nested_odds_event(
    *,
    espn_id: str = "401800001",
    home_abbr: str = "TOR",
    away_abbr: str = "BAL",
    home_display: str = "Toronto Blue Jays",
    away_display: str = "Baltimore Orioles",
    details: str = "BAL -149",
    moneyline: dict | None = None,
    point_spread: dict | None = None,
    total: dict | None = None,
) -> dict:
    """One `events[]` entry shaped like ESPN's current scoreboard response."""
    odds: dict[str, Any] = {
        "provider": {"id": "100", "name": "DraftKings"},
        "details": details,
        "overUnder": 8.5,
        "spread": 1.5,
        # Present but priceless, exactly as live: the flat fields the old parser wanted
        # are simply absent from these objects now.
        "homeTeamOdds": {"favorite": False, "underdog": True},
        "awayTeamOdds": {"favorite": True, "underdog": False},
    }
    if moneyline is not None:
        odds["moneyline"] = moneyline
    if point_spread is not None:
        odds["pointSpread"] = point_spread
    if total is not None:
        odds["total"] = total
    return {
        "id": espn_id,
        "competitions": [
            {
                "date": "2026-09-18T22:40Z",
                "status": {"type": {"completed": False}},
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "0",
                        "team": {"abbreviation": home_abbr, "displayName": home_display},
                    },
                    {
                        "homeAway": "away",
                        "score": "0",
                        "team": {"abbreviation": away_abbr, "displayName": away_display},
                    },
                ],
                "odds": [odds],
            }
        ],
    }


NESTED_MONEYLINE = {
    "home": {"open": {"odds": "+108"}, "close": {"odds": "+123"}},
    "away": {"open": {"odds": "-130"}, "close": {"odds": "-149"}},
}
NESTED_SPREAD = {
    "home": {"open": {"line": "+1.5", "odds": "-149"}, "close": {"line": "+1.5", "odds": "-136"}},
    "away": {"open": {"line": "-1.5", "odds": "+123"}, "close": {"line": "-1.5", "odds": "+113"}},
}
NESTED_TOTAL = {
    "over": {"open": {"line": "o9", "odds": "+100"}, "close": {"line": "o8.5", "odds": "-115"}},
    "under": {"open": {"line": "u9", "odds": "-120"}, "close": {"line": "u8.5", "odds": "-105"}},
}


def nested_client(event: dict, league: str = "mlb") -> EspnClient:
    path = {"mlb": "baseball/mlb", "nfl": "football/nfl", "nba": "basketball/nba"}[league]
    transport = FixtureTransport(
        FIXTURES_DIR,
        [("GET", f"{BASE}/{path}/scoreboard", lambda url, params, body: ({"events": [event]}, {}))],
    )
    return EspnClient(transport, base=BASE, team_resolver=dict_resolver)


def test_nested_blocks_supply_every_price_and_line() -> None:
    client = nested_client(
        nested_odds_event(
            moneyline=NESTED_MONEYLINE, point_spread=NESTED_SPREAD, total=NESTED_TOTAL
        )
    )
    (game,) = client.scoreboard("mlb", date(2026, 9, 18))
    # `close` is the latest quote, so it wins over `open`.
    assert (game.home_moneyline, game.away_moneyline) == (123, -149)
    assert (game.home_spread_odds, game.away_spread_odds) == (-136, 113)
    assert (game.home_spread_point, game.away_spread_point) == (1.5, -1.5)
    assert (game.over_odds, game.under_odds) == (-115, -105)
    assert game.total_line == 8.5  # "o8.5" -> 8.5


def test_nested_blocks_become_all_three_book_markets() -> None:
    client = nested_client(
        nested_odds_event(
            moneyline=NESTED_MONEYLINE, point_spread=NESTED_SPREAD, total=NESTED_TOTAL
        )
    )
    (book_game,) = EspnClient.to_book_games(client.scoreboard("mlb", date(2026, 9, 18)))
    assert {m.key for m in book_game.books[0].markets} == {"h2h", "spreads", "totals"}

    h2h = market(book_game, "h2h")
    assert [(o.team_key, o.price_american) for o in h2h.outcomes] == [("TOR", 123), ("BAL", -149)]

    # The spread comes from each side's own signed line, not from `details`.
    spreads = market(book_game, "spreads")
    assert [(o.team_key, o.price_american, o.point) for o in spreads.outcomes] == [
        ("TOR", -136, 1.5),
        ("BAL", 113, -1.5),
    ]

    totals = market(book_game, "totals")
    assert [(o.name, o.price_american, o.point) for o in totals.outcomes] == [
        ("Over", -115, 8.5),
        ("Under", -105, 8.5),
    ]


def test_open_phase_is_used_when_close_carries_no_price() -> None:
    client = nested_client(
        nested_odds_event(
            moneyline={
                "home": {"open": {"odds": "+108"}, "close": {}},
                "away": {"open": {"odds": "-130"}},
            }
        )
    )
    (game,) = client.scoreboard("mlb", date(2026, 9, 18))
    assert (game.home_moneyline, game.away_moneyline) == (108, -130)


def test_mlb_details_moneyline_never_becomes_a_spread() -> None:
    """For baseball `details` is the MONEYLINE ("CHC -149"). Priced as a spread it would
    ask for a -149-run line no book offers, so it must contribute no spreads market."""
    assert parse_spread_details("BAL -149") is None
    client = nested_client(nested_odds_event(moneyline=NESTED_MONEYLINE, details="BAL -149"))
    (book_game,) = EspnClient.to_book_games(client.scoreboard("mlb", date(2026, 9, 18)))
    assert {m.key for m in book_game.books[0].markets} == {"h2h"}


def test_nested_payload_without_prices_contributes_nothing() -> None:
    """Blocks present but empty must not resurrect the assumed -110/-110."""
    client = nested_client(
        nested_odds_event(moneyline={}, point_spread={"home": {}, "away": {}}, total={})
    )
    assert EspnClient.to_book_games(client.scoreboard("mlb", date(2026, 9, 18))) == []


def test_user_agent_names_a_recognised_http_client() -> None:
    """ESPN's edge 403s a bare custom User-Agent (verified live 2026-09-18). Leading with
    the real httpx token is what gets the request served; the app still identifies itself."""
    assert USER_AGENT.startswith("python-httpx/")
    assert "polymarket-edge-finder" in USER_AGENT
