# Handoff

Everything a fresh session needs to pick this up without re-reading the whole repo.

## What this is

A single-user tool that finds where Polymarket's price disagrees with a de-vigged
consensus of sportsbook lines across NFL, NBA and MLB, sizes the bet with fractional
Kelly, and tracks closing line value on the bets the owner actually places.

**It never places an order.** It reads public market data; the owner taps through to
Polymarket, trades by hand, and logs the bet so the tool can score it. That is a
deliberate safety boundary, not a missing feature — see "Hard constraints" below.

There is no LLM anywhere in the math. De-vig, Kelly and the fee formula are ordinary
arithmetic with tests.

## State as of 2026-09-20

- Branch `claude/trusting-ramanujan-ht0xeg`, head `65b173c`, 5 commits, 122 files.
- PR #1 open as a **draft**, CI green, `mergeable_state: clean`, no review threads.
- 1192 tests pass. `ruff check` and `ruff format --check` clean.
- **Never deployed. No live scan has ever run.** That is the single most important fact
  here, and the reason the PR is still a draft.

## Why it is a draft

The Polymarket, Odds API and ESPN client field names were derived from documentation and
open-source clients, not from live responses — the build environment blocks outbound
access to all three hosts. Every unverified assumption is listed in `docs/RESEARCH.md`
under "Unverified".

Parsing is defensive: anything that fails to parse surfaces on the **Diagnostics** page
rather than being silently dropped. So the confirmation step is cheap — deploy, tap
**Refresh Books**, read Diagnostics. An empty unparseable list means the shapes were
right and the PR can be marked ready.

## The $250 validation plan

The owner is staking **$250**, not to earn from it but to find out whether the edge is
real at a size where being wrong is cheap. Settings that match that bankroll:

| Setting | Value | Why |
|---|---|---|
| `bankroll` | `250` | The actual stake. Default is 1000. |
| `kelly_fraction` | `0.25` | Quarter Kelly. Leave it. |
| `max_stake_pct` | `2.0` | Caps any single bet at $5.00. Leave it. |
| `min_edge` | `0.02` | 2% after fees. Lower only if volume is too thin to learn anything. |

At these settings a typical suggestion lands around **$4–5**. `MIN_STAKE` is $1.00, so
Kelly-sized bets clear the floor — but note that on a high-priced contract (say $0.85) a
5-share Polymarket minimum is $4.25, which brushes the $5.00 cap. Expect occasional
"below the minimum order size" notes on expensive favorites. That is the sizing logic
working, not a bug.

**Timeline honesty:** CLV over ~100 bets is the only trustworthy read, and at a realistic
few qualifying edges per week that is closer to a **full season than a month**. Expected
profit across those 100 bets at this bankroll is roughly $12. Nobody should mistake this
phase for income. It is an experiment that costs about $100 if the edge turns out to be
imaginary.

Judge it on **CLV, not P&L**. Twenty bets of profit is noise.

## What to do first

1. Ask whether the app has been deployed and whether Diagnostics is clean. Nothing else
   matters until a live scan has run once.
2. If Diagnostics shows unparseable entries, fix the client parsing against the real
   payload shape. That is the highest-value work available.
3. If Diagnostics is clean, mark PR #1 ready for review.

`docs/WINDOWS.md` is the free self-host path (Windows desktop + Tailscale, which supplies
the HTTPS certificate iOS requires before it will install a PWA).
`scripts/setup-windows.ps1` does that install from one administrator PowerShell command.
`docs/PHONE.md` is the Fly.io path.

## Open work, in the order it is worth doing

1. **Kalshi client.** Public read API, no key, no signup. A second exchange pricing the
   same games gives cross-venue disagreement, which is more persistent than book-vs-
   Polymarket because the two crowds are separate. Verify the fee formula against live
   responses before trusting it — Kalshi's take is roughly `0.07 x p x (1 - p)`, steeper
   than Polymarket's `0.05 x p x (1 - p)`, and a wrong fee manufactures edges. The review
   round already caught exactly that failure mode with a bad Polymarket fee override.
2. **Maker-side logic.** Makers pay **zero** fees on Polymarket; takers pay the formula
   above. Posting resting bids rather than hitting asks turns the fee from a cost into an
   advantage. Structurally the best risk-adjusted play on this venue and currently
   unbuilt.
3. **Pinnacle availability.** `docs/RESEARCH.md` flags that whether Pinnacle appears on
   The Odds API free tier in 2026 is disputed. Pinnacle is the sharp anchor at weight 3.0;
   DraftKings and FanDuel sit at 1.0 because they are soft. If Pinnacle is absent, the
   consensus is being built from soft lines and the edges are worth much less than they
   look. Check `books_used` on the first live scan.

## Hard constraints

Carried from the owner's instructions and the project CLAUDE.md. These are not
preferences.

- **The app never places an order** on Polymarket or anywhere else. Automating execution
  is off the table until CLV is positive over 100+ bets, and then only on an explicit
  request.
- **No LLM for calculations**, record matching, dedup, permissions or business rules.
  Conventional code with tests.
- **No API keys, passwords or connection strings in source.** Config via `.env`, which is
  gitignored.
- **Never weaken or delete a test to get a green build.**
- **No model identifier** in commit messages, PR titles or bodies, code comments, or any
  other pushed artifact.

## Map of the code

- `app/core/edge.py` — fee, effective price, Kelly, `plan_stake`, depth walking.
  `MIN_STAKE = 1.0`.
- `app/core/` — de-vig methods (multiplicative, additive, power default, Shin), consensus
  weighting, matching.
- `app/clients/` — `polymarket.py`, `oddsapi.py`, `espn.py`, shared `transport.py`.
- `app/services/scan.py` — orchestrates a scan, records conventions and anomalies into the
  notes blob that Diagnostics renders.
- `app/routes/` — edges list, game detail, bet ledger, settings, diagnostics.
- `app/models.py` — book weights live here: Pinnacle 3.0, BetOnline/LowVig 1.5,
  DraftKings/FanDuel 1.0.
- `docs/DECISIONS.md` — 64 dated entries. Every non-obvious choice has a paragraph.
- `docs/RESEARCH.md` — sources, and the "Unverified" list.
