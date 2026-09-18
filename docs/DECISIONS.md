# DECISIONS

Each entry: date, decision, why, consequences.

## 2026-09-18 — Reference price is a de-vigged sharp consensus, not a model
We compare Polymarket to sportsbook consensus rather than predicting outcomes.
A prediction model that beats Pinnacle is a research program; price
discrepancies between a slower venue and sharp books are measurable today and
testable with CLV. Consequence: without an Odds API key the app degrades to the
ESPN single-book fallback, which is a soft reference and is weighted low.

## 2026-09-18 — Power de-vig by default, others selectable
Power (and Shin) de-vig handle favorite–longshot bias better than
multiplicative normalization, which overstates longshot probabilities. Power
is the common choice among CLV-tracking bettors. For two-way markets Shin and
additive coincide, which the tests assert. Selectable in Settings.

## 2026-09-18 — Fee-aware EV using Polymarket's July 2026 sports fee curve
Taker fee per share `0.05 * p * (1 - p)` is added to the ask before computing
edge and Kelly. Maker orders pay zero, so the app also shows the limit price at
which the minimum edge still holds. Fee rate is a preference so it can be
changed when Polymarket changes it; per-market overrides are honored if the API
exposes one.

## 2026-09-18 — Spreads and totals compare only at the identical line
The Odds API featured markets expose one line per book. Comparing a −3 spread
to a −3.5 spread needs a push/half-point model that is not worth the error.
Books that quote a different line are skipped for that market.

## 2026-09-18 — Read-only, tap through to Polymarket to trade
No order signing in v1. A private key on a phone-facing web app is a bad trade
for a personal tool. Deep links open the event on Polymarket.

## 2026-09-18 — Quota-first refresh design
Book refreshes are explicit buttons that show the credit cost and remaining
credits. Default bookmaker list has 6 books, which counts as one region, so a
full three-league refresh costs 9 of 500 monthly credits. The optional
scheduler is off by default.

## 2026-09-18 — SQLite on a Fly.io volume
Single user, tiny write volume, one machine. A 1 GB volume at $0.15/month is
the cheapest persistent option among Fly, Render, and Railway in 2026. Render's
free tier wipes the disk. Schema uses PostgreSQL-compatible names in case the
owner moves later.

## 2026-09-18 — Password cookie instead of OAuth
One user. `APP_PASSWORD` compared in constant time; signed cookie via
`itsdangerous` for 30 days. HTTPS is provided by the host.

## 2026-09-18 — Same-tree parallel implementation with file ownership
The v1 build ran five implementers concurrently against `docs/ARCHITECTURE.md`
with disjoint file lists instead of git worktrees, to avoid a merge step. The
contract file is therefore binding and any deviation must be recorded here.
