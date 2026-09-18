# Build prompt — Polymarket Edge Finder (adjusted)

> This is the adjusted version of the original "Sports Betting AI Build Prompt".
> The original file was not readable from the build environment, so this
> prompt was rewritten from the owner's stated requirements: **personal tool,
> bets placed on Polymarket, NFL + NBA + MLB, must work on a phone.** The
> "What changed and why" section at the bottom lists the deliberate departures
> from a typical sportsbook-oriented "AI betting app" prompt.

## Goal

Build a single-user web app I can open on my phone that tells me, right now,
which Polymarket NFL/NBA/MLB markets are priced below the sharp-market fair
probability, how big the edge is after Polymarket's taker fee, and how much to
stake. Let me log the bet in one tap, settle it automatically when Polymarket
resolves, and show me whether I am beating the closing line over time.

## Non-goals

- Not a product. One user, one password, no signup, no multi-tenant anything.
- No automated trading. The app never signs or submits Polymarket orders.
- No "AI picks". No LLM in the math. The edge comes from arithmetic on public
  prices, not from a model's opinion.
- No player props, futures, or parlays in v1. Moneyline, spread, total only.
- No sportsbook accounts. The books are a *reference price*, not a venue.

## Data sources

1. **Polymarket Gamma API** (public, keyless): events and markets by league
   tag (`tag_slug=nfl|nba|mlb`), with `sportsMarketType`, `line`,
   `gameStartTime`, `clobTokenIds`, `outcomes`, `bestBid/bestAsk`.
2. **Polymarket CLOB API** (public read, keyless): order book depth per token
   so the stake can be priced against real asks, not the last trade.
3. **The Odds API** (free key, 500 credits/month): moneyline, spread, total
   from a configurable bookmaker list; default sharp set is Pinnacle,
   BetOnline, LowVig, Circa, plus DraftKings/FanDuel at low weight.
4. **ESPN scoreboard** (public, keyless, single book): fallback reference and
   schedule when no Odds API key is configured or credits are exhausted.

## Core math (deterministic, tested)

- American odds → implied probability → de-vig per book (power method by
  default; multiplicative, additive, Shin also implemented and selectable).
- Fair probability = weighted consensus of de-vigged sharp books that quote
  the **same line** as the Polymarket market (spreads/totals must match the
  point exactly; moneyline always matches).
- Polymarket effective cost when taking the ask at price `a` with sports
  taker fee rate `r` (0.05 as of July 2026): `a + r * a * (1 - a)`.
  Maker (resting limit order) cost is just the limit price; maker fee is 0.
- Edge = fair − effective cost. EV per dollar = fair / cost − 1.
- Kelly for a binary share at cost `c`: `f* = (p − c) / (1 − c)`. Stake =
  bankroll × f* × kelly_fraction, capped at max_stake_pct of bankroll.
- Fill price for the suggested stake is computed by walking the ask ladder.
- Also show the **limit price** at which the bet would still clear the minimum
  edge with zero maker fee, so I can rest an order instead of paying the fee.
- CLV: record fair prob and Polymarket price at bet time; at game start record
  closing fair prob and closing Polymarket price; CLV = closing fair − cost.

## Screens (phone-first, PWA)

1. **Edges** (home): list of opportunities ≥ min edge, sorted by edge, filter
   by league, each row: matchup, market, side, Polymarket ask, fair %, edge %,
   suggested stake, "Open on Polymarket" link, "Log bet" button. Refresh
   buttons: "Polymarket" (free) and "Books" (shows credits remaining).
2. **Game**: every Polymarket market for that game with per-book de-vigged
   probabilities, the consensus, order-book depth, and the limit-order price.
3. **Bets**: open and settled bets, P&L, average CLV, hit rate; manual settle
   and void controls.
4. **Settings**: bankroll, Kelly fraction, max stake %, min edge, fee rate,
   de-vig method, bookmaker weights, leagues on/off, API key status and quota.
5. **Diagnostics**: last scan log, unmatched markets, unparseable questions,
   raw sample payloads, so live API surprises can be debugged from the phone.

## Delivery

- Password-gated (single `APP_PASSWORD`), HTTPS via the host.
- Dockerfile + `fly.toml` with a 1 GB volume for SQLite; README has a
  ten-minute "get it on your phone" section including Add to Home Screen.
- Demo mode (`DEMO_MODE=true`) runs the whole UI on synthetic fixtures so the
  app is usable before any key is configured.
- CI: ruff + pytest on every push.

## Definition of done

- `make test` and `make lint` green.
- Demo mode renders every screen at a 390 px viewport with no horizontal
  scroll.
- Math module has hand-checked test vectors (documented in the test file).
- Team matching covers all 32 NFL, 30 NBA, 30 MLB teams including 2026 names.
- Unverified live-API assumptions are listed in `docs/RESEARCH.md`.

## What changed and why (vs. a typical sportsbook "AI betting app" prompt)

| Typical prompt | This prompt | Why |
|---|---|---|
| Predict winners with an AI model | Compare Polymarket to de-vigged sharp lines | A model that beats Pinnacle is a research project; arbitrage against a slower market is a spreadsheet. The second one is real. |
| Bet at sportsbooks | Bet on Polymarket only | Owner bets on Polymarket. Books become the reference price. |
| Ignore fees | Fee-aware EV and Kelly | Polymarket charges takers ~5% × p(1−p) per share on sports since July 2026. At 50% that is 1.25 cents per share, which erases most "edges". |
| Every sport | NFL, NBA, MLB | Owner's ask. NFL is the sharpest reference; MLB and NBA have the sample sizes to actually validate an edge. |
| Native mobile app | PWA | One codebase, no app store, works on iOS 16.4+ and Android from the home screen. |
| Auto-bet | Read-only, tap through to trade | Money safety and simplicity. Polymarket order signing is a later, opt-in phase. |
| LLM picks / explanations | No LLM at all | Determinism rule. Nothing to explain that a number cannot. |
| Unlimited data refresh | Quota-aware refresh | 500 free credits/month. A naive poller burns that in a day. |
