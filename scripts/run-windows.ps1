<#
.SYNOPSIS
  Start Edge Finder on Windows, bound to loopback so only Tailscale can reach it.

.DESCRIPTION
  Point a Task Scheduler task at this script to have the app come up with the machine.
  It resolves the repo root from its own location, so it does not care what the task's
  working directory is - the commonest reason a scheduled task works by hand and fails
  at boot.

  Settings come from .env in the repo root (see docs/WINDOWS.md). Nothing is passed on
  the command line, so no secret ends up in the Task Scheduler UI or in a log.
#>

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
$port = if ($env:PORT) { $env:PORT } else { '8000' }

if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Run the install steps in docs/WINDOWS.md first."
}
if (-not (Test-Path (Join-Path $repo '.env'))) {
    throw "No .env in $repo. Copy .env.example to .env and fill it in (docs/WINDOWS.md)."
}

Set-Location $repo

# 127.0.0.1, never 0.0.0.0: `tailscale serve` connects over loopback, and binding the
# LAN interface would put an authenticated-but-single-password app on every network
# this machine joins.
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $port
