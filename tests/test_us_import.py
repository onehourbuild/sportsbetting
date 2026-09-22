"""Translating Polymarket US activity rows into ledger fills.

The translator's job is narrow and its refusals matter more than its successes. A .us
activity has to say *which* of a market's two outcomes was bought; where it does not, the
row is skipped rather than attached to whichever side happens to be first. A bet recorded
against the wrong side prices the opposite team, settles wrong, and looks entirely normal
until it does.

The field names come from notes taken off the live API on 2026-09-21. The outcome field is
the one those notes did not capture, so it is read across the plausible spellings and
refused when absent -- which also means the skip reason tells the next session exactly
which field to look for in a real payload.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.us_import import (
    SKIP_NO_MARKET_SLUG,
    SKIP_NO_OUTCOME,
    SKIP_NO_PRICE_OR_SIZE,
    SKIP_NO_TIMESTAMP,
    outcome_token,
    parse_activity,
)

SLUG = "asc-nfl-nyg-lar-2026-09-21-winner"
WHEN_MS = 1_758_400_000_000


def activity(**overrides):
    base = {
        "id": "act_0001",
        "marketSlug": SLUG,
        "outcomeIndex": 0,
        "price": {"value": "0.535", "currency": "USD"},
        "qty": "27",
        "createTime": WHEN_MS,
        "isAggressor": True,
        "side": "BUY",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- happy path


def test_translates_a_trade() -> None:
    fill, reason = parse_activity(activity())

    assert reason is None
    assert fill.tx == "act_0001"
    assert fill.asset == f"{SLUG}#0"
    assert fill.condition_id == SLUG
    assert fill.price == pytest.approx(0.535)
    assert fill.size == pytest.approx(27)
    assert fill.when == datetime.fromtimestamp(WHEN_MS / 1000, tz=UTC)


def test_unwraps_the_money_object_and_quoted_numbers() -> None:
    """.us wraps some numbers as {"value": ..., "currency": ...} and quotes others."""
    fill, _ = parse_activity(activity(price={"value": 0.42, "currency": "USD"}, qty=3))
    assert fill.price == pytest.approx(0.42) and fill.size == pytest.approx(3)

    fill, _ = parse_activity(activity(price="0.42", qty="3"))
    assert fill.price == pytest.approx(0.42) and fill.size == pytest.approx(3)


# ------------------------------------------------------------------- maker versus taker


def test_a_resting_order_is_a_maker_and_pays_no_fee() -> None:
    """isAggressor false means someone crossed into a resting order. Makers pay nothing."""
    fill, _ = parse_activity(activity(isAggressor=False))
    assert fill.mode == "maker"


def test_crossing_the_spread_is_a_taker() -> None:
    fill, _ = parse_activity(activity(isAggressor=True))
    assert fill.mode == "taker"


def test_an_absent_aggressor_flag_assumes_taker() -> None:
    """Unknown falls back to the expensive reading, which is the safe one to assume."""
    row = activity()
    row.pop("isAggressor")
    fill, _ = parse_activity(row)
    assert fill.mode == "taker"


# ------------------------------------------------------------- identifying the outcome


@pytest.mark.parametrize("key", ["outcomeIndex", "outcomeIdx", "outcome_index"])
def test_reads_the_outcome_index_under_any_of_its_plausible_names(key: str) -> None:
    row = activity()
    row.pop("outcomeIndex")
    row[key] = 1
    assert outcome_token(row, SLUG) == f"{SLUG}#1"


@pytest.mark.parametrize("key", ["tokenId", "assetId", "token_id", "asset_id"])
def test_an_explicit_token_wins_over_a_synthesised_one(key: str) -> None:
    row = activity(**{key: "explicit-token"})
    assert outcome_token(row, SLUG) == "explicit-token"


def test_a_numeric_string_index_is_accepted() -> None:
    row = activity(outcomeIndex="1")
    assert outcome_token(row, SLUG) == f"{SLUG}#1"


def test_a_boolean_is_not_an_index() -> None:
    """True is an int in Python, and `<slug>#1` from a flag would be the wrong side."""
    row = activity(outcomeIndex=True)
    assert outcome_token(row, SLUG) is None


def test_a_row_that_will_not_say_which_outcome_is_refused() -> None:
    """The expensive one: attaching this to outcome 0 would price the opposite team."""
    row = activity()
    row.pop("outcomeIndex")

    fill, reason = parse_activity(row)

    assert fill is None
    assert reason == SKIP_NO_OUTCOME
    # The reason names the fields to look for, so a live payload settles it in one pass.
    assert "outcomeIndex" in reason and "tokenId" in reason


# ---------------------------------------------------------------------- other refusals


def test_a_row_with_no_market_slug_is_refused() -> None:
    assert parse_activity(activity(marketSlug=""))[1] == SKIP_NO_MARKET_SLUG


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": None},
        {"qty": None},
        {"qty": 0},
        {"qty": -5},
        {"price": 0},
        {"price": 1},
        {"price": 1.5},
        {"price": "nonsense"},
    ],
    ids=[
        "no price",
        "no qty",
        "zero qty",
        "negative qty",
        "price 0",
        "price 1",
        "price >1",
        "junk",
    ],
)
def test_prices_and_sizes_outside_the_possible_are_refused(overrides: dict) -> None:
    """A share costs strictly between 0 and 1; anything else would poison the edge math."""
    assert parse_activity(activity(**overrides))[1] == SKIP_NO_PRICE_OR_SIZE


def test_a_row_with_no_timestamp_is_refused() -> None:
    """Without a time there is no kickoff cutoff, so an in-play fill could slip in."""
    assert parse_activity(activity(createTime=None))[1] == SKIP_NO_TIMESTAMP


def test_seconds_and_milliseconds_are_both_understood() -> None:
    in_ms, _ = parse_activity(activity(createTime=WHEN_MS))
    in_s, _ = parse_activity(activity(createTime=WHEN_MS // 1000))
    assert in_ms.when == in_s.when


def test_an_iso_timestamp_is_understood() -> None:
    fill, _ = parse_activity(activity(createTime="2026-09-21T00:00:00Z"))
    assert fill.when == datetime(2026, 9, 21, tzinfo=UTC)
