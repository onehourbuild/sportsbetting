"""Bet ledger: summary tiles, open/settled lists, the htmx log-bet sheet and manual settle.

Display reads the ORM directly; `create_bet`, `settle_bet_manual` and `ledger_summary`
go through `app.services.bets` (module-level import so tests can monkeypatch it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Bet, Game, Market, PmQuote
from app.routes.edges import (
    OppRow,
    iso_utc,
    market_label,
    opportunity_row,
    polymarket_url,
    side_label,
)
from app.services import bets as bets_service
from app.services import prefs as prefs_service
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["bets"])

SETTLE_RESULTS: frozenset[str] = frozenset({"won", "lost", "void"})
BET_MODES: frozenset[str] = frozenset({"taker", "maker"})
SUMMARY_KEYS: tuple[str, ...] = (
    "n_open",
    "n_settled",
    "n_won",
    "n_lost",
    "total_staked",
    "total_pnl",
    "roi",
    "avg_clv",
    "n_clv_positive",
    "n_clv_recorded",
)


# --------------------------------------------------------------------------- view models


@dataclass
class BetView:
    bet: Bet
    market: Market | None
    game: Game | None
    quote: PmQuote | None = None  # latest Polymarket quote for the bet's token

    @property
    def league(self) -> str:
        return self.game.league if self.game is not None else ""

    @property
    def title(self) -> str:
        if self.game is None:
            return self.market.question if self.market is not None else self.bet.market_id
        away = self.game.away_name or self.game.away_key or "?"
        home = self.game.home_name or self.game.home_key or "?"
        return f"{away} @ {home}"

    @property
    def label(self) -> str:
        return market_label(self.market) if self.market is not None else ""

    @property
    def side(self) -> str:
        if self.market is None:
            return self.bet.outcome_name
        return side_label(self.market, self.bet.outcome_key, self.bet.outcome_name)

    @property
    def polymarket_url(self) -> str | None:
        return polymarket_url(self.game, self.market)

    @property
    def current_ask(self) -> float | None:
        return self.quote.best_ask if self.quote is not None else None

    @property
    def current_bid(self) -> float | None:
        return self.quote.best_bid if self.quote is not None else None

    @property
    def mark_pnl(self) -> float | None:
        """Unrealized P&L if the position were sold at the current bid."""
        bid = self.current_bid
        if bid is None:
            return None
        return (bid - self.bet.price) * self.bet.shares - self.bet.fee_usd

    @property
    def start_iso(self) -> str:
        return iso_utc(self.game.start_time) if self.game is not None else ""


# --------------------------------------------------------------------------- helpers


def safe_ledger_summary(session: Session) -> dict[str, Any] | None:
    """`bets_service.ledger_summary` or None while it is unavailable (tiles show '—')."""
    try:
        summary = bets_service.ledger_summary(session)
    except NotImplementedError:
        return None
    except Exception:  # noqa: BLE001 - summary tiles must never take the page down
        log.exception("ledger_summary failed")
        return None
    if not isinstance(summary, dict):
        return None
    return {key: summary.get(key) for key in SUMMARY_KEYS}


def _latest_quote(session: Session, token: str) -> PmQuote | None:
    stmt = (
        select(PmQuote)
        .where(PmQuote.token == token)
        .order_by(PmQuote.fetched_at.desc(), PmQuote.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _bet_views(session: Session, bets: list[Bet], with_quotes: bool) -> list[BetView]:
    views: list[BetView] = []
    for bet in bets:
        market = session.get(Market, bet.market_id)
        game = None
        if market is not None and market.game_id is not None:
            game = session.get(Game, market.game_id)
        quote = _latest_quote(session, bet.token) if with_quotes else None
        views.append(BetView(bet=bet, market=market, game=game, quote=quote))
    return views


def open_bets(session: Session) -> list[BetView]:
    stmt = select(Bet).where(Bet.status == "open").order_by(Bet.placed_at.desc(), Bet.id.desc())
    return _bet_views(session, list(session.scalars(stmt)), with_quotes=True)


def settled_bets(session: Session, limit: int = 100) -> list[BetView]:
    stmt = (
        select(Bet)
        .where(Bet.status != "open")
        .order_by(Bet.settled_at.desc(), Bet.id.desc())
        .limit(limit)
    )
    return _bet_views(session, list(session.scalars(stmt)), with_quotes=False)


def ledger_context(
    session: Session, toast: str | None = None, toast_error: bool = False
) -> dict[str, Any]:
    return {
        "summary": safe_ledger_summary(session),
        "open_bets": open_bets(session),
        "settled_bets": settled_bets(session),
        "toast_message": toast,
        "toast_error": toast_error,
    }


def _form_values(row: OppRow) -> dict[str, Any]:
    return {
        "stake_usd": f"{row.opp.suggested_stake:.2f}",
        "price": f"{row.opp.ask:.2f}",
        "mode": "taker",
        "notes": "",
    }


def _sheet_message(request: Request, title: str, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/sheet_message.html", {"title": title, "message": message}
    )


def _parse_float(label: str, raw: str) -> float:
    text = (raw or "").strip().replace(",", "").replace("$", "").replace("¢", "")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, got '{raw}'.") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a finite number.")
    return value


# --------------------------------------------------------------------------- routes


@router.get("/bets", response_class=HTMLResponse)
async def bets_ledger(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return templates.TemplateResponse(request, "bets.html", ledger_context(session))


@router.get("/bets/new", response_class=HTMLResponse)
async def bet_form(
    request: Request, opportunity_id: int = 0, session: Session = Depends(get_session)
) -> HTMLResponse:
    row = opportunity_row(session, opportunity_id) if opportunity_id else None
    if row is None:
        return _sheet_message(
            request,
            "Opportunity not found",
            "That edge is gone from the latest scan. Refresh and try again.",
        )
    context = {
        "row": row,
        "prefs": prefs_service.get_prefs(session),
        "values": _form_values(row),
        "error": None,
    }
    return templates.TemplateResponse(request, "partials/bet_form.html", context)


@router.post("/bets", response_class=HTMLResponse)
async def bet_create(
    request: Request,
    opportunity_id: int = Form(0),
    stake_usd: str = Form(""),
    price: str = Form(""),
    mode: str = Form("taker"),
    notes: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    row = opportunity_row(session, opportunity_id) if opportunity_id else None
    if row is None:
        return _sheet_message(
            request,
            "Opportunity not found",
            "That edge is gone from the latest scan. Refresh and try again.",
        )
    values = {
        "stake_usd": stake_usd,
        "price": price,
        "mode": mode if mode in BET_MODES else "taker",
        "notes": notes,
    }
    prefs = prefs_service.get_prefs(session)

    def _form_error(message: str) -> HTMLResponse:
        context = {
            "row": row,
            "prefs": prefs,
            "values": values,
            "error": message,
            "toast_message": message,
            "toast_error": True,
        }
        return templates.TemplateResponse(request, "partials/bet_form_fields.html", context)

    try:
        stake_value = _parse_float("Stake", stake_usd)
        price_value = _parse_float("Price", price)
        if mode not in BET_MODES:
            raise ValueError("Mode must be taker or maker.")
        if price_value > 1.0 and price_value <= 100.0:
            price_value = price_value / 100.0  # typed in cents
        bet = bets_service.create_bet(
            session,
            opportunity_id,
            stake_value,
            price_value,
            mode,
            notes=(notes or "").strip(),
        )
    except ValueError as exc:
        return _form_error(str(exc) or "Could not log the bet.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner, logged for later
        log.exception("create_bet failed")
        return _form_error(f"Could not log the bet: {exc or type(exc).__name__}")

    message = (
        f"Logged {bet.stake_usd:,.2f} USD on {bet.outcome_name} @ {bet.price * 100:.0f}¢"
        f" ({bet.mode})"
    )
    context = {
        "bet": bet,
        "row": row,
        "open_bets": open_bets(session),
        "compact": True,
        "toast_message": message,
        "toast_error": False,
    }
    return templates.TemplateResponse(request, "partials/bet_logged.html", context)


@router.post("/bets/{bet_id}/settle", response_class=HTMLResponse)
async def bet_settle(
    request: Request,
    bet_id: int,
    result: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    result = (result or "").strip().lower()
    try:
        if result not in SETTLE_RESULTS:
            raise ValueError("Result must be won, lost or void.")
        bet = bets_service.settle_bet_manual(session, bet_id, result)
    except ValueError as exc:
        context = ledger_context(session, toast=str(exc) or "Could not settle.", toast_error=True)
        return templates.TemplateResponse(request, "partials/ledger_response.html", context)
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner, logged for later
        log.exception("settle_bet_manual failed")
        context = ledger_context(
            session, toast=f"Could not settle: {exc or type(exc).__name__}", toast_error=True
        )
        return templates.TemplateResponse(request, "partials/ledger_response.html", context)

    pnl = bet.pnl_usd
    pnl_text = f" · P&L {pnl:+,.2f} USD" if pnl is not None else ""
    message = f"Bet #{bet.id} settled as {bet.status}{pnl_text}"
    context = ledger_context(session, toast=message, toast_error=False)
    return templates.TemplateResponse(request, "partials/ledger_response.html", context)
