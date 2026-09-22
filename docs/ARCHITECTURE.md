# ARCHITECTURE — module contract

This file is the contract that parallel implementers code against. Names,
signatures, and dataclass fields here are binding. If you must deviate, add a
paragraph to `docs/DECISIONS.md` and update this file in the same change.

```
app/
  main.py            create_app(), lifespan, static mount, routers, auth middleware
  settings.py        Settings (pydantic-settings) — env only, no prefs
  db.py              Base, engine/session helpers (SQLite, PG-compatible names); init_db backfills
                     missing columns (no Alembic in v1)
  logsetup.py        configure_logging(): basicConfig + quiet httpx/httpcore + apiKey redaction filter
  models.py          SQLAlchemy models from SPEC.md
  templating.py      Jinja2 env + filters (pct, usd, american, ago)
  cli.py             `python -m app.cli scan|settle|demo-seed`
  core/
    types.py         frozen dataclasses shared by everything (below)
    odds_math.py     american/decimal <-> prob, de-vig methods, consensus
    edge.py          fee, EV, Kelly, stake, fill price, limit price, build_opportunity
    matching.py      team aliases + match markets to book games + fair_for_outcome
    clv.py           closing line value
    parsing.py       Polymarket question/line parsing (used by clients.polymarket)
  clients/
    transport.py     HttpTransport / FixtureTransport
    polymarket.py    PolymarketClient (Gamma + CLOB)
    oddsapi.py       OddsApiClient
    espn.py          EspnClient
  services/
    prefs.py         get_prefs(session) -> Prefs (creates defaults), update_prefs
    scan.py          run_scan(...) -> ScanResult, run_scan_default, upsert_game/upsert_market
    bets.py          create_bet, settle_bet_manual, settle_open_bets, capture_closing, ledger_summary
    adapters.py      ORM rows -> BookGame / PmMarket (stored snapshot re-pricing, closing fair)
    demo.py          DEMO_NOW, build_demo_transport(settings), seed_demo(session) using fixtures,
                     clear_demo(session), demo_rows_present(session)
    scheduler.py     optional in-process periodic scan (off by default)
  routes/
    auth.py  edges.py  games.py  bets.py  settings.py  diagnostics.py  pwa.py
  templates/  base.html, login.html, edges.html, game.html, bets.html,
              settings.html, diagnostics.html, not_found.html,
              partials/: macros, scan_status, edges_list, scan_result, bet_form,
              bet_form_fields, bet_logged, sheet_message, ledger, ledger_summary,
              open_bets, settled_bets, ledger_response
  static/     app.css, app.js (tiny), manifest.webmanifest, sw.js, icons/
docker/       entrypoint.sh (chown the SQLite dir, drop to `app`, exec uvicorn)
fixtures/     gamma_events_nfl.json, gamma_events_nba.json, gamma_events_mlb.json,
              clob_books.json, oddsapi_nfl.json, oddsapi_nba.json, oddsapi_mlb.json,
              espn_scoreboard_{nfl,nba,mlb}.json, gamma_teams_{nfl,nba,mlb}.json
tests/        conftest.py + one file per module + test_scan.py, test_bets.py,
              test_scheduler.py, test_cli.py, test_e2e_demo.py,
              test_review_security.py, test_review_ui.py (review-round fixes)
```

## `app/core/types.py` (binding)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

League = Literal["nfl", "nba", "mlb"]
LEAGUES: tuple[League, ...] = ("nfl", "nba", "mlb")
MarketType = Literal["moneyline", "spread", "total"]
BookMarketKey = Literal["h2h", "spreads", "totals"]
MARKET_TYPE_TO_BOOK_KEY = {"moneyline": "h2h", "spread": "spreads", "total": "totals"}

@dataclass(frozen=True)
class PmOutcome:
    token_id: str
    name: str                  # raw outcome label from Polymarket ("Chiefs", "Over")
    team_key: str | None       # canonical team key (e.g. "KC") or None for Over/Under
    last_price: float | None   # from outcomePrices
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
    line: float | None         # spread: signed line for line_team; total: the total
    line_team_key: str | None  # spread only: team the signed line applies to
    outcomes: tuple[PmOutcome, PmOutcome]
    game_start: datetime | None    # tz-aware UTC
    home_team_key: str | None
    away_team_key: str | None
    accepting_orders: bool
    closed: bool
    resolved_outcome_index: int | None   # 0 or 1 once resolved, else None
    tick_size: float | None
    min_order_size: float | None
    liquidity: float | None
    volume: float | None
    taker_fee_rate: float | None         # per-market override if the API gives one

@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float

@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: tuple[BookLevel, ...]   # sorted best (highest) first
    asks: tuple[BookLevel, ...]   # sorted best (lowest) first
    tick_size: float | None
    fetched_at: datetime

    @property
    def best_bid(self) -> float | None: ...
    @property
    def best_ask(self) -> float | None: ...

@dataclass(frozen=True)
class BookOutcome:
    name: str                  # raw book label (team full name, "Over", "Under")
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
    bookmaker: str             # Odds API key, e.g. "pinnacle"; ESPN uses "espn"
    title: str
    markets: tuple[BookMarket, ...]

@dataclass(frozen=True)
class BookGame:
    game_id: str               # Odds API id or "espn:<id>"
    league: League
    commence_time: datetime    # tz-aware UTC
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
    method: str                # devig method name
    n_books: int
    books_used: tuple[str, ...]
    line: float | None
    per_book: tuple[tuple[str, float], ...]   # (bookmaker, devigged prob)

@dataclass(frozen=True)
class Opportunity:
    market: PmMarket
    outcome_index: int
    ask: float
    effective_price: float
    fair: FairProb
    edge: float                # fair - effective_price
    ev_per_dollar: float       # fair / effective_price - 1
    kelly: float               # full Kelly fraction of bankroll (>= 0)
    suggested_stake: float     # USD after kelly_fraction and cap
    fill_price: float | None   # avg effective price if suggested_stake walks the asks
    fill_complete: bool
    limit_price: float | None  # RESTING maker price (fee 0) that still clears min_edge:
                               # limit_price_for_edge capped one tick below the best ask
    book_game: BookGame | None
    computed_at: datetime
    fill_usd: float | None = None  # fee-inclusive USD the ask ladder absorbs when fill_complete
                                   # is False (additive, review round)

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
    spread_details: str | None   # e.g. "KC -3.5"
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
```

## `app/core/odds_math.py`

```python
def american_to_prob(odds: int) -> float
def prob_to_american(p: float) -> int
def decimal_to_prob(d: float) -> float
def multiplicative_devig(probs: Sequence[float]) -> list[float]
def additive_devig(probs: Sequence[float]) -> list[float]
def power_devig(probs: Sequence[float], tol: float = 1e-10) -> list[float]
def shin_devig(probs: Sequence[float], tol: float = 1e-10) -> list[float]
def devig(probs: Sequence[float], method: str = "power") -> list[float]   # raises ValueError on unknown
def consensus(samples: Sequence[tuple[str, float]], weights: Mapping[str, float],
              default_weight: float = 1.0, method: str = "power", line: float | None = None) -> FairProb | None
```
`consensus` returns None when `samples` is empty; weights missing from the
mapping use `default_weight`; a weight of 0 excludes the book.

## `app/core/edge.py`

```python
def taker_fee_per_share(price: float, fee_rate: float) -> float      # fee_rate * price * (1 - price)
def effective_price(price: float, fee_rate: float) -> float          # price + fee
def edge(fair: float, cost: float) -> float
def ev_per_dollar(fair: float, cost: float) -> float
def kelly_fraction(fair: float, cost: float) -> float                # max(0, (fair - cost) / (1 - cost))
def stake_for(bankroll: float, kelly: float, kelly_fraction: float, max_stake_pct: float,
              min_stake: float = 1.0) -> float                       # 0.0 when below min_stake
def walk_asks(asks: Sequence[BookLevel], usd: float, fee_rate: float) -> tuple[float, float, bool]
    # returns (avg_effective_price, shares, fully_filled)
def limit_price_for_edge(fair: float, min_edge: float, tick: float = 0.01) -> float | None
    # floor((fair - min_edge) / tick) * tick, None if <= 0
def resting_limit_price(fair: float, min_edge: float, best_ask: float | None,
                        tick: float = 0.01) -> float | None
    # min(limit_price_for_edge(...), best_ask - tick); None when nothing can rest (review round)
def build_opportunity(market: PmMarket, outcome_index: int, book: OrderBook | None,
                      fair: FairProb, prefs: PrefsLike, book_game: BookGame | None,
                      now: datetime) -> Opportunity | None
    # None when there is no ask, or edge < prefs.min_edge, or liquidity below prefs.min_liquidity_usd,
    # or the market is closed / not accepting orders / its game_start <= now (review round);
    # limit_price is the resting price; fill_usd is set when the ladder cannot absorb the stake
```
`PrefsLike` is a Protocol with: bankroll, kelly_fraction, max_stake_pct,
min_edge, taker_fee_rate, min_liquidity_usd (all floats).

## `app/core/matching.py`

```python
TEAM_ALIASES: dict[League, dict[str, str]]   # canonical key -> display name
def team_key(name: str, league: League) -> str | None
    # case/punctuation-insensitive; accepts full name, city, nickname, abbreviation,
    # Odds API names, Polymarket short names, ESPN displayName; None if unknown
def match_games(markets: Sequence[PmMarket], book_games: Sequence[BookGame],
                window_hours: float = 36.0) -> dict[str, BookGame]
    # market_id -> BookGame; same league, same {home,away} pair (order-insensitive),
    # |start difference| <= window; nearest start wins on ties
def fair_for_outcome(market: PmMarket, outcome_index: int, game: BookGame,
                     weights: Mapping[str, float], method: str = "power",
                     default_weight: float = 1.0) -> FairProb | None
    # moneyline: per book take h2h outcomes for both teams, devig, pick this outcome's team
    # spread: outcome team's point must equal market.line for line_team else -market.line
    # total: name Over/Under must match outcome name and point == market.line
    # books with no matching line are skipped; None if no books
```
Canonical keys are the standard abbreviations:
NFL `ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LAR MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB TEN WAS`;
NBA `ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS`;
MLB `ARI ATL BAL BOS CHC CWS CIN CLE COL DET HOU KC LAA LAD MIA MIL MIN NYM NYY ATH PHI PIT SD SF SEA STL TB TEX TOR WSH`
(Athletics have no city in 2026; accept "Oakland Athletics", "Sacramento Athletics", "A's").
Watch the collisions: NFL/NBA/MLB "LAC", "KC", "MIA", "WAS"/"WSH" are per league.

## `app/core/parsing.py`

```python
def parse_market_type(sports_market_type: str | None, question: str) -> MarketType | None
def parse_spread(question: str, line: float | None, outcomes: Sequence[str], league: League)
    -> tuple[float, str] | None      # (signed line, line_team_key)
def parse_total(question: str, line: float | None) -> float | None
def parse_json_list(value: str | list | None) -> list
```

## `app/clients/transport.py`

```python
class Transport(Protocol):
    def get_json(self, url: str, params: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> tuple[Any, Mapping[str, str]]
    def post_json(self, url: str, body: Any, headers=None) -> tuple[Any, Mapping[str, str]]
class HttpTransport(Transport)     # httpx, timeout 15s, 2 retries on 5xx/timeouts, user agent
class FixtureTransport(Transport)  # maps (method, url_prefix) -> path under fixtures/; supports
                                   # a callable for POST bodies; records calls in .calls
class TransportError(Exception)   # message, status, url
```

## `app/clients/polymarket.py`

```python
GAMMA = "https://gamma-api.polymarket.com"; CLOB = "https://clob.polymarket.com"
TeamResolver = Callable[[str, League], str | None]   # defaults to matching.team_key, imported lazily
class PolymarketClient:
    def __init__(self, transport: Transport, gamma_base=GAMMA, clob_base=CLOB,
                 team_resolver: TeamResolver | None = None) -> None
    def events(self, league: League, *, include_closed: bool = False) -> tuple[list[PmMarket], list[dict]]
        # paginates /events?tag_slug=..; second item = unparseable markets [{market_id, question, reason}]
    def market(self, market_id: str, *, league: League | None = None) -> PmMarket | None
        # GET /markets/{id}, for settlement; `league` (additive) is the caller's hint when the
        # payload has no league tag / slug prefix and events() never saw the market
    duplicates_dropped: list[str]   # "event:<id>" / "market:<id>" dropped by the last events() call
    def order_books(self, token_ids: Sequence[str]) -> dict[str, OrderBook]   # POST /books in chunks of 200
    def teams(self, league: League) -> list[dict]
```

## `app/clients/oddsapi.py`

```python
SPORT_KEYS = {"nfl": "americanfootball_nfl", "nba": "basketball_nba", "mlb": "baseball_mlb"}
class OddsApiClient:
    def __init__(self, transport: Transport, api_key: str, base="https://api.the-odds-api.com/v4",
                 team_resolver: TeamResolver | None = None) -> None
    def odds(self, league: League, bookmakers: Sequence[str], markets=("h2h","spreads","totals"))
        -> tuple[list[BookGame], QuotaInfo]
    @staticmethod
    def estimate_cost(n_markets: int, n_bookmakers: int) -> int    # n_markets * ceil(n_bookmakers/10)
```

## `app/clients/espn.py`

```python
class EspnClient:
    def __init__(self, transport: Transport, base="https://site.api.espn.com/apis/site/v2/sports",
                 team_resolver: TeamResolver | None = None) -> None
    def scoreboard(self, league: League, date: date | None = None) -> list[EspnGame]
    @staticmethod
    def to_book_games(games: Sequence[EspnGame]) -> list[BookGame]   # bookmaker "espn", h2h + spreads + totals when present
```

Clients resolve team keys through the injected `team_resolver` so client tests
can pass a dict-backed resolver and never depend on `matching.py`. Fixture
data is defined in `docs/FIXTURES.md`.

## Hand-checked vectors (must appear in tests)

```
american_to_prob: -110 -> 0.523810; +150 -> 0.400000; -200 -> 0.666667; +100 -> 0.5
(-110,-110): mult [0.5,0.5]; add [0.5,0.5]; power [0.5,0.5]; shin [0.5,0.5]
(-200,+170): raw [0.666667,0.370370]; mult [0.642857,0.357143]; add [0.648148,0.351852];
             power [0.650822,0.349178]; shin [0.648148,0.351852]
(-150,+130): mult [0.579832,0.420168]; add [0.582609,0.417391]; power [0.583983,0.416017]; shin [0.582609,0.417391]
(-3000,+1200): mult [0.926366,0.073634]; add [0.945409,0.054591]; power [0.959759,0.040241]; shin [0.945409,0.054591]
(+120,-140): mult [0.437956,0.562044]; add [0.435606,0.564394]; power [0.434435,0.565565]; shin [0.435606,0.564394]
effective_price(0.50,0.05)=0.5125  (0.60)->0.612  (0.30)->0.3105  (0.95)->0.952375  (0.05)->0.052375
kelly_fraction(0.55,0.5125)=0.076923; ev_per_dollar=0.073171; edge=0.0375
kelly_fraction(0.60,0.612)=0.0 (negative clamped)
stake_for(1000,0.076923,0.25,2.0)=19.23; stake_for(1000,0.2,0.25,2.0)=20.00 (capped); stake_for(1000,0.001,0.25,2.0)=0.0 (< $1)
walk_asks([(0.50,100),(0.51,200),(0.55,1000)], usd=100, fee=0.05) -> (0.517324, 193.302328, True)
walk_asks([(0.50,10)], usd=100, fee=0.05) -> (0.5125, 10.0, False)
limit_price_for_edge(0.55,0.02)=0.53; (0.5555,0.02)=0.53; (0.01,0.02)=None
resting_limit_price(0.55,0.02,best_ask=0.50)=0.49; (0.55,0.02,0.60)=0.53; (0.5555,0.02,0.50,tick=0.001)=0.499
build_opportunity(fair 0.688564, asks [(0.64, 5 shares)], defaults) -> suggested_stake 20.00,
  fill_complete False, fill_price 0.65152, fill_usd 3.26
consensus([("pinnacle",0.58),("betonlineag",0.56),("draftkings",0.60)], {pinnacle:3, betonlineag:1.5, draftkings:1}) = 0.578182
```
Tolerance 1e-5 unless stated.

## Services and routes

- `services/scan.py`: `run_scan(session, *, polymarket, oddsapi, espn, prefs, kind: str, leagues, now) -> ScanResult`.
  Pure orchestration; all math in core. Persists Scan, Game, Market, PmQuote, BookQuote, Opportunity; calls
  `bets.capture_closing` and then `bets.settle_open_bets` at the end (closing first, so a bet settled by
  this scan still gets its CLV — review round). Only pre-game, tradable markets are priced; the rest are
  listed in `Scan.notes.unmatched` with reason "game started" / "market closed" /
  "market not accepting orders".
- `services/bets.py`: `create_bet(session, opportunity_id, stake_usd, price, mode) -> Bet`,
  `settle_open_bets(session, markets_by_id) -> int`, `capture_closing(session, scan_id, now) -> int`,
  `ledger_summary(session) -> dict`.
- Routes render Jinja templates; htmx partials for refresh, log-bet form, settle. JSON only at `/healthz`
  and `/api/opportunities` (for debugging).

## Services API

Exact signatures of the service layer (the one contract edit made by the scaffold step).
Routes and the CLI call only these; all math stays in `app/core`.

```python
# app/services/prefs.py
def get_prefs(session: Session) -> Prefs                       # creates the singleton row with defaults
def update_prefs(session: Session, data: dict) -> Prefs        # validates types/ranges, raises ValueError
def validate_prefs(data: Mapping[str, Any]) -> dict            # pure; used by update_prefs

# app/services/scan.py
def run_scan(session: Session, *, polymarket: PolymarketClient, oddsapi: OddsApiClient | None,
             espn: EspnClient | None, prefs: Prefs | PrefsLike, kind: str, leagues: list[str],
             now: datetime) -> ScanResult
def run_scan_default(session: Session, kind: str, leagues: list[str] | None = None,
                     now: datetime | None = None) -> ScanResult   # builds clients from Settings
def estimate_books_cost(prefs: Prefs | PrefsLike) -> int          # credits a Books refresh costs
def quota_status(session: Session) -> dict                        # keys: remaining, used, as_of

# app/services/bets.py
def create_bet(session: Session, opportunity_id: int, stake_usd: float, price: float,
               mode: str, notes: str = "") -> Bet
def settle_bet_manual(session: Session, bet_id: int, result: str) -> Bet   # result: won|lost|void
def settle_open_bets(session: Session, markets_by_id: Mapping[str, PmMarket]) -> int
def capture_closing(session: Session, scan_id: int, now: datetime) -> int
    # closing = the last stored book snapshot / Polymarket ask with fetched_at <= game.start_time;
    # eligible: bets (open, won or lost) whose game has started and whose closing_fair is NULL
def latest_best_ask(session: Session, token: str) -> float | None   # newest stored PmQuote ask
    # create_bet(mode="maker") raises ValueError when price >= latest_best_ask (it would cross)
def ledger_summary(session: Session) -> dict
    # keys: n_open, n_settled, n_won, n_lost, total_staked, total_pnl, roi, avg_clv,
    #       n_clv_positive, n_clv_recorded

# app/services/demo.py
def seed_demo(session: Session, settings: Settings | None = None) -> None
    # fixture-backed scan + an open Chiefs bet + a settled (lost) BAL@TOR Orioles bet; idempotent on
    # the demo bets (Bet.notes == "demo"); never touches the network; refuses when real bets exist
def clear_demo(session: Session) -> dict[str, int]      # wipes the slate + demo bets; ValueError if real bets
def demo_rows_present(session: Session) -> bool
def build_demo_transport(settings: Settings) -> FixtureTransport   # every API route -> fixtures/
DEMO_NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)               # the demo clock

# app/services/adapters.py (integration step)
def pm_market_from_row(market: Market, game: Game | None) -> PmMarket
def book_game_from_rows(game: Game, rows: Sequence[BookQuote]) -> BookGame | None
def stored_book_game(session, game, *, not_before=None, up_to_scan_id=None, not_after=None)
    -> tuple[BookGame | None, int | None]   # not_after (additive): fetched_at <= cutoff, for closing lines

# app/services/scheduler.py
def build_scheduler(settings: Settings) -> Any | None   # None when both intervals are 0
def start_scheduler(settings: Settings) -> Any | None
def stop_scheduler(scheduler: Any | None) -> None

# app/core/clv.py
def clv(closing_fair: float, cost: float) -> float           # closing_fair - cost
def clv_decimal(closing_fair: float, cost: float) -> float   # (1/cost) / (1/closing_fair) - 1
```

Additive keyword arguments (integration step, all optional, existing calls unchanged):
`run_scan_default(..., settings: Settings | None = None)`,
`settle_open_bets(session, markets_by_id, now: datetime | None = None)`,
`seed_demo(session, settings: Settings | None = None)`. `app.settings.set_settings(settings)`
makes the Settings `create_app` was built with the process-wide active Settings that
`get_settings()` returns. `Scan.notes` is `{"unmatched": [...], "unparseable": [...],
"book_source": {league: "oddsapi"|"espn"|"stored:<ids>"|"none"}, "settled": n,
"closing_captured": n, "conventions": {"home_away": str, "match_window_hours": float,
"stale_book_minutes": int}, "unresolved_book_teams": [{"league", "name", "source"}, ...],
"duplicates_dropped": [...]?, "quota": {"used", "remaining"}?}`; every unmatched/unparseable
item carries a `reason`, and the scan service adds the reasons `"no book at line"` (matched,
no book quotes the Polymarket line), `"game started"`, `"market closed"` and `"market not
accepting orders"`. `PmQuote.ask_depth_json` / `bid_depth_json` are `[[price, size], ...]`
(top 10, best first); `PmQuote.fetched_at` is the scan clock. `Opportunity` rows carry
`fill_complete` (bool) and `fill_usd` (float | None). `app/routes/auth.py` exposes
`LoginLimiter` (per-address + global lockout); `create_app` stores one on
`app.state.login_limiter`. `app.db.add_missing_columns(engine)` (called by `init_db`) appends
model columns missing from existing tables.

Model notes (scaffold): `Game.id` is an autoincrement int (used in `/games/{game_id}`);
`Market.id` is the Gamma market id string. All datetime columns use `UtcDateTime`
(a `DateTime(timezone=True)` decorator that normalizes to UTC on write and re-attaches
UTC on read so SQLite round-trips stay tz-aware). `Prefs` default values are exported
as `app.models.DEFAULT_PREFS`. `PrefsLike` lives in `app/core/types.py`.

## Contract deltas from the review rounds

The signatures above are still binding; these are the additions the review rounds made.
Anything not listed here is unchanged.

`app/core/edge.py` gains, alongside the originals:

```python
edge_clearing_depth(asks: Sequence[BookLevel], fair: float, min_edge: float,
                    fee_rate: float) -> tuple[float, float, bool]   # (usd, shares, exhausted)

@dataclass(frozen=True)
class StakePlan:
    stake: float
    fill_price: float | None
    fill_complete: bool
    fill_usd: float | None
    note: str | None

plan_stake(asks: Sequence[BookLevel], fair: float, kelly: float, fee_rate: float,
           prefs: PrefsLike, min_order_size: float | None = None) -> StakePlan
stake_note(opportunity: Opportunity, book: OrderBook | None, prefs: PrefsLike) -> str | None
```

plus `MIN_STAKE` ($1) and `NOTE_MIN_ORDER` / `NOTE_EDGE_CAPPED` / `NOTE_NO_EDGE_DEPTH`.
`build_opportunity` delegates its sizing to `plan_stake`; `walk_asks` is unchanged.

`app/core/matching.py` gains `SERIES_WINDOW_HOURS = {"mlb": 6.0, "nba": 6.0}`,
`GAME_DAY_TZ` (US/Eastern) and the unmatched reason `DIFFERENT_GAME_DAY`
(`"different game day"`). `match_games(..., window_hours=36.0)` keeps its signature;
`window_hours` is the outer bound the per-league rule may only narrow.

`app/services/scan.py` gains `SCAN_LOCK: threading.Lock` and `ScanBusy(RuntimeError)`,
raised by `run_scan_default` when a scan is already in flight. `Scan.notes["conventions"]`
may additionally carry `"taker_fee_overrides": [{"league", "market_id", "raw", "rate"}]`
(`rate: None` = rejected, preference used) and `"events_filters_dropped": true`.

`app/services/bets.py`: `settle_bet_manual` accepts a fourth result `"push"`, `is_push`
and `PUSH_PAYOUT` are exported, `ledger_summary` gains `n_push`, and `create_bet` /
`settle_bet_manual` take an optional `now`.

`app/models.py`: `Opportunity.stake_note` (nullable `String(80)`).

`app/settings.py`: `app_password`, `secret_key` and `odds_api_key` are `SecretStr` — read
them through `app_password_value` / `secret_key_value` / `odds_api_key_value`, or the
module-level `secret_value()` which also tolerates a plain `str` (a `model_copy(update=...)`
bypasses validation). `database_url` stays `str`. New: `trusted_proxy_header`,
`config_error_message(exc)`, and `get_settings()` may raise `SystemExit` on a bad
environment rather than let pydantic print the environment in a traceback.

`app/clients/espn.py`: `ESPN_SIDE_PRICE` is gone — a spread or total is contributed only
when the payload prices both sides. `EspnOddsGame(EspnGame)` carries the four optional
per-side prices; `to_book_games` accepts either type.

`app/clients/polymarket.py`: `PolymarketError` carries `.status`; the client exposes
`taker_fee_overrides` (reset per `events()` call) and `events_filters_dropped` (sticky),
and exports `MAX_PAGES`, `MIN_TAKER_FEE_RATE`, `MAX_TAKER_FEE_RATE` and
`NO_ACCEPTING_ORDERS_FLAG`.

`app/routes/auth.py` gains `sanitize_ip()` and `trusted_proxy_header()`; `client_ip()`
reads no header unless one is configured. `app/main.py` gains `is_cross_site_write()` and
`apply_security_headers()`.
