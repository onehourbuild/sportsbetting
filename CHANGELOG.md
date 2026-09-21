# CHANGELOG

## Unreleased — v1 build (2026-09-18)
- Windows scripts are ASCII-only, enforced by tests/test_windows_scripts.py: PowerShell
  5.1 reads a .ps1 as Windows-1252, so a UTF-8 em dash in a comment broke the parse before
  the first line ran. The same test pins the winget ids to their manifest spelling.
- Windows tool installs: corrected the case-sensitive winget id for Tailscale, find tools
  that are installed but off PATH, decide success by locating the executable rather than
  by winget's exit code, and fall back to the Tailscale MSI.
- Windows install fixed after its first real run: `scripts/bootstrap-windows.ps1` installs
  from a zip over HTTP through `irm | iex` (no Git, no execution-policy wall),
  `setup-windows.ps1` self-elevates instead of relying on `#Requires`, and the Odds API
  key is prompted for rather than passed as a command-line argument. `docs/WINDOWS.md`
  documents the three failures and how each is avoided.
- `docs/HANDOFF.md`: the state a fresh session needs to pick the build back up.
- Adjusted build prompt, spec, research notes, architecture contract.
- Scaffold: pyproject/requirements/Makefile/Dockerfile/fly.toml/CI, Settings, db, models
  (all 8 tables), core types, transport (Http + Fixture), prefs service, templating
  filters, app factory with cookie auth middleware, PWA manifest/sw/icons, 59 tests.
- Core math (`odds_math`, `edge`, `clv`) with the hand-checked contract vectors; team
  aliases for all 92 teams, market-to-game matching and per-line fair probabilities
  (`matching`); Polymarket question/line parsing (`parsing`).
- PolymarketClient (Gamma events/markets/teams + CLOB books) with paging, chunked book
  fetches, defensive parsing and unparseable reporting; synthetic Gamma/CLOB fixtures for
  the seven-game slate; tests/test_polymarket_client.py (27 tests).
- OddsApiClient (key redaction, quota headers, cost estimate) and EspnClient (scoreboard
  as a single -110 book) with synthetic fixtures and tests.
- Phone-first UI: edges list with league chips, scan bar (Refresh Polymarket / Refresh
  Books with credit cost + hx-confirm), opportunity cards with Polymarket deep link and
  htmx log-bet bottom sheet; game detail with per-outcome quotes, opportunities, per-book
  table, top-5 ask depth and limit price; bet ledger with summary tiles, open/settled
  lists, manual settle; settings form for every Prefs field with key/quota/demo status;
  diagnostics with last 20 scans, errors/unmatched/unparseable, raw sample rows;
  dark/light tokens, sticky bottom nav, toast + sheet containers;
  tests/test_routes_ui.py (31 tests).
- Integration (v1 works end to end): `services/scan.py` (`run_scan` for poly/books/both
  with stored-book reuse, ESPN fallback, per-league error isolation, Game/Market upserts,
  PmQuote/BookQuote/Opportunity snapshots, unmatched/unparseable/"no book at line" notes,
  settlement lookups for open bets; `run_scan_default`, `estimate_books_cost`,
  `quota_status`), `services/bets.py` (taker/maker `create_bet`, manual and automatic
  settlement with P&L, closing-line capture with CLV, ledger summary),
  `services/adapters.py` (rows -> core values), `services/demo.py` (fixture transport +
  idempotent seed that settles the demo Orioles bet), `services/scheduler.py`
  (APScheduler, off by default, non-overlapping), CLI `scan|settle|demo-seed`, lifespan
  wiring and an active-Settings override; Docker entrypoint that fixes volume ownership
  and drops privileges; service worker no longer caches authenticated pages; fixture
  corrections (LAL@BOS asks 0.37/0.64, NYY/LAD run line 0.58/0.43);
  tests/test_scan.py, test_bets.py, test_scheduler.py, test_cli.py, test_e2e_demo.py
  (49 tests; 1009 total).
- Review fixes (see docs/DECISIONS.md "Review round" entries): closing line and CLV
  come from the last pre-kickoff snapshot and are captured before settlement; only
  pre-game, tradable markets are priced (started / paused / closed markets listed on
  Diagnostics); the stored limit price rests one tick below the ask and maker bets that
  would cross are rejected; partial fills persisted (`fill_complete`, `fill_usd`) and
  shown, bet form prefills the fillable stake; open-bet mark net of the exit fee;
  series-titled markets without a type are not games, inferred types need a
  `gameStartTime`; `/events` paging is ordered, filtered and de-duplicated; `market()`
  accepts a league hint for settlement; Game rows keyed by Polymarket event id
  (doubleheaders); ESPN dates in US Eastern; stored-snapshot fallback when both fresh
  sources fail; conventions and unresolved book teams recorded in `Scan.notes` and shown
  on Diagnostics; `init_db` backfills missing columns. Security: prod refuses the
  default/short `SECRET_KEY` and short passwords, session cookie bound to the password,
  login lockout with 429/Retry-After, `Cache-Control: no-store` on authenticated
  responses, httpx quieted and `apiKey=` redacted from all logs, Docker image defaults
  to `APP_ENV=prod`, startup warnings for disabled auth and lingering demo bets. Delivery:
  hashed `requirements.lock` installed by Docker/CI/`make setup`, versioned static URLs
  and a stale-while-revalidate service worker cache, network-free `demo-seed` (refused
  outside dev without `DEMO_MODE`) plus `demo-clear`, README "On your phone in 10
  minutes", scheduler/Fly auto-stop caveat. UI: toasts announced via the persistent
  live region, 44 px tap targets, NBA badge token, two-column tiles on narrow phones,
  bottom-sheet scroll lock, out-of-band topbar refresh, light/dark `theme-color`,
  consistent money/share/tick formatting. Tests: 1094 total (85 new), including the
  stored-fallback, settlement-lookup-failure, closing-branch, prefs-boundary,
  scheduler-error, upsert_game-fallback and per-book-skip cases the review listed.
- Review round 2 (see docs/DECISIONS.md "Review round 2" entries). Pricing: the suggested
  stake is capped by the ask depth that still clears `min_edge` (`edge_clearing_depth` /
  `plan_stake`), so a thin top level can no longer produce a negative-EV suggestion; a
  stake below the market's minimum order is reported as 0 with a reason
  (`Opportunity.stake_note`, shown on the card and the game page); a 50/50 resolution
  settles as a **push** (`settle_bet_manual` result `push`, `ledger_summary.n_push`, a
  Push button in the ledger) instead of a void with a fictitious P&L of 0; the match
  window is per league (`SERIES_WINDOW_HOURS`, MLB/NBA 6 h or the same US/Eastern game
  day) so a series game is never priced against the neighbouring day's lines, with a new
  `"different game day"` reason on Diagnostics; a sibling market can no longer overwrite a
  game's kickoff with the event's listing date; `SCAN_LOCK` / `ScanBusy` stop a second
  scan queueing behind the first and spending Odds API credits twice; `create_bet` and
  `settle_bet_manual` take an injectable `now`. Clients and security: a per-market taker
  fee is believed only inside a 0.01–0.20 band (a bare `0.05` read as basis points would
  have erased the fee and manufactured edges everywhere) and every accepted or rejected
  override is recorded in the scan notes and shown on Diagnostics; ESPN contributes a
  spread or total only when the payload prices both sides, never at an invented -110;
  `event.startDate` is no longer borrowed as a kickoff; a missing `acceptingOrders` falls
  back to `enableOrderBook`/`active` and is otherwise reported unparseable; the optional
  `/events` filters degrade instead of failing the page; a misconfigured production start
  raises `SystemExit` with field names only, so a pydantic traceback can never print the
  Odds API key (`SecretStr` for all three secrets, with a subprocess test); proxy address
  headers are trusted only when `TRUSTED_PROXY_HEADER` names one (otherwise the login
  lockout could be reset by rotating a forged header); cross-site state-changing requests
  are refused; `nosniff`, `Referrer-Policy` and (in prod) HSTS on every response; the
  Docker image sets `DATABASE_URL` explicitly and the entrypoint logs the resolved path.
  UI: `POST /scan` runs in the threadpool, so a scan no longer freezes the whole app;
  price prefills are formatted at the market's tick (a 0.001-tick market no longer
  prefills an unusable maker price); the metrics grid stops splitting numbers on a 390 px
  phone; the toast can no longer swallow taps on the sheet's own submit button; landscape
  honours the left/right safe-area insets; the iOS status bar is readable in light mode; a
  vanished opportunity retargets the sheet instead of nesting a second dialog; settled P&L
  is signed; the request clock is injectable, so rendered ages and the stale-book badge
  are asserted on both sides of the boundary. Fixtures: the NFL ESPN scoreboard carries
  real `overOdds`/`underOdds`, matching docs/FIXTURES.md. Tests: 1192 passing.
- Phone QA (Playwright, Chromium at iPhone dimensions): `/`, `/?league=mlb`, `/bets`,
  `/settings`, `/diagnostics`, `/games/{id}`, the open bet sheet, landscape and a 320 px
  phone all render with no horizontal page overflow, no console or page errors and no
  4xx/5xx; the sheet has exactly one dialog and nothing covers its submit button. The one
  element wider than the viewport is the Diagnostics scan table, which scrolls inside its
  own `.table-wrap`.
- `docs/PHONE.md`: deploy-to-Fly and Add-to-Home-Screen guide, iOS PWA caveats, first-run
  demo seed and troubleshooting.
- `docs/WINDOWS.md` and `scripts/run-windows.ps1`: self-host on an always-on Windows
  desktop, reached from the phone over a Tailscale HTTPS address, started by Task
  Scheduler. Covers the prod-on-your-own-machine settings (`APP_ENV=prod` for the Secure
  cookie, `TRUSTED_PROXY_HEADER=x-forwarded-for` because Tailscale proxies from loopback)
  and what the scheduler costs in Odds API credits once the host never sleeps.
- `scripts/setup-windows.ps1`: one admin-PowerShell command does the whole Windows
  install — winget for Git/Python/Tailscale, clone, venv from the hashed lockfile, a
  generated passphrase and secret key written to a BOM-free `.env` (a UTF-8 BOM makes
  pydantic read `\ufeffAPP_ENV` and silently start in dev), the scheduled task, sleep
  disabled, a health-check wait, then `tailscale serve`. Idempotent; never overwrites an
  existing `.env`. Paths use `Path::Combine` rather than `Join-Path`, which resolves its
  argument through the PowerShell provider and throws on a drive that does not exist.
  The three steps it cannot do — Tailscale sign-in, the HTTPS-certificates toggle, and
  the phone — are called out where they fall.
