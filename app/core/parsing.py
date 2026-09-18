"""Polymarket question / line parsing (used by clients.polymarket). Contract: docs/ARCHITECTURE.md.

STUB — replaced by the Polymarket client implementer.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.types import League, MarketType


def parse_market_type(sports_market_type: str | None, question: str) -> MarketType | None:
    """Map Gamma `sportsMarketType` (or the question text) to moneyline|spread|total.

    Returns None for market types we ignore.
    """
    raise NotImplementedError


def parse_spread(
    question: str, line: float | None, outcomes: Sequence[str], league: League
) -> tuple[float, str] | None:
    """(signed line, line_team_key) from e.g. "Spread: Chiefs (-3.5)"; falls back to `line`."""
    raise NotImplementedError


def parse_total(question: str, line: float | None) -> float | None:
    """The total from "O/U 47.5" style questions; falls back to `line`."""
    raise NotImplementedError


def parse_json_list(value: str | list | None) -> list:
    """Gamma sends `outcomes`, `outcomePrices`, `clobTokenIds` as JSON-encoded strings."""
    raise NotImplementedError
