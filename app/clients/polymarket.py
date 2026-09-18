"""Polymarket Gamma (events/markets) + CLOB (order books) client. Contract: docs/ARCHITECTURE.md.

Design notes
- Every request goes through the injected ``Transport``; a ``TransportError`` is re-raised as
  ``PolymarketError`` with the failing URL so the scan and the Diagnostics page see what broke.
  Nothing is swallowed: optional lookups that fail are logged at WARNING and fall back.
- Team names resolve through the injected ``team_resolver`` (default: ``matching.team_key``,
  imported lazily) so client tests can use a dict-backed resolver.
- Gamma sends ``outcomes`` / ``outcomePrices`` / ``clobTokenIds`` as JSON-encoded strings; the
  shared ``app.core.parsing`` helpers decode them and classify market type, spread and total.
- The league discriminator (``tag_slug`` / ``league``) is written into the URL *and* passed in
  ``params`` (httpx replaces the duplicated key, so the wire request has it once). Having it in
  the URL lets a ``FixtureTransport`` route one fixture file per league by URL prefix.

UNVERIFIED against the live API (docs/RESEARCH.md "Unverified"): the home/away convention.
Polymarket's outcome order for a game market is assumed to be ``[away, home]`` (US listing
convention "Away vs. Home" / "Away @ Home"), and ``teamAID`` / ``teamBID`` are assumed to follow
the same order. Matching is order-insensitive, so a wrong guess only swaps display names.

Also unverified, and therefore defended against rather than trusted: the unit of
``takerBaseFee`` (accepted only inside a plausible band, see ``_taker_fee_rate``), whether
``acceptingOrders`` is always present (``_accepting_orders`` falls back to
``enableOrderBook`` / ``active``), and whether Gamma accepts the optional ``/events`` query
parameters (``_events_page`` retries once without them on a 4xx).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.clients.transport import Transport, TransportError
from app.core.parsing import parse_json_list, parse_market_type, parse_spread, parse_total
from app.core.types import LEAGUES, BookLevel, League, MarketType, OrderBook, PmMarket, PmOutcome

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

EVENTS_PAGE_SIZE = 100
TEAMS_PAGE_SIZE = 500
BOOKS_CHUNK_SIZE = 200
MAX_PAGES = 100  # hard stop for offset paging; a real league never has 10k events
SPORTS_MARKET_TYPES: tuple[str, ...] = ("moneyline", "spreads", "totals")  # Gamma filter values
# Band a per-market `takerBaseFee` override must land in (as a rate) to be believed.
# Polymarket's published sports taker fee is 0.05; 0.01-0.20 brackets any plausible change.
MIN_TAKER_FEE_RATE = 0.01
MAX_TAKER_FEE_RATE = 0.2
NO_ACCEPTING_ORDERS_FLAG = "acceptingOrders missing (enableOrderBook/active off)"

TeamResolver = Callable[[str, League], "str | None"]  # defaults to matching.team_key, lazily


class PolymarketError(Exception):
    """A Polymarket request failed or its response could not be used (wraps TransportError)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _Skip(Exception):
    """Internal: a market that must be reported as unparseable with `reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- small helpers


def _default_resolver(name: str, league: League) -> str | None:
    from app.core.matching import team_key  # lazy: keeps client tests independent of matching

    return team_key(name, league)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso_utc(value: Any) -> datetime | None:
    """ISO-8601 -> tz-aware UTC datetime; accepts a trailing ``Z``, ``+00`` and naive (assumed
    UTC) strings such as Gamma's ``2026-09-20T20:25:00Z`` or ``2026-09-20 20:25:00+00``."""
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


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _first_float(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(mapping.get(key))
        if value is not None:
            return value
    return None


def _taker_fee_rate(raw: Any, market_id: str = "") -> float | None:
    """``takerBaseFee`` read as basis points, accepted only inside a plausible sports band.

    The unit of this field is UNVERIFIED (docs/RESEARCH.md item 7). Polymarket's published
    sports taker fee is 5% (500 bps), so a genuine per-market override lands between
    ``MIN_TAKER_FEE_RATE`` and ``MAX_TAKER_FEE_RATE`` once divided by 10000. A fraction
    (``0.05``), a percent (``5``) or a flag (``1``) would otherwise become 5e-06 / 5e-04 /
    1e-04 — an override that silently removes the fee and manufactures edge. Anything
    outside the band is logged and dropped so the fee-rate preference applies instead.
    """
    bps = _as_float(raw)
    if bps is None or bps <= 0:
        return None
    rate = bps / 10000.0
    if MIN_TAKER_FEE_RATE <= rate <= MAX_TAKER_FEE_RATE:
        return rate
    log.warning(
        "Polymarket market %s: implausible takerBaseFee %r (%.6g as basis points); "
        "using the taker fee preference instead",
        market_id or "?",
        raw,
        rate,
    )
    return None


def _accepting_orders(raw: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(accepting, flag_was_missing)``.

    Gamma payload variants omit ``acceptingOrders`` and carry only ``enableOrderBook`` /
    ``active``. Defaulting a missing flag to False would park every market of such a
    payload behind one indistinguishable "not accepting orders" reason, so fall back to
    those two fields and report that the flag itself was absent.
    """
    if raw.get("acceptingOrders") is not None:
        return _as_bool(raw.get("acceptingOrders")), False
    for key in ("enableOrderBook", "active"):
        if raw.get(key) is not None:
            return _as_bool(raw.get(key)), True
    return False, True


def _resolved_index(closed: bool, prices: Sequence[float | None]) -> int | None:
    """0 or 1 once ``outcomePrices`` settles to ``["1","0"]`` / ``["0","1"]`` on a closed market."""
    if not closed or len(prices) != 2 or prices[0] is None or prices[1] is None:
        return None
    a, b = prices
    if abs(a - 1.0) < 1e-9 and abs(b) < 1e-9:
        return 0
    if abs(a) < 1e-9 and abs(b - 1.0) < 1e-9:
        return 1
    return None


def _mirror(price: float | None) -> float | None:
    """Binary CLOB books mirror each other: a bid on outcome 0 at p is an ask on 1 at 1 - p."""
    return None if price is None else round(1.0 - price, 4)


def _as_list(payload: Any, what: str) -> list:
    """Gamma returns bare JSON arrays; tolerate a wrapper object with a single list value."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "events", "markets", "teams", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise PolymarketError(f"unexpected {what} payload: {type(payload).__name__}")


def _league_from(event: Mapping[str, Any], market: Mapping[str, Any]) -> League | None:
    """League of a bare /markets/{id} response: event tag slugs, then event/market slug prefix."""
    for tag in event.get("tags") or []:
        slug = tag.get("slug") if isinstance(tag, dict) else None
        if slug in LEAGUES:
            return slug  # type: ignore[return-value]
    for slug in (event.get("slug"), market.get("slug")):
        if isinstance(slug, str):
            prefix = slug.split("-", 1)[0].lower()
            if prefix in LEAGUES:
                return prefix  # type: ignore[return-value]
    return None


def _market_type_of(raw: Mapping[str, Any]) -> MarketType | None:
    smt = raw.get("sportsMarketType")
    return parse_market_type(None if smt is None else str(smt), str(raw.get("question") or ""))


# --------------------------------------------------------------------------- client


class PolymarketClient:
    def __init__(
        self,
        transport: Transport,
        gamma_base: str = GAMMA,
        clob_base: str = CLOB,
        team_resolver: TeamResolver | None = None,
    ) -> None:
        self.transport = transport
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self._team_resolver = team_resolver
        # league -> {team id: team name} from /teams; None records a failed lookup so a scan
        # does not retry it for every event.
        self._team_names: dict[str, dict[str, str] | None] = {}
        # market id -> league, remembered from events() so market() can classify a bare
        # /markets/{id} response that carries no event/tag information.
        self._league_by_market: dict[str, League] = {}
        # Event / market ids dropped as duplicates by the last events() call. Offset
        # paging over a list that changes between requests can serve an event twice;
        # the scan records these under Scan.notes.
        self.duplicates_dropped: list[str] = []
        # Per-market takerBaseFee overrides seen by the last events() call, as
        # {market_id, raw, rate}; `rate` is None when the value was outside the plausible
        # band and the fee-rate preference applies instead. Observability for Diagnostics.
        self.taker_fee_overrides: list[dict] = []
        # True once Gamma has rejected the optional /events query parameters
        # (order/ascending/sports_market_types) and the client dropped them. Sticky for the
        # life of the client so one 4xx does not cost every league a failed first page.
        self.events_filters_dropped = False

    # -- public API ----------------------------------------------------------

    def events(
        self, league: League, *, include_closed: bool = False
    ) -> tuple[list[PmMarket], list[dict]]:
        """Paginate /events?tag_slug=<league>; second item = unparseable markets
        [{market_id, question, reason}]."""
        if league not in LEAGUES:
            raise PolymarketError(f"unknown league {league!r}; expected one of {LEAGUES}")
        markets: list[PmMarket] = []
        unparseable: list[dict] = []
        seen_markets: set[str] = set()
        self.duplicates_dropped = []
        self.taker_fee_overrides = []
        for event in self._iter_events(league, include_closed):
            if not isinstance(event, dict):
                continue
            # The API already filters on closed=false; repeat it here so fixture-backed runs
            # (which cannot filter server side) behave the same way.
            if not include_closed and _as_bool(event.get("closed")):
                continue
            home_key, away_key = self._event_teams(event, league)
            for raw in event.get("markets") or []:
                if not isinstance(raw, dict):
                    continue
                market_id = str(raw.get("id") or "")
                if market_id and market_id in seen_markets:
                    self._note_duplicate(league, "market", market_id)
                    continue
                try:
                    market = self._parse_market(raw, event, league, home_key, away_key)
                except _Skip as skip:
                    unparseable.append(
                        {
                            "market_id": market_id,
                            "question": str(raw.get("question") or ""),
                            "reason": skip.reason,
                        }
                    )
                    continue
                seen_markets.add(market.market_id)
                self._league_by_market[market.market_id] = league
                markets.append(market)
        return markets, unparseable

    def _note_duplicate(self, league: League, what: str, ident: str) -> None:
        log.warning(
            "Gamma %s events: duplicate %s %s dropped (offset paging drift)", league, what, ident
        )
        self.duplicates_dropped.append(f"{what}:{ident}")

    def market(self, market_id: str, *, league: League | None = None) -> PmMarket | None:
        """GET /markets/{id} (used for settlement). None when the market does not exist or is
        not one of the three supported types; event fields are "" when Gamma omits the event.

        `league` (additive to the contract) is the caller's knowledge of the league, used
        when the payload carries neither a league tag nor a league-prefixed slug and the
        market was not seen by `events()` on this client instance.
        """
        url = f"{self.gamma_base}/markets/{market_id}"
        try:
            payload, _ = self.transport.get_json(url)
        except TransportError as exc:
            if exc.status == 404:
                log.warning("Gamma market %s not found: %s", market_id, exc)
                return None
            raise PolymarketError(f"Gamma market {market_id} fetch failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise PolymarketError(
                f"Gamma market {market_id}: unexpected payload {type(payload).__name__}"
            )
        events = payload.get("events")
        event: dict[str, Any] = {}
        if isinstance(events, list) and events and isinstance(events[0], dict):
            event = events[0]
        resolved = (
            _league_from(event, payload)
            or self._league_by_market.get(str(market_id))
            or (league if league in LEAGUES else None)
        )
        if resolved is None:
            raise PolymarketError(
                f"Gamma market {market_id}: cannot determine league (no league tag or slug prefix)"
            )
        league = resolved
        pseudo_event = dict(event)
        pseudo_event["markets"] = [payload]
        home_key, away_key = self._event_teams(pseudo_event, league)
        try:
            market = self._parse_market(payload, event, league, home_key, away_key)
        except _Skip as skip:
            log.warning("Gamma market %s skipped: %s", market_id, skip.reason)
            return None
        self._league_by_market[market.market_id] = league
        return market

    def order_books(self, token_ids: Sequence[str]) -> dict[str, OrderBook]:
        """POST /books in chunks of 200; token_id -> OrderBook (bids and asks best-first)."""
        ids = list(dict.fromkeys(str(t) for t in token_ids if t))  # dedupe, keep order
        books: dict[str, OrderBook] = {}
        if not ids:
            return books
        url = f"{self.clob_base}/books"
        fetched_at = _utcnow()
        for start in range(0, len(ids), BOOKS_CHUNK_SIZE):
            chunk = ids[start : start + BOOKS_CHUNK_SIZE]
            body = [{"token_id": token} for token in chunk]
            try:
                payload, _ = self.transport.post_json(url, body)
            except TransportError as exc:
                raise PolymarketError(
                    f"CLOB order books fetch failed ({len(chunk)} tokens): {exc}"
                ) from exc
            for token, raw in _iter_books(payload, chunk):
                books[token] = _parse_book(token, raw, fetched_at)
        return books

    def teams(self, league: League) -> list[dict]:
        """GET /teams?league=<league>&limit=500, paginated by offset -> raw team dicts."""
        if league not in LEAGUES:
            raise PolymarketError(f"unknown league {league!r}; expected one of {LEAGUES}")
        url = f"{self.gamma_base}/teams?league={league}"
        teams: list[dict] = []
        offset = 0
        for _page in range(MAX_PAGES):
            params = {"league": league, "limit": TEAMS_PAGE_SIZE, "offset": offset}
            page = _as_list(self._get(url, params, f"{league} teams"), f"{league} teams")
            teams.extend(t for t in page if isinstance(t, dict))
            if len(page) < TEAMS_PAGE_SIZE:
                break
            offset += TEAMS_PAGE_SIZE
        return teams

    # -- requests ------------------------------------------------------------

    def _get(self, url: str, params: Mapping[str, Any], what: str) -> Any:
        try:
            payload, _ = self.transport.get_json(url, params=params)
        except TransportError as exc:
            raise PolymarketError(f"Gamma {what} fetch failed: {exc}", status=exc.status) from exc
        return payload

    def _events_params(self, league: League, include_closed: bool, offset: int) -> dict[str, Any]:
        params: dict[str, Any] = {"tag_slug": league, "active": "true"}
        if not include_closed:
            params["closed"] = "false"
        params["limit"] = EVENTS_PAGE_SIZE
        params["offset"] = offset
        if self.events_filters_dropped:
            return params
        params["order"] = "id"
        params["ascending"] = "true"
        # Documented Gamma filter (docs/RESEARCH.md): only game markets of the three
        # supported types; fixture transports ignore it and the parser re-filters.
        params["sports_market_types"] = list(SPORTS_MARKET_TYPES)
        return params

    def _events_page(self, url: str, league: League, include_closed: bool, offset: int) -> list:
        """One /events page. On a 4xx for the first page, retry once without the optional
        `order` / `ascending` / `sports_market_types` parameters.

        Neither has been exercised live (docs/RESEARCH.md item 13) and both are only
        optimisations — the client re-filters market types and de-duplicates events itself
        — so a Gamma that rejects them must not cost every league every market.
        """
        what = f"{league} events"
        try:
            payload = self._get(url, self._events_params(league, include_closed, offset), what)
        except PolymarketError as exc:
            retryable = offset == 0 and not self.events_filters_dropped
            if not (retryable and exc.status is not None and 400 <= exc.status < 500):
                raise
            log.warning(
                "Gamma %s events: %s rejected the optional order/sports_market_types "
                "parameters; retrying without them (%s)",
                league,
                self.gamma_base,
                exc,
            )
            self.events_filters_dropped = True
            payload = self._get(url, self._events_params(league, include_closed, offset), what)
        return _as_list(payload, what)

    def _iter_events(self, league: League, include_closed: bool) -> Iterator[Any]:
        """Offset-paginate /events. A stable `order=id` means list drift between pages can
        only skip an event, not repeat it; events that still arrive twice are dropped."""
        url = f"{self.gamma_base}/events?tag_slug={league}"
        offset = 0
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            page = self._events_page(url, league, include_closed, offset)
            for event in page:
                event_id = str(event.get("id") or "") if isinstance(event, dict) else ""
                if event_id and event_id in seen:
                    self._note_duplicate(league, "event", event_id)
                    continue
                if event_id:
                    seen.add(event_id)
                yield event
            if len(page) < EVENTS_PAGE_SIZE:
                return
            offset += EVENTS_PAGE_SIZE
        log.warning("Gamma %s events: stopped after %d pages", league, MAX_PAGES)

    # -- teams ---------------------------------------------------------------

    def _resolve(self, name: Any, league: League) -> str | None:
        if not isinstance(name, str) or not name.strip():
            return None
        resolver = self._team_resolver or _default_resolver
        return resolver(name, league)

    def _team_names_for(self, league: League) -> dict[str, str] | None:
        """{team id: team name} from /teams, fetched once per league per client instance."""
        if league in self._team_names:
            return self._team_names[league]
        try:
            rows = self.teams(league)
        except PolymarketError as exc:
            log.warning(
                "Gamma /teams unavailable for %s; deriving home/away from outcomes (%s)",
                league,
                exc,
            )
            self._team_names[league] = None
            return None
        mapping = {
            str(row["id"]): str(row["name"])
            for row in rows
            if row.get("id") is not None and row.get("name")
        }
        self._team_names[league] = mapping
        return mapping

    def _event_teams(
        self, event: Mapping[str, Any], league: League
    ) -> tuple[str | None, str | None]:
        """(home_key, away_key) for an event.

        Preference order: ``teamAID``/``teamBID`` on any market when both resolve through
        /teams, then the moneyline market's outcomes, then a spread market's outcomes.
        ASSUMPTION (unverified, see module docstring): first listed = away, second = home,
        i.e. teamA / outcomes[0] is the away team and teamB / outcomes[1] the home team.
        """
        markets = [m for m in event.get("markets") or [] if isinstance(m, dict)]
        for raw in markets:
            a_id, b_id = raw.get("teamAID"), raw.get("teamBID")
            if a_id in (None, "", 0) or b_id in (None, "", 0):
                continue
            names = self._team_names_for(league)
            if not names:
                break  # /teams unavailable: ids are not resolvable, use outcomes instead
            a_key = self._resolve(names.get(str(a_id)), league)
            b_key = self._resolve(names.get(str(b_id)), league)
            if a_key and b_key and a_key != b_key:
                return b_key, a_key
            break
        for wanted in ("moneyline", "spread"):
            for raw in markets:
                if _market_type_of(raw) != wanted:
                    continue
                names = parse_json_list(raw.get("outcomes"))
                if len(names) != 2:
                    continue
                first, second = (self._resolve(str(n), league) for n in names)
                if first and second and first != second:
                    return second, first
        return None, None

    # -- market parsing ------------------------------------------------------

    def _parse_market(
        self,
        raw: Mapping[str, Any],
        event: Mapping[str, Any],
        league: League,
        home_key: str | None,
        away_key: str | None,
    ) -> PmMarket:
        question = str(raw.get("question") or "")
        market_type = _market_type_of(raw)
        if market_type is None:
            raise _Skip(f"unsupported market type {raw.get('sportsMarketType')!r}")
        smt = raw.get("sportsMarketType")
        inferred = smt is None or not str(smt).strip()
        if inferred and parse_iso_utc(raw.get("gameStartTime")) is None:
            # Classified from the question text alone: without a game start time this may
            # be a series or futures market wearing a game title, so do not price it.
            raise _Skip("no gameStartTime (market type inferred from the question)")
        names = [str(n) for n in parse_json_list(raw.get("outcomes"))]
        if len(names) != 2:
            raise _Skip(f"expected 2 outcomes, got {len(names)}")
        tokens = [str(t) for t in parse_json_list(raw.get("clobTokenIds"))]
        if len(tokens) != 2 or not all(tokens):
            raise _Skip(f"expected 2 clobTokenIds, got {len(tokens)}")
        prices = [_as_float(p) for p in parse_json_list(raw.get("outcomePrices"))]
        if len(prices) != 2:
            prices = [None, None]

        keys: list[str | None] = [None, None]
        if market_type != "total":
            keys = [self._resolve(name, league) for name in names]

        raw_line = _as_float(raw.get("line"))
        line: float | None = None
        line_team: str | None = None
        if market_type == "spread":
            parsed = parse_spread(question, raw_line, names, league)
            if parsed is None:
                raise _Skip("spread line/team not parseable")
            line, parsed_team = parsed
            line_team = _align_line_team(parsed_team, question, names, keys)
            if line_team is None:
                raise _Skip(f"spread team {parsed_team!r} is not one of the outcomes")
        elif market_type == "total":
            line = parse_total(question, raw_line)
            if line is None:
                raise _Skip("total line not parseable")

        closed = _as_bool(raw.get("closed"))
        accepting, flag_missing = _accepting_orders(raw)
        if flag_missing and not accepting and not closed:
            # Nothing in the payload says this market can be traded. Closed markets are kept:
            # `market()` re-reads them for settlement.
            raise _Skip(NO_ACCEPTING_ORDERS_FLAG)
        best_bid = _as_float(raw.get("bestBid"))
        best_ask = _as_float(raw.get("bestAsk"))
        outcomes = (
            PmOutcome(tokens[0], names[0], keys[0], prices[0], best_bid, best_ask),
            # Gamma's bestBid/bestAsk describe outcome 0's token; outcome 1's book is its mirror.
            PmOutcome(
                tokens[1], names[1], keys[1], prices[1], _mirror(best_ask), _mirror(best_bid)
            ),
        )
        # Only `gameStartTime` is a kickoff. An event's `startDate` is the listing /
        # creation timestamp on Gamma (the fixture sets it six days before the game), so
        # using it as a fallback would backdate the whole game and make the scan treat it
        # as already started. `None` is a valid game_start: matching falls back to its
        # unique home/away pair rule and the scan uses the matched book's commence_time.
        game_start = parse_iso_utc(raw.get("gameStartTime"))
        if game_start is None:
            log.debug(
                "Polymarket market %s has no gameStartTime; leaving game_start unset",
                raw.get("id"),
            )
        return PmMarket(
            market_id=str(raw.get("id") or ""),
            condition_id=str(raw.get("conditionId") or ""),
            slug=str(raw.get("slug") or ""),
            question=question,
            event_id=str(event.get("id") or ""),
            event_slug=str(event.get("slug") or ""),
            event_title=str(event.get("title") or ""),
            league=league,
            market_type=market_type,
            line=line,
            line_team_key=line_team,
            outcomes=outcomes,
            game_start=game_start,
            home_team_key=home_key,
            away_team_key=away_key,
            accepting_orders=accepting,
            closed=closed,
            resolved_outcome_index=_resolved_index(closed, prices),
            tick_size=_as_float(raw.get("orderPriceMinTickSize")),
            min_order_size=_as_float(raw.get("orderMinSize")),
            liquidity=_first_float(raw, "liquidityNum", "liquidity"),
            volume=_first_float(raw, "volumeNum", "volume"),
            taker_fee_rate=self._fee_override(raw),
        )

    def _fee_override(self, raw: Mapping[str, Any]) -> float | None:
        """Per-market taker fee, recorded on the client so Diagnostics can show it."""
        market_id = str(raw.get("id") or "")
        rate = _taker_fee_rate(raw.get("takerBaseFee"), market_id)
        if raw.get("takerBaseFee") not in (None, 0, "0", ""):
            self.taker_fee_overrides.append(
                {"market_id": market_id, "raw": raw.get("takerBaseFee"), "rate": rate}
            )
        return rate


def _align_line_team(
    parsed_team: str, question: str, names: Sequence[str], keys: Sequence[str | None]
) -> str | None:
    """Make ``line_team_key`` one of this market's outcome keys.

    ``parse_spread`` resolves the team with ``matching.team_key``; when the injected resolver
    uses the same key space the parsed key is already an outcome key. Otherwise pick the
    outcome whose label is named in the question so the market stays internally consistent.
    """
    if parsed_team in keys:
        return parsed_team
    haystack = f" {question.lower()} ".replace(":", " ").replace("(", " ")
    for name, key in zip(names, keys, strict=True):
        if key and f" {name.lower()} " in haystack:
            return key
    return None


# --------------------------------------------------------------------------- order books


def _iter_books(payload: Any, chunk: Sequence[str]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Yield (token_id, raw book) from a /books response.

    The live API returns a list in request order with ``asset_id`` set; positional order is
    the fallback when ``asset_id`` is missing. A JSON object keyed by token id (the fixture
    shape) is also accepted and filtered to the tokens that were asked for.
    """
    if isinstance(payload, list):
        for index, raw in enumerate(payload):
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "") or (chunk[index] if index < len(chunk) else "")
            if token:
                yield token, raw
        return
    if isinstance(payload, dict):
        wanted = set(chunk)
        for key, raw in payload.items():
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or key)
            if token in wanted:
                yield token, raw
        return
    raise PolymarketError(f"unexpected CLOB /books payload: {type(payload).__name__}")


def _levels(raw: Any) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        price, size = _as_float(item.get("price")), _as_float(item.get("size"))
        if price is None or size is None or size <= 0:
            continue
        levels.append(BookLevel(price=price, size=size))
    return levels


def _parse_book(token: str, raw: Mapping[str, Any], fetched_at: datetime) -> OrderBook:
    bids = sorted(_levels(raw.get("bids")), key=lambda lvl: lvl.price, reverse=True)
    asks = sorted(_levels(raw.get("asks")), key=lambda lvl: lvl.price)
    return OrderBook(
        token_id=token,
        bids=tuple(bids),
        asks=tuple(asks),
        tick_size=_as_float(raw.get("tick_size")),
        fetched_at=fetched_at,
    )


__all__ = [
    "BOOKS_CHUNK_SIZE",
    "CLOB",
    "EVENTS_PAGE_SIZE",
    "GAMMA",
    "MAX_PAGES",
    "MAX_TAKER_FEE_RATE",
    "MIN_TAKER_FEE_RATE",
    "NO_ACCEPTING_ORDERS_FLAG",
    "SPORTS_MARKET_TYPES",
    "TEAMS_PAGE_SIZE",
    "PolymarketClient",
    "PolymarketError",
    "TeamResolver",
    "parse_iso_utc",
]
