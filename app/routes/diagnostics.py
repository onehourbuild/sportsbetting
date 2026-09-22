"""Diagnostics: recent scans, the latest scan's errors / unmatched / unparseable lists,
raw sample rows, and environment flags (never secrets)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Bet, BookQuote, Game, Market, Opportunity, PmQuote, Scan
from app.routes.edges import app_settings, safe_quota_status
from app.services.prefs import get_prefs, odds_api_key_for
from app.templating import templates

router = APIRouter(tags=["diagnostics"])

RECENT_SCANS = 20


def row_to_dict(obj: Any) -> dict[str, Any]:
    """Column values of one ORM row (relationships excluded)."""
    return {attr.key: getattr(obj, attr.key) for attr in inspect(obj).mapper.column_attrs}


def pretty_json(obj: Any) -> str:
    if obj is None:
        return "null"
    return json.dumps(row_to_dict(obj), indent=2, sort_keys=True, default=str)


def note_items(value: Any) -> list[dict[str, Any]]:
    """Normalize a Scan.notes list into [{reason, fields}] for the template."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [{"reason": value, "fields": []}]
    if not isinstance(value, list | tuple):
        return []
    items: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, dict):
            reason = entry.get("reason")
            fields = [(k, v) for k, v in entry.items() if k != "reason" and v not in (None, "")]
            items.append({"reason": reason, "fields": fields})
        else:
            items.append({"reason": str(entry), "fields": []})
    return items


def scan_errors(scan: Scan | None) -> list[str]:
    if scan is None:
        return []
    errors = scan.errors
    if isinstance(errors, str):
        return [errors]
    if not isinstance(errors, list | tuple):
        return []
    return [e if isinstance(e, str) else json.dumps(e, default=str) for e in errors]


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


@router.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    scans = list(
        session.scalars(
            select(Scan).order_by(Scan.started_at.desc(), Scan.id.desc()).limit(RECENT_SCANS)
        )
    )
    latest = scans[0] if scans else None
    notes = latest.notes if latest is not None and isinstance(latest.notes, dict) else {}
    pm_sample = session.scalars(select(PmQuote).order_by(PmQuote.id.desc()).limit(1)).first()
    book_sample = session.scalars(select(BookQuote).order_by(BookQuote.id.desc()).limit(1)).first()
    settings = app_settings(request)
    shown_separately = ("unmatched", "unparseable", "conventions", "unresolved_book_teams")
    conventions = notes.get("conventions")
    context = {
        "scans": scans,
        "latest": latest,
        "errors": scan_errors(latest),
        "unmatched": note_items(notes.get("unmatched")),
        "unparseable": note_items(notes.get("unparseable")),
        # Parser conventions the scan assumed (home/away order, match window, staleness)
        # and book team labels the resolver could not name: both must be observable here
        # because the live API formats are unverified (CLAUDE.md rule 4).
        "conventions": conventions if isinstance(conventions, dict) else {},
        "unresolved_teams": note_items(notes.get("unresolved_book_teams")),
        "other_notes": {k: v for k, v in notes.items() if k not in shown_separately},
        "samples": [
            ("PmQuote", pm_sample, pretty_json(pm_sample)),
            ("BookQuote", book_sample, pretty_json(book_sample)),
        ],
        "counts": {
            "games": _count(session, Game),
            "markets": _count(session, Market),
            "opportunities": _count(session, Opportunity),
            "bets": _count(session, Bet),
            "pm_quotes": _count(session, PmQuote),
            "book_quotes": _count(session, BookQuote),
        },
        "quota": safe_quota_status(session),
        "has_odds_key": bool(odds_api_key_for(settings, get_prefs(session))),
        "demo_mode": bool(settings.demo_mode),
        "app_env": settings.app_env,
        "scheduler_poly_minutes": settings.scheduler_poly_minutes,
        "scheduler_books_hours": settings.scheduler_books_hours,
    }
    return templates.TemplateResponse(request, "diagnostics.html", context)
