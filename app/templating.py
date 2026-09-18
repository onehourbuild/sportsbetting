"""Jinja2 environment + display filters. Filters never raise on None; they render "—"."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from app import APP_NAME, APP_VERSION

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DASH = "—"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: Any, digits: int = 1) -> str:
    """0.5234 -> '52.3%'."""
    number = _num(value)
    if number is None:
        return DASH
    return f"{number * 100:.{digits}f}%"


def usd(value: Any, digits: int = 2) -> str:
    """19.234 -> '$19.23'; negatives as '-$4.50'."""
    number = _num(value)
    if number is None:
        return DASH
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.{digits}f}"


def american(value: Any) -> str:
    """130 -> '+130'; -150 -> '-150'."""
    number = _num(value)
    if number is None:
        return DASH
    odds = int(round(number))
    return f"{odds:+d}"


def cents(value: Any) -> str:
    """0.55 -> '55¢' (prices between 0 and 1)."""
    number = _num(value)
    if number is None:
        return DASH
    c = number * 100
    text = f"{c:.1f}".rstrip("0").rstrip(".") if c != int(c) else f"{int(c)}"
    return f"{text}¢"


def ago(value: Any, now: datetime | None = None) -> str:
    """datetime -> '3m ago' / '2h ago' / '4d ago'; future values render 'in 5m'."""
    if not isinstance(value, datetime):
        return DASH
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    seconds = (now - value).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 45:
        text = "just now"
        return text if not future else "in a moment"
    minutes = seconds / 60
    if minutes < 60:
        text = f"{int(minutes)}m"
    elif minutes < 60 * 24:
        text = f"{int(minutes // 60)}h"
    else:
        text = f"{int(minutes // (60 * 24))}d"
    return f"in {text}" if future else f"{text} ago"


def signed_pct(value: Any, digits: int = 1) -> str:
    """0.021 -> '+2.1%'."""
    number = _num(value)
    if number is None:
        return DASH
    return f"{number * 100:+.{digits}f}%"


def dt_short(value: Any) -> str:
    """datetime -> 'Sat 20 Sep 20:25Z'."""
    if not isinstance(value, datetime):
        return DASH
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%a %d %b %H:%M") + "Z"


FILTERS = {
    "pct": pct,
    "usd": usd,
    "american": american,
    "ago": ago,
    "cents": cents,
    "signed_pct": signed_pct,
    "dt_short": dt_short,
}


def create_templates(directory: Path | str = TEMPLATES_DIR) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(directory))
    templates.env.filters.update(FILTERS)
    templates.env.globals.update(
        {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "LEAGUES": ("nfl", "nba", "mlb"),
        }
    )
    return templates


templates = create_templates()

__all__ = ["FILTERS", "ago", "american", "cents", "create_templates", "pct", "templates", "usd"]
