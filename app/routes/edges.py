"""Home: edges list, the two refresh buttons, and the JSON debug endpoint.

Display data is read straight from the ORM. The services layer is called only for
actions (`run_scan_default`) and for the two quota helpers, which are wrapped so the
page still renders while they are stubbed or when the Odds API is unreachable.

This module also hosts the small display helpers the other route modules share
(`latest_ok_scan`, `opportunity_rows`, `market_label`, ...). They live here rather than
in a separate module because the route files are the UI's whole ownership surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Game, Market, Opportunity, Prefs, Scan
from app.services import prefs as prefs_service
from app.services import scan as scan_service
from app.settings import Settings, get_settings
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["edges"])

LEAGUE_FILTERS: tuple[str, ...] = ("all", "nfl", "nba", "mlb")
SCAN_KINDS: frozenset[str] = frozenset({"poly", "books", "both"})
POLYMARKET_EVENT_URL = "https://polymarket.com/event/{slug}"
MINUS = "−"  # typographic minus: easier to read than a hyphen on a phone


# --------------------------------------------------------------------------- helpers


def app_settings(request: Request) -> Settings:
    """The Settings the app was built with (tests build the app with their own)."""
    settings = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_settings()


def normalize_league(value: str | None) -> str:
    league = (value or "all").strip().lower()
    return league if league in LEAGUE_FILTERS else "all"


def fmt_line(value: float | None, signed: bool = False) -> str:
    """3.5 -> '3.5'; -3.5 -> '−3.5'; signed=True adds '+' to positives."""
    if value is None:
        return ""
    text = f"{abs(value):g}"
    if value < 0:
        return f"{MINUS}{text}"
    return f"+{text}" if signed else text


def market_label(market: Market) -> str:
    """'Moneyline' | 'Spread KC −3.5' | 'Total 47.5'."""
    if market.market_type == "spread":
        team = f"{market.line_team_key} " if market.line_team_key else ""
        return f"Spread {team}{fmt_line(market.line, signed=True)}".rstrip()
    if market.market_type == "total":
        return f"Total {fmt_line(market.line)}".rstrip()
    return "Moneyline"


def side_label(market: Market, outcome_key: str | None, outcome_name: str) -> str:
    """The outcome as the bettor reads it: 'Bills +3.5', 'Under 47.5', 'Chiefs'."""
    if market.market_type == "spread" and market.line is not None:
        if market.line_team_key and outcome_key and outcome_key != market.line_team_key:
            return f"{outcome_name} {fmt_line(-market.line, signed=True)}"
        return f"{outcome_name} {fmt_line(market.line, signed=True)}"
    if market.market_type == "total" and market.line is not None:
        return f"{outcome_name} {fmt_line(market.line)}"
    return outcome_name


def polymarket_url(game: Game | None, market: Market | None = None) -> str | None:
    slug = game.pm_event_slug if game is not None else None
    if not slug and market is not None and market.slug:
        slug = market.slug
    return POLYMARKET_EVENT_URL.format(slug=slug) if slug else None


def latest_scan(session: Session, ok_only: bool = False) -> Scan | None:
    stmt = select(Scan).order_by(Scan.started_at.desc(), Scan.id.desc()).limit(1)
    if ok_only:
        stmt = stmt.where(Scan.ok.is_(True))
    return session.scalars(stmt).first()


def latest_ok_scan(session: Session) -> Scan | None:
    return latest_scan(session, ok_only=True)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class OppRow:
    """One opportunity card: the ORM rows plus the derived display strings."""

    opp: Opportunity
    market: Market
    game: Game | None

    @property
    def league(self) -> str:
        return self.game.league if self.game is not None else ""

    @property
    def title(self) -> str:
        if self.game is None:
            return self.market.question
        away = self.game.away_name or self.game.away_key or "?"
        home = self.game.home_name or self.game.home_key or "?"
        return f"{away} @ {home}"

    @property
    def short_title(self) -> str:
        if self.game is None:
            return self.market.question
        away = self.game.away_key or self.game.away_name or "?"
        home = self.game.home_key or self.game.home_name or "?"
        return f"{away} @ {home}"

    @property
    def market_label(self) -> str:
        return market_label(self.market)

    @property
    def side(self) -> str:
        return side_label(self.market, self.opp.outcome_key, self.opp.outcome_name)

    @property
    def polymarket_url(self) -> str | None:
        return polymarket_url(self.game, self.market)

    @property
    def start_time(self) -> datetime | None:
        return self.game.start_time if self.game is not None else None

    @property
    def start_iso(self) -> str:
        return iso_utc(self.start_time) or ""

    def to_dict(self) -> dict[str, Any]:
        game = self.game
        return {
            "opportunity_id": self.opp.id,
            "scan_id": self.opp.scan_id,
            "league": self.league,
            "game_id": game.id if game is not None else None,
            "away": game.away_name if game is not None else None,
            "home": game.home_name if game is not None else None,
            "start_time": iso_utc(self.start_time),
            "market_id": self.market.id,
            "market_type": self.market.market_type,
            "market_label": self.market_label,
            "line": self.market.line,
            "outcome_key": self.opp.outcome_key,
            "outcome_name": self.opp.outcome_name,
            "side": self.side,
            "token": self.opp.token,
            "ask": self.opp.ask,
            "effective_price": self.opp.effective_price,
            "fair_prob": self.opp.fair_prob,
            "fair_method": self.opp.fair_method,
            "edge": self.opp.edge,
            "ev_per_dollar": self.opp.ev_per_dollar,
            "kelly": self.opp.kelly,
            "suggested_stake": self.opp.suggested_stake,
            "fill_price": self.opp.fill_price,
            "limit_price": self.opp.limit_price,
            "n_books": self.opp.n_books,
            "books_used": list(self.opp.books_used or []),
            "computed_at": iso_utc(self.opp.computed_at),
            "event_slug": game.pm_event_slug if game is not None else None,
            "polymarket_url": self.polymarket_url,
        }


def opportunity_rows(
    session: Session, scan: Scan | None, min_edge: float, league: str = "all"
) -> list[OppRow]:
    """Opportunities of `scan` with edge >= min_edge, best edge first."""
    if scan is None:
        return []
    stmt = (
        select(Opportunity, Market, Game)
        .join(Market, Opportunity.market_id == Market.id)
        .outerjoin(Game, Market.game_id == Game.id)
        .where(Opportunity.scan_id == scan.id, Opportunity.edge >= min_edge)
        .order_by(Opportunity.edge.desc(), Opportunity.id.asc())
    )
    if league != "all":
        stmt = stmt.where(Game.league == league)
    return [OppRow(opp=o, market=m, game=g) for o, m, g in session.execute(stmt).all()]


def opportunity_row(session: Session, opportunity_id: int) -> OppRow | None:
    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        return None
    market = session.get(Market, opp.market_id)
    if market is None:
        return None
    game = session.get(Game, market.game_id) if market.game_id is not None else None
    return OppRow(opp=opp, market=market, game=game)


def safe_quota_status(session: Session) -> dict[str, Any]:
    """`scan_service.quota_status` with a display-only fallback; never breaks a page."""
    try:
        status = scan_service.quota_status(session) or {}
    except NotImplementedError:
        status = {}
    except Exception:  # noqa: BLE001 - a quota badge must never take the page down
        log.exception("quota_status failed")
        status = {}
    return {
        "remaining": status.get("remaining"),
        "used": status.get("used"),
        "as_of": status.get("as_of"),
    }


def safe_books_cost(prefs: Prefs) -> int | None:
    try:
        return int(scan_service.estimate_books_cost(prefs))
    except NotImplementedError:
        return None
    except Exception:  # noqa: BLE001 - a cost hint must never take the page down
        log.exception("estimate_books_cost failed")
        return None


def scan_context(request: Request, session: Session, prefs: Prefs) -> dict[str, Any]:
    """Everything the scan bar / status partial needs."""
    settings = app_settings(request)
    return {
        "last_scan": latest_scan(session),
        "quota": safe_quota_status(session),
        "books_cost": safe_books_cost(prefs),
        "has_odds_key": bool(settings.odds_api_key),
        "demo_mode": bool(settings.demo_mode),
        "now": datetime.now(UTC),
    }


def edges_context(
    request: Request,
    session: Session,
    league: str,
    toast: str | None = None,
    toast_error: bool = False,
) -> dict[str, Any]:
    prefs = prefs_service.get_prefs(session)
    scan = latest_ok_scan(session)
    rows = opportunity_rows(session, scan, prefs.min_edge, league)
    context: dict[str, Any] = {
        "prefs": prefs,
        "scan": scan,
        "rows": rows,
        "league": league,
        "league_filters": LEAGUE_FILTERS,
        "toast_message": toast,
        "toast_error": toast_error,
    }
    context.update(scan_context(request, session, prefs))
    return context


def scan_summary(result: Any) -> str:
    """One-line toast text for a finished scan."""
    n_opps = getattr(result, "n_opps", 0) or 0
    n_matched = getattr(result, "n_matched", 0) or 0
    n_markets = getattr(result, "n_markets", 0) or 0
    parts = [
        f"Scan done: {n_opps} edge{'s' if n_opps != 1 else ''}",
        f"{n_matched}/{n_markets} markets matched",
    ]
    used = getattr(result, "credits_used", None)
    remaining = getattr(result, "credits_remaining", None)
    if used:
        parts.append(f"{used} credits used")
    if remaining is not None:
        parts.append(f"{remaining} left")
    errors = list(getattr(result, "errors", None) or [])
    if errors:
        parts.append(f"{len(errors)} error{'s' if len(errors) != 1 else ''}: {errors[0]}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- routes


@router.get("/", response_class=HTMLResponse)
async def edges_home(
    request: Request, league: str = "all", session: Session = Depends(get_session)
) -> HTMLResponse:
    context = edges_context(request, session, normalize_league(league))
    return templates.TemplateResponse(request, "edges.html", context)


@router.post("/scan", response_class=HTMLResponse)
async def run_scan(
    request: Request,
    kind: str = "poly",
    league: str = "all",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Run a scan; respond with the refreshed list + an out-of-band toast (always HTTP 200
    so htmx swaps the toast in on failure too)."""
    league = normalize_league(league)
    kind = (kind or "").strip().lower()
    if kind not in SCAN_KINDS:
        context = edges_context(
            request, session, league, toast=f"Unknown scan kind '{kind}'.", toast_error=True
        )
        return templates.TemplateResponse(request, "partials/scan_result.html", context)

    prefs = prefs_service.get_prefs(session)
    leagues = list(prefs.leagues_enabled or [])
    try:
        result = scan_service.run_scan_default(session, kind, leagues=leagues)
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner as a toast
        log.exception("scan failed (kind=%s)", kind)
        detail = str(exc) or type(exc).__name__
        context = edges_context(
            request, session, league, toast=f"Scan failed: {detail}", toast_error=True
        )
        return templates.TemplateResponse(request, "partials/scan_result.html", context)

    has_errors = bool(getattr(result, "errors", None))
    context = edges_context(
        request, session, league, toast=scan_summary(result), toast_error=has_errors
    )
    return templates.TemplateResponse(request, "partials/scan_result.html", context)


@router.get("/api/opportunities")
async def api_opportunities(
    league: str = "all", session: Session = Depends(get_session)
) -> JSONResponse:
    prefs = prefs_service.get_prefs(session)
    scan = latest_ok_scan(session)
    rows = opportunity_rows(session, scan, prefs.min_edge, normalize_league(league))
    return JSONResponse([row.to_dict() for row in rows])
