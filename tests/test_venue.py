"""The venue preference, and the taker fee that follows it.

Polymarket is two exchanges. polymarket.com blocks US residents from trading; polymarket.us
is the regulated US product and charges a taker fee coefficient of 0.0695 rather than 0.05
-- verified across all 814 markets of one NFL game on 2026-09-21 (docs/RESEARCH.md).

A fee set too low is not a cosmetic error. At a 50c price, 0.05 costs 1.25c a share and
0.0695 costs 1.74c; the half-cent difference is most of a 2% edge, so a .us account priced
with the .com fee sees edges that are not there. That is the exact failure docs/DECISIONS.md
records from the near-zero fee override, so the venue owns the fee rather than leaving one
number to be remembered.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db import LEGACY_TAKER_FEE_RATE, add_missing_columns, reconcile_venue_fee
from app.models import DEFAULT_VENUE, VENUE_TAKER_FEE, VENUES
from app.services import prefs as prefs_service


def test_the_two_venues_charge_different_fees() -> None:
    assert VENUE_TAKER_FEE["polymarket_us"] == 0.0695
    assert VENUE_TAKER_FEE["polymarket_com"] == 0.05
    assert set(VENUE_TAKER_FEE) == set(VENUES)


def test_the_default_venue_matches_the_client_the_app_actually_reads() -> None:
    """Defaulting to .us while reading .com data would claim fees the data cannot support."""
    assert DEFAULT_VENUE == "polymarket_com"


@pytest.mark.parametrize("venue", VENUES)
def test_known_venues_validate(venue: str) -> None:
    assert prefs_service.validate_prefs({"venue": venue}) == {"venue": venue}


def test_venue_is_normalised_and_unknown_ones_are_rejected() -> None:
    assert prefs_service.validate_prefs({"venue": "  POLYMARKET_US "}) == {"venue": "polymarket_us"}
    with pytest.raises(ValueError, match="venue must be one of"):
        prefs_service.validate_prefs({"venue": "kalshi"})


def test_switching_venue_carries_an_untouched_fee_with_it(db_session: Session) -> None:
    prefs = prefs_service.get_prefs(db_session)
    assert prefs.venue == "polymarket_com"
    assert prefs.taker_fee_rate == 0.05

    updated = prefs_service.update_prefs(db_session, {"venue": "polymarket_us"})

    assert updated.venue == "polymarket_us"
    assert updated.taker_fee_rate == 0.0695, "the fee must follow the venue, or edges lie"


def test_switching_venue_leaves_a_deliberately_chosen_fee_alone(db_session: Session) -> None:
    prefs_service.update_prefs(db_session, {"taker_fee_rate": "0.03"})

    updated = prefs_service.update_prefs(db_session, {"venue": "polymarket_us"})

    assert updated.venue == "polymarket_us"
    assert updated.taker_fee_rate == 0.03, "an owner's own rate is a decision, not a default"


def test_saving_without_changing_venue_does_not_disturb_the_fee(db_session: Session) -> None:
    prefs_service.update_prefs(db_session, {"venue": "polymarket_us"})
    prefs_service.update_prefs(db_session, {"bankroll": "250"})

    prefs = prefs_service.get_prefs(db_session)
    assert prefs.venue == "polymarket_us"
    assert prefs.taker_fee_rate == 0.0695


def test_a_form_that_omits_venue_leaves_it_alone(db_session: Session) -> None:
    """The select always submits a value, so blank means 'not in this form', not 'clear it'.

    Silently resetting the venue would silently reset the fee with it, which is the whole
    failure this preference exists to prevent.
    """
    from app.routes.settings import values_to_update

    base = {
        "bankroll": "250",
        "kelly_fraction": "0.25",
        "max_stake_pct": "2",
        "min_edge": "0.02",
        "taker_fee_rate": "0.0695",
        "match_window_hours": "36",
        "min_liquidity_usd": "100",
        "stale_book_minutes": "720",
        "devig_method": "power",
        "bookmakers": "pinnacle",
        "book_weights": "pinnacle=3",
        "leagues_enabled": ["nfl"],
    }
    prefs_service.update_prefs(db_session, {"venue": "polymarket_us"})

    data = values_to_update({**base, "venue": ""})
    assert "venue" not in data

    prefs_service.update_prefs(db_session, data)
    assert prefs_service.get_prefs(db_session).venue == "polymarket_us"

    assert values_to_update({**base, "venue": "polymarket_com"})["venue"] == "polymarket_com"


# ------------------------------------------------------- the one-time migration fix-up


def test_reconcile_corrects_a_pre_venue_database(db_session: Session, engine: Engine) -> None:
    """A row written before venues knew only the .com fee; on .us that number is wrong."""
    prefs = prefs_service.get_prefs(db_session)
    prefs.venue = "polymarket_us"
    prefs.taker_fee_rate = LEGACY_TAKER_FEE_RATE
    db_session.commit()

    changed = reconcile_venue_fee(engine, ["prefs.venue"])

    assert changed is True
    db_session.expire_all()
    assert prefs_service.get_prefs(db_session).taker_fee_rate == 0.0695


def test_reconcile_does_nothing_unless_the_venue_column_was_just_added(
    db_session: Session, engine: Engine
) -> None:
    prefs = prefs_service.get_prefs(db_session)
    prefs.venue = "polymarket_us"
    prefs.taker_fee_rate = LEGACY_TAKER_FEE_RATE
    db_session.commit()

    assert reconcile_venue_fee(engine, []) is False
    assert reconcile_venue_fee(engine, ["prefs.something_else"]) is False
    db_session.expire_all()
    assert prefs_service.get_prefs(db_session).taker_fee_rate == LEGACY_TAKER_FEE_RATE


def test_reconcile_leaves_a_customised_rate_alone(db_session: Session, engine: Engine) -> None:
    prefs = prefs_service.get_prefs(db_session)
    prefs.venue = "polymarket_us"
    prefs.taker_fee_rate = 0.042
    db_session.commit()

    assert reconcile_venue_fee(engine, ["prefs.venue"]) is False
    db_session.expire_all()
    assert prefs_service.get_prefs(db_session).taker_fee_rate == 0.042


def test_reconcile_is_a_no_op_for_a_com_account(db_session: Session, engine: Engine) -> None:
    """0.05 is correct on .com, so there is nothing to correct."""
    prefs_service.get_prefs(db_session)

    assert reconcile_venue_fee(engine, ["prefs.venue"]) is False
    db_session.expire_all()
    assert prefs_service.get_prefs(db_session).taker_fee_rate == 0.05


def test_the_venue_column_is_added_to_an_existing_database(tmp_path) -> None:
    """Their install upgrades in place: there is no Alembic, so init_db appends columns.

    Built as a separate database because SQLAlchemy's Inspector caches reflection per
    engine: a column dropped underneath a live engine still looks present.
    """
    from sqlalchemy import create_engine

    from app.db import Base

    url = f"sqlite:///{tmp_path / 'pre-venue.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE prefs DROP COLUMN venue")
    engine.dispose()

    engine = create_engine(url)
    try:
        added = add_missing_columns(engine)
        assert "prefs.venue" in added
        # And the restored column is usable: a row written through it comes back with the
        # default rather than NULL, which a NOT NULL column would otherwise reject.
        with Session(engine) as session:
            assert prefs_service.get_prefs(session).venue == DEFAULT_VENUE
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------ the UI


def test_settings_page_offers_both_venues_and_saves_the_choice(client, db_session: Session) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    assert 'name="venue"' in page.text
    assert "polymarket_us" in page.text and "polymarket_com" in page.text

    form = {
        key: str(value)
        for key, value in prefs_service.get_prefs(db_session).__dict__.items()
        if not key.startswith("_")
    }
    response = client.post(
        "/settings",
        data={
            "bankroll": "250",
            "kelly_fraction": "0.25",
            "max_stake_pct": "2",
            "min_edge": "0.02",
            "taker_fee_rate": "0.05",
            "match_window_hours": "36",
            "min_liquidity_usd": "100",
            "stale_book_minutes": "720",
            "devig_method": "power",
            "bookmakers": "pinnacle",
            "book_weights": "pinnacle=3",
            "leagues_enabled": ["nfl"],
            "venue": "polymarket_us",
        },
    )

    assert response.status_code == 200
    assert "Saved." in response.text
    db_session.expire_all()
    prefs = prefs_service.get_prefs(db_session)
    assert prefs.venue == "polymarket_us"
    assert prefs.taker_fee_rate == 0.0695, "picking .us in the form must move the fee too"
    assert form  # guards against the dict comprehension above silently yielding nothing


def test_settings_page_warns_when_the_fee_does_not_match_the_venue(
    client, db_session: Session
) -> None:
    prefs = prefs_service.get_prefs(db_session)
    prefs.venue = "polymarket_us"
    prefs.taker_fee_rate = 0.05
    db_session.commit()

    page = client.get("/settings")

    assert "Check this." in page.text
    assert "0.0695" in page.text
