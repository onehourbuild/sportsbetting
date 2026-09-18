# Running it free on a Windows desktop, reachable from your phone

Your desktop hosts the app; [Tailscale](https://tailscale.com) gives it a real HTTPS
address that only your own devices can reach. No hosting bill, no port forwarding, no
opening anything to the public internet, and — because the machine is always on — the
background scheduler can do the scanning so edges are waiting for you instead of being
something you have to pull.

Free: Tailscale's Personal plan covers this comfortably (100 devices, one user). Verify
their current terms; free tiers change.

**The one catch:** the app is up only while the desktop is awake and the process is
running. Step 5 starts it with Windows and step 6 stops the machine sleeping. If the
desktop is off, the phone shows the offline page.

---

## 1. Install Python 3.11

```powershell
winget install Python.Python.3.11
```

Close and reopen PowerShell, then check it took:

```powershell
py -3.11 --version
```

(If `winget` isn't available, grab the installer from python.org and tick **Add
python.exe to PATH**.)

## 2. Get the code and build the environment

Pick a folder that isn't in OneDrive — OneDrive sync and an open SQLite file are a bad
combination.

```powershell
mkdir C:\apps
cd C:\apps
git clone https://github.com/onehourbuild/sportsbetting
cd sportsbetting
git checkout claude/trusting-ramanujan-ht0xeg   # until the PR is merged

py -3.11 -m venv .venv
.venv\Scripts\pip install --require-hashes -r requirements.lock
```

Sanity check before going further — this should end in `1192 passed`:

```powershell
.venv\Scripts\python -m pytest
```

## 3. Configure it

```powershell
copy .env.example .env
notepad .env
```

Set these five. Everything else can stay as it ships.

```ini
APP_ENV=prod
APP_PASSWORD=some long passphrase you can type on glass
SECRET_KEY=<paste the 64-character value from the command below>
ODDS_API_KEY=<your key from the-odds-api.com, or leave empty>
DATABASE_URL=sqlite:///C:/apps/sportsbetting/data/app.db
```

Generate the secret key:

```powershell
.venv\Scripts\python -c "import secrets; print(secrets.token_hex(32))"
```

Notes on each:

- **`APP_ENV=prod`** is correct even though this is your own machine. It enforces a real
  password, refuses the placeholder secret key, and marks the session cookie `Secure` —
  which works because Tailscale serves real HTTPS. `dev` would leave the door open.
- **`APP_PASSWORD`** must be at least 12 characters, **`SECRET_KEY`** at least 32; the
  app refuses to start otherwise rather than booting unprotected. Changing either signs
  out every device.
- **`DATABASE_URL`** uses forward slashes after `sqlite:///`, including the drive letter.
  That is your bet ledger — put it somewhere you will remember to back up.

Then, once Tailscale is running (step 4), add one more line:

```ini
TRUSTED_PROXY_HEADER=x-forwarded-for
```

Tailscale proxies from loopback, so without this every request looks like it came from
`127.0.0.1` and the login lockout counts attempts globally instead of per device. Not a
hole — it just means five wrong guesses lock out the whole app rather than one phone.
Harmless to set; if Tailscale turns out not to send the header on your version, the app
logs a warning and falls back to the socket address.

## 4. Tailscale

1. Install it on the desktop: `winget install tailscale.tailscale`, then sign in.
2. Install the Tailscale app on your iPhone and sign in with the same account.
3. In the [admin console](https://login.tailscale.com/admin/dns), enable **MagicDNS** and
   **HTTPS Certificates**. The HTTPS toggle is the one that matters — without it there is
   no valid certificate, and without a valid certificate iOS will not install the app to
   your home screen.
4. Start the app once by hand (`.\scripts\run-windows.ps1`), leave it running, and in a
   second PowerShell window:

   ```powershell
   & "C:\Program Files\Tailscale\tailscale.exe" serve --bg 8000
   ```

   It prints the address it is now serving — something like
   `https://desktop.tailxxxx.ts.net/`. That is your app's URL, reachable from any of your
   devices, anywhere, with no port forwarding. `--bg` persists the configuration, so it
   comes back after a reboot on its own.

   `tailscale serve status` shows what is configured; `tailscale serve --https=443 off`
   removes it.

## 5. Start it with Windows

Task Scheduler, so it comes up after a reboot without you logging in.

1. Open **Task Scheduler** → **Create Task** (not "Basic Task").
2. **General:** name it `Edge Finder`. Select **Run whether user is logged on or not**
   and tick **Run with highest privileges** only if you hit permission trouble — normally
   you do not need it.
3. **Triggers:** New → **At startup**. Tick **Delay task for: 30 seconds** so Tailscale
   is up first.
4. **Actions:** New → Start a program.
   - Program: `powershell.exe`
   - Arguments:
     `-NoProfile -ExecutionPolicy Bypass -File "C:\apps\sportsbetting\scripts\run-windows.ps1"`
5. **Settings:** tick **If the task fails, restart every** 1 minute, up to 3 times. Untick
   **Stop the task if it runs longer than** — this one is meant to run forever.

Right-click the task and **Run** to test it without rebooting. Then open
`https://<your-machine>.ts.net` on the desktop's own browser to confirm.

The script deliberately reads everything from `.env`, so your password and API key never
appear in the Task Scheduler UI.

## 6. Stop the desktop sleeping

A sleeping host is an app your phone cannot reach. From an **admin** PowerShell:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Leave the display timeout alone — the monitor sleeping is fine.

## 7. Put it on the phone

On the iPhone, with Tailscale connected, open `https://<your-machine>.ts.net` **in
Safari** (Chrome on iOS cannot install a PWA). Log in with your `APP_PASSWORD`, then tap
**Share** → **Add to Home Screen** → **Add**.

You get an "Edges" icon that opens standalone — no address bar, its own card in the app
switcher, dark-mode aware. The Tailscale VPN has to be connected on the phone for it to
load; it reconnects on its own, so in practice you just open the app.

## 8. Turn the scheduler on

This is what an always-on host buys you over a sleeping cloud machine. In `.env`:

```ini
SCHEDULER_POLY_MINUTES=15
SCHEDULER_BOOKS_HOURS=6
```

Polymarket refreshes are free, so every 15 minutes costs nothing. Book refreshes spend
Odds API credits: at 6 hours that's 4 scans a day × 9 credits = 36 a day, which overruns
the 500/month free tier. Either set `SCHEDULER_BOOKS_HOURS=12` (18/day, about 550 — still
slightly over) or leave it at `0` and tap **Refresh Books** when you are actually about to
bet. Restart the task after editing `.env`.

Check your remaining credits any time on the **Diagnostics** page.

## 9. Updating, backing up, and going live

```powershell
cd C:\apps\sportsbetting
git pull
.venv\Scripts\pip install --require-hashes -r requirements.lock
```

Then restart the scheduled task. Close and reopen the app on the phone to pick up the
new version.

**Back up** `C:\apps\sportsbetting\data\app.db`. Copy it while the app is stopped, or use
`.venv\Scripts\python -c "import sqlite3,shutil; ..."` if you want it hot — but honestly,
stopping the task for ten seconds is simpler. That one file is every bet, every closing
line and your whole CLV record.

**Before you trust the numbers,** clear the synthetic data if you ever seeded it:

```powershell
.venv\Scripts\python -m app.cli demo-clear
```

It refuses to run once you have logged a real bet, so it cannot eat your ledger.

---

## Troubleshooting

**"Add to Home Screen" isn't in the Share sheet.** You're not in Safari, or HTTPS
certificates aren't enabled in the Tailscale admin console, so you're on a cert iOS
doesn't trust. Check the URL shows a padlock.

**The phone can't load it at all.** Is Tailscale connected on the phone (check the app)?
Is the desktop awake? Is the scheduled task running — Task Scheduler shows **Running**
under Status?

**It loads on the desktop but not the phone.** `tailscale serve status` on the desktop.
If it's empty, the serve config was lost — re-run the `serve --bg` command.

**Login bounces back to the login screen.** The session cookie is `Secure`, so it needs
HTTPS. If you're hitting `http://localhost:8000` directly with `APP_ENV=prod`, that's
expected — use the `ts.net` URL.

**The task runs by hand but not at startup.** Almost always the working directory. The
script resolves the repo from its own path specifically to avoid that, so check the
`-File` argument is the full path and that Python is installed for all users rather than
just your profile.

**Everything is empty and Refresh does nothing.** Open **Diagnostics** — it shows the last
scan's result per source, your remaining Odds API credits, and anything that failed to
parse. That's faster than reading logs.
