"""The Odds API v4 client. Contract: docs/ARCHITECTURE.md; wire format: docs/RESEARCH.md.

    GET {base}/sports/{sport_key}/odds
        ?apiKey=…&bookmakers=a,b,c&markets=h2h,spreads,totals&oddsFormat=american&dateFormat=iso

Response: a list of games ``{id, sport_key, commence_time, home_team, away_team,
bookmakers:[{key, title, last_update, markets:[{key, last_update, outcomes:[{name, price,
point?}]}]}]}``. Quota headers: ``x-requests-remaining``, ``x-requests-used``,
``x-requests-last``. Cost per call is ``markets x ceil(bookmakers / 10)`` credits (every
ten bookmakers count as one region).

The API key travels only in the query string. It is never logged, and it is redacted from
any exception this module raises (`OddsApiError`).

The parser is defensive: malformed games, bookmakers, markets and outcomes are skipped
with a log line instead of failing the whole refresh, and a team the resolver cannot
name yields an empty team key ("") so the matcher can report it on Diagnostics.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.clients.polymarket import TeamResolver
from app.clients.transport import Transport, TransportError
from app.core.types import BookGame, BookMarket, BookOutcome, BookQuote, League, QuotaInfo

log = logging.getLogger(__name__)

SPORT_KEYS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba", "mlb": "baseball_mlb"}
DEFAULT_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_MARKETS: tuple[str, ...] = ("h2h", "spreads", "totals")
BOOK_MARKET_KEYS: frozenset[str] = frozenset({"h2h", "spreads", "totals"})
BOOKMAKERS_PER_REGION = 10
REDACTED = "***"

HEADER_REMAINING = "x-requests-remaining"
HEADER_USED = "x-requests-used"
HEADER_LAST = "x-requests-last"


class OddsApiError(TransportError):
    """A failure talking to The Odds API. The API key is redacted from message and url."""


# --------------------------------------------------------------------------- helpers


def _default_team_resolver(name: str, league: League) -> str | None:
    # Lazy import so client tests can inject a dict-backed resolver and never load matching.
    from app.core.matching import team_key

    return team_key(name, league)


def parse_iso_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime; None when unparseable.

    Accepts a trailing ``Z``, an explicit offset, missing seconds (ESPN writes
    ``2026-09-20T20:25Z``) and naive timestamps (assumed UTC).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_american(value: Any) -> int | None:
    """Coerce an American price to int. Rejects bools, |price| < 100 (a decimal price that
    slipped through) and anything non-numeric. ``EVEN`` / ``PK`` mean +100."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.upper() in {"EVEN", "EV", "PK", "PICK"}:
            return 100
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, int):
        price = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        price = int(value)
    else:
        return None
    if abs(price) < 100:
        return None
    return price


def to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> int | None:
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


def parse_quota(headers: Mapping[str, str] | None) -> QuotaInfo:
    """Read the three quota headers case-insensitively; each is an int or None."""
    lowered: dict[str, str] = {}
    for key, value in (headers or {}).items():
        lowered[str(key).lower()] = str(value)
    return QuotaInfo(
        remaining=_to_int(lowered.get(HEADER_REMAINING)),
        used=_to_int(lowered.get(HEADER_USED)),
        last_cost=_to_int(lowered.get(HEADER_LAST)),
    )


# --------------------------------------------------------------------------- client


def plain_api_key(value: Any) -> str:
    """The key as text.

    Accepts a `pydantic.SecretStr` (how `Settings` stores `ODDS_API_KEY`) without importing
    pydantic here, so a `SecretStr` can never reach the query string, where it would be sent
    as its `**********` placeholder.
    """
    if value is None:
        return ""
    reveal = getattr(value, "get_secret_value", None)
    if callable(reveal):
        return str(reveal())
    return str(value)


class OddsApiClient:
    def __init__(
        self,
        transport: Transport,
        api_key: str | Any,
        base: str = DEFAULT_BASE,
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.api_key = plain_api_key(api_key)
        self.base = base.rstrip("/")
        self._team_resolver: TeamResolver = team_resolver or _default_team_resolver
        # Raw team labels the resolver could not name, as {league, name}; the scan copies
        # them into Scan.notes so a renamed team shows up on Diagnostics instead of as a
        # silent "no book game".
        self.unresolved_teams: list[dict[str, str]] = []

    def __repr__(self) -> str:
        state = "set" if self.api_key else "unset"
        return f"OddsApiClient(base={self.base!r}, api_key={state})"

    # -- public --------------------------------------------------------------

    def odds(
        self,
        league: League,
        bookmakers: Sequence[str],
        markets: Sequence[str] = DEFAULT_MARKETS,
    ) -> tuple[list[BookGame], QuotaInfo]:
        """GET /sports/{sport_key}/odds for the given bookmakers and markets.

        Returns the parsed games and the quota headers. Raises ``ValueError`` for an
        unsupported league or an empty bookmaker/market list (an empty ``bookmakers``
        would make the API fall back to regions and cost more credits), and
        ``OddsApiError`` for transport failures.
        """
        sport_key = SPORT_KEYS.get(league)
        if sport_key is None:
            raise ValueError(f"unsupported league {league!r}; expected one of {sorted(SPORT_KEYS)}")
        books = [b.strip() for b in bookmakers if isinstance(b, str) and b.strip()]
        keys = [m.strip() for m in markets if isinstance(m, str) and m.strip()]
        if not books:
            raise ValueError("bookmakers must not be empty")
        if not keys:
            raise ValueError("markets must not be empty")

        url = f"{self.base}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "bookmakers": ",".join(books),
            "markets": ",".join(keys),
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        log.info("GET %s bookmakers=%s markets=%s", url, params["bookmakers"], params["markets"])
        try:
            payload, headers = self.transport.get_json(url, params=params)
        except TransportError as exc:
            raise OddsApiError(
                self._redact(exc.message) or "Odds API request failed",
                status=exc.status,
                url=self._redact(exc.url),
            ) from None
        except Exception as exc:  # never let a raw exception carry the key upward
            raise OddsApiError(
                self._redact(f"{type(exc).__name__}: {exc}") or type(exc).__name__,
                status=None,
                url=url,
            ) from None

        quota = parse_quota(headers)
        games = self._parse_games(payload, league, url)
        log.info(
            "odds %s: %d games; credits remaining=%s used=%s last=%s",
            league,
            len(games),
            quota.remaining,
            quota.used,
            quota.last_cost,
        )
        return games, quota

    @staticmethod
    def estimate_cost(n_markets: int, n_bookmakers: int) -> int:
        """Credits one call costs: ``n_markets * ceil(n_bookmakers / 10)`` (at least 1 region)."""
        regions = max(1, math.ceil(max(0, int(n_bookmakers)) / BOOKMAKERS_PER_REGION))
        return max(0, int(n_markets)) * regions

    # -- internals -----------------------------------------------------------

    def _redact(self, text: str | None) -> str | None:
        if text is None or not self.api_key:
            return text
        redacted = text.replace(self.api_key, REDACTED)
        encoded = quote(self.api_key, safe="")
        if encoded != self.api_key:
            redacted = redacted.replace(encoded, REDACTED)
        return redacted

    def _resolve(self, name: str, league: League) -> str | None:
        try:
            return self._team_resolver(name, league)
        except Exception:  # a resolver bug must not take the whole refresh down
            log.exception("team resolver failed for %r (%s)", name, league)
            return None

    def _note_unresolved(self, league: League, name: str) -> None:
        entry = {"league": str(league), "name": name, "source": "oddsapi"}
        if entry not in self.unresolved_teams:
            log.warning("Odds API %s: unresolved team %r", league, name)
            self.unresolved_teams.append(entry)

    def _parse_games(self, payload: Any, league: League, url: str) -> list[BookGame]:
        if isinstance(payload, dict) and "message" in payload:
            raise OddsApiError(
                f"Odds API error: {self._redact(str(payload.get('message')))}", status=None, url=url
            )
        if not isinstance(payload, list):
            raise OddsApiError(
                f"unexpected Odds API response shape: {type(payload).__name__}",
                status=None,
                url=url,
            )
        games: list[BookGame] = []
        for raw in payload:
            game = self._parse_game(raw, league)
            if game is not None:
                games.append(game)
        return games

    def _parse_game(self, raw: Any, league: League) -> BookGame | None:
        if not isinstance(raw, dict):
            log.warning("Odds API %s: skipping non-object game entry", league)
            return None
        game_id = str(raw.get("id") or "").strip()
        home_name = str(raw.get("home_team") or "").strip()
        away_name = str(raw.get("away_team") or "").strip()
        commence = parse_iso_utc(raw.get("commence_time"))
        if not game_id or not home_name or not away_name or commence is None:
            log.warning(
                "Odds API %s: skipping game %r (missing id/teams/commence_time)",
                league,
                game_id or raw.get("id"),
            )
            return None
        home_key = self._resolve(home_name, league) or ""
        away_key = self._resolve(away_name, league) or ""
        for name, key in ((home_name, home_key), (away_name, away_key)):
            if not key:
                self._note_unresolved(league, name)
        books: list[BookQuote] = []
        raw_books = raw.get("bookmakers")
        for raw_book in raw_books if isinstance(raw_books, list) else []:
            quote_ = self._parse_book(raw_book, league)
            if quote_ is not None:
                books.append(quote_)
        return BookGame(
            game_id=game_id,
            league=league,
            commence_time=commence,
            home_team_key=home_key,
            away_team_key=away_key,
            home_team_name=home_name,
            away_team_name=away_name,
            books=tuple(books),
        )

    def _parse_book(self, raw: Any, league: League) -> BookQuote | None:
        if not isinstance(raw, dict):
            return None
        key = str(raw.get("key") or "").strip()
        if not key:
            return None
        title = str(raw.get("title") or key).strip() or key
        book_updated = parse_iso_utc(raw.get("last_update"))
        markets: list[BookMarket] = []
        raw_markets = raw.get("markets")
        for raw_market in raw_markets if isinstance(raw_markets, list) else []:
            market = self._parse_market(raw_market, league, book_updated)
            if market is not None:
                markets.append(market)
        if not markets:
            log.debug("Odds API %s: bookmaker %s has no usable markets", league, key)
            return None
        return BookQuote(bookmaker=key, title=title, markets=tuple(markets))

    def _parse_market(
        self, raw: Any, league: League, fallback_updated: datetime | None
    ) -> BookMarket | None:
        if not isinstance(raw, dict):
            return None
        key = str(raw.get("key") or "").strip()
        if key not in BOOK_MARKET_KEYS:
            return None
        outcomes: list[BookOutcome] = []
        raw_outcomes = raw.get("outcomes")
        for raw_outcome in raw_outcomes if isinstance(raw_outcomes, list) else []:
            outcome = self._parse_outcome(raw_outcome, key, league)
            if outcome is not None:
                outcomes.append(outcome)
        if not outcomes:
            return None
        last_update = parse_iso_utc(raw.get("last_update")) or fallback_updated
        return BookMarket(
            key=key,  # type: ignore[arg-type]  validated against BOOK_MARKET_KEYS above
            outcomes=tuple(outcomes),
            last_update=last_update,
        )

    def _parse_outcome(self, raw: Any, market_key: str, league: League) -> BookOutcome | None:
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        price = to_american(raw.get("price"))
        if not name or price is None:
            log.debug("Odds API %s: skipping %s outcome %r (name/price)", league, market_key, raw)
            return None
        point = to_float(raw.get("point")) if "point" in raw else None
        team_key = self._resolve(name, league) if market_key != "totals" else None
        return BookOutcome(name=name, team_key=team_key, price_american=price, point=point)


__all__ = [
    "BOOKMAKERS_PER_REGION",
    "BOOK_MARKET_KEYS",
    "DEFAULT_BASE",
    "DEFAULT_MARKETS",
    "HEADER_LAST",
    "HEADER_REMAINING",
    "HEADER_USED",
    "SPORT_KEYS",
    "OddsApiClient",
    "OddsApiError",
    "parse_iso_utc",
    "plain_api_key",
    "parse_quota",
    "to_american",
    "to_float",
]
