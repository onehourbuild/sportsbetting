"""The Polymarket US market-data client.

The fixture mirrors the shapes docs/RESEARCH.md recorded off live .us responses on
2026-09-21, including the one that makes this client cautious: the spread market titled
"Los Angeles Rams wins by over 6.5 points" carries the question "Will the New York Giants
cover 6.5 points?" and outcomes ["-6.50", "+6.50"]. Title, question and outcomes disagree
about whose side is whose.

This environment cannot reach gateway.polymarket.us, so fixtures plus refusal are the only
honest verification available. The tests therefore assert as much about what the client
*declines* as about what it parses -- a market it refuses is one nobody can lose money on,
and a side it guessed wrong would be invisible until settlement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clients.polymarket_us import (
    SKIP_NOT_FULL_GAME,
    SKIP_SPREAD_AMBIGUOUS,
    PolymarketUsClient,
    PolymarketUsError,
)
from app.clients.transport import FixtureTransport

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def client() -> PolymarketUsClient:
    route = ("GET", "https://gateway.polymarket.us/v1/events", "polymarket_us_events_nfl.json")
    transport = FixtureTransport(FIXTURES, routes=[route])
    return PolymarketUsClient(transport)


def _by_type(markets):
    return {m.market_type: m for m in markets}


# ------------------------------------------------------------------- what it parses


def test_parses_the_full_game_moneyline(client: PolymarketUsClient) -> None:
    markets, _ = client.events("nfl")
    moneyline = _by_type(markets)["moneyline"]

    assert moneyline.market_id == "asc-nfl-nyg-lar-2026-09-21-winner"
    assert {o.team_key for o in moneyline.outcomes} == {"LAR", "NYG"}
    rams = next(o for o in moneyline.outcomes if o.team_key == "LAR")
    assert rams.best_ask == pytest.approx(0.74)
    assert rams.last_price == pytest.approx(0.73)


def test_parses_the_full_game_total_with_its_line(client: PolymarketUsClient) -> None:
    markets, _ = client.events("nfl")
    total = _by_type(markets)["total"]

    assert total.line == pytest.approx(47.5)
    assert [o.team_key for o in total.outcomes] == [None, None]
    assert "Over" in total.outcomes[0].name


def test_reads_the_us_fee_coefficient_per_market(client: PolymarketUsClient) -> None:
    """0.0695, not .com's 0.05. A fee too low reports edges that are not there."""
    markets, _ = client.events("nfl")

    assert all(m.taker_fee_rate == pytest.approx(0.0695) for m in markets)
    assert {entry["rate"] for entry in client.fee_coefficients} == {0.0695}


def test_carries_tick_size_and_minimum_order_size(client: PolymarketUsClient) -> None:
    """Sizing caps a suggestion at the market's own minimum, so it has to be real."""
    moneyline = _by_type(client.events("nfl")[0])["moneyline"]

    assert moneyline.tick_size == pytest.approx(0.01)
    assert moneyline.min_order_size == pytest.approx(5)


def test_synthesises_stable_unique_token_ids(client: PolymarketUsClient) -> None:
    """.us has no CLOB token id, but the ledger needs something stable to key an outcome."""
    markets, _ = client.events("nfl")
    tokens = [o.token_id for m in markets for o in m.outcomes]

    assert len(tokens) == len(set(tokens))
    assert all(token.startswith("asc-nfl-") for token in tokens)
    # Stable across calls, or a re-scan would orphan every logged bet.
    again = [o.token_id for m in client.events("nfl")[0] for o in m.outcomes]
    assert tokens == again


def test_the_slug_is_the_identity_since_there_is_no_condition_id(
    client: PolymarketUsClient,
) -> None:
    moneyline = _by_type(client.events("nfl")[0])["moneyline"]

    assert moneyline.condition_id == moneyline.slug == moneyline.market_id


def test_resolves_the_event_teams(client: PolymarketUsClient) -> None:
    moneyline = _by_type(client.events("nfl")[0])["moneyline"]

    assert moneyline.home_team_key == "LAR"
    assert moneyline.away_team_key == "NYG"


# ------------------------------------------------------------------ what it refuses


def test_refuses_the_spread_rather_than_guess_the_side(client: PolymarketUsClient) -> None:
    """The expensive one. A wrong side recommends the opposite team at a plausible price.

    Refusing costs coverage. Guessing costs money, and stays invisible until settlement.
    """
    markets, unparseable = client.events("nfl")

    assert "spread" not in _by_type(markets)
    refused = next(u for u in unparseable if u["market_id"].endswith("pos-6pt5"))
    assert refused["reason"] == SKIP_SPREAD_AMBIGUOUS
    assert "wrong team" in refused["reason"]


def test_skips_markets_that_are_not_full_game(client: PolymarketUsClient) -> None:
    """A second-half spread and a player prop are different models, not full-game lines."""
    _, unparseable = client.events("nfl")
    reasons = {u["market_id"]: u["reason"] for u in unparseable}

    assert reasons["asc-nfl-nyg-lar-2026-09-21-2h-pos-3pt5"] == SKIP_NOT_FULL_GAME
    assert reasons["asc-nfl-nyg-lar-2026-09-21-anytime-td-nacua"] == SKIP_NOT_FULL_GAME


def test_everything_refused_is_reported_not_dropped(client: PolymarketUsClient) -> None:
    """Five NFL markets in, two priced, three explained. Nothing vanishes silently."""
    markets, unparseable = client.events("nfl")

    assert len(markets) == 2
    assert len(unparseable) == 3
    assert all(u["reason"] and u["question"] for u in unparseable)


# --------------------------------------------------------------------- league picking


def test_selects_events_by_league(client: PolymarketUsClient) -> None:
    nfl, _ = client.events("nfl")
    nba, _ = client.events("nba")

    assert {m.event_slug for m in nfl} == {"nfl-nyg-lar-2026-09-21"}
    assert {m.event_slug for m in nba} == {"nba-bos-mia-2026-09-21"}
    assert {o.team_key for m in nba for o in m.outcomes} == {"BOS", "MIA"}


def test_a_league_with_no_events_is_empty_not_an_error(client: PolymarketUsClient) -> None:
    markets, unparseable = client.events("mlb")

    assert markets == [] and unparseable == []


def test_rejects_an_unknown_league(client: PolymarketUsClient) -> None:
    with pytest.raises(PolymarketUsError, match="unknown league"):
        client.events("nhl")  # type: ignore[arg-type]


# ------------------------------------------------------------------------ robustness


def test_a_non_list_payload_raises_rather_than_returning_nothing() -> None:
    """Silently returning zero markets would read as 'no games today'."""
    transport = FixtureTransport(FIXTURES)
    transport.add_route("GET", "https://gateway.polymarket.us/v1/events", lambda *a: ({}, {}))
    with pytest.raises(PolymarketUsError, match="expected a list"):
        PolymarketUsClient(transport).events("nfl")


def test_a_transport_failure_is_wrapped_with_context() -> None:
    def explode(*_args):
        raise RuntimeError("upstream down")

    transport = FixtureTransport(FIXTURES)
    transport.add_route("GET", "https://gateway.polymarket.us/v1/events", explode)
    with pytest.raises(PolymarketUsError, match="events fetch failed"):
        PolymarketUsClient(transport).events("nfl")


def test_a_market_with_no_prices_is_refused(tmp_path: Path) -> None:
    """An unpriced market cannot be an edge, and a None ask must not reach the math."""
    import json

    payload = {
        "events": [
            {
                "slug": "nfl-aaa-bbb-2026-09-21",
                "homeTeam": "Los Angeles Rams",
                "awayTeam": "New York Giants",
                "markets": [
                    {
                        "marketSlug": "m1",
                        "sportsMarketType": "football_team_full_game_winner",
                        "outcomes": ["Los Angeles Rams", "New York Giants"],
                    }
                ],
            }
        ]
    }
    (tmp_path / "events.json").write_text(json.dumps(payload))
    transport = FixtureTransport(
        tmp_path, routes=[("GET", "https://gateway.polymarket.us/v1/events", "events.json")]
    )

    markets, unparseable = PolymarketUsClient(transport).events("nfl")

    assert markets == []
    assert unparseable[0]["reason"] == "no usable price on either outcome"
