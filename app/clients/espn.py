"""ESPN scoreboard client (keyless fallback, single low-weight book).

Contract: docs/ARCHITECTURE.md; wire format: docs/RESEARCH.md.

    GET {base}/{football/nfl | basketball/nba | baseball/mlb}/scoreboard?dates=YYYYMMDD

Reads ``events[].competitions[0]``: ``competitors[] {homeAway, team{displayName,
abbreviation, shortDisplayName}, score}``, ``status.type.completed`` and, when present,
``odds[0] {provider{name}, details ("KC -3.5"), overUnder, homeTeamOdds{moneyLine,
spreadOdds}, awayTeamOdds{moneyLine, spreadOdds}, overOdds, underOdds}``. Odds vanish for
finished games.

ESPN is a single low-weight book, but it is the ONLY book when there is no Odds API key,
and then its prices set the fair probability outright. So it contributes a spread or total
only when the payload carries a real price for each side (``spreadOdds`` per team,
``overOdds``/``underOdds``): assuming -110/-110 would de-vig to exactly 0.5 whatever the
real price is, and turn every side asking below ~0.475 into a fabricated opportunity.
Moneylines always carry both prices, so h2h is unaffected.

UNVERIFIED against the live API from the build environment (see docs/RESEARCH.md); the
parser is defensive and skips events it cannot read, logging why. A non-browser
User-Agent is sent explicitly (the HTTP transport sets one too).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.clients.oddsapi import parse_iso_utc, to_american, to_float
from app.clients.polymarket import TeamResolver
from app.clients.transport import USER_AGENT, Transport
from app.core.types import BookGame, BookMarket, BookOutcome, BookQuote, EspnGame, League

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_PATHS: dict[str, str] = {"nfl": "football/nfl", "nba": "basketball/nba", "mlb": "baseball/mlb"}
ESPN_BOOKMAKER = "espn"
ESPN_DEFAULT_TITLE = "ESPN BET"

# "KC -3.5", "NE +7", "GS -6.5". Pick'ems ("EVEN", "PK") yield no spreads market.
_DETAILS_RE = re.compile(
    r"^\s*(?P<team>[A-Za-z][A-Za-z0-9.&' -]*?)\s+(?P<line>[+-]?\d+(?:\.\d+)?)\s*$"
)


@dataclass(frozen=True)
class EspnOddsGame(EspnGame):
    """`EspnGame` plus the per-side prices ESPN's odds block carries.

    Additive: `EspnGame` is the contract type in `app/core/types.py` and stays as it is.
    `to_book_games` accepts either, and a plain `EspnGame` (no per-side prices) contributes
    only its moneylines.
    """

    home_spread_odds: int | None = None
    away_spread_odds: int | None = None
    over_odds: int | None = None
    under_odds: int | None = None


def _default_team_resolver(name: str, league: League) -> str | None:
    # Lazy import so client tests can inject a dict-backed resolver and never load matching.
    from app.core.matching import team_key

    return team_key(name, league)


def parse_spread_details(details: str | None) -> tuple[str, float] | None:
    """``"KC -3.5"`` -> ``("KC", -3.5)``; None for pick'ems, blanks and anything else."""
    if not isinstance(details, str):
        return None
    match = _DETAILS_RE.match(details)
    if match is None:
        return None
    try:
        line = float(match.group("line"))
    except ValueError:
        return None
    return match.group("team").strip(), line


def _to_score(value: Any) -> int | None:
    """ESPN scores are strings ("5"); some endpoints nest them as {value, displayValue}."""
    if isinstance(value, dict):
        value = value.get("value", value.get("displayValue"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _first_dict(value: Any) -> dict | None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


class EspnClient:
    def __init__(
        self,
        transport: Transport,
        base: str = DEFAULT_BASE,
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.base = base.rstrip("/")
        self._team_resolver: TeamResolver = team_resolver or _default_team_resolver
        # Raw team labels the resolver could not name (see OddsApiClient.unresolved_teams).
        self.unresolved_teams: list[dict[str, str]] = []

    # -- public --------------------------------------------------------------

    def scoreboard(self, league: League, date: date | None = None) -> list[EspnGame]:
        """GET .../{sport}/{league}/scoreboard (``dates=YYYYMMDD`` when a date is given)."""
        path = ESPN_PATHS.get(league)
        if path is None:
            raise ValueError(f"unsupported league {league!r}; expected one of {sorted(ESPN_PATHS)}")
        url = f"{self.base}/{path}/scoreboard"
        params = {"dates": date.strftime("%Y%m%d")} if date is not None else None
        log.info("GET %s dates=%s", url, params["dates"] if params else "(today)")
        payload, _headers = self.transport.get_json(
            url, params=params, headers={"User-Agent": USER_AGENT}
        )
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            log.warning("ESPN %s: scoreboard has no events list", league)
            return []
        games: list[EspnGame] = []
        for raw in events:
            game = self._parse_event(raw, league)
            if game is not None:
                games.append(game)
        with_odds = sum(1 for g in games if g.odds_provider is not None)
        log.info("ESPN %s: %d games (%d with odds)", league, len(games), with_odds)
        return games

    @staticmethod
    def to_book_games(games: Sequence[EspnGame]) -> list[BookGame]:
        """Treat ESPN's single odds block as bookmaker ``espn``. Games without odds are
        skipped. h2h needs both moneylines; spreads need parseable ``details`` naming one of
        the two teams *and* a ``spreadOdds`` for each side; totals need ``overUnder`` *and*
        both ``overOdds`` and ``underOdds``. Prices are never invented."""
        out: list[BookGame] = []
        for game in games:
            markets = _espn_markets(game)
            if not markets:
                continue
            quote = BookQuote(
                bookmaker=ESPN_BOOKMAKER,
                title=game.odds_provider or ESPN_DEFAULT_TITLE,
                markets=tuple(markets),
            )
            out.append(
                BookGame(
                    game_id=f"espn:{game.espn_id}",
                    league=game.league,
                    commence_time=game.start_time,
                    home_team_key=game.home_team_key,
                    away_team_key=game.away_team_key,
                    home_team_name=game.home_name,
                    away_team_name=game.away_name,
                    books=(quote,),
                )
            )
        return out

    # -- internals -----------------------------------------------------------

    def _resolve(self, name: str, league: League) -> str | None:
        if not name:
            return None
        try:
            return self._team_resolver(name, league)
        except Exception:  # a resolver bug must not take the whole refresh down
            log.exception("team resolver failed for %r (%s)", name, league)
            return None

    def _parse_event(self, raw: Any, league: League) -> EspnGame | None:
        if not isinstance(raw, dict):
            log.warning("ESPN %s: skipping non-object event entry", league)
            return None
        espn_id = str(raw.get("id") or "").strip()
        competition = _first_dict(raw.get("competitions")) or {}
        start = parse_iso_utc(competition.get("date") or raw.get("date"))
        home = away = None
        competitors = competition.get("competitors")
        for competitor in competitors if isinstance(competitors, list) else []:
            if not isinstance(competitor, dict):
                continue
            side = str(competitor.get("homeAway") or "").strip().lower()
            if side == "home" and home is None:
                home = competitor
            elif side == "away" and away is None:
                away = competitor
        if not espn_id or start is None or home is None or away is None:
            log.warning(
                "ESPN %s: skipping event %r (missing id/date/competitors)",
                league,
                espn_id or raw.get("name"),
            )
            return None

        home_name, home_key, home_score = self._competitor(home, league)
        away_name, away_key, away_score = self._competitor(away, league)

        status = competition.get("status")
        if not isinstance(status, dict):
            status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
        status_type = status.get("type") if isinstance(status.get("type"), dict) else {}
        completed = bool(status_type.get("completed", False))

        provider: str | None = None
        details: str | None = None
        over_under: float | None = None
        home_ml: int | None = None
        away_ml: int | None = None
        home_spread_odds: int | None = None
        away_spread_odds: int | None = None
        over_odds: int | None = None
        under_odds: int | None = None
        odds = _first_dict(competition.get("odds"))
        if odds is not None:
            provider_obj = odds.get("provider")
            if isinstance(provider_obj, dict) and provider_obj.get("name"):
                provider = str(provider_obj["name"]).strip() or ESPN_DEFAULT_TITLE
            elif isinstance(provider_obj, str) and provider_obj.strip():
                provider = provider_obj.strip()
            else:
                provider = ESPN_DEFAULT_TITLE
            raw_details = odds.get("details")
            details = (str(raw_details).strip() or None) if raw_details is not None else None
            over_under = to_float(odds.get("overUnder"))
            home_odds = odds.get("homeTeamOdds")
            away_odds = odds.get("awayTeamOdds")
            if isinstance(home_odds, dict):
                home_ml = to_american(home_odds.get("moneyLine"))
                home_spread_odds = to_american(home_odds.get("spreadOdds"))
            if isinstance(away_odds, dict):
                away_ml = to_american(away_odds.get("moneyLine"))
                away_spread_odds = to_american(away_odds.get("spreadOdds"))
            over_odds = to_american(odds.get("overOdds"))
            under_odds = to_american(odds.get("underOdds"))

        return EspnOddsGame(
            espn_id=espn_id,
            league=league,
            start_time=start,
            home_team_key=home_key,
            away_team_key=away_key,
            home_name=home_name,
            away_name=away_name,
            home_score=home_score,
            away_score=away_score,
            completed=completed,
            odds_provider=provider,
            home_moneyline=home_ml,
            away_moneyline=away_ml,
            spread_details=details,
            over_under=over_under,
            home_spread_odds=home_spread_odds,
            away_spread_odds=away_spread_odds,
            over_odds=over_odds,
            under_odds=under_odds,
        )

    def _competitor(self, raw: dict, league: League) -> tuple[str, str, int | None]:
        team = raw.get("team") if isinstance(raw.get("team"), dict) else {}
        display = str(team.get("displayName") or "").strip()
        abbreviation = str(team.get("abbreviation") or "").strip()
        short = str(team.get("shortDisplayName") or "").strip()
        location = str(team.get("location") or "").strip()
        nickname = str(team.get("name") or "").strip()
        name = display or f"{location} {nickname}".strip() or short or abbreviation
        key = (
            self._resolve(display, league)
            or self._resolve(abbreviation, league)
            or self._resolve(short, league)
            or ""
        )
        if not key:
            entry = {"league": str(league), "name": name or abbreviation, "source": "espn"}
            if entry not in self.unresolved_teams:
                log.warning("ESPN %s: unresolved team %r (%s)", league, name, abbreviation)
                self.unresolved_teams.append(entry)
        return name, key, _to_score(raw.get("score"))


def _side_for_token(game: EspnGame, token: str) -> str | None:
    """Map the team token in ``details`` ("KC") to "home" or "away", or None."""
    upper = token.strip().upper()
    if not upper:
        return None
    if game.home_team_key and upper == game.home_team_key.upper():
        return "home"
    if game.away_team_key and upper == game.away_team_key.upper():
        return "away"
    # ESPN abbreviations differ from our keys for a few teams (GS, WSH, SA, NY, UTAH, CHW…):
    # fall back to the alias table when the canonical keys did not match directly.
    resolved = _default_team_resolver(token, game.league)
    if resolved and resolved == game.home_team_key:
        return "home"
    if resolved and resolved == game.away_team_key:
        return "away"
    return None


def _side_prices(game: EspnGame, home: str, away: str) -> tuple[int, int] | None:
    """The two per-side American prices when the payload carried both, else None."""
    home_price = getattr(game, home, None)
    away_price = getattr(game, away, None)
    if isinstance(home_price, int) and isinstance(away_price, int):
        return home_price, away_price
    return None


def _espn_markets(game: EspnGame) -> list[BookMarket]:
    home_key = game.home_team_key or None
    away_key = game.away_team_key or None
    markets: list[BookMarket] = []

    if game.home_moneyline is not None and game.away_moneyline is not None:
        markets.append(
            BookMarket(
                key="h2h",
                outcomes=(
                    BookOutcome(game.home_name, home_key, game.home_moneyline, None),
                    BookOutcome(game.away_name, away_key, game.away_moneyline, None),
                ),
                last_update=None,
            )
        )

    parsed = parse_spread_details(game.spread_details)
    spread_prices = _side_prices(game, "home_spread_odds", "away_spread_odds")
    if parsed is not None and spread_prices is None:
        log.debug(
            "ESPN %s: spread %r has no per-side spreadOdds; contributing no spreads market",
            game.league,
            game.spread_details,
        )
    elif parsed is not None and spread_prices is not None:
        token, line = parsed
        side = _side_for_token(game, token)
        if side is None:
            log.debug(
                "ESPN %s: cannot place spread %r on %s/%s",
                game.league,
                game.spread_details,
                game.away_team_key,
                game.home_team_key,
            )
        else:
            home_price, away_price = spread_prices
            other = 0.0 if line == 0 else -line
            home_point, away_point = (line, other) if side == "home" else (other, line)
            markets.append(
                BookMarket(
                    key="spreads",
                    outcomes=(
                        BookOutcome(game.home_name, home_key, home_price, home_point),
                        BookOutcome(game.away_name, away_key, away_price, away_point),
                    ),
                    last_update=None,
                )
            )

    total_prices = _side_prices(game, "over_odds", "under_odds")
    if game.over_under is not None and total_prices is None:
        log.debug(
            "ESPN %s: total %s has no overOdds/underOdds; contributing no totals market",
            game.league,
            game.over_under,
        )
    elif game.over_under is not None and total_prices is not None:
        over_price, under_price = total_prices
        markets.append(
            BookMarket(
                key="totals",
                outcomes=(
                    BookOutcome("Over", None, over_price, game.over_under),
                    BookOutcome("Under", None, under_price, game.over_under),
                ),
                last_update=None,
            )
        )
    return markets


__all__ = [
    "DEFAULT_BASE",
    "ESPN_BOOKMAKER",
    "ESPN_DEFAULT_TITLE",
    "ESPN_PATHS",
    "EspnClient",
    "EspnOddsGame",
    "parse_spread_details",
]
