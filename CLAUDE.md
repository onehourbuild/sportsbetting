# CLAUDE.md — Polymarket Edge Finder (personal tool)

## What this is

A single-user, phone-first web app that finds +EV bets on **Polymarket** sports
markets (NFL, NBA, MLB) by comparing Polymarket prices against a de-vigged
consensus of sharp sportsbook lines, sizes the bet with fractional Kelly, logs
bets, settles them from Polymarket's own resolution, and tracks closing line
value (CLV). It runs as a PWA (add to home screen) on the owner's phone.

The single success metric is **realized CLV and P&L on logged bets**. Not
number of alerts, not "AI picks".

## How to work in this repo

1. Read `SPEC.md`, `docs/ARCHITECTURE.md`, and `docs/RESEARCH.md` before code.
   `PROMPT.md` is the build prompt this app was generated from.
2. Small, commit-sized changes. Run `make test` and `make lint` after each
   material change. Never weaken or delete a test to get green.
3. Keep `CHANGELOG.md` and `docs/DECISIONS.md` current. Every non-obvious
   choice gets a paragraph in DECISIONS.
4. If you cannot verify something against a live API from your environment,
   say so in the code comment and in `docs/RESEARCH.md` "Unverified" list, and
   make the parser defensive and observable (surface it on the Diagnostics page).

## Hard rules

**Determinism.** No LLM anywhere in the betting math, matching, or settlement.
Probabilities, de-vig, Kelly, team matching, and P&L are conventional code with
tests. There is no LLM in v1 at all.

**Money safety.** The app never places orders on Polymarket. It reads public
market data and the owner taps through to Polymarket to trade. Any future
order-placement feature requires an explicit, documented opt-in and a private
key that is never committed.

**Secrets.** Config via environment variables / `.env` (gitignored). No API
keys or passwords in source, fixtures, or tests. `.env.example` documents every
setting.

**Quota awareness.** The Odds API free tier is 500 credits/month and a call
costs `markets x regions` (or `markets x ceil(bookmakers/10)` when a
`bookmakers` list is given). Book refreshes are explicit, show remaining
credits, and never run in a tight loop. Polymarket and ESPN calls are keyless
and free but still rate-limited by good manners (cache, batch).

**Synthetic fixtures.** Tests and demo mode use synthetic JSON in `fixtures/`
shaped like the real APIs. No fixture may contain a real API key.

## Stack (locked)

Python 3.11+, FastAPI, SQLAlchemy 2, SQLite (file on a persistent volume),
Jinja2 + htmx server-rendered PWA, httpx, pytest, ruff, Docker, Fly.io deploy
config. No SPA framework. No background workers beyond an optional in-process
scheduler.
