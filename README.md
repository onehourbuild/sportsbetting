# Polymarket Edge Finder

A personal, single-user, phone-first web app that compares Polymarket NFL, NBA and
MLB prices against a de-vigged consensus of sharp sportsbook lines, sizes bets with
fractional Kelly, logs them, settles them from Polymarket's own resolution, and
tracks closing line value (CLV). Runs as a PWA you add to your home screen. It never
places orders: you tap through to Polymarket to trade.

The docs: `SPEC.md` (what it does), `docs/PHONE.md` (deploy it and put it on your
phone), `docs/ARCHITECTURE.md` (module contract), `docs/RESEARCH.md` (API notes and the
unverified list), `docs/DECISIONS.md` (why).

## Try it locally in two minutes (no keys, synthetic data)

`make setup` shells out to [uv](https://docs.astral.sh/uv/). Install it first (one line,
no sudo), or use the plain-pip fallback below.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # prerequisite for `make setup`
make setup                 # uv venv + the hashed lock (requirements.lock)
cp .env.example .env       # APP_ENV=dev, APP_PASSWORD empty = no login gate locally
DEMO_MODE=true make run    # http://localhost:8000 with the synthetic fixture slate
```

Without uv (Python 3.11+ and pip only). This builds the same `.venv/` that every other
make target uses, so `make run`, `make test` and `make lint` work afterwards:

```sh
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
```

Demo mode seeds seven synthetic games and two demo bets from `fixtures/` and serves
every API from those files. Both refresh buttons work; nothing touches the network.
`make demo-seed` seeds the same slate into whatever `DATABASE_URL` points at (dev
only; it never touches the network), and `make demo-clear` removes it again. Do the
clear before you go live: demo bets otherwise stay in your P&L and CLV.

## On your phone in 10 minutes (Fly.io)

Cost: the 1 GB volume is $0.15 a month and the `shared-cpu-1x` machine sleeps when
idle, so compute is billed only for the minutes you are actually using it — call it a
quarter a month for personal use. Keeping the machine awake for scheduled scans
(`min_machines_running = 1`, see the scheduler caveat below) is the case that costs
about $2 a month. The Odds API free tier (500 credits a month) covers roughly 55
three-league book refreshes; Polymarket and ESPN are free.

1. Install `flyctl` and sign in: `curl -L https://fly.io/install.sh | sh`, then
   `fly auth login`.
2. From the repo root create the app without deploying. Accept the generated name
   (or pick one) and put it in `fly.toml` (`app = "..."`):

   ```sh
   fly launch --no-deploy --copy-config
   ```

3. Create the volume that `fly.toml` mounts at `/data` (the first deploy fails
   without it):

   ```sh
   fly volumes create data --size 1 --region iad
   ```

4. Set the secrets. The image runs with `APP_ENV=prod`, so it refuses to start unless
   `APP_PASSWORD` has at least 12 characters and `SECRET_KEY` is a random string of at
   least 32 characters (it signs the login cookie; the default in the repo is public).
   Add `ODDS_API_KEY` if you have one (free at https://the-odds-api.com); without it
   book refreshes use the ESPN scoreboard as a single low-weight book.

   ```sh
   fly secrets set \
     APP_PASSWORD='choose-a-long-passphrase' \
     SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))') \
     ODDS_API_KEY='...optional...'
   ```

5. Deploy and open it:

   ```sh
   fly deploy
   fly open          # https://<app>.fly.dev
   ```

6. Add it to your home screen. iOS Safari: Share, then "Add to Home Screen". Android
   Chrome: the three-dot menu, then "Add to Home screen" / "Install app". Sign in
   once; the cookie lasts 30 days.

7. Tap **Refresh Books** (costs 9 credits for three leagues, the button says so),
   then log a bet from an edge card. Bets settle themselves on the next refresh after
   Polymarket resolves the market, and each bet gets its closing line value from the
   last pre-kickoff snapshot.

Later: `fly logs` for the server log, `fly ssh console` for a shell on the machine,
`fly secrets set APP_PASSWORD=...` to change the password (this signs everyone out;
rotating `SECRET_KEY` does the same), `fly deploy` after every change.

**Scheduler caveat.** `SCHEDULER_POLY_MINUTES` / `SCHEDULER_BOOKS_HOURS` run inside
the web process. With the default `min_machines_running = 0` the machine sleeps when
you are not using the app, so scheduled scans do not fire. Either set
`min_machines_running = 1` in `fly.toml` (the machine then costs about $2 a month
continuously) or leave the scheduler off and run scans on demand from the phone.

## Share it straight from your own PC (no account, no host)

`share.ps1` starts the app on `localhost:8042` and points a Cloudflare **quick tunnel** at
it, which gives you a public HTTPS URL you can open on your phone or send to someone. No
Cloudflare account and nothing to pay.

```powershell
.\share.ps1
```

Two things to know before you rely on it. The hostname is **random and new on every run**,
so a link you shared yesterday is dead today — quick tunnels are for trying something out,
not for a link people keep. And it is up only while that window is open and the PC is on.
For a URL that survives a reboot you want either a named Cloudflare tunnel (free account +
a domain on it) or a real host — the Fly.io section above.

Also worth saying plainly: this app is **single-user**. There is one password and one bet
ledger. Anyone you give the link and password to sees your bankroll and your P&L, and can
log and settle bets in your ledger. Fine for looking at the edge list together; not a
shared betting app.

## Anywhere else (plain Docker, Railway, Render)

The image runs with `APP_ENV=prod` and `DATABASE_URL=sqlite:////data/app.db`, so **the
SQLite file lives at `/data/app.db` and that directory must be a volume** — otherwise the
bet ledger goes into the container layer and disappears with the container. The entrypoint
prints the database path it resolved at startup; check it in the first lines of the log.

```sh
docker build -t edge-finder .
docker run -p 8080:8080 -v edges-data:/data \
  -e APP_PASSWORD='choose-a-long-passphrase' \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  edge-finder
```

On Railway or Render, attach a persistent disk mounted at `/data` (or point
`DATABASE_URL` at wherever their disk is mounted). Render's free web services have
ephemeral disks: SQLite is wiped on every deploy there.

Behind a proxy that terminates TLS, set `TRUSTED_PROXY_HEADER` to the header that carries
the real client address (Fly.io: `fly-client-ip`). It is empty by default, and while it is
empty the login throttle counts by socket peer — because any header an attacker can set
would otherwise give them a fresh identity for every password guess.

## Does it actually win? (forward test and back test)

The app's own success metric is P&L and closing-line value on logged bets. Two commands
build the evidence for that, because an empty ledger proves nothing either way.

**Forward test — from now on.** Every scan records *every* outcome it can price, whatever
the edge, into `forward_samples`: the ask, the fee actually charged, the fee-inclusive
price, the book fair value, the depth at the best ask and the hours to kickoff. Each one
is graded from Polymarket's own resolution on a later scan. That matters because
`Opportunity` rows only exist above your minimum edge, so they can never tell you whether
that minimum was the right one — and with ESPN as the only book almost nothing clears it.

```sh
python -m app.cli forward-report              # every threshold, side by side
python -m app.cli forward-report --league mlb --threshold 1 --threshold 2.5
```

It needs scans to keep running. `forward-scan.cmd` runs one and appends to
`data/forward-scan.log`; on Windows the scheduled task **"EdgeFinder forward scan"** runs
it hourly (`schtasks /delete /tn "EdgeFinder forward scan" /f` removes it). Polymarket and
ESPN are free, so this spends no Odds API credits.

**Back test — Polymarket's past.** `backtest-harvest` walks resolved sports markets and
rebuilds each one's last *pre-kickoff* traded price, then `backtest-report` shows what the
market charged against what actually happened.

```sh
python -m app.cli backtest-harvest --league mlb --league nfl   # slow; resumable
python -m app.cli backtest-report --league mlb
```

Read the limits before you read the numbers:

- It does **not** test this app's strategy. That needs historical sportsbook lines, which
  are paid data (The Odds API keeps them back to June 2020; ESPN drops odds from finished
  games). What it tests is whether Polymarket's own price is calibrated — if 60c favourites
  win 60% of the time there is no free money in the price, and if a band wins more often
  than it costs, that is an edge needing no book at all.
- **There is no five-year history.** Polymarket's per-game sports markets start Oct 2023
  (NFL), Dec 2023 (NBA) and Aug 2024 (MLB).
- Trades after kickoff are discarded and a "close" more than 12 hours stale is excluded by
  default. Both are the easy ways to make a back test look profitable when it is not.
- Tuning a threshold until the past looks good is how you build something that loses money
  in future. Prefer closing-line value over profit, and keep a season you never tuned on.

## Routes

| Route | What it is |
|---|---|
| `/` | Edges: opportunities from the latest scan, league chips, the two refresh buttons |
| `/games/{id}` | One game: every market, per-book table, ask depth, resting limit price |
| `/bets` | Ledger and summary; `/bets/new` is the htmx bet form |
| `/settings` | Bankroll, Kelly, edge, fee, de-vig, bookmakers and weights, leagues |
| `/games` | Every game the scans stored, by league; upcoming and started shown separately |
| `/diagnostics` | Recent scans, errors, unmatched / unparseable markets, conventions, raw rows |
| `/login`, `/logout` | Password gate (five wrong guesses lock the address out) |
| `/healthz` | Public JSON health check |

## Make targets

| Target | Does |
|---|---|
| `make setup` | Create `.venv` and install the hashed lock |
| `make lock` | Regenerate `requirements.lock` from `requirements.txt` |
| `make run` | Dev server on port 8000 |
| `make test` / `make lint` / `make format` | pytest / ruff check / ruff format |
| `make scan` | `python -m app.cli scan` (Polymarket refresh against the stored books) |
| `make demo-seed` / `make demo-clear` | Seed or remove the synthetic slate and demo bets |
| `make docker-build` | Build the image locally |

CLI: `python -m app.cli scan --kind poly|books|both [--league nfl]`, `settle`,
`demo-seed`, `demo-clear`.

## Environment

Every setting is documented in `.env.example`. The important ones: `APP_ENV`
(`dev` locally, `prod` in the image), `APP_PASSWORD`, `SECRET_KEY`, `DATABASE_URL`,
`ODDS_API_KEY` (optional), `TRUSTED_PROXY_HEADER` (empty unless a proxy in front of the
app sets a client-address header; `fly-client-ip` on Fly), `DEMO_MODE`, `SCHEDULER_*`,
`LOG_LEVEL`. Secrets never go in the repo, in fixtures or in logs: `APP_PASSWORD`,
`SECRET_KEY` and `ODDS_API_KEY` are held as `SecretStr` so nothing can print them by
accident, a bad configuration exits with a message that quotes no values, and the Odds API
key is redacted from log lines.
