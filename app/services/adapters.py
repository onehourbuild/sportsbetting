"""ORM rows -> core dataclasses.

A Polymarket-only scan re-prices the live order books against the *stored* sportsbook
snapshot, and closing-line capture needs the same thing for a bet whose edge fell below
the minimum. Both rebuild `BookGame` / `PmMarket` values from the rows the last scan
persisted so `app.core` never has to know about the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.matching import team_key
from app.core.types import (
    BookGame,
    BookMarket,
    BookOutcome,
    PmMarket,
    PmOutcome,
)
from app.core.types import (
    BookQuote as CoreBookQuote,
)
from app.models import BookQuote, Game, Market

RESOLVED_LETTER: dict[int, str] = {0: "a", 1: "b"}
RESOLVED_INDEX: dict[str, int] = {"a": 0, "b": 1}


def pm_market_from_row(market: Market, game: Game | None) -> PmMarket:
    """A `PmMarket` carrying exactly what the `Market` (+ `Game`) rows know; prices None."""
    league = game.league if game is not None else "nfl"
    outcomes = (
        PmOutcome(
            market.outcome_a_token, market.outcome_a_name, market.outcome_a_key, None, None, None
        ),
        PmOutcome(
            market.outcome_b_token, market.outcome_b_name, market.outcome_b_key, None, None, None
        ),
    )
    resolved = RESOLVED_INDEX.get(market.resolved_outcome or "")
    return PmMarket(
        market_id=market.id,
        condition_id=market.condition_id or "",
        slug=market.slug or "",
        question=market.question or "",
        event_id=(game.pm_event_id if game is not None else None) or "",
        event_slug=(game.pm_event_slug if game is not None else None) or "",
        event_title="",
        league=league,  # type: ignore[arg-type]
        market_type=market.market_type,  # type: ignore[arg-type]
        line=market.line,
        line_team_key=market.line_team_key,
        outcomes=outcomes,
        game_start=game.start_time if game is not None else None,
        home_team_key=game.home_key if game is not None else None,
        away_team_key=game.away_key if game is not None else None,
        accepting_orders=bool(market.accepting_orders),
        closed=bool(market.closed),
        resolved_outcome_index=resolved,
        tick_size=market.tick_size,
        min_order_size=market.min_order_size,
        liquidity=market.liquidity,
        volume=market.volume,
        taker_fee_rate=None,
    )


def book_game_from_rows(game: Game, rows: Sequence[BookQuote]) -> BookGame | None:
    """Rebuild one `BookGame` from stored `BookQuote` rows of a single scan.

    None when the game lacks team keys or a start time, or when there are no rows.
    Team keys on the outcomes are re-resolved from the stored book labels.
    """
    if not rows or not game.home_key or not game.away_key or game.start_time is None:
        return None
    grouped: dict[str, dict[str, list[BookQuote]]] = {}
    for row in rows:
        grouped.setdefault(row.bookmaker, {}).setdefault(row.market_key, []).append(row)
    books: list[CoreBookQuote] = []
    for bookmaker in sorted(grouped):
        markets: list[BookMarket] = []
        for key in ("h2h", "spreads", "totals"):
            quotes = grouped[bookmaker].get(key)
            if not quotes:
                continue
            outcomes = tuple(
                BookOutcome(
                    name=q.outcome_name,
                    team_key=None if key == "totals" else team_key(q.outcome_name, game.league),  # type: ignore[arg-type]
                    price_american=q.price_american,
                    point=q.point,
                )
                for q in quotes
            )
            stamps = [q.last_update for q in quotes if q.last_update is not None]
            markets.append(
                BookMarket(
                    key=key,  # type: ignore[arg-type]
                    outcomes=outcomes,
                    last_update=max(stamps) if stamps else None,
                )
            )
        if markets:
            books.append(
                CoreBookQuote(bookmaker=bookmaker, title=bookmaker, markets=tuple(markets))
            )
    if not books:
        return None
    return BookGame(
        game_id=game.book_game_id or f"db:{game.id}",
        league=game.league,  # type: ignore[arg-type]
        commence_time=game.start_time,
        home_team_key=game.home_key,
        away_team_key=game.away_key,
        home_team_name=game.home_name or game.home_key,
        away_team_name=game.away_name or game.away_key,
        books=tuple(books),
    )


def latest_book_scan_id(
    session: Session,
    game_id: int,
    *,
    not_before: datetime | None = None,
    up_to_scan_id: int | None = None,
    not_after: datetime | None = None,
) -> int | None:
    """The most recent scan that stored book quotes for `game_id` (optionally only quotes
    fetched at or after `not_before`, at or before `not_after`, and only scans up to
    `up_to_scan_id`). `not_after` is what makes a snapshot a *closing* snapshot: the
    last one fetched before the game started."""
    stmt = select(func.max(BookQuote.scan_id)).where(BookQuote.game_id == game_id)
    if not_before is not None:
        stmt = stmt.where(BookQuote.fetched_at >= not_before)
    if not_after is not None:
        stmt = stmt.where(BookQuote.fetched_at <= not_after)
    if up_to_scan_id is not None:
        stmt = stmt.where(BookQuote.scan_id <= up_to_scan_id)
    value = session.scalar(stmt)
    return int(value) if value is not None else None


def stored_book_game(
    session: Session,
    game: Game,
    *,
    not_before: datetime | None = None,
    up_to_scan_id: int | None = None,
    not_after: datetime | None = None,
) -> tuple[BookGame | None, int | None]:
    """(BookGame rebuilt from the latest stored snapshot, that snapshot's scan id)."""
    scan_id = latest_book_scan_id(
        session,
        game.id,
        not_before=not_before,
        up_to_scan_id=up_to_scan_id,
        not_after=not_after,
    )
    if scan_id is None:
        return None, None
    rows = list(
        session.scalars(
            select(BookQuote)
            .where(BookQuote.game_id == game.id, BookQuote.scan_id == scan_id)
            .order_by(BookQuote.id.asc())
        )
    )
    return book_game_from_rows(game, rows), scan_id


__all__ = [
    "RESOLVED_INDEX",
    "RESOLVED_LETTER",
    "book_game_from_rows",
    "latest_book_scan_id",
    "pm_market_from_row",
    "stored_book_game",
]
