"""Scan orchestration. Contract: docs/ARCHITECTURE.md "Services API" and SPEC.md "Scan".

Pure orchestration: every number comes from `app.core`, every row goes through the ORM.

Kinds
- ``poly``: Polymarket events + order books for the enabled leagues, re-priced against the
  most recent *stored* sportsbook snapshot per game that is not older than
  ``prefs.stale_book_minutes``. Never calls the Odds API.
- ``books``: the Odds API per league (skipped without a key or bookmakers; ESPN scoreboard
  fallback when the call fails or there is no key and ``prefs.espn_fallback_enabled``),
  then everything ``poly`` does with the fresh books. If both sources fail (or ESPN has
  no games for the league) the league falls back to the stored snapshot so the scan
  still prices what it can.
- ``both``: books first, then Polymarket, in one scan row.

Only pre-game, tradable markets are priced: a market that is closed, not accepting
orders, or whose game has started (Polymarket's ``gameStartTime`` or the matched book's
``commence_time`` at or before ``now``) is listed under ``Scan.notes.unmatched`` with the
reason instead of being compared with a pre-game book snapshot.

Failure policy: one league failing never aborts the others (the reason lands in
``Scan.errors``); an exception outside the per-league loop marks the scan failed and is
re-raised after the row is persisted.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.espn import EspnClient
from app.clients.oddsapi import OddsApiClient
from app.clients.polymarket import PolymarketClient
from app.clients.transport import HttpTransport
from app.core.edge import build_opportunity, stake_note
from app.core.matching import TEAM_ALIASES, fair_for_outcome, match_games, unmatched_reasons
from app.core.types import (
    LEAGUES,
    BookGame,
    OrderBook,
    PmMarket,
    PrefsLike,
    QuotaInfo,
    ScanResult,
)
from app.core.types import (
    Opportunity as CoreOpportunity,
)
from app.models import (
    DEFAULT_PREFS,
    Bet,
    BookQuote,
    Game,
    Market,
    Opportunity,
    PmQuote,
    Prefs,
    Scan,
)
from app.services import bets as bets_service
from app.services import forward, wallet_import
from app.services.adapters import RESOLVED_LETTER, stored_book_game
from app.services.prefs import get_prefs
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)

SCAN_KINDS: frozenset[str] = frozenset({"poly", "books", "both"})
BOOK_MARKETS: tuple[str, ...] = ("h2h", "spreads", "totals")
DEPTH_LEVELS = 10
NO_BOOK_AT_LINE = "no book at line"
GAME_STARTED = "game started"
MARKET_CLOSED = "market closed"
MARKET_PAUSED = "market not accepting orders"
DEMO_API_KEY = "demo"  # never sent anywhere: the demo transport serves fixtures
# Two Game rows for the same teams closer than this are the same game (an MLB doubleheader
# is hours apart); used when a market carries no Polymarket event id.
SAME_GAME_TOLERANCE = timedelta(hours=3)
# ESPN buckets its scoreboard by the US Eastern calendar date, not UTC.
ESPN_TZ = ZoneInfo("America/New_York")
HOME_AWAY_CONVENTION = "assumed Polymarket outcomes / teamA,teamB = [away, home] (unverified)"

# One scan at a time per process: two scans writing Scan/Game/Market rows concurrently would
# interleave their upserts, and a Books refresh that piles up spends credits twice. The
# scheduler has its own lock for its two jobs; this one also covers manual refreshes.
SCAN_LOCK = threading.Lock()


class ScanBusy(RuntimeError):
    """`run_scan_default` was called while another scan is running (the UI shows a toast)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pref(prefs: Any, name: str) -> Any:
    """Prefs field with the SPEC default for `PrefsLike` objects that lack it."""
    value = getattr(prefs, name, None)
    return DEFAULT_PREFS[name] if value is None else value


# --------------------------------------------------------------------------- state


@dataclass
class _State:
    scan: Scan
    kind: str
    prefs: Any
    now: datetime
    n_markets: int = 0
    n_matched: int = 0
    n_opps: int = 0
    credits_used: int | None = None
    credits_remaining: int | None = None
    quota_used: int | None = None
    errors: list[str] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    unparseable: list[dict] = field(default_factory=list)
    book_source: dict[str, str] = field(default_factory=dict)
    markets_by_id: dict[str, PmMarket] = field(default_factory=dict)
    duplicates_dropped: list[str] = field(default_factory=list)
    fee_overrides: list[dict] = field(default_factory=list)
    events_filters_dropped: bool = False

    def error(self, league: str, what: str, exc: BaseException) -> None:
        message = f"{league}: {what}: {type(exc).__name__}: {exc}"
        log.warning(message)
        self.errors.append(message)

    def record_quota(self, quota: QuotaInfo | None, n_bookmakers: int) -> None:
        if quota is None:
            return
        cost = quota.last_cost
        if cost is None:
            cost = OddsApiClient.estimate_cost(len(BOOK_MARKETS), n_bookmakers)
        self.credits_used = (self.credits_used or 0) + int(cost)
        if quota.remaining is not None:
            self.credits_remaining = quota.remaining
        if quota.used is not None:
            self.quota_used = quota.used


# --------------------------------------------------------------------------- persistence


def _display_name(league: str, key: str | None, fallback: str | None) -> str | None:
    if key and key in TEAM_ALIASES.get(league, {}):
        return TEAM_ALIASES[league][key]
    return fallback or key


def _market_teams(market: PmMarket) -> tuple[str | None, str | None]:
    """(home_key, away_key): the event's when known, else the outcomes as [away, home]."""
    if market.home_team_key and market.away_team_key:
        return market.home_team_key, market.away_team_key
    if market.market_type in ("moneyline", "spread"):
        away, home = (o.team_key for o in market.outcomes)
        if away and home and away != home:
            return home, away
    return market.home_team_key, market.away_team_key


def _game_status(market: PmMarket, start: datetime | None, now: datetime) -> str:
    """Status from the *game's* kickoff, not the market's own (possibly absent) one."""
    if market.closed:
        return "final"
    if start is not None and start <= now:
        return "live"
    return "scheduled"


def _kickoff(game: Game, market: PmMarket) -> datetime | None:
    """The kickoff to store for `game` given one of its markets, or None to leave it alone.

    Only a market's own `gameStartTime` is a kickoff. Gamma's event `startDate` is generally
    the listing timestamp, so a sibling market that inherits it must never drag an
    established kickoff backwards (that would mark a Sunday game "live" since the Monday it
    was listed, and stop it being priced). The moneyline market is the authority: it is the
    one market every game has, so its start wins outright; other market types may only fill
    an empty kickoff or push it later.
    """
    start = market.game_start
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    current = game.start_time
    if current is None or market.market_type == "moneyline":
        return start
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return start if start > current else None


def upsert_game(session: Session, market: PmMarket, now: datetime) -> Game:
    """Find-or-create the Game for a Polymarket market.

    Looked up by the Polymarket event id first (one event per game, so a same-day
    doubleheader is two rows), then by league + home + away with a start time within
    `SAME_GAME_TOLERANCE` of the market's (for rows created without an event id).

    The stored kickoff follows `_kickoff`: a sibling market can never move it backwards, so
    one market missing `gameStartTime` cannot make the whole game look like it has started.
    """
    home, away = _market_teams(market)
    start = market.game_start
    game: Game | None = None
    if market.event_id:
        game = session.scalars(
            select(Game)
            .where(Game.league == market.league, Game.pm_event_id == market.event_id)
            .order_by(Game.id.asc())
        ).first()
    if game is None and home and away and start is not None:
        game = session.scalars(
            select(Game)
            .where(
                Game.league == market.league,
                Game.home_key == home,
                Game.away_key == away,
                Game.start_time >= start - SAME_GAME_TOLERANCE,
                Game.start_time <= start + SAME_GAME_TOLERANCE,
            )
            .order_by(Game.id.asc())
        ).first()
    if game is None:
        game = Game(league=market.league)
        session.add(game)

    outcome_names = [o.name for o in market.outcomes]
    home_fallback = outcome_names[1] if market.market_type != "total" else None
    away_fallback = outcome_names[0] if market.market_type != "total" else None
    game.league = market.league
    if home:
        game.home_key = home
        game.home_name = _display_name(market.league, home, home_fallback)
    if away:
        game.away_key = away
        game.away_name = _display_name(market.league, away, away_fallback)
    kickoff = _kickoff(game, market)
    if kickoff is not None:
        game.start_time = kickoff
    if market.event_slug:
        game.pm_event_slug = market.event_slug
    if market.event_id:
        game.pm_event_id = market.event_id
    status = _game_status(market, game.start_time, now)
    if game.status != "final" or status == "final":
        game.status = status
    session.flush()
    return game


def upsert_market(session: Session, market: PmMarket, game: Game | None, now: datetime) -> Market:
    """Insert or update the Market row (keyed by the Gamma market id)."""
    row = session.get(Market, market.market_id)
    if row is None:
        row = Market(id=market.market_id)
        session.add(row)
    a, b = market.outcomes
    row.game_id = game.id if game is not None else row.game_id
    row.market_type = market.market_type
    row.line = market.line
    row.line_team_key = market.line_team_key
    row.question = market.question or ""
    row.slug = market.slug or None
    row.condition_id = market.condition_id or None
    row.outcome_a_name, row.outcome_a_key, row.outcome_a_token = a.name, a.team_key, a.token_id
    row.outcome_b_name, row.outcome_b_key, row.outcome_b_token = b.name, b.team_key, b.token_id
    row.tick_size = market.tick_size
    row.min_order_size = market.min_order_size
    row.accepting_orders = bool(market.accepting_orders)
    row.closed = bool(market.closed)
    if market.resolved_outcome_index is not None:
        row.resolved_outcome = RESOLVED_LETTER.get(market.resolved_outcome_index)
    row.liquidity = market.liquidity
    row.volume = market.volume
    row.last_seen_at = now
    session.flush()
    return row


def _depth(levels: Sequence[Any]) -> list[list[float]]:
    return [[float(lvl.price), float(lvl.size)] for lvl in levels[:DEPTH_LEVELS]]


def _store_pm_quotes(
    session: Session, scan: Scan, market: PmMarket, books: Mapping[str, OrderBook], now: datetime
) -> None:
    for outcome in market.outcomes:
        book = books.get(outcome.token_id)
        bid = book.best_bid if book is not None else None
        ask = book.best_ask if book is not None else None
        session.add(
            PmQuote(
                scan_id=scan.id,
                market_id=market.market_id,
                token=outcome.token_id,
                best_bid=bid,
                best_ask=ask,
                mid=round((bid + ask) / 2.0, 6) if bid is not None and ask is not None else None,
                ask_depth_json=_depth(book.asks) if book is not None else [],
                bid_depth_json=_depth(book.bids) if book is not None else [],
                # The scan clock, not the wall clock of the fetch: closing-line capture
                # compares this with the game start, and demo scans run on a fixed clock.
                fetched_at=now,
            )
        )


def _store_book_quotes(
    session: Session, scan: Scan, game: Game, book_game: BookGame, now: datetime
) -> None:
    for quote in book_game.books:
        for market in quote.markets:
            for outcome in market.outcomes:
                session.add(
                    BookQuote(
                        scan_id=scan.id,
                        game_id=game.id,
                        bookmaker=quote.bookmaker,
                        market_key=market.key,
                        outcome_name=outcome.name,
                        price_american=outcome.price_american,
                        point=outcome.point,
                        last_update=market.last_update,
                        fetched_at=now,
                    )
                )


def _store_opportunity(
    session: Session, scan: Scan, opp: CoreOpportunity, note: str | None = None
) -> Opportunity:
    outcome = opp.market.outcomes[opp.outcome_index]
    row = Opportunity(
        scan_id=scan.id,
        market_id=opp.market.market_id,
        token=outcome.token_id,
        outcome_key=outcome.team_key,
        outcome_name=outcome.name,
        ask=opp.ask,
        effective_price=opp.effective_price,
        fair_prob=opp.fair.value,
        fair_method=opp.fair.method,
        n_books=opp.fair.n_books,
        books_used=list(opp.fair.books_used),
        edge=opp.edge,
        ev_per_dollar=opp.ev_per_dollar,
        kelly=opp.kelly,
        suggested_stake=opp.suggested_stake,
        fill_price=opp.fill_price,
        fill_complete=bool(opp.fill_complete),
        fill_usd=opp.fill_usd,
        stake_note=note,
        limit_price=opp.limit_price,
        computed_at=opp.computed_at,
    )
    session.add(row)
    return row


def _skip_reason(market: PmMarket, now: datetime) -> str | None:
    """Why a market must not be priced against a pre-game book snapshot, or None."""
    if market.closed:
        return MARKET_CLOSED
    if not market.accepting_orders:
        return MARKET_PAUSED
    if market.game_start is not None and market.game_start <= now:
        return GAME_STARTED
    return None


def _note_skipped(state: _State, league: str, market: PmMarket, reason: str) -> None:
    state.unmatched.append(
        {
            "market_id": market.market_id,
            "question": market.question,
            "league": league,
            "market_type": market.market_type,
            "line": market.line,
            "reason": reason,
        }
    )


# --------------------------------------------------------------------------- books


def _espn_book_games(espn: EspnClient, league: str, state: _State) -> list[BookGame] | None:
    """ESPN scoreboard for yesterday, today and tomorrow in US Eastern time (the scoreboard
    is bucketed by Eastern calendar date, so a West-coast night game that starts after
    00:00 UTC sits under the previous date), de-duplicated by event id.

    None when every day failed, so the caller falls back to the stored snapshot.
    """
    seen: dict[str, Any] = {}
    today = state.now.astimezone(ESPN_TZ).date()
    failures = 0
    days = (today - timedelta(days=1), today, today + timedelta(days=1))
    for day in days:
        try:
            games = espn.scoreboard(league, day)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - one day failing is not fatal
            state.error(league, f"espn scoreboard {day.isoformat()}", exc)
            failures += 1
            continue
        for game in games:
            seen.setdefault(game.espn_id, game)
    if failures == len(days):
        return None
    return EspnClient.to_book_games(list(seen.values()))


def _fetch_books(
    league: str,
    oddsapi: OddsApiClient | None,
    espn: EspnClient | None,
    state: _State,
) -> tuple[list[BookGame], str] | None:
    """Fresh book games for a league: Odds API, else ESPN fallback, else None (use stored)."""
    bookmakers = [b for b in (_pref(state.prefs, "bookmakers") or []) if b]
    if oddsapi is not None and bookmakers:
        try:
            games, quota = oddsapi.odds(league, bookmakers)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - fall through to ESPN / stored
            state.error(league, "odds api", exc)
        else:
            state.record_quota(quota, len(bookmakers))
            return games, "oddsapi"
    if espn is not None and _pref(state.prefs, "espn_fallback_enabled"):
        games = _espn_book_games(espn, league, state)
        if games:
            return games, "espn"
        # every day failed, or ESPN has no priced games for the league: use the snapshot
    return None


def _stored_books(
    session: Session, games: Sequence[Game], state: _State
) -> tuple[list[BookGame], str]:
    """The freshest stored snapshot per game (not older than stale_book_minutes)."""
    stale_minutes = int(_pref(state.prefs, "stale_book_minutes") or 0)
    cutoff = state.now - timedelta(minutes=stale_minutes) if stale_minutes > 0 else None
    out: list[BookGame] = []
    scan_ids: set[int] = set()
    for game in games:
        book_game, scan_id = stored_book_game(session, game, not_before=cutoff)
        if book_game is not None and scan_id is not None:
            out.append(book_game)
            scan_ids.add(scan_id)
    source = "stored:" + ",".join(str(s) for s in sorted(scan_ids)) if scan_ids else "none"
    return out, source


# --------------------------------------------------------------------------- one league


def _scan_league(
    session: Session,
    league: str,
    *,
    polymarket: PolymarketClient,
    oddsapi: OddsApiClient | None,
    espn: EspnClient | None,
    state: _State,
) -> None:
    scan, prefs, now = state.scan, state.prefs, state.now

    markets, unparseable = polymarket.events(league)  # type: ignore[arg-type]
    for item in unparseable:
        state.unparseable.append({**item, "league": league})
    for dup in getattr(polymarket, "duplicates_dropped", None) or []:
        state.duplicates_dropped.append(f"{league}:{dup}")
    # Per-market fee overrides the client accepted or rejected. The client's list is reset
    # on every events() call, so it has to be drained here, per league.
    for override in getattr(polymarket, "taker_fee_overrides", None) or []:
        state.fee_overrides.append({**override, "league": league})
    if getattr(polymarket, "events_filters_dropped", False):
        state.events_filters_dropped = True
    state.n_markets += len(markets)
    if not markets:
        state.book_source[league] = "none"
        return

    tokens = [o.token_id for m in markets for o in m.outcomes]
    try:
        books = polymarket.order_books(tokens)
    except Exception as exc:  # noqa: BLE001 - markets are still worth persisting
        state.error(league, "order books", exc)
        books = {}

    # Games and markets first, so stored books can be looked up per game row.
    game_rows: dict[str, Game] = {}
    games_in_order: list[Game] = []
    for market in markets:
        game = upsert_game(session, market, now)
        upsert_market(session, market, game, now)
        game_rows[market.market_id] = game
        if game not in games_in_order:
            games_in_order.append(game)
        state.markets_by_id[market.market_id] = market

    fresh: tuple[list[BookGame], str] | None = None
    if state.kind in ("books", "both"):
        fresh = _fetch_books(league, oddsapi, espn, state)
    if fresh is not None:
        book_games, source = fresh
    else:
        book_games, source = _stored_books(session, games_in_order, state)
    state.book_source[league] = source

    # Only pre-game, tradable markets are compared with the (pre-game) book snapshot.
    tradable: list[PmMarket] = []
    for market in markets:
        reason = _skip_reason(market, now)
        if reason is None:
            tradable.append(market)
        else:
            _note_skipped(state, league, market, reason)

    window = float(_pref(prefs, "match_window_hours"))
    matched = match_games(tradable, book_games, window)
    state.unmatched.extend(unmatched_reasons(tradable, book_games, window))
    for market_id, book_game in list(matched.items()):
        if book_game.commence_time <= now:  # the book says the game is under way
            del matched[market_id]
            _note_skipped(state, league, state.markets_by_id[market_id], GAME_STARTED)
    state.n_matched += len(matched)

    if fresh is not None:
        stored_games: set[int] = set()
        for market_id, book_game in matched.items():
            game = game_rows[market_id]
            if game.id in stored_games:
                continue
            stored_games.add(game.id)
            game.book_game_id = book_game.game_id
            if book_game.game_id.startswith("espn:"):
                game.espn_event_id = book_game.game_id.split(":", 1)[1]
            _store_book_quotes(session, scan, game, book_game, now)

    weights = _pref(prefs, "book_weights") or {}
    method = _pref(prefs, "devig_method")
    for market in markets:
        _store_pm_quotes(session, scan, market, books, now)
        book_game = matched.get(market.market_id)
        if book_game is None:
            continue
        priced = False
        for index in (0, 1):
            outcome = market.outcomes[index]
            try:
                fair = fair_for_outcome(market, index, book_game, weights, method)
            except ValueError as exc:
                state.error(league, f"fair for {market.market_id}/{outcome.name}", exc)
                continue
            if fair is None:
                continue
            priced = True
            book = books.get(outcome.token_id)
            if book is not None and book.token_id != outcome.token_id:
                raise RuntimeError(
                    f"order book keyed by {book.token_id} used for {outcome.token_id}"
                )
            # Forward test: record what we saw whatever the edge, so the threshold question
            # can be asked later from data instead of being fixed now. Never let it break a
            # scan -- it is observation, not part of the pricing path.
            try:
                forward.record_sample(
                    session,
                    scan_id=scan.id,
                    market=market,
                    outcome_index=index,
                    book=book,
                    fair=fair,
                    prefs_fee_rate=float(_pref(prefs, "taker_fee_rate")),
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001
                state.error(league, f"forward sample for {market.market_id}", exc)
            try:
                opp = build_opportunity(market, index, book, fair, prefs, book_game, now)
            except ValueError as exc:
                state.error(league, f"opportunity for {market.market_id}/{outcome.name}", exc)
                continue
            if opp is not None:
                _store_opportunity(session, scan, opp, stake_note(opp, book, prefs))
                state.n_opps += 1
        if not priced:
            state.unmatched.append(
                {
                    "market_id": market.market_id,
                    "question": market.question,
                    "league": league,
                    "market_type": market.market_type,
                    "line": market.line,
                    "reason": NO_BOOK_AT_LINE,
                }
            )
    session.flush()


# --------------------------------------------------------------------------- settlement


def _markets_for_open_bets(session: Session, polymarket: PolymarketClient, state: _State) -> None:
    """Fetch (GET /markets/{id}) every open bet's market that the scan did not already see
    (closed markets are not part of the active slate) so settlement can run."""
    open_ids = {
        market_id
        for (market_id,) in session.execute(
            select(Bet.market_id).where(Bet.status == "open").distinct()
        )
    }
    for market_id in sorted(open_ids - set(state.markets_by_id)):
        row = session.get(Market, market_id)
        game = session.get(Game, row.game_id) if row is not None and row.game_id else None
        league = game.league if game is not None else None
        try:
            market = polymarket.market(market_id, league=league)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - settlement of one bet must not abort the scan
            state.error("settle", f"market {market_id}", exc)
            continue
        if market is None:
            continue
        state.markets_by_id[market_id] = market
        upsert_market(session, market, game, state.now)


def _markets_for_pending_samples(
    session: Session, polymarket: PolymarketClient, state: _State
) -> list[str]:
    """Same idea as `_markets_for_open_bets`, for the forward test: a resolved market has
    left the active slate, so its result has to be fetched by id before samples can be
    graded. Capped by `forward.MAX_SETTLE_PER_SCAN` so a long backlog costs a bounded
    number of requests per scan and catches up over several runs."""
    wanted = forward.pending_market_ids(session, state.now)
    fetched: list[str] = []
    for market_id in wanted:
        fetched.append(market_id)
        if market_id in state.markets_by_id:
            continue
        row = session.get(Market, market_id)
        game = session.get(Game, row.game_id) if row is not None and row.game_id else None
        league = game.league if game is not None else None
        try:
            market = polymarket.market(market_id, league=league)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - one market never aborts the scan
            state.error("forward", f"market {market_id}", exc)
            continue
        if market is None:
            continue
        state.markets_by_id[market_id] = market
        upsert_market(session, market, game, state.now)
    return fetched


# --------------------------------------------------------------------------- entry points


def run_scan(
    session: Session,
    *,
    polymarket: PolymarketClient,
    oddsapi: OddsApiClient | None,
    espn: EspnClient | None,
    prefs: Prefs | PrefsLike,
    kind: str,
    leagues: list[str],
    now: datetime,
) -> ScanResult:
    """Fetch, match, compute (core), persist Scan/Game/Market/PmQuote/BookQuote/Opportunity,
    then `bets.settle_open_bets` and `bets.capture_closing`. See the module docstring."""
    kind = (kind or "").strip().lower()
    if kind not in SCAN_KINDS:
        raise ValueError(f"unknown scan kind {kind!r}; expected one of {sorted(SCAN_KINDS)}")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    wanted = [lg for lg in leagues if lg in LEAGUES]
    started = time.monotonic()

    scan = Scan(started_at=now, kind=kind, leagues=list(wanted), ok=False, errors=[], notes={})
    session.add(scan)
    session.flush()
    state = _State(scan=scan, kind=kind, prefs=prefs, now=now)
    for client in (oddsapi, espn):
        unresolved = getattr(client, "unresolved_teams", None)
        if isinstance(unresolved, list):
            unresolved.clear()  # this scan's findings only

    try:
        for league in wanted:
            try:
                _scan_league(
                    session, league, polymarket=polymarket, oddsapi=oddsapi, espn=espn, state=state
                )
            except Exception as exc:  # noqa: BLE001 - one league never aborts the others
                log.exception("scan of %s failed", league)
                state.error(league, "scan", exc)
        session.flush()
        # The owner's own Polymarket fills become ledger rows before settlement runs, so a
        # bet placed and resolved between two scans is imported, settled and given its
        # closing line by this one scan.
        wallet = str(_pref(prefs, "pm_wallet") or "").strip()
        wallet_note: dict[str, Any] | None = None
        if wallet:
            try:
                wallet_note = wallet_import.import_wallet_trades(
                    session, polymarket, wallet=wallet, now=now, prefs=prefs
                ).as_note()
            except Exception as exc:  # noqa: BLE001 - the ledger import must not fail a scan
                # No rollback: the import commits only once, at its end, so a failure
                # before that leaves nothing of its own pending, and a rollback here would
                # discard this scan's own flushed rows.
                log.exception("wallet import failed")
                state.error("wallet", "import", exc)
        _markets_for_open_bets(session, polymarket, state)
        # Closing first: a bet whose market resolved between two scans is settled by this
        # same scan and must still get its closing line (capture_closing reads pre-kickoff
        # snapshots only, so the order never changes the values it records).
        n_closing = bets_service.capture_closing(session, scan.id, now)
        n_settled = bets_service.settle_open_bets(session, state.markets_by_id, now)
        # Forward test: grade whatever has resolved since the last scan.
        try:
            sampled_ids = _markets_for_pending_samples(session, polymarket, state)
            forward.settle(session, state.markets_by_id, now)
            forward.capture_closing(session, sampled_ids)
        except Exception as exc:  # noqa: BLE001 - observation must not fail a scan
            log.exception("forward-test settlement failed")
            state.error("forward", "settle", exc)
    except Exception as exc:
        session.rollback()
        scan = session.merge(scan)
        scan.ok = False
        scan.errors = [*state.errors, f"scan: {type(exc).__name__}: {exc}"]
        scan.finished_at = now + timedelta(seconds=time.monotonic() - started)
        session.add(scan)
        session.commit()
        raise

    scan.n_markets = state.n_markets
    scan.n_matched = state.n_matched
    scan.n_opps = state.n_opps
    scan.credits_used = state.credits_used
    scan.credits_remaining = state.credits_remaining
    scan.errors = list(state.errors)
    unresolved_teams: list[dict[str, str]] = []
    for client in (oddsapi, espn):
        for entry in getattr(client, "unresolved_teams", None) or []:
            if entry not in unresolved_teams:
                unresolved_teams.append(dict(entry))
    notes: dict[str, Any] = {
        "unmatched": state.unmatched,
        "unparseable": state.unparseable,
        "book_source": state.book_source,
        "settled": n_settled,
        "closing_captured": n_closing,
        # Parser conventions in force for this scan, so Diagnostics can show what the
        # numbers assume (docs/RESEARCH.md "Unverified" 6).
        "conventions": {
            "home_away": HOME_AWAY_CONVENTION,
            "match_window_hours": float(_pref(prefs, "match_window_hours")),
            "stale_book_minutes": int(_pref(prefs, "stale_book_minutes") or 0),
        },
        "unresolved_book_teams": unresolved_teams,
    }
    if state.duplicates_dropped:
        notes["duplicates_dropped"] = list(state.duplicates_dropped)
    if state.fee_overrides:
        # A rejected override (rate None) means the market quoted a fee outside the
        # plausible band and the preference was used instead — worth seeing, because it
        # changes every edge on that market.
        notes["conventions"]["taker_fee_overrides"] = list(state.fee_overrides)
    if state.events_filters_dropped:
        notes["conventions"]["events_filters_dropped"] = True
    if state.quota_used is not None or state.credits_remaining is not None:
        notes["quota"] = {"used": state.quota_used, "remaining": state.credits_remaining}
    if wallet_note is not None:
        notes["wallet_import"] = wallet_note
    scan.notes = notes
    # A scan that saw nothing at all and only errors is a failure; partial results are ok.
    scan.ok = not (state.errors and state.n_markets == 0)
    scan.finished_at = now + timedelta(seconds=time.monotonic() - started)
    session.commit()
    session.refresh(scan)
    return ScanResult(
        scan_id=scan.id,
        kind=kind,
        leagues=list(wanted),
        n_markets=state.n_markets,
        n_matched=state.n_matched,
        n_opps=state.n_opps,
        credits_used=state.credits_used,
        credits_remaining=state.credits_remaining,
        errors=list(state.errors),
        unmatched=list(state.unmatched),
        unparseable=list(state.unparseable),
    )


def run_scan_default(
    session: Session,
    kind: str,
    leagues: list[str] | None = None,
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
) -> ScanResult:
    """Build clients from Settings (fixtures in demo mode, HTTP otherwise) and call `run_scan`.

    In demo mode `now` defaults to the fixed demo clock (docs/FIXTURES.md) so the synthetic
    slate is always in the future and its book snapshot never goes stale.

    One scan at a time: `SCAN_LOCK` is taken without blocking and `ScanBusy` is raised when
    another scan already holds it, so a double tap on Refresh (or a scheduled job landing on
    a manual one) is refused instead of queued.
    """
    if not SCAN_LOCK.acquire(blocking=False):
        raise ScanBusy("a scan is already running")
    try:
        settings = settings or get_settings()
        prefs = get_prefs(session)
        wanted = list(leagues) if leagues else list(prefs.leagues_enabled or [])
        transport: Any
        if settings.demo_mode:
            from app.services.demo import DEMO_NOW, build_demo_transport

            transport = build_demo_transport(settings)
            api_key = settings.odds_api_key or DEMO_API_KEY
            now = now or DEMO_NOW
        else:
            transport = HttpTransport()
            api_key = settings.odds_api_key
            now = now or _utcnow()
        polymarket = PolymarketClient(transport)
        oddsapi = OddsApiClient(transport, api_key) if api_key else None
        espn = EspnClient(transport)
        try:
            return run_scan(
                session,
                polymarket=polymarket,
                oddsapi=oddsapi,
                espn=espn,
                prefs=prefs,
                kind=kind,
                leagues=wanted,
                now=now,
            )
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
    finally:
        SCAN_LOCK.release()


def estimate_books_cost(prefs: Prefs | PrefsLike) -> int:
    """Credits a Books refresh would cost: 3 markets x ceil(bookmakers / 10) per enabled league."""
    bookmakers = [b for b in (_pref(prefs, "bookmakers") or []) if b]
    leagues = [lg for lg in (_pref(prefs, "leagues_enabled") or []) if lg in LEAGUES]
    if not bookmakers or not leagues:
        return 0
    return OddsApiClient.estimate_cost(len(BOOK_MARKETS), len(bookmakers)) * len(leagues)


def quota_status(session: Session) -> dict:
    """Latest known Odds API quota.

    Keys: "remaining" (int|None), "used" (int|None: the API's monthly used counter when
    it was recorded, else that scan's own credit spend), "as_of" (datetime|None).
    """
    scan = session.scalars(
        select(Scan)
        .where(Scan.credits_remaining.is_not(None))
        .order_by(Scan.started_at.desc(), Scan.id.desc())
        .limit(1)
    ).first()
    if scan is None:
        return {"remaining": None, "used": None, "as_of": None}
    quota = scan.notes.get("quota") if isinstance(scan.notes, dict) else None
    used = quota.get("used") if isinstance(quota, dict) else None
    return {
        "remaining": scan.credits_remaining,
        "used": used if used is not None else scan.credits_used,
        "as_of": scan.started_at,
    }


__all__ = [
    "GAME_STARTED",
    "HOME_AWAY_CONVENTION",
    "MARKET_CLOSED",
    "MARKET_PAUSED",
    "NO_BOOK_AT_LINE",
    "SAME_GAME_TOLERANCE",
    "SCAN_KINDS",
    "SCAN_LOCK",
    "ScanBusy",
    "estimate_books_cost",
    "quota_status",
    "run_scan",
    "run_scan_default",
    "upsert_game",
    "upsert_market",
]
