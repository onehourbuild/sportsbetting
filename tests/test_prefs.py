"""Prefs service: defaults, validation, round trip."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models import DEFAULT_BOOK_WEIGHTS, DEFAULT_BOOKMAKERS, Prefs
from app.services.prefs import get_prefs, update_prefs, validate_prefs


def test_get_prefs_creates_singleton_with_defaults(db_session: Session) -> None:
    prefs = get_prefs(db_session)
    assert prefs.id == 1
    assert prefs.bankroll == 1000.0
    assert prefs.kelly_fraction == 0.25
    assert prefs.max_stake_pct == 2.0
    assert prefs.min_edge == 0.02
    assert prefs.taker_fee_rate == 0.05
    assert prefs.devig_method == "power"
    assert prefs.bookmakers == DEFAULT_BOOKMAKERS
    assert prefs.book_weights == DEFAULT_BOOK_WEIGHTS
    assert prefs.leagues_enabled == ["nfl", "nba", "mlb"]
    assert prefs.espn_fallback_enabled is True
    assert prefs.match_window_hours == 36.0
    assert prefs.min_liquidity_usd == 100.0
    assert prefs.stale_book_minutes == 720
    assert prefs.updated_at.tzinfo is not None

    again = get_prefs(db_session)
    assert again.id == prefs.id
    assert db_session.query(Prefs).count() == 1


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"bankroll": 0}, "bankroll"),
        ({"bankroll": -5}, "bankroll"),
        ({"bankroll": "abc"}, "bankroll must be a number"),
        ({"kelly_fraction": 0}, "kelly_fraction"),
        ({"kelly_fraction": 1.5}, "kelly_fraction"),
        ({"max_stake_pct": 0}, "max_stake_pct"),
        ({"max_stake_pct": 101}, "max_stake_pct"),
        ({"min_edge": -0.01}, "min_edge"),
        ({"min_edge": 0.5}, "min_edge"),
        ({"taker_fee_rate": 0.2}, "taker_fee_rate"),
        ({"taker_fee_rate": -0.1}, "taker_fee_rate"),
        ({"devig_method": "magic"}, "devig_method must be one of"),
        ({"devig_method": 3}, "devig_method must be a string"),
        ({"bookmakers": []}, "at least one"),
        ({"bookmakers": [1, 2]}, "entries must be strings"),
        ({"bookmakers": ["bad slug!"]}, "not a valid slug"),
        ({"book_weights": ["pinnacle"]}, "book_weights must be an object"),
        ({"book_weights": {"pinnacle": -1}}, "must be >= 0"),
        ({"book_weights": {"pinnacle": "heavy"}}, "must be a number"),
        ({"leagues_enabled": ["nhl"]}, "leagues_enabled may only contain"),
        ({"stale_book_minutes": -1}, "stale_book_minutes"),
        ({"stale_book_minutes": 1.5}, "must be an integer"),
        ({"espn_fallback_enabled": "maybe"}, "true or false"),
        ({"match_window_hours": 0}, "match_window_hours"),
        ({"min_liquidity_usd": -1}, "min_liquidity_usd"),
        ({"not_a_pref": 1}, "unknown preference"),
        # review round: branches the settings form can reach but nothing covered
        ({"bankroll": "nan"}, "bankroll must be a finite number"),
        ({"bankroll": "inf"}, "bankroll must be a finite number"),
        ({"bankroll": float("inf")}, "bankroll must be a finite number"),
        ({"min_edge": float("nan")}, "min_edge must be a finite number"),
        ({"bankroll": None}, "bankroll must be a number, got NoneType"),
        ({"bankroll": [1000]}, "bankroll must be a number, got list"),
        ({"stale_book_minutes": "abc"}, "stale_book_minutes must be an integer, got 'abc'"),
        ({"stale_book_minutes": "1.5"}, "stale_book_minutes must be an integer, got '1.5'"),
        ({"stale_book_minutes": None}, "stale_book_minutes must be an integer, got NoneType"),
        ({"bookmakers": 7}, "bookmakers must be a list of strings"),
        ({"bookmakers": None}, "bookmakers must be a list of strings"),
        ({"bookmakers": ""}, "bookmakers must contain at least one"),
        ({"bookmakers": "   "}, "bookmakers must contain at least one"),
        ({"book_weights": {"": 1.0}}, "book_weights keys must be bookmaker slugs"),
        ({"book_weights": {"   ": 1.0}}, "book_weights keys must be bookmaker slugs"),
        ({"book_weights": {3: 1.0}}, "book_weights keys must be bookmaker slugs"),
        ({"book_weights": {"pinnacle": "nan"}}, "must be a finite number"),
        ({"book_weights": {"pinnacle": None}}, "must be a number, got NoneType"),
        ({"espn_fallback_enabled": 2}, "true or false"),
        ({"espn_fallback_enabled": None}, "true or false"),
        ({"leagues_enabled": 3}, "leagues_enabled must be a list of strings"),
        ({"leagues_enabled": [7]}, "entries must be strings"),
        ({"devig_method": None}, "devig_method must be a string"),
    ],
)
def test_update_prefs_rejects_bad_values(db_session: Session, data: dict, fragment: str) -> None:
    before = get_prefs(db_session).bankroll
    with pytest.raises(ValueError, match=fragment):
        update_prefs(db_session, data)
    assert get_prefs(db_session).bankroll == before  # nothing partially applied


def test_update_prefs_round_trip(db_session: Session) -> None:
    updated = update_prefs(
        db_session,
        {
            "bankroll": "2,500",
            "kelly_fraction": 0.5,
            "max_stake_pct": 5,
            "min_edge": "0.03",
            "taker_fee_rate": 0.04,
            "devig_method": "Shin",
            "bookmakers": "Pinnacle, circasports,pinnacle",
            "book_weights": {"pinnacle": 4, "ESPN": "0.25"},
            "leagues_enabled": ["mlb", "nfl"],
            "espn_fallback_enabled": "off",
            "match_window_hours": 24,
            "min_liquidity_usd": 0,
            "stale_book_minutes": "60",
        },
    )
    assert updated.bankroll == 2500.0
    assert updated.kelly_fraction == 0.5
    assert updated.max_stake_pct == 5.0
    assert updated.min_edge == 0.03
    assert updated.taker_fee_rate == 0.04
    assert updated.devig_method == "shin"
    assert updated.bookmakers == ["pinnacle", "circasports"]
    assert updated.book_weights == {"pinnacle": 4.0, "espn": 0.25}
    assert updated.leagues_enabled == ["nfl", "mlb"]  # canonical order
    assert updated.espn_fallback_enabled is False
    assert updated.match_window_hours == 24.0
    assert updated.min_liquidity_usd == 0.0
    assert updated.stale_book_minutes == 60

    db_session.expire_all()
    reloaded = get_prefs(db_session)
    assert reloaded.bankroll == 2500.0
    assert reloaded.bookmakers == ["pinnacle", "circasports"]
    assert reloaded.leagues_enabled == ["nfl", "mlb"]


def test_update_prefs_partial_keeps_other_fields(db_session: Session) -> None:
    get_prefs(db_session)
    update_prefs(db_session, {"min_edge": 0.05})
    prefs = get_prefs(db_session)
    assert prefs.min_edge == 0.05
    assert prefs.bankroll == 1000.0
    assert prefs.bookmakers == DEFAULT_BOOKMAKERS


def test_validate_prefs_is_pure() -> None:
    assert validate_prefs({"espn_fallback_enabled": "1", "leagues_enabled": "nba"}) == {
        "espn_fallback_enabled": True,
        "leagues_enabled": ["nba"],
    }


# --------------------------------------------------------------------------- review fixes


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"kelly_fraction": 1.0}, 1.0),  # (0, 1]
        ({"kelly_fraction": "0.000001"}, 1e-6),
        ({"max_stake_pct": 100}, 100.0),  # (0, 100]
        ({"min_edge": 0}, 0.0),  # [0, 0.5)
        ({"min_edge": 0.4999}, 0.4999),
        ({"taker_fee_rate": 0}, 0.0),  # [0, 0.2)
        ({"taker_fee_rate": 0.1999}, 0.1999),
        ({"match_window_hours": 336}, 336.0),  # (0, 336]
        ({"match_window_hours": 0.5}, 0.5),
        ({"min_liquidity_usd": 0}, 0.0),  # [0, inf)
        ({"stale_book_minutes": 0}, 0),  # [0, 43200]; 0 = never stale
        ({"stale_book_minutes": 43200}, 43200),
    ],
)
def test_validate_prefs_accepts_the_inclusive_boundaries(data: dict, expected) -> None:
    (key,) = data
    value = validate_prefs(data)[key]
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize(
    "data",
    [
        {"match_window_hours": 337},
        {"match_window_hours": 336.001},
        {"stale_book_minutes": 43201},
        {"kelly_fraction": 1.0001},
        {"max_stake_pct": 100.01},
        {"bankroll": True},  # bools are not numbers
        {"min_edge": False},
        {"stale_book_minutes": True},
    ],
)
def test_validate_prefs_rejects_just_past_the_boundary_and_bools(data: dict) -> None:
    with pytest.raises(ValueError):
        validate_prefs(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"espn_fallback_enabled": 1}, True),  # numeric booleans (an HTML form posts "1")
        ({"espn_fallback_enabled": 0}, False),
        ({"espn_fallback_enabled": 1.0}, True),
        ({"espn_fallback_enabled": 0.0}, False),
        ({"espn_fallback_enabled": ""}, False),  # unchecked checkbox
        ({"espn_fallback_enabled": "ON"}, True),
        ({"stale_book_minutes": 30.0}, 30),  # a whole float is an integer
    ],
)
def test_validate_prefs_accepts_form_shaped_values(data: dict, expected) -> None:
    (key,) = data
    value = validate_prefs(data)[key]
    assert value == expected and type(value) is type(expected)


def test_a_rejected_field_applies_nothing_at_all(db_session: Session) -> None:
    """update_prefs validates everything before it writes anything."""
    before = get_prefs(db_session)
    bankroll, fee = before.bankroll, before.taker_fee_rate
    with pytest.raises(ValueError, match="stale_book_minutes"):
        update_prefs(db_session, {"bankroll": 5000, "stale_book_minutes": "abc"})
    db_session.expire_all()
    after = get_prefs(db_session)
    assert (after.bankroll, after.taker_fee_rate) == (bankroll, fee)


def test_leagues_enabled_must_keep_at_least_one_league(db_session: Session) -> None:
    with pytest.raises(ValueError, match="at least one league"):
        validate_prefs({"leagues_enabled": ""})
    with pytest.raises(ValueError, match="at least one league"):
        validate_prefs({"leagues_enabled": []})
    with pytest.raises(ValueError, match="at least one league"):
        update_prefs(db_session, {"leagues_enabled": []})
    assert get_prefs(db_session).leagues_enabled == ["nfl", "nba", "mlb"]
