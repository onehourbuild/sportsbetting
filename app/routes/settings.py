"""Preferences form (every Prefs field) plus key / quota / demo status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_session
from app.models import VENUE_LABELS, VENUE_TAKER_FEE, Prefs
from app.routes.edges import scan_context
from app.services import prefs as prefs_service
from app.templating import templates

router = APIRouter(tags=["settings"])

FLOAT_FIELDS: tuple[str, ...] = (
    "bankroll",
    "kelly_fraction",
    "max_stake_pct",
    "min_edge",
    "taker_fee_rate",
    "match_window_hours",
    "min_liquidity_usd",
)
INT_FIELDS: tuple[str, ...] = ("stale_book_minutes",)
DEVIG_METHODS: tuple[str, ...] = ("power", "shin", "multiplicative", "additive")
LEAGUES: tuple[str, ...] = ("nfl", "nba", "mlb")


# --------------------------------------------------------------------------- helpers


def _num_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def prefs_to_values(prefs: Prefs) -> dict[str, Any]:
    """Prefs row -> the strings the form shows."""
    values: dict[str, Any] = {name: _num_text(getattr(prefs, name)) for name in FLOAT_FIELDS}
    for name in INT_FIELDS:
        values[name] = _num_text(getattr(prefs, name))
    values["devig_method"] = prefs.devig_method
    values["bookmakers"] = ", ".join(prefs.bookmakers or [])
    values["book_weights"] = "\n".join(
        f"{book}={_num_text(weight)}" for book, weight in (prefs.book_weights or {}).items()
    )
    values["leagues_enabled"] = list(prefs.leagues_enabled or [])
    values["espn_fallback_enabled"] = bool(prefs.espn_fallback_enabled)
    values["pm_wallet"] = prefs.pm_wallet or ""
    values["venue"] = prefs.venue
    return values


def form_to_values(form: FormData) -> dict[str, Any]:
    """Submitted form -> the same shape as `prefs_to_values` (so edits survive an error)."""
    values: dict[str, Any] = {}
    for name in FLOAT_FIELDS + INT_FIELDS:
        values[name] = str(form.get(name, "") or "").strip()
    values["devig_method"] = str(form.get("devig_method", "") or "").strip().lower()
    values["bookmakers"] = str(form.get("bookmakers", "") or "").strip()
    values["book_weights"] = str(form.get("book_weights", "") or "").strip()
    values["leagues_enabled"] = [
        str(v).strip().lower() for v in form.getlist("leagues_enabled") if str(v).strip()
    ]
    values["espn_fallback_enabled"] = "espn_fallback_enabled" in form
    values["pm_wallet"] = str(form.get("pm_wallet", "") or "").strip()
    values["venue"] = str(form.get("venue", "") or "").strip().lower()
    return values


def parse_book_weights(text: str) -> dict[str, str]:
    """One 'bookmaker=weight' per line (also accepts ':' or whitespace as the separator).

    Values stay strings; `update_prefs` coerces and range-checks them.
    """
    weights: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                book, weight = line.split(sep, 1)
                break
        else:
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(
                    f"book_weights line {number} must look like 'pinnacle=3', got '{line}'"
                )
            book, weight = parts
        book, weight = book.strip().lower(), weight.strip()
        if not book or not weight:
            raise ValueError(
                f"book_weights line {number} must look like 'pinnacle=3', got '{line}'"
            )
        weights[book] = weight
    return weights


def values_to_update(values: dict[str, Any]) -> dict[str, Any]:
    """Form values -> the dict `update_prefs` validates. Raises ValueError on malformed lines."""
    data: dict[str, Any] = {}
    for name in FLOAT_FIELDS + INT_FIELDS:
        data[name] = values.get(name, "")
    data["devig_method"] = values.get("devig_method", "")
    data["bookmakers"] = values.get("bookmakers", "")
    data["book_weights"] = parse_book_weights(values.get("book_weights", "") or "")
    data["leagues_enabled"] = list(values.get("leagues_enabled") or [])
    data["espn_fallback_enabled"] = "on" if values.get("espn_fallback_enabled") else "off"
    data["pm_wallet"] = values.get("pm_wallet", "") or ""
    # A form that does not carry the venue field at all leaves the stored one alone. The
    # select always submits one, so blank here means "not part of this submission" rather
    # than "clear it", and silently resetting someone's venue would silently reset their
    # fee with it.
    venue = values.get("venue", "") or ""
    if venue:
        data["venue"] = venue
    return data


def _venue_fee_context(values: dict[str, Any]) -> dict[str, Any]:
    """Whether the stored fee matches what the chosen venue actually charges.

    The two exchanges differ (0.0695 on .us, 0.05 on .com), and a fee left at the other
    one's number silently overstates every edge. Rather than correct it behind the owner's
    back on a page they are already editing, say so where they can see it.
    """
    venue = values.get("venue") or ""
    expected = VENUE_TAKER_FEE.get(venue)
    if expected is None:
        return {"venue_fee_hint": "", "venue_fee_mismatch": False, "venue_fee_expected": ""}
    expected_text = _num_text(expected)
    try:
        current = float(str(values.get("taker_fee_rate", "")).strip())
    except (TypeError, ValueError):
        current = None
    return {
        "venue_fee_hint": f"{VENUE_LABELS[venue]}: {expected_text}",
        "venue_fee_mismatch": current is not None and current != expected,
        "venue_fee_expected": expected_text,
    }


def settings_context(
    request: Request,
    session: Session,
    values: dict[str, Any],
    *,
    saved: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    prefs = prefs_service.get_prefs(session)
    context: dict[str, Any] = {
        "prefs": prefs,
        "values": values,
        "saved": saved,
        "error": error,
        "devig_methods": DEVIG_METHODS,
        "leagues": LEAGUES,
        "venue_labels": VENUE_LABELS,
    }
    context.update(_venue_fee_context(values))
    context.update(scan_context(request, session, prefs))
    return context


# --------------------------------------------------------------------------- routes


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    prefs = prefs_service.get_prefs(session)
    context = settings_context(request, session, prefs_to_values(prefs))
    return templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    form = await request.form()
    values = form_to_values(form)
    try:
        prefs = prefs_service.update_prefs(session, values_to_update(values))
    except ValueError as exc:
        context = settings_context(request, session, values, error=str(exc))
        return templates.TemplateResponse(request, "settings.html", context)
    context = settings_context(request, session, prefs_to_values(prefs), saved=True)
    return templates.TemplateResponse(request, "settings.html", context)
