"""SQLAlchemy models — one class per table in SPEC.md "Data model".

All timestamps are UTC-aware. SQLite drops tzinfo on storage, so `UtcDateTime`
normalizes to UTC on write and re-attaches UTC on read; on PostgreSQL it is a
plain `TIMESTAMP WITH TIME ZONE`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# --------------------------------------------------------------------------- defaults

DEFAULT_BOOKMAKERS: list[str] = [
    "pinnacle",
    "betonlineag",
    "lowvig",
    "circasports",
    "draftkings",
    "fanduel",
]
DEFAULT_BOOK_WEIGHTS: dict[str, float] = {
    "pinnacle": 3.0,
    "circasports": 2.0,
    "betonlineag": 1.5,
    "lowvig": 1.5,
    "draftkings": 1.0,
    "fanduel": 1.0,
    "espn": 0.5,
}
DEFAULT_LEAGUES: list[str] = ["nfl", "nba", "mlb"]

# Polymarket ships two exchanges, and they do not charge the same. polymarket.com blocks
# US residents from trading and shows an interstitial pointing at polymarket.us, which is
# the US-regulated product; its taker fee coefficient is 0.0695, verified across all 814
# markets of one NFL game on 2026-09-21, against 0.05 on .com.
#
# The fee is not cosmetic. `docs/DECISIONS.md` records that a fee set too low manufactures
# edges that are not there, and 0.05 on a .us account is exactly that: at a 50c price it
# understates the cost by half a cent a share, which is most of a 2% edge. So the venue
# owns the fee default rather than leaving one number to be remembered.
VENUES: tuple[str, ...] = ("polymarket_us", "polymarket_com")
VENUE_TAKER_FEE: dict[str, float] = {
    "polymarket_us": 0.0695,
    "polymarket_com": 0.05,
}
VENUE_LABELS: dict[str, str] = {
    "polymarket_us": "Polymarket US (regulated, US residents)",
    "polymarket_com": "Polymarket (rest of world)",
}
# Default to .com because that is the exchange the market-data client actually reads
# today. A default of .us would price against .com data while claiming .us fees, which is
# a worse lie than the one it fixes. Flip this when the .us client lands; a US owner picks
# .us in Settings meanwhile, and the fee follows the venue automatically.
DEFAULT_VENUE = "polymarket_com"

DEFAULT_PREFS: dict[str, Any] = {
    "bankroll": 1000.0,
    "kelly_fraction": 0.25,
    "max_stake_pct": 2.0,
    "min_edge": 0.02,
    "venue": DEFAULT_VENUE,
    "taker_fee_rate": VENUE_TAKER_FEE[DEFAULT_VENUE],
    "devig_method": "power",
    "bookmakers": list(DEFAULT_BOOKMAKERS),
    "book_weights": dict(DEFAULT_BOOK_WEIGHTS),
    "leagues_enabled": list(DEFAULT_LEAGUES),
    "espn_fallback_enabled": True,
    "use_market_fee": True,
    "match_window_hours": 36.0,
    "min_liquidity_usd": 100.0,
    "stale_book_minutes": 720,
    # Polymarket proxy wallet (0x + 40 hex) whose fills are imported into the ledger; "" = off.
    "pm_wallet": "",
    # The Odds API key. Env `ODDS_API_KEY` still works and wins; this is the pasteable one.
    "odds_api_key": "",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- types


class UtcDateTime(TypeDecorator[datetime]):
    """tz-aware DateTime that survives SQLite round trips as UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# --------------------------------------------------------------------------- tables


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    home_key: Mapped[str | None] = mapped_column(String(8))
    away_key: Mapped[str | None] = mapped_column(String(8))
    home_name: Mapped[str | None] = mapped_column(String(80))
    away_name: Mapped[str | None] = mapped_column(String(80))
    start_time: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    pm_event_slug: Mapped[str | None] = mapped_column(String(160), index=True)
    pm_event_id: Mapped[str | None] = mapped_column(String(40), index=True)
    book_game_id: Mapped[str | None] = mapped_column(String(80), index=True)
    espn_event_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")

    markets: Mapped[list[Market]] = relationship(back_populates="game")

    def __repr__(self) -> str:
        return (
            f"<Game id={self.id} {self.league} {self.away_key}@{self.home_key} "
            f"start={self.start_time} status={self.status}>"
        )


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # Gamma market id
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), index=True)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    line: Mapped[float | None] = mapped_column(Float)
    line_team_key: Mapped[str | None] = mapped_column(String(8))
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    slug: Mapped[str | None] = mapped_column(String(200))
    condition_id: Mapped[str | None] = mapped_column(String(80))
    outcome_a_name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    outcome_a_key: Mapped[str | None] = mapped_column(String(8))
    outcome_a_token: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    outcome_b_name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    outcome_b_key: Mapped[str | None] = mapped_column(String(8))
    outcome_b_token: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    tick_size: Mapped[float | None] = mapped_column(Float)
    min_order_size: Mapped[float | None] = mapped_column(Float)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved_outcome: Mapped[str | None] = mapped_column(String(1))  # "a" | "b" | None
    liquidity: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    game: Mapped[Game | None] = relationship(back_populates="markets")
    opportunities: Mapped[list[Opportunity]] = relationship(back_populates="market")
    bets: Mapped[list[Bet]] = relationship(back_populates="market")

    def __repr__(self) -> str:
        return (
            f"<Market id={self.id} {self.market_type} line={self.line} "
            f"{self.outcome_a_name}/{self.outcome_b_name} closed={self.closed}>"
        )


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="poly")  # poly|books|both
    leagues: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    credits_used: Mapped[int | None] = mapped_column(Integer)
    credits_remaining: Mapped[int | None] = mapped_column(Integer)
    n_markets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_opps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    opportunities: Mapped[list[Opportunity]] = relationship(back_populates="scan")

    def __repr__(self) -> str:
        return (
            f"<Scan id={self.id} kind={self.kind} ok={self.ok} markets={self.n_markets} "
            f"matched={self.n_matched} opps={self.n_opps}>"
        )


class PmQuote(Base):
    __tablename__ = "pm_quotes"
    __table_args__ = (Index("ix_pm_quotes_scan_market", "scan_id", "market_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"), nullable=False)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), nullable=False)
    token: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    mid: Mapped[float | None] = mapped_column(Float)
    ask_depth_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    bid_depth_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    market: Mapped[Market] = relationship()

    def __repr__(self) -> str:
        return (
            f"<PmQuote scan={self.scan_id} market={self.market_id} token=…{self.token[-6:]} "
            f"bid={self.best_bid} ask={self.best_ask}>"
        )


class BookQuote(Base):
    __tablename__ = "book_quotes"
    __table_args__ = (Index("ix_book_quotes_scan_game", "scan_id", "game_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"), nullable=False)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), nullable=False)
    bookmaker: Mapped[str] = mapped_column(String(40), nullable=False)
    market_key: Mapped[str] = mapped_column(String(16), nullable=False)  # h2h|spreads|totals
    outcome_name: Mapped[str] = mapped_column(String(80), nullable=False)
    price_american: Mapped[int] = mapped_column(Integer, nullable=False)
    point: Mapped[float | None] = mapped_column(Float)
    last_update: Mapped[datetime | None] = mapped_column(UtcDateTime)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    game: Mapped[Game] = relationship()

    def __repr__(self) -> str:
        return (
            f"<BookQuote scan={self.scan_id} game={self.game_id} {self.bookmaker} "
            f"{self.market_key} {self.outcome_name} {self.price_american:+d} pt={self.point}>"
        )


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"), nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome_key: Mapped[str | None] = mapped_column(String(8))
    outcome_name: Mapped[str] = mapped_column(String(80), nullable=False)
    ask: Mapped[float] = mapped_column(Float, nullable=False)
    effective_price: Mapped[float] = mapped_column(Float, nullable=False)
    fair_prob: Mapped[float] = mapped_column(Float, nullable=False)
    fair_method: Mapped[str] = mapped_column(String(16), nullable=False, default="power")
    n_books: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    books_used: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    edge: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    ev_per_dollar: Mapped[float] = mapped_column(Float, nullable=False)
    kelly: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_stake: Mapped[float] = mapped_column(Float, nullable=False)
    fill_price: Mapped[float | None] = mapped_column(Float)
    # False when the stored ask ladder could not absorb suggested_stake; fill_usd is then
    # the fee-inclusive dollars it could take at fill_price.
    fill_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fill_usd: Mapped[float | None] = mapped_column(Float)
    # Why suggested_stake is not the raw Kelly number (capped by the depth that still clears
    # min_edge, or 0 because it would be under the market's minimum order size). NULL when
    # there is nothing to explain. Backfilled on existing databases by add_missing_columns.
    stake_note: Mapped[str | None] = mapped_column(String(80))
    limit_price: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    market: Mapped[Market] = relationship(back_populates="opportunities")
    scan: Mapped[Scan] = relationship(back_populates="opportunities")

    def __repr__(self) -> str:
        return (
            f"<Opportunity id={self.id} market={self.market_id} {self.outcome_name} "
            f"ask={self.ask} fair={self.fair_prob:.3f} edge={self.edge:+.3f} "
            f"stake={self.suggested_stake}>"
        )


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome_key: Mapped[str | None] = mapped_column(String(8))
    outcome_name: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="taker")  # taker|maker
    price: Mapped[float] = mapped_column(Float, nullable=False)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    stake_usd: Mapped[float] = mapped_column(Float, nullable=False)
    fee_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fair_at_bet: Mapped[float | None] = mapped_column(Float)
    edge_at_bet: Mapped[float | None] = mapped_column(Float)
    placed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    status: Mapped[str] = mapped_column(
        String(8), nullable=False, default="open", index=True
    )  # open|won|lost|void
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    pnl_usd: Mapped[float | None] = mapped_column(Float)
    closing_fair: Mapped[float | None] = mapped_column(Float)
    closing_pm_price: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "manual" (logged from an edge card) or "wallet" (imported from the owner's Polymarket
    # fills). An imported bet carries the "<transactionHash>:<asset>" it came from so a
    # re-import is a no-op.
    source: Mapped[str] = mapped_column(String(8), nullable=False, default="manual")
    import_key: Mapped[str | None] = mapped_column(String(160), index=True)

    market: Mapped[Market] = relationship(back_populates="bets")

    def __repr__(self) -> str:
        return (
            f"<Bet id={self.id} market={self.market_id} {self.outcome_name} {self.mode} "
            f"price={self.price} stake={self.stake_usd} status={self.status} pnl={self.pnl_usd}>"
        )


class ForwardSample(Base):
    """One priced outcome at one moment, recorded **whatever its edge**, then graded.

    This is the forward test. `Opportunity` only ever stores outcomes that already clear
    `prefs.min_edge`, so a database full of them can answer "did my 2% bets win?" and
    nothing else — and with ESPN as the only book almost nothing clears 2%, so it answers
    nothing at all. A sample is written for every outcome the scan could price, so the
    question "what would a 1% / 2% / 5% threshold have returned?" can be asked later,
    against the same rows, without having committed to a threshold up front.

    `won` is filled in from Polymarket's own resolution once the market closes; until then
    it is NULL and the row is "pending". `pnl_per_dollar` is the return on one dollar
    staked at `effective_price` (fee included): (1 - price) / price on a win, -1 on a loss.
    """

    __tablename__ = "forward_samples"
    __table_args__ = (
        Index("ix_forward_samples_scan_market", "scan_id", "market_id"),
        # The report scans pending rows by kickoff and grades by edge; both are hot.
        Index("ix_forward_samples_pending", "won", "game_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"), nullable=False)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    outcome_index: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome_name: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome_key: Mapped[str | None] = mapped_column(String(8))

    league: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    line: Mapped[float | None] = mapped_column(Float)

    # What Polymarket asked, and what a taker really pays once the fee is added.
    ask: Mapped[float] = mapped_column(Float, nullable=False)
    effective_price: Mapped[float] = mapped_column(Float, nullable=False)
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False)
    # Dollars of ask depth at the best level: a "3% edge" on $12 of depth is not a bet.
    top_ask_usd: Mapped[float | None] = mapped_column(Float)
    liquidity: Mapped[float | None] = mapped_column(Float)

    # What the books said, de-vigged.
    fair_prob: Mapped[float] = mapped_column(Float, nullable=False)
    fair_method: Mapped[str] = mapped_column(String(16), nullable=False, default="power")
    n_books: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    books_used: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    edge: Mapped[float] = mapped_column(Float, nullable=False, index=True)

    game_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Hours between the sample and kickoff: lets the report ask whether the edge is real
    # early and gone late, or the other way round.
    hours_to_start: Mapped[float | None] = mapped_column(Float)
    sampled_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    # Graded from Polymarket's resolution. NULL = still pending.
    won: Mapped[bool | None] = mapped_column(Boolean)
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    pnl_per_dollar: Mapped[float | None] = mapped_column(Float)
    # Polymarket's own last pre-kickoff price for this token, for closing-line value.
    closing_pm_price: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[float | None] = mapped_column(Float)

    market: Mapped[Market] = relationship()

    def __repr__(self) -> str:
        state = "pending" if self.won is None else ("won" if self.won else "lost")
        return (
            f"<ForwardSample {self.league}/{self.market_type} {self.outcome_name} "
            f"ask={self.ask} fair={self.fair_prob:.3f} edge={self.edge:+.4f} {state}>"
        )


class HistoricalSample(Base):
    """One resolved outcome from Polymarket's past, with the price it last traded at before
    kickoff. The back test.

    Built from two endpoints that survive resolution, because the obvious ones do not:
    `GET /prices-history` returns nothing for a closed market (verified on markets up to
    $400M volume) and ESPN strips odds off finished games, so neither the app's own price
    source nor its book source has any memory. What does survive is Gamma's resolution
    (`outcomePrices` becomes ["1","0"] or ["0","1"]) and `data-api/trades`, which still
    lists every individual trade with a timestamp.

    There is no `fair_prob` here on purpose. Historical sportsbook lines are paid data, so
    this table cannot answer "was Polymarket mispriced against the books?". It answers the
    question underneath it — is Polymarket itself well calibrated, and does any price band
    win more often than it costs — which needs no book at all.
    """

    __tablename__ = "historical_samples"
    __table_args__ = (Index("ix_historical_league_start", "league", "game_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    condition_id: Mapped[str | None] = mapped_column(String(80))
    league: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    line: Mapped[float | None] = mapped_column(Float)
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")

    outcome_index: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome_name: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    game_start: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # Last trade STRICTLY before kickoff: Polymarket's own closing price for this outcome.
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_trade_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Hours between that last trade and kickoff. A "closing" price from 30 hours out is a
    # stale market, not a close, and the report can exclude it.
    close_age_hours: Mapped[float | None] = mapped_column(Float)
    n_trades_pre: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    won: Mapped[bool] = mapped_column(Boolean, nullable=False)
    harvested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return (
            f"<HistoricalSample {self.league}/{self.market_type} {self.outcome_name} "
            f"close={self.close_price} won={self.won}>"
        )


class Prefs(Base):
    """Single-row user preferences (see SPEC.md "Preferences")."""

    __tablename__ = "prefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    bankroll: Mapped[float] = mapped_column(Float, nullable=False, default=1000.0)
    kelly_fraction: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    max_stake_pct: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    min_edge: Mapped[float] = mapped_column(Float, nullable=False, default=0.02)
    venue: Mapped[str] = mapped_column(String(20), nullable=False, default=DEFAULT_VENUE)
    taker_fee_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=VENUE_TAKER_FEE[DEFAULT_VENUE]
    )
    devig_method: Mapped[str] = mapped_column(String(16), nullable=False, default="power")
    bookmakers: Mapped[list] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_BOOKMAKERS)
    )
    book_weights: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=lambda: dict(DEFAULT_BOOK_WEIGHTS)
    )
    leagues_enabled: Mapped[list] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_LEAGUES)
    )
    espn_fallback_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Whether Gamma's per-market `takerBaseFee` outranks `taker_fee_rate` above.
    # True is right on polymarket.com, where Gamma reports that venue's own fee and the
    # preference is only a fallback. It is wrong on polymarket.us, which charges a
    # different coefficient (0.0695 against .com's 0.10) and has no Gamma of its own: there
    # the .com figure silently overrides whatever the owner set, and every edge is costed
    # at a fee they do not pay. Off means the preference always wins.
    use_market_fee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Set here or in `.env` as ODDS_API_KEY. The env value wins when both are present, so a
    # deployment cannot be silently repointed by whoever can reach the Settings page.
    odds_api_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    match_window_hours: Mapped[float] = mapped_column(Float, nullable=False, default=36.0)
    min_liquidity_usd: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    stale_book_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=720)
    pm_wallet: Mapped[str] = mapped_column(String(42), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<Prefs bankroll={self.bankroll} kelly={self.kelly_fraction} "
            f"min_edge={self.min_edge} fee={self.taker_fee_rate} devig={self.devig_method}>"
        )


__all__ = [
    "DEFAULT_BOOKMAKERS",
    "DEFAULT_BOOK_WEIGHTS",
    "DEFAULT_LEAGUES",
    "DEFAULT_PREFS",
    "Bet",
    "BookQuote",
    "Game",
    "Market",
    "Opportunity",
    "PmQuote",
    "Prefs",
    "Scan",
    "UtcDateTime",
    "utcnow",
]
