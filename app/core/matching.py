"""Team aliases, market-to-game matching, fair probability per outcome.

Contract: docs/ARCHITECTURE.md.

STUB — replaced by the matching implementer. TEAM_ALIASES must cover all 32 NFL,
30 NBA and 30 MLB teams (canonical key -> display name) per the contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.types import BookGame, FairProb, League, PmMarket

TEAM_ALIASES: dict[League, dict[str, str]] = {}  # placeholder; canonical key -> display name


def team_key(name: str, league: League) -> str | None:
    """Canonical abbreviation for any accepted spelling of a team name; None if unknown.

    Case/punctuation-insensitive; accepts full name, city, nickname, abbreviation,
    Odds API names, Polymarket short names and ESPN displayName.
    """
    raise NotImplementedError


def match_games(
    markets: Sequence[PmMarket],
    book_games: Sequence[BookGame],
    window_hours: float = 36.0,
) -> dict[str, BookGame]:
    """market_id -> BookGame: same league, same {home, away} pair (order-insensitive),
    |start difference| <= window; nearest start wins on ties."""
    raise NotImplementedError


def fair_for_outcome(
    market: PmMarket,
    outcome_index: int,
    game: BookGame,
    weights: Mapping[str, float],
    method: str = "power",
    default_weight: float = 1.0,
) -> FairProb | None:
    """Consensus fair probability for one outcome using only books quoting the same line."""
    raise NotImplementedError
