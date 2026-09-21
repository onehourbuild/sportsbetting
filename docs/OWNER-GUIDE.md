# Owner guide

Plain answers to the questions that come up once the app is running. For the build
handoff see `docs/HANDOFF.md`; for the current state see the latest `docs/STATUS-*.md`.

## How do I record a bet I placed?

**In the app (the way it is designed).** Home page (Edges) lists what cleared `min_edge`
on the latest scan. Tap a row, tap **Log bet**, and enter the stake in dollars, the price
you actually got (cents or a 0-1 fraction both work), whether you were a **taker** (hit the
ask) or a **maker** (your resting order got filled), and any note. It lands in **Bets**
(`/bets`) with your entry price, the fair value the app had at the time, and the current
Polymarket mark. When the market resolves the app settles it from Polymarket's own
result and records closing line value.

Two limits to know about, as of 2026-09-20:

- A bet can only be logged against an edge the app found. There is no freehand form for
  "I bet something the app did not flag". If you want that, it is a small build item.
- Kalshi is not wired up. The app reads Polymarket only; a Kalshi client is the first
  item on the open-work list in `docs/HANDOFF.md`, and the endpoint notes in
  `docs/STATUS-2026-09-20.md` are enough to build it.

**Telling Claude in chat.** Just say it in one line with these six things:

> venue, game, side, market and line, price, size, taker or maker

Example: "Polymarket, Giants at Rams Monday, Rams -6.5, bought 8 shares at 53c, taker."
That is enough to compute edge against the book consensus at that moment, put it in the
ledger (once the freehand form exists) and grade it later. Screenshots of the Polymarket
or Kalshi order confirmation work too.

## Worked example: Monday Night Football, Giants at Rams (21 Sep 2026, 8:15 pm ET)

What the app sees, from its own database and the two exchanges' public APIs:

| Market | Polymarket ask | Kalshi ask | ESPN line | De-vigged fair | Edge after 5% fee |
|---|---|---|---|---|---|
| Rams to win | 0.74 | 0.74 | -305 | 0.737 | -2.3% |
| Giants to win | 0.27 | 0.27 | +245 | 0.263 | -2.6% |
| Over 47.5 | 0.51 | n/a | -110 | 0.500 | -3.5% |
| Under 47.5 | 0.50 | n/a | -110 | 0.500 | -2.5% |

Spread: the book is at Rams -7 (+100 / Giants +7 at -120). Polymarket has no -7 line, only
-6.5 (ask 0.53) and -7.5 (ask 0.46), so the app reports "no book at line" and does not
price them. Eyeballed, -6.5 at 0.53 and -7.5 at 0.46 bracket the book's 47.8% at -7
about where the key number 7 should put them.

**Which bet would I take? None.** Polymarket, Kalshi and the book all agree to the cent
on the moneyline, the total is a coin flip on every venue, and the 5% taker fee turns
"priced fairly" into "priced against you" by two to three points. The app's answer is the
right one here: it only speaks when the exchange disagrees with sharp books by more than
the fee, and tonight it does not. That is the normal case. The handoff's estimate is a few
qualifying edges per week across all three leagues, not one per game.

Caveat on the numbers above: the fair values come from ESPN's single line because the
sportsbook refresh (Pinnacle, Circa, BetOnline, LowVig, DraftKings, FanDuel) has not run
yet on the phone install. Once it has, the fair column will be a weighted consensus and
can differ by a point or two. It will not turn a -2.3% into a +2%.

## How does the app improve itself?

It does not learn on its own, and that is deliberate. There is no model being trained and
no LLM in the math. Every number is arithmetic with tests: de-vig the book prices, weight
the books, subtract the fee, size with quarter Kelly. What improves is the **evidence** it
collects and the **settings** you change because of it:

1. **Forward test** (`forward_samples`). Every outcome it can price is recorded, whatever
   the edge, and graded when Polymarket resolves the market. `python -m app.cli
   forward-report` shows win rate, return and CLV by edge threshold. This answers "is 2%
   the right `min_edge`?" from data instead of a guess. 6320 samples graded so far.
2. **Closing line value on your logged bets.** The one metric the project is judged on.
   If your entry price beats the closing price on average over 100+ bets, the edge is
   real; P&L over 20 bets is noise.
3. **Back test** (`historical_samples`, `backtest-harvest` / `backtest-report`).
   Rebuilds the last pre-kickoff price on resolved markets and reports calibration by
   price band, net of fees. 8246 samples harvested.
4. **Diagnostics.** Anything the parsers could not read, any team they could not resolve,
   any fee override Polymarket applies, is shown rather than dropped. That is how a wrong
   assumption gets found and fixed in code.

The things that would actually move the needle, in order: real sportsbook lines instead
of ESPN alone (one tap, costs 9 credits), a Kalshi feed for cross-venue disagreement,
and maker-side logic (makers pay zero fee on Polymarket, which flips the fee from a cost
into an edge). All three are on the open-work list.

## Where do I put money in?

Not in this app. It never holds funds, never places orders, and has no keys to either
exchange. You fund the exchange directly and the app links you to the market.

- **Polymarket.** Sign in at polymarket.com or in the Polymarket app, then **Deposit**.
  Balances are held in USDC. The deposit options you see (card, bank, crypto transfer)
  depend on your location and which Polymarket product you are eligible for.
- **Kalshi.** kalshi.com or the Kalshi app, then **Deposit**; bank transfer or debit
  card, US-regulated (CFTC). Dollar balances.

For the $250 plan: put the $250 on the venue you will actually use, set **Bankroll = 250**
in the app's Settings, and expect suggestions around $4 to $5 each (quarter Kelly, 2%
cap). Do not top it up on the strength of a good week. The plan is to learn whether the
edge exists, and the tuition if it does not is roughly $100 over a season.

This is a description of mechanics, not financial advice.
