# Putting Edge Finder on your phone

The app is a PWA: a normal web app that iOS and Android will install to the home screen
as a standalone app (its own icon, no browser chrome, its own window in the app
switcher). There is no App Store review, no TestFlight, and nothing to reinstall when
you deploy an update — the next launch picks it up.

Two requirements, both non-negotiable on iOS:

1. It must be served over **HTTPS** on a real domain. `http://192.168.x.x` will load in
   Safari but will not install, and the service worker will not register.
2. You must add it from **Safari** (Chrome on iOS cannot install a PWA).

---

## 1. Deploy it

Fly.io, because it gives you HTTPS and a persistent disk on the free-ish tier and the
repo already carries `fly.toml` and a `Dockerfile`.

```sh
brew install flyctl          # or: curl -L https://fly.io/install.sh | sh
fly auth signup              # or: fly auth login

cd sportsbetting
fly launch --no-deploy       # pick your own app name; say NO to a Postgres/Redis database
fly volumes create data --size 1 --region iad   # the SQLite file lives here
```

`fly launch` rewrites `app = ` in `fly.toml` with the name you chose. Leave the rest of
the file alone — the `[[mounts]]` block and `DATABASE_URL=sqlite:////data/app.db` (four
slashes: that is an absolute path) are what keep your ledger across restarts.

Set the two secrets. Never put these in `fly.toml` — that file is committed.

```sh
fly secrets set APP_PASSWORD='a long passphrase you will type on a phone keyboard'
fly secrets set SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
fly secrets set ODDS_API_KEY=your_key_from_the-odds-api.com   # optional, see below
fly deploy
```

`APP_PASSWORD` must be at least 12 characters and `SECRET_KEY` at least 32 — outside
dev the app refuses to start without them rather than booting wide open. Pick a
passphrase, not a password: you will be typing it on glass. You only do it once per
device; the session cookie lasts 30 days.

Then `fly open`, or just visit `https://<your-app>.fly.dev`.

### Cost and sleeping

`min_machines_running = 0` in `fly.toml` lets the machine sleep when you are not using
it, which is the cheap setting. The tradeoff: the in-process scheduler only runs while
the process is up, so **no scheduled scan fires while the app is asleep**. That is fine
for the way this tool is meant to be used — you open it, you tap Refresh, you look. If
you want unattended background scans, set `min_machines_running = 1` (roughly $2/month)
or run `python -m app.cli scan --kind books` from a cron box you already own.

Cold start from sleep is a couple of seconds. You will notice it on the first tap.

---

## 2. Install it on the home screen

**iPhone / iPad (iOS 16.4 or newer):**

1. Open `https://<your-app>.fly.dev` in **Safari**.
2. Log in once with your `APP_PASSWORD`. Let iOS save it to the keychain.
3. Tap the **Share** button (the square with the arrow), scroll down, tap
   **Add to Home Screen**, then **Add**.

You now have an "Edges" icon. Launched from there it runs standalone — no address bar,
its own card in the app switcher, dark-mode aware, and it respects the notch and the
home indicator in both orientations.

**Android (Chrome):** open the URL, then menu → **Install app** (or **Add to Home
screen**). Chrome will usually offer it on its own after a few seconds.

### Things worth knowing about iOS PWAs

- **The session is separate from Safari's.** The installed app has its own cookie jar,
  so you log in once inside the app even though you are already logged in in Safari.
- **Updates are automatic.** Deploy, then close the app from the app switcher and
  reopen it. The service worker's cache name carries the app version, so a deploy
  invalidates the old cache instead of serving stale pages.
- **Offline is deliberately shallow.** The stylesheet, script and htmx are precached, so
  the app launches and renders instead of showing Safari's error page; with no
  connection it serves a small "Offline" page. Pages themselves are never written to the
  cache — partly because a stale price is worse than no price on a market you are about
  to trade, partly so a lost phone can't replay your ledger out of the cache.
- **Don't delete the icon to "reinstall".** Removing the home-screen icon on iOS also
  drops its storage, which means logging in again. Harmless, just annoying.

---

## 3. First run

Before you point it at real money, run it against the synthetic slate so you can see
every screen populated:

```sh
fly ssh console -C "python -m app.cli demo-seed"    # or DEMO_MODE=true locally
```

That seeds the fixture games, quotes and two example bets. When you are done:

```sh
fly ssh console -C "python -m app.cli demo-clear"
```

`demo-clear` refuses to run if you have logged any real bets, so it cannot eat your
ledger.

**The Odds API key is optional but it is the whole point.** Without it the app falls
back to the ESPN scoreboard, which gives you moneyline-ish numbers from a single
low-weight source — enough to keep the screens alive, not enough to trust an edge to.
The free tier at the-odds-api.com is 500 credits/month; a books refresh across NFL, NBA
and MLB costs a handful of credits, so budget your refreshes rather than pulling to
refresh out of habit. The Diagnostics screen shows your remaining quota as reported by
the API itself.

---

## 4. How you actually use it

The app **never places an order**. It reads public Polymarket prices, compares them to a
de-vigged consensus of sharp book lines, and shows you where the two disagree by more
than your threshold. When you want to act on one, you tap through to Polymarket and
trade there yourself, then log the bet back in the app so it can track CLV.

That split is on purpose. The math is deterministic and testable; the money movement
stays under your thumb.

---

## Troubleshooting

**"Add to Home Screen" is missing.** You are not in Safari, or the page is not on
HTTPS, or the manifest failed to load. Check `https://<your-app>.fly.dev/manifest.webmanifest`
returns JSON.

**It installed but opens in a browser tab.** The manifest was cached before
`display: standalone` was served. Remove the icon, hard-reload the page in Safari, and
add it again.

**Login loops back to the login screen.** The session cookie is signed with
`SECRET_KEY`; rotating that key (or changing `APP_PASSWORD`) invalidates every existing
session. Log in again. If it still loops, you are on plain HTTP — the cookie is
`Secure`, so a browser will not store it.

**Locked out after typing the password wrong.** Five failures from one address triggers
an exponential backoff. Wait it out, or `fly secrets set APP_PASSWORD=...` to reset (a
password change clears every session too).

**Everything is empty and Refresh does nothing.** Open **Diagnostics**. It shows the
last scan's outcome per source, your Odds API quota, and any parse failures, which is
faster than reading `fly logs`.
