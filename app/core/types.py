"""Frozen dataclasses shared by everything. BINDING — see docs/ARCHITECTURE.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

League = Literal["nfl", "nba", "mlb"]
LEAGUES: tuple[League, ...] = ("nfl", "nba", "mlb")
MarketType = Literal["moneyline", "spread", "total"]
BookMarketKey = Literal["h2h", "spreads", "totals"]
MARKET_TYPE_TO_BOOK_KEY = {"moneyline": "h2h", "spread": "spreads", "total": "totals"}


class PrefsLike(Protocol):
    """The subset of Prefs the math needs (structural; the ORM row satisfies it)."""

    bankroll: float
    kelly_fraction: float
    max_stake_pct: float
    min_edge: float
    taker_fee_rate: float
    min_liquidity_usd: float


@dataclass(frozen=True)
class PmOutcome:
    token_id: str
    name: str  # raw outcome label from Polymarket ("Chiefs", "Over")
    team_key: str | None  # canonical team key (e.g. "KC") or None for Over/Under
    last_price: float | None  # from outcomePrices
    best_bid: float | None
    best_ask: float | None


@dataclass(frozen=True)
class PmMarket:
    market_id: str
    condition_id: str
    slug: str
    question: str
    event_id: str
    event_slug: str
    event_title: str
    league: League
    market_type: MarketType
    line: float | None  # spread: signed line for line_team; total: the total
    line_team_key: str | None  # spread only: team the signed line applies to
    outcomes: tuple[PmOutcome, PmOutcome]
    game_start: datetime | None  # tz-aware UTC
    home_team_key: str | None
    away_team_key: str | None
    accepting_orders: bool
    closed: bool
    resolved_outcome_index: int | None  # 0 or 1 once resolved, else None
    tick_size: float | None
    min_order_size: float | None
    liquidity: float | None
    volume: float | None
    taker_fee_rate: float | None  # per-market override if the API gives one


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: tuple[BookLevel, ...]  # sorted best (highest) first
    asks: tuple[BookLevel, ...]  # sorted best (lowest) first
    tick_size: float | None
    fetched_at: datetime

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


@dataclass(frozen=True)
class BookOutcome:
    name: str  # raw book label (team full name, "Over", "Under")
    team_key: str | None
    price_american: int
    point: float | None


@dataclass(frozen=True)
class BookMarket:
    key: BookMarketKey
    outcomes: tuple[BookOutcome, ...]
    last_update: datetime | None


@dataclass(frozen=True)
class BookQuote:
    bookmaker: str  # Odds API key, e.g. "pinnacle"; ESPN uses "espn"
    title: str
    markets: tuple[BookMarket, ...]


@dataclass(frozen=True)
class BookGame:
    game_id: str  # Odds API id or "espn:<id>"
    league: League
    commence_time: datetime  # tz-aware UTC
    home_team_key: str
    away_team_key: str
    home_team_name: str
    away_team_name: str
    books: tuple[BookQuote, ...]


@dataclass(frozen=True)
class QuotaInfo:
    remaining: int | None
    used: int | None
    last_cost: int | None


@dataclass(frozen=True)
class FairProb:
    value: float
    method: str  # devig method name
    n_books: int
    books_used: tuple[str, ...]
    line: float | None
    per_book: tuple[tuple[str, float], ...]  # (bookmaker, devigged prob)


@dataclass(frozen=True)
class Opportunity:
    market: PmMarket
    outcome_index: int
    ask: float
    effective_price: float
    fair: FairProb
    edge: float  # fair - effective_price
    ev_per_dollar: float  # fair / effective_price - 1
    kelly: float  # full Kelly fraction of bankroll (>= 0)
    suggested_stake: float  # USD after kelly_fraction and cap
    fill_price: float | None  # avg effective price if suggested_stake walks the asks
    fill_complete: bool
    limit_price: float | None  # maker price that still clears min_edge (fee 0)
    book_game: BookGame | None
    computed_at: datetime


@dataclass(frozen=True)
class EspnGame:
    espn_id: str
    league: League
    start_time: datetime
    home_team_key: str
    away_team_key: str
    home_name: str
    away_name: str
    home_score: int | None
    away_score: int | None
    completed: bool
    odds_provider: str | None
    home_moneyline: int | None
    away_moneyline: int | None
    spread_details: str | None  # e.g. "KC -3.5"
    over_under: float | None


@dataclass
class ScanResult:
    scan_id: int
    kind: str
    leagues: list[str]
    n_markets: int
    n_matched: int
    n_opps: int
    credits_used: int | None
    credits_remaining: int | None
    errors: list[str] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    unparseable: list[dict] = field(default_factory=list)


__all__ = [
    "LEAGUES",
    "MARKET_TYPE_TO_BOOK_KEY",
    "BookGame",
    "BookLevel",
    "BookMarket",
    "BookMarketKey",
    "BookOutcome",
    "BookQuote",
    "EspnGame",
    "FairProb",
    "League",
    "MarketType",
    "Opportunity",
    "OrderBook",
    "PmMarket",
    "PmOutcome",
    "PrefsLike",
    "QuotaInfo",
    "ScanResult",
]
