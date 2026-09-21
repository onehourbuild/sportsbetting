"""Single-row preferences: read (creating defaults) and validated update."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models import DEFAULT_PREFS, Prefs, utcnow

DEVIG_METHODS: frozenset[str] = frozenset({"multiplicative", "additive", "power", "shin"})
LEAGUES: tuple[str, ...] = ("nfl", "nba", "mlb")

PREFS_ROW_ID = 1

_FLOAT_RANGES: dict[str, tuple[float, float, bool, bool]] = {
    # field: (min, max, min_inclusive, max_inclusive)
    "bankroll": (0.0, float("inf"), False, False),
    "kelly_fraction": (0.0, 1.0, False, True),
    "max_stake_pct": (0.0, 100.0, False, True),
    "min_edge": (0.0, 0.5, True, False),
    "taker_fee_rate": (0.0, 0.2, True, False),
    "match_window_hours": (0.0, 24.0 * 14, False, True),
    "min_liquidity_usd": (0.0, float("inf"), True, False),
}
_INT_RANGES: dict[str, tuple[int, int]] = {
    "stale_book_minutes": (0, 60 * 24 * 30),
}
_BOOL_FIELDS: frozenset[str] = frozenset({"espn_fallback_enabled"})
_WALLET_FIELDS: frozenset[str] = frozenset({"pm_wallet"})
# An Ethereum-style address: Polymarket's proxy wallets are ordinary 20-byte addresses.
WALLET_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")
_KNOWN_FIELDS: frozenset[str] = frozenset(DEFAULT_PREFS)


def get_prefs(session: Session) -> Prefs:
    """Return the singleton Prefs row, creating it with defaults when missing."""
    prefs = session.get(Prefs, PREFS_ROW_ID)
    if prefs is None:
        prefs = Prefs(id=PREFS_ROW_ID, **{k: _copy(v) for k, v in DEFAULT_PREFS.items()})
        session.add(prefs)
        session.commit()
        session.refresh(prefs)
    return prefs


def update_prefs(session: Session, data: Mapping[str, Any]) -> Prefs:
    """Validate `data` (types and ranges) and apply it to the singleton row.

    Raises ValueError with a readable message on the first problem. Unknown keys
    are rejected so typos in forms do not silently disappear.
    """
    cleaned = validate_prefs(data)
    prefs = get_prefs(session)
    for key, value in cleaned.items():
        setattr(prefs, key, value)
    prefs.updated_at = utcnow()
    session.add(prefs)
    session.commit()
    session.refresh(prefs)
    return prefs


def validate_prefs(data: Mapping[str, Any]) -> dict[str, Any]:
    """Pure validation; returns the coerced subset of `data`."""
    cleaned: dict[str, Any] = {}
    for key, raw in data.items():
        if key not in _KNOWN_FIELDS:
            raise ValueError(f"unknown preference '{key}'")
        if key in _FLOAT_RANGES:
            cleaned[key] = _validate_float(key, raw)
        elif key in _INT_RANGES:
            cleaned[key] = _validate_int(key, raw)
        elif key in _BOOL_FIELDS:
            cleaned[key] = _validate_bool(key, raw)
        elif key in _WALLET_FIELDS:
            cleaned[key] = _validate_wallet(key, raw)
        elif key == "devig_method":
            cleaned[key] = _validate_devig(raw)
        elif key == "bookmakers":
            cleaned[key] = _validate_bookmakers(raw)
        elif key == "book_weights":
            cleaned[key] = _validate_book_weights(raw)
        elif key == "leagues_enabled":
            cleaned[key] = _validate_leagues(raw)
        else:  # pragma: no cover - every known field is handled above
            raise ValueError(f"unhandled preference '{key}'")
    return cleaned


# --------------------------------------------------------------------------- validators


def _copy(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _to_float(key: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a number, got a boolean")
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip().replace(",", "").replace("$", "").replace("%", ""))
        except ValueError as exc:
            raise ValueError(f"{key} must be a number, got '{raw}'") from exc
    else:
        raise ValueError(f"{key} must be a number, got {type(raw).__name__}")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{key} must be a finite number")
    return value


def _validate_float(key: str, raw: Any) -> float:
    lo, hi, lo_inc, hi_inc = _FLOAT_RANGES[key]
    value = _to_float(key, raw)
    lo_ok = value >= lo if lo_inc else value > lo
    hi_ok = value <= hi if hi_inc else value < hi
    if not (lo_ok and hi_ok):
        lo_s = "[" if lo_inc else "("
        hi_s = "]" if hi_inc else ")"
        hi_txt = "inf" if hi == float("inf") else f"{hi:g}"
        raise ValueError(f"{key} must be in {lo_s}{lo:g}, {hi_txt}{hi_s}, got {value:g}")
    return value


def _validate_int(key: str, raw: Any) -> int:
    lo, hi = _INT_RANGES[key]
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be an integer, got a boolean")
    if isinstance(raw, str):
        try:
            raw = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer, got '{raw}'") from exc
    if isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError(f"{key} must be an integer, got {raw}")
        raw = int(raw)
    if not isinstance(raw, int):
        raise ValueError(f"{key} must be an integer, got {type(raw).__name__}")
    if not lo <= raw <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}, got {raw}")
    return raw


def _validate_bool(key: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    raise ValueError(f"{key} must be true or false, got '{raw}'")


def _validate_wallet(key: str, raw: Any) -> str:
    """Empty (off) or a 0x-prefixed 40-hex address, stored lower-case."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be text")
    value = raw.strip()
    if not value:
        return ""
    if not WALLET_RE.match(value):
        raise ValueError(f"{key} must be a 0x address of 40 hex characters, got '{value}'")
    return value.lower()


def _validate_devig(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("devig_method must be a string")
    method = raw.strip().lower()
    if method not in DEVIG_METHODS:
        allowed = ", ".join(sorted(DEVIG_METHODS))
        raise ValueError(f"devig_method must be one of {allowed}, got '{raw}'")
    return method


def _slug_list(key: str, raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [s for s in (part.strip() for part in raw.replace("\n", ",").split(",")) if s]
    elif isinstance(raw, list | tuple | set | frozenset):
        items = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError(f"{key} entries must be strings, got {type(item).__name__}")
            item = item.strip()
            if item:
                items.append(item)
    else:
        raise ValueError(f"{key} must be a list of strings")
    return items


def _validate_bookmakers(raw: Any) -> list[str]:
    items = _slug_list("bookmakers", raw)
    out: list[str] = []
    for slug in items:
        slug = slug.lower()
        if not slug.replace("_", "").isalnum():
            raise ValueError(f"bookmakers entry '{slug}' is not a valid slug")
        if slug not in out:
            out.append(slug)
    if not out:
        raise ValueError("bookmakers must contain at least one bookmaker slug")
    return out


def _validate_book_weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise ValueError("book_weights must be an object of bookmaker -> weight")
    out: dict[str, float] = {}
    for name, weight in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("book_weights keys must be bookmaker slugs")
        value = _to_float(f"book_weights[{name}]", weight)
        if value < 0:
            raise ValueError(f"book_weights[{name}] must be >= 0, got {value:g}")
        out[name.strip().lower()] = value
    return out


def _validate_leagues(raw: Any) -> list[str]:
    items = [s.lower() for s in _slug_list("leagues_enabled", raw)]
    unknown = [s for s in items if s not in LEAGUES]
    if unknown:
        raise ValueError(
            f"leagues_enabled may only contain {', '.join(LEAGUES)}; got {', '.join(unknown)}"
        )
    if not items:
        # Every league off would make every scan a silent no-op.
        raise ValueError("leagues_enabled must contain at least one league")
    return [league for league in LEAGUES if league in items]


__all__ = ["DEVIG_METHODS", "get_prefs", "update_prefs", "validate_prefs"]
