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
