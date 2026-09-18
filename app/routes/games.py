"""Game detail: every market of a game with the latest Polymarket quotes, the opportunity
per outcome (if any), a per-book table, the ask depth and the maker limit price."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.matching import team_key
from app.core.types import MARKET_TYPE_TO_BOOK_KEY
from app.db import get_session
from app.models import BookQuote, Game, Market, Opportunity, PmQuote
from app.routes.edges import (
    fmt_line,
    get_now,
    iso_utc,
    latest_ok_scan,
    market_label,
    polymarket_url,
    side_label,
)
from app.services import prefs as prefs_service
from app.templating import american, templates

router = APIRouter(tags=["games"])

MARKET_ORDER: dict[str, int] = {"moneyline": 0, "spread": 1, "total": 2}
DEPTH_LEVELS = 5


# --------------------------------------------------------------------------- view models


@dataclass
class BookRow:
    """One bookmaker's prices for the two outcomes of a market."""

    bookmaker: str
    a: BookQuote | None = None
    b: BookQuote | None = None
    stale: bool = False

    @staticmethod
    def _text(quote: BookQuote | None) -> str:
        if quote is None:
            return "—"
        text = american(quote.price_american)
        if quote.point is not None:
            text += f" ({fmt_line(quote.point, signed=True)})"
        return text

    @property
    def a_text(self) -> str:
        return self._text(self.a)

    @property
    def b_text(self) -> str:
        return self._text(self.b)

    @property
    def last_update(self) -> datetime | None:
        stamps = [q.last_update for q in (self.a, self.b) if q is not None and q.last_update]
        return max(stamps) if stamps else None


@dataclass
class OutcomeView:
    index: int
    letter: str  # "a" | "b" (matches Market.resolved_outcome)
    name: str
    key: str | None
    token: str
    side: str
    quote: PmQuote | None = None
    opp: Opportunity | None = None
    asks: list[dict[str, float]] = field(default_factory=list)
    bids: list[dict[str, float]] = field(default_factory=list)

    @property
    def limit_price(self) -> float | None:
        return self.opp.limit_price if self.opp is not None else None


@dataclass
class MarketView:
    market: Market
    label: str
    book_key: str
    outcomes: list[OutcomeView]
    book_rows: list[BookRow]


# --------------------------------------------------------------------------- helpers


def depth_levels(
    raw: Any, limit: int = DEPTH_LEVELS, descending: bool = False
) -> list[dict[str, float]]:
    """Parse a stored depth list into [{price, size, cum_usd}], best level first.

    Accepts `[[price, size], ...]` or `[{"price": p, "size": s}, ...]`, numbers or strings,
    or the same as a JSON string. Anything unparseable is skipped, never raised.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list | tuple):
        return []
    levels: list[tuple[float, float]] = []
    for item in raw:
        price: Any
        size: Any
        if isinstance(item, dict):
            price, size = item.get("price"), item.get("size")
        elif isinstance(item, list | tuple) and len(item) >= 2:
            price, size = item[0], item[1]
        else:
            continue
        try:
            levels.append((float(price), float(size)))
        except (TypeError, ValueError):
            continue
    levels.sort(key=lambda lv: lv[0], reverse=descending)
    out: list[dict[str, float]] = []
    cum = 0.0
    for price, size in levels[:limit]:
        cum += price * size
        out.append({"price": price, "size": size, "cum_usd": cum})
    return out


def _outcome_slot(market: Market, league: str, quote: BookQuote) -> int | None:
    """Which market outcome (0=a, 1=b) a book row describes, or None."""
    name = (quote.outcome_name or "").strip()
    names = ((market.outcome_a_name or "").strip(), (market.outcome_b_name or "").strip())
    if market.market_type == "total":
        low = name.lower()
        for idx, oname in enumerate(names):
            if oname.lower() == low:
                return idx
        return None
    key = team_key(name, league)  # type: ignore[arg-type]
    if key is not None:
        if key == market.outcome_a_key:
            return 0
        if key == market.outcome_b_key:
            return 1
    low = name.lower()
    for idx, oname in enumerate(names):
        oname_low = oname.lower()
        if oname_low and (oname_low == low or oname_low in low or low in oname_low):
            return idx
    return None


def _expected_point(market: Market, slot: int) -> float | None:
    if market.line is None:
        return None
    if market.market_type == "total":
        return market.line
    if market.market_type == "spread":
        key = market.outcome_a_key if slot == 0 else market.outcome_b_key
        if market.line_team_key and key and key != market.line_team_key:
            return -market.line
        return market.line
    return None


def _points_match(expected: float | None, actual: float | None) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    return abs(expected - actual) < 1e-9


def book_rows_for(
    market: Market,
    league: str,
    quotes: list[BookQuote],
    stale_after: timedelta | None,
    now: datetime,
) -> list[BookRow]:
    """Group the latest book quotes of one market key into one row per bookmaker."""
    key = MARKET_TYPE_TO_BOOK_KEY.get(market.market_type, market.market_type)
    rows: dict[str, BookRow] = {}
    for quote in quotes:
        if quote.market_key != key:
            continue
        slot = _outcome_slot(market, league, quote)
        if slot is None:
            continue
        row = rows.setdefault(quote.bookmaker, BookRow(bookmaker=quote.bookmaker))
        attr = "a" if slot == 0 else "b"
        current: BookQuote | None = getattr(row, attr)
        expected = _expected_point(market, slot)
        if current is None or (
            not _points_match(expected, current.point) and _points_match(expected, quote.point)
        ):
            setattr(row, attr, quote)
    out = list(rows.values())
    if stale_after is not None:
        for row in out:
            stamp = row.last_update
            row.stale = stamp is not None and (now - stamp) > stale_after
    return out


def latest_pm_quotes(session: Session, market_id: str) -> dict[str, PmQuote]:
    stmt = (
        select(PmQuote)
        .where(PmQuote.market_id == market_id)
        .order_by(PmQuote.fetched_at.desc(), PmQuote.id.desc())
        .limit(50)
    )
    out: dict[str, PmQuote] = {}
    for quote in session.scalars(stmt):
        out.setdefault(quote.token, quote)
    return out


def latest_book_quotes(session: Session, game_id: int) -> list[BookQuote]:
    latest_scan_id = session.scalar(
        select(func.max(BookQuote.scan_id)).where(BookQuote.game_id == game_id)
    )
    if latest_scan_id is None:
        return []
    stmt = (
        select(BookQuote)
        .where(BookQuote.game_id == game_id, BookQuote.scan_id == latest_scan_id)
        .order_by(BookQuote.bookmaker.asc(), BookQuote.id.asc())
    )
    return list(session.scalars(stmt))


def build_market_views(session: Session, game: Game, now: datetime) -> list[MarketView]:
    prefs = prefs_service.get_prefs(session)
    stale_after = (
        timedelta(minutes=prefs.stale_book_minutes) if prefs.stale_book_minutes > 0 else None
    )
    markets = sorted(
        game.markets,
        key=lambda m: (MARKET_ORDER.get(m.market_type, 9), m.line or 0.0, m.id),
    )
    scan = latest_ok_scan(session)
    opps: dict[tuple[str, str], Opportunity] = {}
    if scan is not None and markets:
        stmt = select(Opportunity).where(
            Opportunity.scan_id == scan.id,
            Opportunity.market_id.in_([m.id for m in markets]),
        )
        for opp in session.scalars(stmt):
            opps[(opp.market_id, opp.token)] = opp
    book_quotes = latest_book_quotes(session, game.id)

    views: list[MarketView] = []
    for market in markets:
        quotes = latest_pm_quotes(session, market.id)
        outcomes: list[OutcomeView] = []
        specs = (
            (0, "a", market.outcome_a_name, market.outcome_a_key, market.outcome_a_token),
            (1, "b", market.outcome_b_name, market.outcome_b_key, market.outcome_b_token),
        )
        for index, letter, name, key, token in specs:
            quote = quotes.get(token)
            outcomes.append(
                OutcomeView(
                    index=index,
                    letter=letter,
                    name=name,
                    key=key,
                    token=token,
                    side=side_label(market, key, name),
                    quote=quote,
                    opp=opps.get((market.id, token)),
                    asks=depth_levels(quote.ask_depth_json) if quote else [],
                    bids=depth_levels(quote.bid_depth_json, descending=True) if quote else [],
                )
            )
        views.append(
            MarketView(
                market=market,
                label=market_label(market),
                book_key=MARKET_TYPE_TO_BOOK_KEY.get(market.market_type, market.market_type),
                outcomes=outcomes,
                book_rows=book_rows_for(market, game.league, book_quotes, stale_after, now),
            )
        )
    return views


# --------------------------------------------------------------------------- routes


@router.get("/games/{game_id}", response_class=HTMLResponse)
async def game_detail(
    request: Request,
    game_id: int,
    session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> HTMLResponse:
    game = session.get(Game, game_id)
    if game is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"what": f"Game {game_id}", "hint": "It may not have been scanned yet."},
            status_code=404,
        )
    context = {
        "game": game,
        "start_iso": iso_utc(game.start_time) or "",
        "markets": build_market_views(session, game, now),
        "pm_url": polymarket_url(game),
        "now": now,
    }
    return templates.TemplateResponse(request, "game.html", context)
