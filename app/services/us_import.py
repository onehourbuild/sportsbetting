"""Translate Polymarket US activity rows into the ledger's `Fill` shape.

The .us feed is a different shape from .com's, but everything downstream -- grouping,
deduplication, the kickoff cutoff, fair value at bet time, the Bet row -- is identical. So
this module only translates, and hands off to `wallet_import.import_fills`. Two feeds, one
set of rules about what becomes a bet.

Two differences from .com are worth stating.

**`isAggressor` is the maker/taker flag the .com feed never had.** A maker pays the limit
price and no fee at all; a taker pays the fee on top. Assuming taker for a maker fill
overstates what the bet cost and understates the edge it was taken at, so where .us says,
the ledger uses it rather than guessing conservatively.

**The outcome has to be identifiable.** `PolymarketUsClient` synthesises token ids as
``<marketSlug>#<index>``, because .us has no CLOB token id. An activity row therefore has
to say *which* of a market's two outcomes was bought. Where it does not, the row is skipped
with a reason naming the fields that were looked for -- not attached to whichever outcome
happens to be first. A bet recorded against the wrong side prices the opposite team and
settles wrong, and it would look entirely normal until it did.

The field names below come from notes taken off the live API on 2026-09-21
(docs/RESEARCH.md). The outcome field is the one that was *not* captured, so it is read
defensively across the plausible spellings and refused when absent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.services.wallet_import import Fill

log = logging.getLogger(__name__)

SOURCE_US = "polymarket_us"
SKIP_NO_OUTCOME = (
    "cannot tell which outcome this trade was on (no outcomeIndex / outcomeIdx / tokenId / assetId)"
)
SKIP_NO_MARKET_SLUG = "no marketSlug"
SKIP_NO_PRICE_OR_SIZE = "no usable price or quantity"
SKIP_NO_TIMESTAMP = "no createTime"

# Where the outcome index might live, in the order they would be trusted.
OUTCOME_INDEX_KEYS = ("outcomeIndex", "outcomeIdx", "outcome_index")
TOKEN_KEYS = ("tokenId", "assetId", "token_id", "asset_id")


def _as_float(value: Any) -> float | None:
    """.us wraps some numbers as {"value": "0.53", "currency": "USD"} and quotes others."""
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _as_when(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    from app.clients.polymarket import parse_iso_utc

    return parse_iso_utc(value)


def outcome_token(raw: Mapping[str, Any], slug: str) -> str | None:
    """The synthesised token id this trade belongs to, or None if the row will not say.

    Returning None is the whole point of this function: the caller skips the row rather
    than attaching it to an arbitrary side.
    """
    for key in TOKEN_KEYS:
        token = raw.get(key)
        if isinstance(token, str) and token.strip():
            return token.strip()
    for key in OUTCOME_INDEX_KEYS:
        index = raw.get(key)
        if isinstance(index, bool):
            continue
        if isinstance(index, int):
            return f"{slug}#{index}"
        if isinstance(index, str) and index.strip().isdigit():
            return f"{slug}#{int(index)}"
    return None


def parse_activity(raw: Mapping[str, Any]) -> tuple[Fill | None, str | None]:
    """(fill, skip_reason). Exactly one of the two is ever set."""
    slug = str(raw.get("marketSlug") or "").strip()
    if not slug:
        return None, SKIP_NO_MARKET_SLUG

    token = outcome_token(raw, slug)
    if token is None:
        return None, SKIP_NO_OUTCOME

    price = _as_float(raw.get("price"))
    size = _as_float(raw.get("qty"))
    if price is None or size is None or size <= 0 or not (0.0 < price < 1.0):
        return None, SKIP_NO_PRICE_OR_SIZE

    when = _as_when(raw.get("createTime"))
    if when is None:
        return None, SKIP_NO_TIMESTAMP

    side = str(raw.get("side") or "BUY").strip().upper()
    # `isAggressor` true means this order crossed the spread: a taker. False means it was
    # resting and someone else crossed into it: a maker, who pays no fee. Absent means
    # unknown, and unknown falls back to taker, which is the expensive reading and so the
    # safe one to assume.
    aggressor = raw.get("isAggressor")
    mode = "maker" if aggressor is False else "taker"

    return (
        Fill(
            # .us has no transaction hash; the activity id is the unit of identity, and
            # keying on it is what makes a re-import a no-op.
            tx=str(raw.get("id") or ""),
            asset=token,
            # The slug plays the conditionId role throughout the .us path.
            condition_id=slug,
            side=side,
            price=price,
            size=size,
            when=when,
            title=str(raw.get("title") or raw.get("marketTitle") or slug),
            mode=mode,
        ),
        None,
    )


__all__ = [
    "OUTCOME_INDEX_KEYS",
    "SKIP_NO_MARKET_SLUG",
    "SKIP_NO_OUTCOME",
    "SKIP_NO_PRICE_OR_SIZE",
    "SKIP_NO_TIMESTAMP",
    "SOURCE_US",
    "TOKEN_KEYS",
    "outcome_token",
    "parse_activity",
]
