# SPEC — Polymarket Edge Finder v1

## Users and access

One owner. `APP_PASSWORD` gates every page except `/healthz`, `/manifest.webmanifest`,
`/sw.js`, and `/static/*`. Login sets a signed cookie for 30 days.

## Leagues and markets

Leagues: `nfl`, `nba`, `mlb` (each can be toggled off in Settings).
Market types: `moneyline`, `spread`, `total`. Everything else on Polymarket is ignored.

## Flows

### Scan (the core loop)

1. Polymarket: fetch active, unresolved events per enabled league; flatten to
   markets of the three types; parse outcomes, token ids, line, game start.
2. Polymarket: fetch order books in batches for every outcome token.
3. Books: if an Odds API key is present and credits allow, fetch
   `h2h,spreads,totals` for the enabled leagues using the configured
   bookmaker list. Record credits used/remaining. Else, if ESPN fallback is
   enabled, fetch the ESPN scoreboard and treat it as a single low-weight book.
4. Match each Polymarket market to a book game by (league, resolved home
   team, resolved away team, start time within 36 hours). Unmatched markets
   are listed on Diagnostics with a reason.
5. For each outcome: fair probability = weighted consensus of de-vigged book
   probabilities at the same line. Effective cost = best ask + taker fee.
   Compute edge, EV, Kelly, suggested stake, fill price at that stake, and
   the maker limit price for the minimum edge.
6. Persist a Scan row, price snapshots, and Opportunity rows. The home page
   reads the latest scan; nothing is computed at request time except display.
7. Closing and settle: for bets whose game has started and lack closing data,
   store the closing fair prob and closing Polymarket price from the **last
   snapshot taken before the game started** (never in-play data) and compute
   CLV; then, for every open Bet whose market is now `closed` on Gamma with a
   resolved outcome, mark won/lost and compute P&L. Markets that are closed,
   paused or already under way are never priced in step 5; they are listed on
   Diagnostics with the reason.

Two buttons drive it: **Refresh Polymarket** (steps 1, 2, 4–7 using the last
book snapshot) and **Refresh Books** (step 3 then 4–7). Each shows how many
credits it will cost before it runs and how many remain after.

### Log a bet

From an opportunity row: stake (prefilled with suggested, or with the fillable
amount when the ask ladder is thin), price (prefilled with the ask, editable for
a limit order), mode taker/maker (a maker price at or above the current ask is
refused: it would cross). Creates a Bet with fair prob, edge, fee, and shares at
log time. Manual settle/void controls exist.

### Bet ledger

Open bets with live price and unrealized edge; settled bets with P&L; summary
of total P&L, ROI, average CLV, count of positive-CLV bets.

## Screens

| Route | Purpose |
|---|---|
| `/` | Edges list, league filter chips, refresh buttons, quota badge |
| `/games/{game_id}` | All markets for a game, per-book table, depth, limit price |
| `/bets` | Ledger and summary; `/bets/new` htmx form; `/bets/{id}/settle` |
| `/settings` | Prefs form (see below) and key/quota status |
| `/diagnostics` | Last scans, errors, unmatched, unparseable, raw samples |
| `/login`, `/logout` | Password gate |
| `/healthz` | 200 JSON |

All pages are usable at 390 px width: single column, 44 px tap targets, no
horizontal scroll, sticky bottom nav (Edges, Bets, Settings, Diag).

## Preferences (DB, editable in app)

bankroll (USD), kelly_fraction (0.25), max_stake_pct (2.0), min_edge (0.02),
taker_fee_rate (0.05), devig_method (power), bookmakers (list), book_weights
(json), leagues enabled, espn_fallback_enabled (true), match_window_hours (36),
min_liquidity_usd (100), stale_book_minutes (720).

Environment (`.env`): `APP_PASSWORD`, `SECRET_KEY`, `DATABASE_URL`,
`ODDS_API_KEY` (optional), `DEMO_MODE`, `APP_ENV`, `SCHEDULER_POLY_MINUTES`
(0 = off), `SCHEDULER_BOOKS_HOURS` (0 = off), `LOG_LEVEL`.

## Data model (SQLAlchemy)

- `Game`: id, league, home_key, away_key, home_name, away_name, start_time,
  pm_event_slug, pm_event_id, book_game_id, espn_event_id, status.
- `Market`: id (Gamma market id), game_id, market_type, line, line_team_key,
  question, slug, condition_id, outcome_a_name/key, outcome_a_token,
  outcome_b_name/key, outcome_b_token, tick_size, min_order_size,
  accepting_orders, closed, resolved_outcome (a/b/None), liquidity, volume,
  last_seen_at.
- `Scan`: id, started_at, finished_at, kind (poly|books|both), leagues,
  ok, credits_used, credits_remaining, n_markets, n_matched, n_opps, errors
  (json), notes (json: unmatched, unparseable).
- `PmQuote`: id, scan_id, market_id, token, best_bid, best_ask, mid,
  ask_depth_json, bid_depth_json, fetched_at.
- `BookQuote`: id, scan_id, game_id, bookmaker, market_key, outcome_name,
  price_american, point, last_update, fetched_at.
- `Opportunity`: id, scan_id, market_id, token, outcome_key, outcome_name,
  ask, effective_price, fair_prob, fair_method, n_books, books_used (json),
  edge, ev_per_dollar, kelly, suggested_stake, fill_price, fill_complete,
  fill_usd, limit_price (resting: one tick below the ask at most), computed_at.
- `Bet`: id, market_id, token, outcome_key, outcome_name, mode (taker|maker),
  price, shares, stake_usd, fee_usd, fair_at_bet, edge_at_bet, placed_at,
  status (open|won|lost|void), settled_at, pnl_usd, closing_fair,
  closing_pm_price, clv, notes.
- `Prefs`: single row, fields above.

## Acceptance criteria

1. `make test` passes; `make lint` clean.
2. `DEMO_MODE=true make run` serves every screen with fixture data; a scan in
   demo mode produces ≥ 3 opportunities across the three leagues.
3. Math tests include the hand-checked vectors in `docs/ARCHITECTURE.md`.
4. Matching tests cover every team in all three leagues with at least the
   full name, nickname, and Odds API name.
5. Playwright screenshot at 390×844 of `/`, `/games/{id}`, `/bets`,
   `/settings`, `/diagnostics` shows no horizontal overflow.
6. `docker build` succeeds; `fly.toml` mounts `/data`; README "On your
   phone in 10 minutes" is complete.
