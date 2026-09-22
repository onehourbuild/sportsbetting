# Start Edge Finder and put it on a public HTTPS URL you can send to people.
#
# Runs the app on localhost:8042 and points a Cloudflare "quick tunnel" at it. No
# Cloudflare account is needed, but the hostname is RANDOM AND NEW EVERY TIME this script
# runs, so the link you shared last time stops working. Everything stays up only while
# this window is open; closing it takes the app offline.
#
# For a link that survives a reboot you need a named tunnel (a free Cloudflare account
# plus a domain on it) or a real host — see README.md for the Fly.io path.

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$port = 8042

if (-not (Test-Path "$repo\.venv\Scripts\python.exe")) {
    throw "No .venv found. Create it first: uv venv .venv --python 3.11; uv pip install -r requirements.txt"
}
if (-not (Test-Path "$repo\.env")) {
    throw "No .env found. Copy .env.example to .env and set APP_PASSWORD and SECRET_KEY."
}

Write-Host "Starting Edge Finder on http://localhost:$port ..." -ForegroundColor Cyan
$app = Start-Process -FilePath "$repo\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "$port" `
    -WorkingDirectory $repo -PassThru -WindowStyle Hidden

# Wait for the port to answer before opening it to the world.
$ready = $false
foreach ($i in 1..30) {
    Start-Sleep -Milliseconds 500
    try {
        Invoke-WebRequest "http://localhost:$port/healthz" -TimeoutSec 2 -UseBasicParsing | Out-Null
        $ready = $true
        break
    } catch { }
}
if (-not $ready) {
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
    throw "The app did not come up on port $port. Run it in the foreground to see why: .venv\Scripts\python.exe -m uvicorn app.main:app --port $port"
}
Write-Host "App is up (pid $($app.Id))." -ForegroundColor Green

$log = Join-Path $env:TEMP "edge-finder-tunnel.log"
if (Test-Path $log) { Remove-Item $log -Force }
Write-Host "Opening the public tunnel ..." -ForegroundColor Cyan
$tunnel = Start-Process -FilePath "cloudflared" `
    -ArgumentList "tunnel", "--url", "http://localhost:$port", "--no-autoupdate", "--logfile", $log `
    -PassThru -WindowStyle Hidden

$url = $null
foreach ($i in 1..40) {
    Start-Sleep -Milliseconds 500
    if (Test-Path $log) {
        $hit = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { $url = $hit.Matches[0].Value; break }
    }
}

if ($url) {
    Write-Host ""
    Write-Host "  Share this link:  $url" -ForegroundColor Yellow
    Write-Host "  Password:         the APP_PASSWORD in your .env"
    Write-Host ""
    Write-Host "  On your phone: open it in Safari, then Share -> Add to Home Screen."
    Write-Host "  Anyone with the link AND the password sees the same app and the same"
    Write-Host "  bet ledger as you -- it is a single-user app."
    Write-Host ""
} else {
    Write-Warning "The tunnel did not report a URL. Check $log"
}

Write-Host "Press Ctrl+C or close this window to take it offline." -ForegroundColor DarkGray
try {
    Wait-Process -Id $tunnel.Id
} finally {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped." -ForegroundColor DarkGray
}
