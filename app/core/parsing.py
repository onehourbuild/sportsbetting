"""Polymarket question / line parsing (used by clients.polymarket).

Contract: docs/ARCHITECTURE.md (binding names and signatures).

The live Gamma formats are UNVERIFIED (docs/RESEARCH.md "Unverified" 1 and 5): every
parser here is defensive, prefers structured fields when they are unambiguous, falls
back to question text, and returns None rather than guessing when neither is usable so
the market lands on the Diagnostics page instead of producing a wrong edge.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from app.core.matching import normalize_name, team_key
from app.core.types import League, MarketType

# --------------------------------------------------------------------------- market type

# Exact (normalized) Gamma `sportsMarketType` values we accept, plus common spellings.
_MARKET_TYPE_ALIASES: dict[str, MarketType] = {
    "moneyline": "moneyline",
    "money_line": "moneyline",
    "ml": "moneyline",
    "h2h": "moneyline",
    "winner": "moneyline",
    "match_winner": "moneyline",
    "game_winner": "moneyline",
    "spreads": "spread",
    "spread": "spread",
    "handicap": "spread",
    "point_spread": "spread",
    "pointspread": "spread",
    "run_line": "spread",
    "runline": "spread",
    "puck_line": "spread",
    "totals": "total",
    "total": "total",
    "over_under": "total",
    "overunder": "total",
    "o/u": "total",
    "ou": "total",
    "total_points": "total",
    "total_runs": "total",
    "game_total": "total",
}

# Anything scoped to part of a game, a player or a season is not a full-game market and
# must not be compared to full-game book lines. Bare words that also appear in real game
# titles ("Championship", "World Series Game 3") are only rejected behind a verb.
_PERIOD_OR_PROP = re.compile(
    r"\b(?:1h|2h|h1|h2|[1-4]q|q[1-4])\b"
    r"|\b(?:1st|2nd|3rd|4th|first|second|third|fourth)\s+(?:half|quarter|period|inning|five|5)\b"
    r"|\b(?:half|halftime|quarter|period|inning|innings|f5|team\s+total|player|prop|props|"
    r"alternate|alt|parlay|future|futures|mvp|cy\s+young|rookie\s+of\s+the\s+year|"
    r"touchdowns?|yards|rebounds|assists|strikeouts|home\s+runs?|to\s+score|first\s+basket)\b",
    re.IGNORECASE,
)
_FUTURES = re.compile(
    r"\b(?:win|wins|winning|make|makes|reach|reaches|clinch|clinches)\s+(?:the\s+)?"
    r"(?:\w+\s+){0,3}?"
    r"(?:super\s+bowl|world\s+series|finals|championship|title|pennant|division|conference|"
    r"playoffs|series|cup|mvp)\b",
    re.IGNORECASE,
)

_SPREAD_HINT = re.compile(r"\bspreads?\b|\bhandicap\b|\bcovers?\b|\brun\s?line\b|\bpuck\s?line\b")
_TOTAL_HINT = re.compile(
    r"\bo/u\b|\bover\s*[/\-]\s*under\b|\bover\s+under\b|\btotals?\b|\b(?:over|under)\s+\d"
)
_MONEYLINE_HINT = re.compile(
    r"\bmoney\s?line\b|\bml\b|\bwins?\b|\bwinner\b|\bbeats?\b|\bvs\b|\bversus\b|\bv\.\s"
)


def _question_text(question: str | None) -> str:
    text = (question or "").replace("−", "-").replace("–", "-").replace("—", "-")
    return text.replace("＋", "+")


def _is_not_full_game(question: str) -> bool:
    text = question.lower()
    return bool(_PERIOD_OR_PROP.search(text) or _FUTURES.search(text))


def parse_market_type(sports_market_type: str | None, question: str) -> MarketType | None:
    """Map Gamma `sportsMarketType` (or the question text) to moneyline|spread|total.

    A known type string wins; an unknown non-empty type string is a market we ignore
    (None). Only when the type is missing do question heuristics apply, in the order
    spread, total, moneyline. Period-, player- or season-scoped questions return None.
    """
    text = _question_text(question)
    if text and _is_not_full_game(text):
        return None
    if sports_market_type is not None and str(sports_market_type).strip():
        key = str(sports_market_type).strip().lower().replace("-", "_").replace(" ", "_")
        return _MARKET_TYPE_ALIASES.get(key)
    lowered = text.lower()
    if not lowered:
        return None
    if _SPREAD_HINT.search(lowered):
        return "spread"
    if _TOTAL_HINT.search(lowered):
        return "total"
    if _MONEYLINE_HINT.search(lowered):
        return "moneyline"
    return None


# --------------------------------------------------------------------------- spread

# A signed number like "-3.5", "(+3.5)", "-3", "+ 7". The sign may not follow a word
# character or a period (rules out "2026-09-20", "Week-1", "3.5-point"), the integer part
# is at most two digits, and the number may not run into a date/ratio/percent.
_SIGNED_NUMBER = re.compile(r"(?<![\w.])([+-])\s?(\d{1,2}(?:\.\d+)?)(?![\d\-/:%])")

# Words that surround a team name in a spread question and never belong to a team name.
_FILLER = frozenset(
    {
        "spread",
        "spreads",
        "line",
        "handicap",
        "vs",
        "v",
        "versus",
        "at",
        "cover",
        "covers",
        "to",
        "the",
        "will",
        "win",
        "by",
        "and",
        "or",
        "of",
        "game",
        "match",
        "matchup",
        "point",
        "points",
        "run",
        "runs",
    }
)
_AFTER_SEGMENT_END = re.compile(r"[:,|(@]|\bvs\b|\bv\b|\bversus\b|\bat\b", re.IGNORECASE)


def _strip_filler(words: list[str]) -> list[str]:
    start, end = 0, len(words)
    while start < end and words[start] in _FILLER:
        start += 1
    while end > start and words[end - 1] in _FILLER:
        end -= 1
    return words[start:end]


def _resolve_segment(segment: str, league: League, from_end: bool = True) -> str | None:
    """Team key from a fragment of question text next to the signed number.

    `team_key` already tries the whole fragment, its last word and its last two words; on
    top of that try the last three words and then the leading one to three words (for
    fragments like "Chiefs vs Bills 2026-09-20"). With `from_end=False` (text after the
    number) the leading words are tried first.
    """
    words = _strip_filler(normalize_name(segment).split())
    if not words:
        return None
    n = len(words)
    tail = [(n - 3, n)]
    head = [(0, 1), (0, 2), (0, 3)]
    order = [(0, n), *tail, *head] if from_end else [(0, n), *head, *tail]
    seen: set[tuple[int, int]] = set()
    for a, b in order:
        if a < 0 or b > n or a >= b or (a, b) in seen:
            continue
        seen.add((a, b))
        key = team_key(" ".join(words[a:b]), league)
        if key is not None:
            return key
    return None


def _leading_segment(text: str) -> str:
    match = _AFTER_SEGMENT_END.search(text)
    return text[: match.start()] if match else text


def _outcome_team(name: str, league: League) -> str | None:
    """Team key of an outcome label; Yes/No labels (any casing) are never a team."""
    if normalize_name(name) in ("yes", "no"):
        return None
    return team_key(name, league)


def _mentioned_outcomes(
    text: str, outcomes: Sequence[str], keys: Sequence[str | None]
) -> list[str]:
    """Outcome keys whose label (or its nickname, the last word) occurs in the question."""
    haystack = f" {normalize_name(text)} "
    found: list[str] = []
    for label, key in zip(outcomes, keys, strict=True):
        if key is None or key in found:
            continue
        needle = normalize_name(label)
        candidates = {needle, needle.split()[-1]} if needle else set()
        if any(f" {c} " in haystack for c in candidates):
            found.append(key)
    return found


def parse_spread(
    question: str, line: float | None, outcomes: Sequence[str], league: League
) -> tuple[float, str] | None:
    """(signed line, line_team_key) from e.g. "Spread: Chiefs (-3.5)"; falls back to `line`.

    Accepted question shapes: "Spread: Chiefs (-3.5)", "Chiefs -3.5", "Kansas City
    Chiefs (+3.5)", "Chiefs -3.5 vs Bills", "Bills +3.5 vs Chiefs". The team is read from
    the text immediately before the signed number, then from the text right after it.

    ASSUMPTION (unverified against the live API, see docs/RESEARCH.md): when the question
    carries no signed number, Gamma's `line` is taken as already signed from the
    perspective of the FIRST listed outcome (Polymarket lists the line team first and the
    favorite carries a negative line). A team named in the question that is one of the
    outcomes overrides that. If live data shows an unsigned `line`, the result will simply
    never match a book line and the market shows up on Diagnostics as "no book at line".
    """
    text = _question_text(question)
    outcome_keys = [_outcome_team(o, league) for o in outcomes]
    allowed = {k for k in outcome_keys if k}
    first_outcome_key = outcome_keys[0] if outcome_keys else None

    match = _SIGNED_NUMBER.search(text)
    if match is not None:
        value = float(match.group(2))
        if match.group(1) == "-":
            value = -value
        key = _resolve_segment(text[: match.start()], league, from_end=True)
        if key is None:
            key = _resolve_segment(_leading_segment(text[match.end() :]), league, from_end=False)
        if key is not None and allowed and key not in allowed:
            return None  # question names a team that is not one of the outcomes
        if key is None:
            key = first_outcome_key  # see ASSUMPTION above
        if key is None:
            return None
        return value, key

    if line is None:
        return None
    mentioned = _mentioned_outcomes(text, outcomes, outcome_keys)
    key: str | None
    if len(mentioned) == 1:
        key = mentioned[0]
    elif not mentioned:
        key = _resolve_segment(text, league, from_end=True)
        if key is not None and allowed and key not in allowed:
            return None
    else:
        key = None  # both teams named and no signed number: nothing anchors the line
    if key is None:
        key = first_outcome_key  # see ASSUMPTION above
    if key is None:
        return None
    return float(line), key


# --------------------------------------------------------------------------- total

_NUMBER = r"(\d{1,3}(?:\.\d+)?)"
_TOTAL_PATTERNS = (
    re.compile(
        r"\b(?:o/u|over\s*[/\-]\s*under|over\s+under|totals?(?:\s+(?:points|pts|runs|goals))?)"
        r"\s*[:\-]?\s*" + _NUMBER + r"\b"
    ),
    re.compile(r"\b" + _NUMBER + r"\s*(?:points|pts|runs|goals)\b"),
    re.compile(r"\b(?:over|under)\s+" + _NUMBER + r"\b"),
)


def parse_total(question: str, line: float | None) -> float | None:
    """The total from "O/U 47.5", "Over/Under 47.5", "Total: 47.5" or "47.5 points";
    falls back to `line`; None when neither is available."""
    text = _question_text(question).lower()
    for pattern in _TOTAL_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return float(match.group(1))
    if line is None:
        return None
    try:
        return float(line)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- json lists


def parse_json_list(value: str | list | None) -> list:
    """Gamma sends `outcomes`, `outcomePrices`, `clobTokenIds` as JSON-encoded strings.

    Accepts a real list/tuple, a JSON array string (also when double-encoded), or None.
    Anything else, including malformed JSON, yields [] so the caller can flag the market.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    if isinstance(data, (list, tuple)):
        return list(data)
    return []


__all__ = ["parse_json_list", "parse_market_type", "parse_spread", "parse_total"]
