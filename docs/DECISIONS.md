# DECISIONS

Each entry: date, decision, why, consequences.

## 2026-09-21 — Tool installs are decided by finding the executable, not by exit code
The first real install died on Tailscale with "No package found matching input
criteria" followed by "Tailscale installed but 'tailscale' is still not on
PATH" — two misleading messages from one cause. `winget install --exact`
matches the package id *case-sensitively*, so the manifest id
`Tailscale.Tailscale` has to be spelled exactly that way; `tailscale.tailscale`
matches nothing. The second message then fired because the code treated "winget
returned" as "winget installed".

Three changes. The id is spelled correctly. Whether a tool is present is now
decided by `Resolve-Tool`, which looks on PATH and then in the standard Program
Files locations, adding whatever it finds to this process's PATH — an installer
writes PATH for *new* sessions, and an already-installed tool should be found
rather than reinstalled. And success is judged by locating the executable
afterwards rather than by `$LASTEXITCODE`, which reports failure for the benign
"already installed, upgrade attempted" path.

Tailscale also gets a direct-MSI fallback if winget produces nothing usable.
That URL could not be reached from the build environment, so the fallback is
wrapped in a try and reports why it failed rather than masking the original
problem.

Consequence: a missing tool now produces one accurate error naming the tool and
saying the script is safe to re-run, instead of two contradictory ones.

## 2026-09-21 — The Windows install bootstraps over HTTP, not git
The first real run of the documented one-liner failed three ways at once, and
all three were in the instructions rather than the installer. Windows checks
the execution policy when it *loads* a `.ps1`, before it reads anything inside
it, so `#Requires -RunAsAdministrator` never got the chance to print a useful
message — the run died on "running scripts is disabled on this system". The
documented `winget install Git.Git` reported "Installer failed with exit code:
1" on a machine that already had Git, because winget treated it as an upgrade;
the working Git was untouched, but the error reads like a fatal one. And the
session wasn't elevated, which would have failed later at the scheduled task
anyway.

So: `scripts/bootstrap-windows.ps1` is piped into `Invoke-Expression`, which
runs it from memory where the execution policy does not apply. It downloads a
zip rather than cloning, removing the Git dependency and the upgrade failure
with it. `setup-windows.ps1` dropped `#Requires` for an explicit administrator
check that re-launches itself elevated. The Odds API key is now prompted for
rather than passed as an argument — an empty `-OddsApiKey` was a parse error
waiting to happen, and a key on the command line is visible in the process list
to every user on the machine.

Consequence: the install no longer depends on Git being present or on the user
remembering to elevate, and the branch is pinned in a URL that has to be
updated when this merges to main.

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

## 2026-09-18 — Core math raises ValueError on degenerate input instead of returning a number
American odds strictly between -100 and +100, probabilities outside (0,1), fewer than
two outcomes to a de-vig, negative book weights, non-positive cost to ev/kelly/clv_decimal.
`shin_devig` additionally rejects an underround (sum of raw probs < 1) because Shin's
model has no solution there; `power_devig` handles underround (k < 1). These are
single-book quotes so real data never hits them, but a silent wrong probability is worse
than a loud failure in a tool that sizes real bets. Consequence: `fair_for_outcome`
catches the ValueError per book, logs it and skips that book, so one bad quote never
aborts a scan; the scan service additionally records any ValueError that still escapes
in `Scan.errors`.

## 2026-09-18 — Rounding and edge cases in the sizing math
`stake_for` rounds to the cent and compares the rounded stake with `min_stake`; this is
what makes the contract vector 19.23 hold at 1e-5. `walk_asks` sorts the ask ladder by
price before walking and reports avg price 0.0 when zero shares were bought;
`build_opportunity` never calls it with a zero stake. `kelly_fraction` returns 0.0 for
cost >= 1 rather than dividing by zero.

## 2026-09-18 — Team resolution rejects Polymarket's binary labels
`team_key` is case- and punctuation-insensitive with one exception: "Yes" in any casing
and the title-case label "No" return None, while "NO" and "no" still resolve to New
Orleans. Without this, a Yes/No market on a Saints or Pelicans game would silently be
priced as a New Orleans moneyline. The Polymarket client also skips the resolver for
Yes/No outcome labels entirely.

## 2026-09-18 — Spread line ownership when the question has no signed number
`parse_spread` reads the team adjacent to the signed number in the question. If the
question has no signed number, Gamma's `line` is assumed to be signed from the
perspective of the first listed outcome unless exactly one outcome team is named in the
question. Unverified against live data; a wrong assignment can only cause a market to be
skipped as "no book at line", never a false opportunity.

## 2026-09-18 — Polymarket client conventions
(1) Home/away: Polymarket does not document which outcome is the home team. The client
assumes outcomes are listed [away, home] and that teamAID/teamBID follow the same order;
it prefers teamAID/teamBID resolved through GET /teams (fetched once per league per
client instance, WARNING + outcome fallback when unavailable), then the moneyline
outcomes, then a spread market. `match_games` is order-insensitive so a wrong guess only
swaps display names, never the edge. The scan service's `upsert_game` applies the same
[away, home] fallback when an event carries no team ids, and takes display names from the
canonical alias table so a game reads "Kansas City Chiefs @ Buffalo Bills" whichever
source named it. (2) The league discriminator is embedded in the request URL and also
passed in params so a FixtureTransport can route one fixture file per league by URL
prefix. (3) `parse_market_type` returns None for unknown `sportsMarketType` strings and
for period/prop/futures questions; the client reports those as unparseable with reason
"unsupported market type ..." and the scan stores them in `Scan.notes.unparseable`.

## 2026-09-18 — ESPN spreads and totals are priced at -110 per side
ESPN's scoreboard exposes a spread line and a total but per-side prices are unverified,
so `EspnClient.to_book_games` assumes -110/-110, which de-vigs to 0.5/0.5 at the quoted
line. ESPN contributes line information rather than price information for
spreads/totals, which is why `book_weights["espn"]` defaults to 0.5. Moneylines are taken
as given.

## 2026-09-18 — Odds API errors are re-raised as OddsApiError with the key redacted
`OddsApiClient` wraps every transport failure in `OddsApiError` (a `TransportError`
subclass) with the raw and URL-encoded key replaced by `***` in both message and url, and
never logs params. Diagnostics can show the full error text safely; the scan service
records the message verbatim in `Scan.errors`.

## 2026-09-18 — Routes read the ORM directly and call services only for actions
Every page is built from Scan/Opportunity/PmQuote/BookQuote/Bet rows so nothing is
computed at request time; `run_scan_default`, `create_bet` and `settle_bet_manual` are
the only service actions and are imported at module level so tests can monkeypatch them.
Action failures return HTTP 200 with an out-of-band `#toast` and re-render the current
list; display-only helpers degrade to an em-dash instead of taking the page down.

## 2026-09-18 — UtcDateTime column type instead of raw DateTime(timezone=True)
SQLite discards tzinfo, so a plain `DateTime(timezone=True)` comes back naive.
`app.models.UtcDateTime` converts to UTC on bind and re-attaches UTC on load, so the same
code works on SQLite now and on PostgreSQL later. All datetimes in the app are UTC-aware;
naive values passed in are assumed UTC.

## 2026-09-18 — Ruff excludes Markdown
`ruff format` rewrites fenced Python blocks in .md files and reformatted the binding code
blocks in docs/ARCHITECTURE.md. Markdown is excluded in pyproject (`extend-exclude`) so
the contract text is only ever changed on purpose.

## 2026-09-18 — Scaffold deviations recorded after the fact
`fly.toml` `[env]` additionally sets `APP_ENV="prod"` so the Settings validator refuses
to start on Fly without `APP_PASSWORD` (intended). A `.dockerignore` keeps `.venv`,
`.git`, `.env`, `*.db` and `data/` out of `COPY . .`. `app/db.py` exposes
`set_engine(engine)`, `sqlite_file_path(url)` and `get_engine(url=None)`; the SQLite
parent directory is created lazily on first connect so building an engine has no
filesystem side effect. `prefs.py` exports `validate_prefs(data)` (pure) next to
`update_prefs`. The Docker image could not be built in the build environment (no Docker
daemon); the Dockerfile and entrypoint are syntax-checked only.

## 2026-09-18 — Container entrypoint fixes volume ownership, then drops privileges
Fly.io mounts volumes root-owned, so a container that starts as the non-root `app` user
cannot create `/data/app.db`. `docker/entrypoint.sh` runs as root just long enough to
`mkdir -p` and `chown app:app` the directory of the SQLite file derived from
`DATABASE_URL` (default `/data`), then `exec setpriv --reuid=app --regid=app
--init-groups uvicorn ...`. The Dockerfile therefore has no `USER app` line; if the
container is started unprivileged anyway the chown step is skipped. The image is
otherwise unchanged.

## 2026-09-18 — Service worker never caches authenticated pages
The first service worker cached every HTML page network-first with a cache fallback,
which would replay ledger and settings HTML from the phone's cache after logout or on a
lost device. Pages and htmx partials are now network-only; navigations that fail offline
get a small inline "Offline" response instead of a cached page. `/static/*` is served
stale-while-revalidate (cached copy now, refetch in the background) from a cache named
`edges-v2-<APP_VERSION>`; the `/sw.js` route substitutes the version into the worker and
`base.html` / `login.html` load `/static/*.css|js?v=<APP_VERSION>`, so a deploy reaches
the installed PWA on the next load instead of never (the review found the old
cache-first branch kept stale CSS/JS until the cache name was edited by hand).

## 2026-09-18 — Bet stake and P&L convention
`Bet.stake_usd` is the total cash out of pocket including the taker fee. For a taker at
price `a` with fee rate `r`, cost per share is `a + r*a*(1-a)`, `shares = stake / cost`
and `fee_usd = stake - shares*a`; a maker pays exactly the limit price with no fee. A
winning share pays $1, so `pnl_won = shares - stake_usd`, `pnl_lost = -stake_usd`, and a
void is 0. Mark-to-bid P&L on the ledger is `(bid - price)*shares - fee_usd`, i.e.
`bid*shares - stake_usd`, consistent with the same convention. `edge_at_bet` is
recomputed at the price the owner actually logged (which may differ from the ask), and
the taker fee rate is the one the opportunity was priced with (inferred from
`effective_price - ask`), so a per-market fee override survives into the bet.
`ledger_summary` counts `total_staked` and ROI over decided bets only (won + lost); voids
return the stake. `avg_clv` is None until a bet has closing data.

## 2026-09-18 — Scan failure policy and stored-book fallback
One league failing (Gamma, CLOB, Odds API, ESPN) never aborts the others: the reason is
appended to `Scan.errors` with a `league: step:` prefix and the scan continues. A books
refresh whose Odds API call fails falls back to ESPN when enabled, and a league with no
fresh source at all falls back to the most recent stored snapshot for each game that is
not older than `stale_book_minutes` (a snapshot with a fetch time in the future relative
to `now` counts as fresh, which is what keeps demo data usable). `Scan.ok` is False only
when the scan saw no market at all and has errors, or when an exception escapes the
per-league loop; in that last case the row is persisted with the error and the exception
is re-raised so the UI shows a red toast. Partial scans are `ok` and become the home page
even when `errors` is non-empty (the toast turns red). `Scan.notes.book_source` records,
per league, whether books came from `oddsapi`, `espn`, `stored:<scan ids>` or `none`.

## 2026-09-18 — Settlement fetches each open bet's market individually
Gamma's `/events?closed=false` slate no longer contains a resolved market, so after the
per-league loop the scan calls `GET /markets/{id}` for every open bet whose market was
not in the slate, updates the `Market` row and passes the result to
`bets.settle_open_bets`. A failed lookup is recorded in `Scan.errors` and the bet stays
open until the next scan. `settle_open_bets` accepts an optional `now` (additive to the
contract) so the settlement timestamp is the scan clock, not the wall clock.

## 2026-09-18 — Closing line = the last snapshot taken before kickoff (revised in review)
`capture_closing` runs for bets whose game has started and lack closing data. "Closing"
is defined by the game clock, not the scan clock: the closing fair is recomputed with
`matching.fair_for_outcome` (configured de-vig method and book weights) from the stored
BookQuote snapshot with the greatest `fetched_at <= game.start_time`, and
`closing_pm_price` is the best ask of the PmQuote for the bet's token with the greatest
`fetched_at <= game.start_time`. Nothing fetched after the start is ever used, because the
only scans that can run after the start see in-play book odds and an in-game Polymarket
price, and CLV measured against those is a measure of the game state, not of the close
(the review reproduced a +0.22 "CLV" that was really +0.023). For this to work
`PmQuote.fetched_at` is the scan clock (`now`), the same clock `BookQuote.fetched_at`
uses, rather than the wall clock of the CLOB fetch. CLV = closing fair - fee-inclusive
cost per share (`stake_usd / shares`). The first capture wins. Closing is captured
*before* settlement in `run_scan`, so a bet whose market resolved between two scans (the
owner's morning-scan / evening-game / next-morning-scan pattern) is settled and CLV'd by
the same scan; won and lost bets that still lack closing data remain eligible so a bet
settled by hand gets its CLV on the next scan too. A game with no pre-kickoff snapshot
simply never gets a CLV and is excluded from `avg_clv`.

## 2026-09-18 — Demo clock and demo transport
Demo mode runs the whole pipeline against `FixtureTransport` routes built by
`services.demo.build_demo_transport` (Gamma events/teams per league, CLOB `/books` from
`clob_books.json`, `GET /markets/{id}` served from the same event fixtures, Odds API and
ESPN per league, quota headers 497/3/3). `run_scan_default` defaults `now` to the fixed
demo clock 2026-09-19T15:00Z in demo mode so the slate's games stay in the future and the
book snapshot never goes stale no matter when the demo is opened; the `ago` filter renders
those timestamps as "in 1d" when the wall clock is behind the demo clock and "3d ago" once
it is ahead, both of which read sanely. In demo mode the Odds API client is built with a
placeholder key (`"demo"`, never sent anywhere) so the Books button and the quota badge
exercise the same code path as production. `seed_demo` is self-contained (it builds the
clients on the fixture transport and calls `run_scan` directly, so it never touches the
network whatever `DEMO_MODE` says), idempotent on the demo bets (`Bet.notes == "demo"`)
rather than on "any Scan row", and refuses with a warning when real bets are in the
ledger. It orders its work so the settle logic is actually exercised: it upserts the
closed BAL@TOR market, logs an open Orioles bet placed before that game, runs the `both`
scan (whose settlement step marks the bet lost with P&L), then logs the open Chiefs bet
from the real Chiefs opportunity via `create_bet` at the suggested stake, back-dated one
hour. The Orioles bet's closing data is pre-filled because a finished game has no books
or asks left to compute it from. `python -m app.cli demo-seed` refuses outside dev
unless `DEMO_MODE=true` (the Makefile target sets it), and `demo-clear` removes the
synthetic slate and demo bets (refusing while real bets exist); the app logs a warning
at startup when demo bets linger with `DEMO_MODE` off, because they count in P&L/CLV.

## 2026-09-18 — Active Settings override instead of request plumbing
The services layer has no request in hand, yet `run_scan_default` needs `demo_mode`,
`odds_api_key` and `fixtures_dir`. `app.settings.set_settings(settings)` makes the
Settings a `create_app` was built with the process-wide active Settings that
`get_settings()` returns (falling back to the environment), so tests that build an app
with their own Settings never read a developer's `.env`. `run_scan_default` and
`seed_demo` also take an optional `settings=` keyword (additive to the contract) for
callers that have one.

## 2026-09-18 — Fixture corrections
Row 3 (LAL@BOS) could not produce an opportunity at the listed prices (Celtics fair 0.689
vs effective 0.671 at a 0.66 ask), so the Celtics best ask is 0.64 and the Lakers best ask
0.37 in docs/FIXTURES.md, `gamma_events_nba.json` and `clob_books.json`; the demo scan now
yields a Celtics ML edge of about +0.037. Row 5's run line was internally inconsistent:
the books priced Yankees +1.5 at -140 (fair about 0.57) while the Polymarket ask for that
side was 0.44, which produced a bogus +11.5% "edge" in the demo. The run-line asks are now
0.58 (Yankees +1.5) / 0.43 (Dodgers -1.5) so neither side clears the minimum edge and the
demo shows exactly the four intended opportunities.

## 2026-09-18 — `services/adapters.py` rebuilds core values from rows
Re-pricing a Polymarket-only scan and computing a closing fair both need `BookGame` and
`PmMarket` values built from stored rows. Those adapters live in a small module of their
own (`pm_market_from_row`, `book_game_from_rows`, `stored_book_game`) rather than in
`scan.py`, because `bets.py` needs them too and `scan.py` already imports `bets.py`.

## 2026-09-18 — Review round: only pre-game, tradable markets are priced
Polymarket trades in-game and Gamma's `closed=false` slate keeps a game's markets while
it is under way, so a poly scan during a game compared the live price with the pre-game
book snapshot and reported the difference as a huge edge. `_scan_league` now skips, and
lists under `Scan.notes.unmatched` with the reason (`market closed`, `market not accepting
orders`, `game started`), every market that is closed, not accepting orders, or whose
`gameStartTime` is at or before `now`; a matched book game whose `commence_time` has
passed is treated the same. `build_opportunity` applies the same guards as defence in
depth. Live quotes are still stored (the ledger's mark needs them), just never priced.

## 2026-09-18 — Review round: the limit price must be able to rest
`limit_price_for_edge` (the contract vector, unchanged) gives the highest fee-free price
that still clears the minimum edge, but whenever an opportunity exists that price sits at
or above the best ask, where a bid crosses the book and fills as a taker. The
`Opportunity.limit_price` the app stores and shows is now `resting_limit_price`: the same
value capped one market tick below the best ask, None when nothing can rest. `create_bet`
rejects a maker order priced at or above the latest stored ask with an actionable
message. The test vectors that encoded the crossing price (0.53 against a 0.50 ask) were
updated to the resting one (0.49); the ledger test's maker bets now rest one tick below
the asks. Consequence: a maker bet is always logged at a price that could actually rest.

## 2026-09-18 — Review round: partial fills are persisted, the mark is net of the exit fee
`walk_asks`' `fill_complete` flag was dropped on the way to the database. `Opportunity`
now stores `fill_complete` and `fill_usd` (the fee-inclusive dollars the stored ladder can
absorb at `fill_price`), the cards and the game page flag a partial fill, and the bet
form prefills the fillable amount rather than the Kelly stake. The unrealized mark on an
open bet subtracts the taker fee a sale at the bid would pay (`shares * rate * bid *
(1 - bid)`, rate implied from the bet's own fee, else the preference) and is labelled
"Mark net". Because v1 has no Alembic, `init_db` now appends model columns missing from
an existing table with `ALTER TABLE ... ADD COLUMN` (boolean defaults as `TRUE`/`FALSE`,
which SQLite >= 3.23 and PostgreSQL both accept); it never drops or retypes.

## 2026-09-18 — Review round: Polymarket parsing and paging hardening
(1) With no `sportsMarketType`, a question naming a series or round (finals, series,
playoffs, semifinals, wild card, ALDS/ALCS/NLDS/NLCS, "best of") is not a game unless it
names a game number ("World Series Game 3"), because "NBA Finals: Thunder vs. Pacers"
otherwise became a moneyline matched to game 1's book odds. "Championship" and "Super
Bowl" stay eligible: they are single games in the NFL and an existing test pins "AFC
Championship: Chiefs vs. Bills" as a game, so the review's suggestion to reject them was
not followed. (2) A market whose type was inferred from the question must carry a
`gameStartTime`; otherwise it is reported as unparseable ("no gameStartTime") instead of
inheriting the event's `startDate`. (3) `/events` is requested with `order=id`,
`ascending=true` and the documented `sports_market_types` filter, `_iter_events` drops an
event id seen twice and `events()` drops a market id seen twice; drops are logged and
recorded under `Scan.notes.duplicates_dropped`. (4) `market()` takes an optional
`league=` hint (additive) that `_markets_for_open_bets` fills from the stored `Game.league`,
so settlement no longer fails forever on a bare payload without a league tag or prefix.

## 2026-09-18 — Review round: Game rows are keyed by the Polymarket event id
`upsert_game` looked a game up by (league, home, away, UTC day), which collapsed an MLB
doubleheader into one row that was then priced with the first game's lines. It now looks
up by `pm_event_id` first and falls back to (league, home, away) with a start time within
three hours, so two same-day events are two rows with their own book snapshots.

## 2026-09-18 — Review round: ESPN dates are US Eastern, and "no fresh source" really means stored
ESPN buckets its scoreboard by the Eastern calendar date, so the fallback now requests ET
yesterday, today and tomorrow (three keyless calls, de-duplicated by event id); the UTC
variant missed every West-coast night game for scans between 00:00Z and 04:00Z. When the
Odds API fails and every ESPN day fails too (or ESPN lists no priced games), `_fetch_books`
returns None and the league falls back to the stored snapshot as the docstring always
promised; previously an empty ESPN list with source "espn" silently priced nothing.

## 2026-09-18 — Review round: conventions and unresolved teams are observable
Every scan records `Scan.notes.conventions` (the assumed `[away, home]` outcome order,
the match window and the staleness limit in force) and `Scan.notes.unresolved_book_teams`
(book labels the alias table could not name, collected from the clients'
`unresolved_teams` lists, logged at WARNING), and Diagnostics renders both; the game page
carries a "home/away assumed" hint until the convention is verified against live data.

## 2026-09-18 — Review round: production hardening of the password gate
Outside dev the Settings validator now also refuses the public default `SECRET_KEY` (or
one shorter than 32 characters) and an `APP_PASSWORD` shorter than 12 characters: with
the default key anyone who has read the repo can mint a valid 30-day cookie. The cookie
payload is an HMAC of the password under the secret key instead of the constant "ok", so
changing `APP_PASSWORD` or rotating `SECRET_KEY` revokes every cookie (documented in the
README and `.env.example`). `POST /login` is throttled by an in-process `LoginLimiter`:
five consecutive failures from one address (first `X-Forwarded-For` / `Fly-Client-IP`
hop) or fifty from anywhere lock the address out with a lockout that doubles from 30 s
and caps at an hour, answered with HTTP 429 and `Retry-After`; failures are logged at
WARNING with the address. The global counter is a deliberate denial-of-service trade-off
for a one-user app: it stops address rotation at the cost of a bounded lockout for the
owner. Every authenticated response carries `Cache-Control: no-store` / `Pragma:
no-cache` so a lost phone cannot show the ledger from the HTTP or back/forward cache after
logout. The Docker image sets `APP_ENV=prod` itself, so running it anywhere without the
secrets fails at startup instead of serving every page open; `docker run -e APP_ENV=dev`
opts back in. The app logs a loud warning at startup when auth is disabled.

## 2026-09-18 — Review round: the Odds API key never reaches the logs
httpx logs every request line with its query string at INFO, which printed `apiKey=...`
on every Books refresh. `app/logsetup.py` (used by the app factory and the CLI) sets the
`httpx` / `httpcore` loggers to WARNING and installs a `RedactApiKeyFilter` on those
loggers and on every root handler that rewrites `apiKey=<value>` to `apiKey=***` in any
record before it is formatted, so no future logger can leak it either.

## 2026-09-18 — Review round: pinned, hashed dependencies
`requirements.txt` stays the loose spec (now with upper bounds on the APIs the code relies
on: FastAPI/Starlette 1.x, APScheduler 3.x, `tzdata` added for `zoneinfo`) and
`requirements.lock` is the `uv pip compile --generate-hashes` output that the Dockerfile,
CI and `make setup` install with `--require-hashes`. Upgrades go through `make lock` and a
reviewed diff instead of whatever PyPI serves on deploy day.

## 2026-09-18 — Review round: scheduler on Fly, and other documentation debts
The in-process scheduler lives in the web process, which Fly stops when idle
(`min_machines_running = 0`), so scheduled scans never fire unless the machine is kept
running (`min_machines_running = 1`, about $2/month) or an external cron calls the CLI;
this is now stated in `.env.example`, `fly.toml` and the README. The README gained the
ten-minute phone guide (volume, secrets including `SECRET_KEY`, deploy, Add to Home
Screen, routes, make targets, costs).

## 2026-09-18 — Review round: UI details
Server toasts swap into the persistent `#toast` live region with `hx-swap-oob="innerHTML"`
(a `<span class="toast-msg" data-error>`), which `app.js` un-hides before the swap and
tones afterwards, because replacing the region node is not announced by screen readers.
The edge/bet card title link, the back link and `<details>` summaries are 44 px tall; the
NBA badge colour is a token with a light-mode value; the ledger tiles go to two columns
under 420 px and never break inside a number; the bottom sheet locks page scrolling
(`body.sheet-open`, `overscroll-behavior: contain`); the Edges topbar is re-rendered
out-of-band with every scan result; `theme-color` has light and dark variants; money,
P&L (`usd_signed`), share counts (one decimal) and the tick (cents) use the same helpers
in toasts and templates, which changed the two tests that pinned the old `+4.50 USD`
toast to `+$4.50`. An empty `leagues_enabled` is rejected (every league off would make
every scan a silent no-op).

## 2026-09-18 — Review round 2: the scan runs in the threadpool
`POST /scan` was an `async def` calling the blocking, synchronous scan (sync httpx) on the
event loop, so with one uvicorn worker every other request — the stylesheet, a second tab,
the ledger — waited for the whole scan. A request issued 0.3 s into a 3 s scan took 2.72 s,
which on a phone reads as a hung app. The handler is now a plain `def`, which FastAPI runs
in the threadpool. `def` rather than `run_in_threadpool(...)`: `get_session` is a
request-scoped dependency, and a sync handler puts the dependency and the endpoint on the
same threadpool thread, so the Session is never touched from two threads (SQLite engines
already use `check_same_thread=False`). The other page handlers stay `async def` — they do
microsecond SQLite reads. Regression test holds a stubbed slow scan in flight and times a
concurrent `GET /healthz`: ~5 s on the old handler, milliseconds now.

## 2026-09-18 — Review round 2: a second scan cannot be queued behind the first
`run_scan_default` raises `scan.ScanBusy` when a scan is already in flight, and the route
renders it as an ordinary (non-red) toast with the list intact rather than a 500. The
client half: both refresh buttons carry `hx-disabled-elt="#scan-status button"`, so one tap
disables the pair. Double-tapping Refresh Books was otherwise spending Odds API credits
twice for one slate.

## 2026-09-18 — Review round 2: price prefills are formatted at the market's tick
The bet form formatted the ask with `%.2f`. Polymarket ticks in 0.001 below 0.04 and above
0.96, so a 0.035 ask prefilled as `0.04` (shares off by 12.5%) and a resting 0.499 was
rounded up to `0.50` — at the ask, where `create_bet` refuses a maker order, making the
Maker toggle unusable on those markets. `routes/bets.fmt_price(value, tick_size)` formats
at three decimals below a 0.01 tick and two otherwise, and falls back to `f"{value:g}"`
whenever the rounding would move the stored price at all, so an absent or too-coarse
`tick_size` can never silently change a price. Both prefills and both `data-price-*`
attributes come from that one helper.

## 2026-09-18 — Review round 2: phone layout, the toast, the status bar and the notch
The `.metrics` grid dropped to two columns only below 359 px while its cells need ~63 px
for `-$12.30` and ~126 px for `multiplicative`, and a 390–414 px phone gives 52–66 px: the
breakpoint is now 419 px, `.metrics-3` holds three columns inside it, and `.metrics dd` is
`white-space: nowrap` with ellipsis as the last-resort guard, so a value never splits
inside a number. The toast is `pointer-events: none` and moves to the top of the screen
while a sheet is open — it used to sit exactly over the sheet's "Log bet" button and
swallow taps there for the six seconds an error toast is shown.
`apple-mobile-web-app-status-bar-style` is `default` rather than `black-translucent`, which
painted white status-bar text over the near-white light-mode topbar; iOS now colours the
bar from the `theme-color` metas. `viewport-fit=cover` was set but only the top and bottom
insets were honoured, so in landscape on a notched iPhone the nav items, brand, chips and
card edges sat under the notch: `.page`, `.topbar`, `.sheet-panel` and `.login-body` use
`max(var(--gutter), env(safe-area-inset-left/right))` and `.bottom-nav` adds both insets.
A vanished opportunity now answers `POST /bets` with `HX-Retarget: #sheet` +
`HX-Reswap: innerHTML` instead of nesting a second `role="dialog"` and a duplicate
`#sheet-title` inside the open sheet. Settled-bet P&L and the ledger P&L tile use
`usd_signed`, so a $4.50 profit and a $4.50 stake no longer differ only by colour.

## 2026-09-18 — Review round 2: the request clock is injectable
`routes/edges.get_now()` is a dependency returning `datetime.now(UTC)`, threaded through
`/`, `POST /scan` and `/games/{id}` into the template context, and templates render ages as
`|ago(now)`. The filter's `now=` parameter existed but nothing passed it, so no rendered age
and no age-derived badge was asserted anywhere. Tests now freeze the clock with
`dependency_overrides` and assert the rendered "Last scan 5m ago" and the stale-book badge
on both sides of the 720-minute boundary (719 minutes: no badge; 721: badge). Partials
without a `now` in context still call the bare `|ago`, which falls back to the wall clock.

## 2026-09-18 — A per-market taker fee is believed only inside a plausible band

`takerBaseFee` is read as basis points, but its unit is unverified (RESEARCH item 7) and
the old guard accepted anything in `(0, 0.2)` after dividing by 10000. A fraction
(`0.05` → 5e-06), a percent (`5` → 5e-04) or a flag (`1` → 1e-04) therefore passed as a
"per-market override" that removed the taker fee entirely: with fair 0.5205 and an ask of
0.50, `build_opportunity` correctly returns None at the real 5% fee and manufactures a
+0.0205 edge under each bogus encoding. A fee override now has to land in
`MIN_TAKER_FEE_RATE..MAX_TAKER_FEE_RATE` (0.01–0.20, bracketing Polymarket's published
0.05); anything else logs a WARNING naming the market and the raw value, and the
preference applies. The band is deliberately wider than a plain "must be 0.05" so a real
fee change still gets through, and deliberately narrow enough that every mis-unit
encoding of a sports fee falls outside it.

Observability: `PolymarketClient.taker_fee_overrides` collects `{market_id, raw, rate}`
for every market that carried a non-zero `takerBaseFee` during the last `events()` call,
with `rate=None` meaning "rejected, using the preference". **Hook for the scan owner**:
`Scan.notes["conventions"]["taker_fee_overrides"] = polymarket.taker_fee_overrides`
(collected per league inside `run_scan`, since `events()` resets the list). Nothing in
`app/services/scan.py` was touched.

## 2026-09-18 — ESPN contributes a spread or total only when it priced both sides

ESPN is one low-weight book in the consensus, but with no Odds API key (or after a failed
call) it is the *only* book, and then its prices are the fair probability. Pricing both
sides at -110 de-vigs to exactly 0.5 whatever the real price is, so every spread and total
side asking below ~0.475 was reported as an opportunity with `n_books=1`: a Cowboys +4.5
at 0.46 showed +0.0276 where a book pricing DAL +4.5 at +105 / PHI -125 implies 0.4654
(-0.0070). The client now reads `homeTeamOdds.spreadOdds` / `awayTeamOdds.spreadOdds` and
`overOdds` / `underOdds`, and contributes the market only when both sides carry a real
price. Moneylines are unchanged (ESPN always carries both). `ESPN_SIDE_PRICE` is gone.

The per-side prices ride on `EspnOddsGame`, a frozen dataclass in `app/clients/espn.py`
that extends the contract's `EspnGame` with four optional fields, because
`app/core/types.py` belongs to another owner. `to_book_games` reads them with `getattr`,
so a plain `EspnGame` still works and contributes h2h only.

## 2026-09-18 — An event's `startDate` is not a kickoff

`_parse_market` used `event.startDate` whenever a market had no `gameStartTime`. On Gamma
that field is the listing/creation timestamp (the fixture sets it six days before the
game), so one market missing `gameStartTime` backdated the whole game and the scan treated
it as already started. The fallback is gone: such a market keeps `game_start = None`, which
matching handles through its unique home/away pair rule and the scan through the matched
book's `commence_time`. Markets whose *type* was inferred from the question still require a
`gameStartTime` (unchanged) — that rule is about series/futures markets wearing a game
title, not about kickoff.

## 2026-09-18 — A missing `acceptingOrders` falls back to `enableOrderBook`/`active`

`_as_bool(raw.get("acceptingOrders"))` was False when the key was absent, so a payload
variant that omits it (older markets carry only `enableOrderBook`/`active`) made a scan
store quotes for everything, price nothing, and fill Diagnostics with one indistinguishable
reason. `_accepting_orders` now falls back to `enableOrderBook`, then `active`. When
nothing in the payload says the market is tradable and it is not closed, it is reported as
unparseable with the distinct reason **"acceptingOrders missing (enableOrderBook/active
off)"**, so the payload change is recognisable on Diagnostics instead of hiding behind the
scan's generic "market not accepting orders". Closed markets are kept whatever the flags
say, because `market()` re-reads them for settlement.

## 2026-09-18 — The optional `/events` query parameters degrade instead of failing

`order=id&ascending=true` and the repeated `sports_market_types` filter are unverified
(RESEARCH item 13) and are only optimisations: the client re-filters market types and
de-duplicates events itself. A 400/422 on the first page used to mean zero markets for
every league on every scan until someone edited the code. `_events_page` now retries the
first page once without those parameters, logs at WARNING, and sets
`PolymarketClient.events_filters_dropped` (sticky for the client's life, so the second and
third league do not each pay for a rejected request). 5xx still propagates — that is not a
parameter problem.

## 2026-09-18 — A misconfigured production start no longer prints the environment

`_require_password_outside_dev` raises `ValueError` inside a pydantic model validator, so
pydantic wrapped it in a `ValidationError` whose string form contains
`input_value=<the raw env dict>`; `app/main.py` builds `Settings` at import, so the
uncaught traceback went to stderr and `fly logs`. A prod start with `ODDS_API_KEY` set
printed 22 of its 32 characters. `_settings_from_env` now catches `ValidationError` and
raises `SystemExit(config_error_message(exc))`, built only from
`errors(include_input=False, include_url=False)` — and `from None`, so the chained
exception cannot print the inputs as "during handling of the above exception".

`app_password`, `secret_key` and `odds_api_key` are `SecretStr`, so any future
`repr()`/log of a Settings object prints `**********`. Plain text is read through the new
`app_password_value` / `secret_key_value` / `odds_api_key_value` properties.
`database_url` stays `str`: `app/db.py` and `app/cli.py` pass it straight to SQLAlchemy and
both are outside this scope. `OddsApiClient` accepts either a `str` or a `SecretStr` and
unwraps it (`plain_api_key`), so `app/services/scan.py` keeps working unchanged — without
that, a `SecretStr` would have reached the query string as its placeholder.

## 2026-09-18 — Proxy address headers are trusted only when configured

`client_ip()` trusted `Fly-Client-IP` and then the *first* `X-Forwarded-For` hop
unconditionally. Off Fly — `docker run`, Railway, Render, all advertised in the Dockerfile
comment — anyone can set either header, so twelve wrong passwords with twelve different
`Fly-Client-IP` values returned twelve 401s and never tripped `MAX_FAILURES_PER_IP=5`, and
the WARNING log recorded the spoofed string. The new `TRUSTED_PROXY_HEADER` setting is
empty by default (socket peer only); when set, only that header is read, and only its last
hop — the one the nearest proxy appended. The value must parse as an IP address
(`sanitize_ip`, bracket and zone-id aware, truncated to 45 characters) or it is dropped
with a WARNING that does not quote it, so nothing attacker-supplied reaches a log line or
the limiter's key space.

**For the coordinator:** add to `fly.toml` `[env]`:

    TRUSTED_PROXY_HEADER = "fly-client-ip"

and to `.env.example`:

    # Header carrying the real client IP, set ONLY if a proxy you control adds it
    # (Fly.io: fly-client-ip). Empty = trust nothing but the socket peer.
    TRUSTED_PROXY_HEADER=

## 2026-09-18 — CSRF: state-changing requests must look same-origin

`POST /scan?kind=books` spends Odds API credits, `/bets`, `/bets/{id}/settle`, `/settings`
and `/logout` write state, and the only protection was the cookie's `SameSite=Lax`. The
auth middleware now rejects any non-GET/HEAD/OPTIONS request with `Sec-Fetch-Site:
cross-site`, or with an `Origin`/`Referer` whose host differs from the request `Host`
(a literal `null` origin counts as cross-site), with 403 and a WARNING. A request carrying
none of those headers is allowed: no browser omits all three on a cross-site POST, and the
CLI, curl and TestClient send none. htmx sends `Origin` on POST, so no template changed.
The check runs before the public-path shortcut, so cross-site `POST /login` is refused too.

## 2026-09-18 — Security headers on every response

`Strict-Transport-Security: max-age=31536000; includeSubDomains` whenever
`settings.cookie_secure` is true (i.e. `APP_ENV != dev`): fly.toml's `force_https` only
redirects, and the first cleartext request on hostile Wi-Fi can be answered with a cloned
login form that captures the password — the only auth factor. Not in dev, where an HSTS
header on `http://localhost` would pin the whole host for every other local project.
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin` go out on every
response, dev included. The middleware was split into `_dispatch` (CSRF + auth) and
`apply_security_headers`, so public pages, `/healthz` and `/static/*` are covered too.

## 2026-09-18 — The image says where the database is

`Dockerfile` now sets `DATABASE_URL=sqlite:////data/app.db` alongside `PORT` and
`APP_ENV`. The app default is the *relative* `sqlite:///./data/app.db` (`/app/data/app.db`
in the image), so only `fly.toml`'s `[env]` was papering over it: a plain `docker run` (or
Railway/Render) wrote the bet ledger to the ephemeral container layer while the entrypoint
chowned an unused `/data`. The README documents `-v <volume>:/data`.

**For the coordinator:** `docker/entrypoint.sh` is outside this scope; apply

```diff
@@
 port="${PORT:-8080}"
 
+# Say where the ledger is going: the commonest deployment mistake is a DATABASE_URL
+# pointing at the container layer instead of the mounted volume.
+case "$url" in
+  sqlite:*) echo "entrypoint: SQLite database ${path:-$url} (directory $dir)" ;;
+  *)        echo "entrypoint: DATABASE_URL scheme ${url%%://*} (directory $dir)" ;;
+esac
+
 if [ "$(id -u)" = "0" ]; then
```

## 2026-09-18 — Smaller notes

- `.gitignore` and `.dockerignore` gained `.coverage`, `.coverage.*`, `htmlcov/`: a bulk
  `git add -A` was committing the binary coverage artifact and `COPY . .` shipped it.
- The README's "two minutes" quickstart started with `make setup`, which shells out to
  `uv` without ever saying to install it (`make: uv: No such file or directory`). It now
  gives the uv install one-liner as a prerequisite and a plain-pip fallback
  (`python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r
  requirements.lock`), which was verified to work.
- `tests/test_prefs.py` covers the previously untouched `validate_prefs` rejection
  branches that the settings form can actually reach: non-finite floats (`"nan"`, `"inf"`,
  `float("inf")`), non-numeric and fractional strings for `stale_book_minutes`, non-list
  `bookmakers`, blank/non-string `book_weights` keys, non-boolean numbers — plus the
  accept side (numeric 0/1 booleans, `""` for an unchecked checkbox, a whole float for an
  int field) and a test that a rejected field applies nothing at all.
- `tests/test_polymarket_client.py` covers the `MAX_PAGES` hard stop: a handler that
  serves the same full page for every offset stops after exactly 100 requests, logs
  "stopped after 100 pages", returns 100 unique markets and records 9900 dropped
  duplicates.

## 2026-09-18 — Review round 2: a stake is sized by the depth that still clears min_edge
`build_opportunity` sized the bet from the **best ask only**: Kelly, `stake_for`, then
`walk_asks` with that fixed dollar amount. Against a ladder of `[(0.64, 10), (0.75, 5000)]`
and a fair of 0.688564, the $20 suggestion walked onto the 0.75 wall, filled at an average
of 0.72052 — above fair, a negative-EV recommendation — and still reported
`fill_complete=True` with no warning. `edge_clearing_depth(asks, fair, min_edge, fee_rate)`
now walks the ladder level by level over the levels whose **own** fee-inclusive price still
satisfies `fair - cost >= min_edge`, and `plan_stake` is the single sizing implementation:
only an *edge* stop truncates the stake (floored to the cent, reported as
`fill_complete=False` with `fill_usd` = the edge-clearing dollars, so the partial-fill UI
and the prefill take over), and a truncation below the $1 floor suggests nothing. A merely
*thin* ladder is unchanged, which keeps the contract's hand-checked vector intact, and
`walk_asks` itself is untouched. The cap is marginal, not average: a level dearer than
`fair - min_edge` is a bet we would not place on its own, so we do not place it as the tail
of a bigger one.

## 2026-09-18 — Review round 2: a stake under the minimum order is 0 with a reason
Polymarket rejects an order below `Market.min_order_size` (5 shares in the fixtures), so a
$100 bankroll on the Chiefs wanted $1.33 — 2.36 shares — and the app showed it as a
placeable, fully fillable suggestion. `plan_stake` reports `suggested_stake = 0.0` with a
note instead; an unknown or zero minimum never blocks a stake. The reason rides on a new
nullable `opportunities.stake_note` column, backfilled by `db.add_missing_columns`, because
`core.types.Opportunity` is the binding contract and gained no field:
`edge.stake_note(opportunity, book, prefs)` re-derives it from the same `plan_stake`, one
implementation and two call sites. The UI shows the note beside a capped or zero stake and
renders the stake itself as "—"; because the ledger records what the owner *did* and not
only what the app advised, the log-bet form still opens at the smallest placeable stake
(`routes/bets._placeable_stake`) rather than a 0 that `create_bet` rejects.

## 2026-09-18 — Review round 2: a 50/50 resolution is a push, not a void
Polymarket resolves a cancelled or postponed game with `outcomePrices ["0.5","0.5"]`, which
is neither `["1","0"]` nor `["0","1"]`, so the bet never settled; voiding it by hand then
recorded P&L 0 when a push actually pays $0.50 a share — a taker at 0.57 with a $10 stake
(17.174606 shares) is **-$1.41**. `bets.is_push(market)` recognises it, `settle_open_bets`
settles those as `"push"` leaving `Market.resolved_outcome` NULL (there is no winning side),
and `settle_bet_manual` accepts a fourth result `push` (`pnl = 0.5*shares - stake_usd`).
`void` keeps its meaning: an order that never filled, P&L 0, counted in neither
`total_staked` nor ROI. A push is **decided** — it is in `total_staked`, therefore in ROI,
and `ledger_summary` gained `n_push`, which the ledger tile shows only once one exists.

## 2026-09-18 — Review round 2: the match window is per league
MLB and NBA play the same opponent on consecutive days and books post the next day's MLB
lines late, so a flat 36 h window matched Polymarket's game N+1 to the book's game N —
different starting pitchers, very different prices — priced the difference as an edge and,
worse, wrote game N's lines under game N+1's `Game` row, poisoning every later poly-only
rescan. `match_games`'s `window_hours` is now the NFL/default **outer bound** that the
per-league rule may only narrow: `SERIES_WINDOW_HOURS = {"mlb": 6.0, "nba": 6.0}` matches a
candidate within 6 h **or** on the same US/Eastern calendar day (`GAME_DAY_TZ`), so a 13:05
ET day game still matches 20:10 ET lines while a next-day series game never does. NFL is
unchanged. Diagnostics gains a distinct reason, `"different game day"`, so "the
neighbouring game in this series" is visibly not "no lines at all". The trade is deliberate:
more unmatched rows in real use, no silently mispriced ones.

## 2026-09-18 — Review round 2: a sibling market cannot move a game's kickoff
A typed market without `gameStartTime` inherited the event's `startDate` — generally the
*listing* timestamp — and `upsert_game` wrote it over the real kickoff a sibling market had
just stored, so the game looked live since the day it was listed and every scan skipped it.
`scan._kickoff(game, market)` now leaves the stored kickoff alone when the market has none,
takes it when there is none stored, takes a **moneyline** market's as authoritative (every
game has one), and otherwise accepts only a *later* time (a postponement), never an earlier
one. `_game_status` reads the game's resolved kickoff rather than the market's own, so a
market arriving with `game_start=None` can no longer flip a scheduled game to "live". The
client-side half — not propagating `startDate` as a kickoff at all — is recorded above.

## 2026-09-18 — Review round 2: one scan at a time
`scan.SCAN_LOCK` (a `threading.Lock`) and `scan.ScanBusy(RuntimeError)`: `run_scan_default`
acquires it non-blockingly and raises rather than queueing. Two scans writing Scan/Game/
Market rows concurrently would interleave their upserts, and a Books refresh queued behind
another would spend Odds API credits twice. The scheduler catches it and skips; the route
renders it as an ordinary toast. Per-market taker-fee overrides the Polymarket client
accepted or rejected are drained into `Scan.notes["conventions"]["taker_fee_overrides"]`
per league (the client resets its list on every `events()` call) and shown on Diagnostics,
because a rejected override changes every edge on that market.
