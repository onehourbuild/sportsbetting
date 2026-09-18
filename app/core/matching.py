"""Team aliases, market-to-game matching, fair probability per outcome.

Contract: docs/ARCHITECTURE.md (binding names and signatures).

Everything here is deterministic, pure Python, and covered by tests/test_matching.py.
`TEAM_ALIASES` is the contract's canonical-key -> display-name table; the private
`_LOOKUP` table maps every accepted spelling (normalized) to a canonical key per league.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from app.core import odds_math
from app.core.types import (
    MARKET_TYPE_TO_BOOK_KEY,
    BookGame,
    BookOutcome,
    BookQuote,
    FairProb,
    League,
    PmMarket,
    PmOutcome,
)

# --------------------------------------------------------------------------- normalization

# Periods and apostrophes vanish ("St. Louis" -> "st louis", "A's" -> "as"); every other
# non-alphanumeric character becomes a space ("D-backs" -> "d backs", "G-Men" -> "g men").
_DROP_CHARS = str.maketrans("", "", ".'’`")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_name(name: str) -> str:
    """Lowercase, strip periods/apostrophes, turn other punctuation into spaces, collapse
    whitespace and drop a leading "the". Aliases and lookups go through the same function."""
    text = name.translate(_DROP_CHARS).lower()
    text = _NON_ALNUM.sub(" ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


# --------------------------------------------------------------------------- team tables
#
# (canonical key, display name, "alias; alias; ...").  The key and the display name are
# always accepted too.  Aliases cover: The Odds API full names, ESPN displayName /
# shortDisplayName / abbreviation, common abbreviation variants, city or region alone where
# it is unambiguous within the league, nicknames, and 2026 realities (Athletics with no
# city, Commanders, Guardians, Las Vegas Raiders).  Ambiguous city names within a league
# ("Los Angeles" in every league, "New York" in NFL and MLB, "Chicago" in MLB) are
# deliberately absent so they resolve to None instead of guessing.

_NFL_TEAMS: tuple[tuple[str, str, str], ...] = (
    ("ARI", "Arizona Cardinals", "Arizona; Cardinals; Cards; ARZ; AZ Cardinals"),
    ("ATL", "Atlanta Falcons", "Atlanta; Falcons"),
    ("BAL", "Baltimore Ravens", "Baltimore; Ravens"),
    ("BUF", "Buffalo Bills", "Buffalo; Bills"),
    ("CAR", "Carolina Panthers", "Carolina; Panthers"),
    ("CHI", "Chicago Bears", "Chicago; Bears"),
    ("CIN", "Cincinnati Bengals", "Cincinnati; Bengals"),
    ("CLE", "Cleveland Browns", "Cleveland; Browns"),
    ("DAL", "Dallas Cowboys", "Dallas; Cowboys"),
    ("DEN", "Denver Broncos", "Denver; Broncos"),
    ("DET", "Detroit Lions", "Detroit; Lions"),
    ("GB", "Green Bay Packers", "Green Bay; Packers; GNB; GB Packers"),
    ("HOU", "Houston Texans", "Houston; Texans"),
    ("IND", "Indianapolis Colts", "Indianapolis; Indy; Colts"),
    ("JAX", "Jacksonville Jaguars", "Jacksonville; Jaguars; Jags; JAC"),
    ("KC", "Kansas City Chiefs", "Kansas City; Chiefs; KAN; KC Chiefs"),
    (
        "LV",
        "Las Vegas Raiders",
        "Las Vegas; Vegas; Raiders; LVR; Oakland Raiders; Oakland; OAK; LV Raiders",
    ),
    (
        "LAC",
        "Los Angeles Chargers",
        "Chargers; LA Chargers; San Diego Chargers; Bolts",
    ),
    ("LAR", "Los Angeles Rams", "Rams; LA Rams; St. Louis Rams"),
    ("MIA", "Miami Dolphins", "Miami; Dolphins; Fins"),
    ("MIN", "Minnesota Vikings", "Minnesota; Vikings; Vikes"),
    ("NE", "New England Patriots", "New England; Patriots; Pats; NWE"),
    ("NO", "New Orleans Saints", "New Orleans; Saints; NOR"),
    ("NYG", "New York Giants", "Giants; NY Giants; G-Men"),
    ("NYJ", "New York Jets", "Jets; NY Jets"),
    ("PHI", "Philadelphia Eagles", "Philadelphia; Philly; Eagles"),
    ("PIT", "Pittsburgh Steelers", "Pittsburgh; Steelers"),
    ("SF", "San Francisco 49ers", "San Francisco; 49ers; Niners; SFO; SF 49ers"),
    ("SEA", "Seattle Seahawks", "Seattle; Seahawks; Hawks"),
    ("TB", "Tampa Bay Buccaneers", "Tampa Bay; Tampa; Buccaneers; Bucs; TAM"),
    ("TEN", "Tennessee Titans", "Tennessee; Titans"),
    (
        "WAS",
        "Washington Commanders",
        "Washington; Commanders; WSH; Washington Football Team; Football Team",
    ),
)

_NBA_TEAMS: tuple[tuple[str, str, str], ...] = (
    ("ATL", "Atlanta Hawks", "Atlanta; Hawks"),
    ("BOS", "Boston Celtics", "Boston; Celtics"),
    ("BKN", "Brooklyn Nets", "Brooklyn; Nets; BRK; New Jersey Nets; NJN"),
    ("CHA", "Charlotte Hornets", "Charlotte; Hornets; CHO; Charlotte Bobcats; Bobcats"),
    ("CHI", "Chicago Bulls", "Chicago; Bulls"),
    ("CLE", "Cleveland Cavaliers", "Cleveland; Cavaliers; Cavs"),
    ("DAL", "Dallas Mavericks", "Dallas; Mavericks; Mavs"),
    ("DEN", "Denver Nuggets", "Denver; Nuggets"),
    ("DET", "Detroit Pistons", "Detroit; Pistons"),
    ("GSW", "Golden State Warriors", "Golden State; Warriors; Dubs; GS; GS Warriors"),
    ("HOU", "Houston Rockets", "Houston; Rockets"),
    ("IND", "Indiana Pacers", "Indiana; Indianapolis; Pacers"),
    ("LAC", "Los Angeles Clippers", "LA Clippers; Clippers; Clips"),
    ("LAL", "Los Angeles Lakers", "LA Lakers; Lakers"),
    ("MEM", "Memphis Grizzlies", "Memphis; Grizzlies; Grizz"),
    ("MIA", "Miami Heat", "Miami; Heat"),
    ("MIL", "Milwaukee Bucks", "Milwaukee; Bucks"),
    ("MIN", "Minnesota Timberwolves", "Minnesota; Timberwolves; Wolves; T-Wolves"),
    (
        "NOP",
        "New Orleans Pelicans",
        "New Orleans; Pelicans; Pels; NO; NOR; NO Pelicans; New Orleans Hornets",
    ),
    ("NYK", "New York Knicks", "New York; Knicks; NY; NY Knicks"),
    ("OKC", "Oklahoma City Thunder", "Oklahoma City; Thunder; OKC Thunder"),
    ("ORL", "Orlando Magic", "Orlando; Magic"),
    ("PHI", "Philadelphia 76ers", "Philadelphia; Philly; 76ers; Sixers"),
    ("PHX", "Phoenix Suns", "Phoenix; Suns; PHO"),
    ("POR", "Portland Trail Blazers", "Portland; Trail Blazers; Blazers; Trailblazers"),
    ("SAC", "Sacramento Kings", "Sacramento; Kings"),
    ("SAS", "San Antonio Spurs", "San Antonio; Spurs; SA; SA Spurs"),
    ("TOR", "Toronto Raptors", "Toronto; Raptors; Raps"),
    ("UTA", "Utah Jazz", "Utah; Jazz; UTAH"),
    ("WAS", "Washington Wizards", "Washington; Wizards; WSH; Wiz"),
)

_MLB_TEAMS: tuple[tuple[str, str, str], ...] = (
    (
        "ARI",
        "Arizona Diamondbacks",
        "Arizona; Diamondbacks; D-backs; Dbacks; AZ; Arizona D-backs",
    ),
    ("ATL", "Atlanta Braves", "Atlanta; Braves"),
    ("BAL", "Baltimore Orioles", "Baltimore; Orioles; O's"),
    ("BOS", "Boston Red Sox", "Boston; Red Sox; BoSox"),
    ("CHC", "Chicago Cubs", "Cubs; Cubbies"),
    ("CWS", "Chicago White Sox", "White Sox; CHW; ChiSox"),
    ("CIN", "Cincinnati Reds", "Cincinnati; Reds"),
    ("CLE", "Cleveland Guardians", "Cleveland; Guardians; Cleveland Indians; Indians"),
    ("COL", "Colorado Rockies", "Colorado; Rockies"),
    ("DET", "Detroit Tigers", "Detroit; Tigers"),
    ("HOU", "Houston Astros", "Houston; Astros"),
    ("KC", "Kansas City Royals", "Kansas City; Royals; KCR; KC Royals"),
    (
        "LAA",
        "Los Angeles Angels",
        "LA Angels; Angels; Anaheim; Anaheim Angels; Los Angeles Angels of Anaheim; ANA; Halos",
    ),
    ("LAD", "Los Angeles Dodgers", "LA Dodgers; Dodgers"),
    ("MIA", "Miami Marlins", "Miami; Marlins; Florida Marlins; FLA"),
    ("MIL", "Milwaukee Brewers", "Milwaukee; Brewers; Brew Crew"),
    ("MIN", "Minnesota Twins", "Minnesota; Twins"),
    ("NYM", "New York Mets", "Mets; NY Mets"),
    ("NYY", "New York Yankees", "Yankees; NY Yankees; Yanks"),
    (
        "ATH",
        "Athletics",
        "A's; Oakland Athletics; Sacramento Athletics; Oakland A's; Sacramento A's; "
        "Las Vegas Athletics; Oakland; Sacramento; OAK",
    ),
    ("PHI", "Philadelphia Phillies", "Philadelphia; Philly; Phillies; Phils"),
    ("PIT", "Pittsburgh Pirates", "Pittsburgh; Pirates"),
    ("SD", "San Diego Padres", "San Diego; Padres; SDP"),
    ("SF", "San Francisco Giants", "San Francisco; Giants; SFG; SF Giants"),
    ("SEA", "Seattle Mariners", "Seattle; Mariners; M's"),
    ("STL", "St. Louis Cardinals", "St. Louis; Saint Louis; Cardinals; Cards"),
    ("TB", "Tampa Bay Rays", "Tampa Bay; Tampa; Rays; TBR; Devil Rays; Tampa Bay Devil Rays"),
    ("TEX", "Texas Rangers", "Texas; Rangers; Arlington"),
    ("TOR", "Toronto Blue Jays", "Toronto; Blue Jays; Jays"),
    ("WSH", "Washington Nationals", "Washington; Nationals; Nats; WAS; WSN"),
)


def _build_tables(
    teams: Sequence[tuple[str, str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (canonical key -> display name, normalized alias -> canonical key).

    Raises ValueError if one spelling would map to two different teams in the same league,
    so an ambiguous alias can never silently resolve to the wrong side.
    """
    display: dict[str, str] = {}
    lookup: dict[str, str] = {}
    for key, name, aliases in teams:
        display[key] = name
        for alias in (key, name, *aliases.split(";")):
            normalized = normalize_name(alias)
            if not normalized:
                continue
            previous = lookup.get(normalized)
            if previous is not None and previous != key:
                raise ValueError(f"alias {alias!r} maps to both {previous} and {key}")
            lookup[normalized] = key
    return display, lookup


_NFL_DISPLAY, _NFL_LOOKUP = _build_tables(_NFL_TEAMS)
_NBA_DISPLAY, _NBA_LOOKUP = _build_tables(_NBA_TEAMS)
_MLB_DISPLAY, _MLB_LOOKUP = _build_tables(_MLB_TEAMS)

TEAM_ALIASES: dict[League, dict[str, str]] = {
    "nfl": _NFL_DISPLAY,
    "nba": _NBA_DISPLAY,
    "mlb": _MLB_DISPLAY,
}
_LOOKUP: dict[str, dict[str, str]] = {
    "nfl": _NFL_LOOKUP,
    "nba": _NBA_LOOKUP,
    "mlb": _MLB_LOOKUP,
}


def team_key(name: str, league: League) -> str | None:
    """Canonical abbreviation for any accepted spelling of a team name; None if unknown.

    Case/punctuation-insensitive; accepts full name, city, nickname, abbreviation,
    Odds API names, Polymarket short names and ESPN displayName. Tries the whole
    normalized string, then its last word, then its last two words, so "KC Chiefs",
    "Spread: Chiefs" and "Boston Red Sox" all resolve. Never raises.

    One deliberate exception to case-insensitivity: the binary outcome labels "Yes" (any
    casing) and title-case "No" return None, so a Yes/No market can never be read as a
    New Orleans side. "NO" and "no" still resolve to New Orleans.
    """
    if not isinstance(name, str):
        return None
    table = _LOOKUP.get(league) if isinstance(league, str) else None
    if table is None:
        return None
    text = normalize_name(name)
    if not text:
        return None
    if text == "yes" or (text == "no" and name.strip().startswith("No")):
        # "Yes"/"No" are Polymarket's binary outcome labels, never a team. The title-case
        # "No" is that label; "NO" (ESPN/Odds API) and "no" (event slugs) are New Orleans.
        return None
    hit = table.get(text)
    if hit is not None:
        return hit
    words = text.split()
    if len(words) >= 2:
        hit = table.get(words[-1])
        if hit is not None:
            return hit
    if len(words) >= 3:
        hit = table.get(" ".join(words[-2:]))
        if hit is not None:
            return hit
    return None


# --------------------------------------------------------------------------- matching


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _market_pair(market: PmMarket) -> frozenset[str] | None:
    """The unordered pair of team keys a market is about, or None when unknown.

    The Polymarket client sets home/away on every market from the event; when either is
    missing we fall back to the outcomes for moneyline and spread markets (their two
    outcomes are the two teams). Totals have Over/Under outcomes so nothing to infer.
    """
    keys = {k for k in (market.home_team_key, market.away_team_key) if k}
    if len(keys) == 2:
        return frozenset(keys)
    if market.market_type in ("moneyline", "spread"):
        keys = {o.team_key for o in market.outcomes if o.team_key}
        if len(keys) == 2:
            return frozenset(keys)
    return None


_GameIndex = dict[tuple[str, frozenset[str]], list[BookGame]]


def _index_games(book_games: Sequence[BookGame]) -> _GameIndex:
    index: _GameIndex = {}
    for game in book_games:
        pair = frozenset((game.home_team_key, game.away_team_key))
        if len(pair) != 2:
            continue
        index.setdefault((game.league, pair), []).append(game)
    return index


def _match_one(
    market: PmMarket,
    index: Mapping[tuple[str, frozenset[str]], Sequence[BookGame]],
    window: timedelta,
) -> tuple[BookGame | None, str | None]:
    """(matched game, None) or (None, reason)."""
    pair = _market_pair(market)
    if pair is None:
        return None, "no teams"
    candidates = index.get((market.league, pair), ())
    if not candidates:
        return None, "no book game"
    start = _as_utc(market.game_start)
    if start is None:
        # Without a start time we can only match when the pairing is unique (MLB
        # doubleheaders are the case where it is not).
        if len(candidates) == 1:
            return candidates[0], None
        return None, "no start time"
    best: BookGame | None = None
    best_delta: timedelta | None = None
    for game in candidates:
        commence = _as_utc(game.commence_time)
        if commence is None:
            continue
        delta = abs(commence - start)
        if delta > window:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = game, delta
    if best is None:
        return None, "outside match window"
    return best, None


def match_games(
    markets: Sequence[PmMarket],
    book_games: Sequence[BookGame],
    window_hours: float = 36.0,
) -> dict[str, BookGame]:
    """market_id -> BookGame: same league, same {home, away} pair (order-insensitive),
    |start difference| <= window; nearest start wins on ties."""
    index = _index_games(book_games)
    window = timedelta(hours=float(window_hours))
    matched: dict[str, BookGame] = {}
    for market in markets:
        game, _reason = _match_one(market, index, window)
        if game is not None:
            matched[market.market_id] = game
    return matched


def unmatched_reasons(
    markets: Sequence[PmMarket],
    book_games: Sequence[BookGame],
    window_hours: float = 36.0,
) -> list[dict]:
    """Companion to `match_games` for the Diagnostics page: one dict per unmatched market
    with keys market_id, question, league, market_type, reason. Reasons are
    "no teams", "no book game", "no start time", "outside match window"."""
    index = _index_games(book_games)
    window = timedelta(hours=float(window_hours))
    out: list[dict] = []
    for market in markets:
        game, reason = _match_one(market, index, window)
        if game is None:
            out.append(
                {
                    "market_id": market.market_id,
                    "question": market.question,
                    "league": market.league,
                    "market_type": market.market_type,
                    "reason": reason or "unmatched",
                }
            )
    return out


# --------------------------------------------------------------------------- fair probability


def _same_point(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) < 1e-9


def _book_outcomes(quote: BookQuote, key: str) -> list[BookOutcome]:
    """All outcomes across every market of `quote` with the given Odds API key."""
    out: list[BookOutcome] = []
    for market in quote.markets:
        if market.key == key:
            out.extend(market.outcomes)
    return out


def _find_team(
    outcomes: Sequence[BookOutcome], team: str, point: float | None, require_point: bool
) -> BookOutcome | None:
    for outcome in outcomes:
        if outcome.team_key != team:
            continue
        if require_point and not _same_point(outcome.point, point):
            continue
        return outcome
    return None


def _find_side(outcomes: Sequence[BookOutcome], side: str, point: float) -> BookOutcome | None:
    for outcome in outcomes:
        if outcome.name.strip().lower() == side and _same_point(outcome.point, point):
            return outcome
    return None


def _total_side(outcome: PmOutcome) -> str | None:
    words = normalize_name(outcome.name).split()
    if words and words[0] in ("over", "under"):
        return words[0]
    return None


def _pair_for_book(
    market: PmMarket,
    outcome: PmOutcome,
    other: PmOutcome,
    game: BookGame,
    outcomes: Sequence[BookOutcome],
) -> tuple[BookOutcome, BookOutcome] | None:
    """(this side, opposite side) from one book at the market's line, or None to skip it."""
    if market.market_type == "total":
        side = _total_side(outcome)
        if side is None or market.line is None:
            return None
        opposite = "under" if side == "over" else "over"
        this = _find_side(outcomes, side, market.line)
        opp = _find_side(outcomes, opposite, market.line)
        if this is None or opp is None:
            return None
        return this, opp

    this_key = outcome.team_key
    if this_key is None:
        return None
    opp_key = other.team_key
    if opp_key is None:
        opp_key = game.away_team_key if this_key == game.home_team_key else game.home_team_key

    if market.market_type == "moneyline":
        this = _find_team(outcomes, this_key, None, require_point=False)
        opp = _find_team(outcomes, opp_key, None, require_point=False)
    elif market.market_type == "spread":
        if market.line is None or market.line_team_key is None:
            return None
        if market.line_team_key == this_key:
            required = float(market.line)
        elif other.team_key is None or market.line_team_key == other.team_key:
            required = -float(market.line)
        else:
            return None  # line belongs to neither outcome: bad market data
        this = _find_team(outcomes, this_key, required, require_point=True)
        opp = _find_team(outcomes, opp_key, -required, require_point=True)
    else:
        return None
    if this is None or opp is None:
        return None
    return this, opp


def fair_for_outcome(
    market: PmMarket,
    outcome_index: int,
    game: BookGame,
    weights: Mapping[str, float],
    method: str = "power",
    default_weight: float = 1.0,
) -> FairProb | None:
    """Consensus fair probability for one outcome using only books quoting the same line.

    moneyline: per book take the h2h prices for both teams, de-vig the pair, keep this
    outcome's team. spread: this team's point must equal market.line when it is the
    line team, else -market.line, and the other team must be quoted at the mirrored
    point. total: Over/Under at point == market.line. Books missing either side of the
    pair are skipped; None when no book qualifies.
    """
    if outcome_index not in (0, 1):
        raise ValueError(f"outcome_index must be 0 or 1, got {outcome_index!r}")
    if market.market_type not in MARKET_TYPE_TO_BOOK_KEY:
        return None
    book_key = MARKET_TYPE_TO_BOOK_KEY[market.market_type]
    outcome = market.outcomes[outcome_index]
    other = market.outcomes[1 - outcome_index]

    samples: list[tuple[str, float]] = []
    for quote in game.books:
        pair = _pair_for_book(market, outcome, other, game, _book_outcomes(quote, book_key))
        if pair is None:
            continue
        raw = [
            odds_math.american_to_prob(pair[0].price_american),
            odds_math.american_to_prob(pair[1].price_american),
        ]
        devigged = odds_math.devig(raw, method)
        samples.append((quote.bookmaker, float(devigged[0])))
    if not samples:
        return None
    line = None if market.market_type == "moneyline" else market.line
    return odds_math.consensus(
        samples, weights, default_weight=default_weight, method=method, line=line
    )


__all__ = [
    "TEAM_ALIASES",
    "fair_for_outcome",
    "match_games",
    "normalize_name",
    "team_key",
    "unmatched_reasons",
]
