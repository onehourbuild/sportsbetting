<#
.SYNOPSIS
  Report what is and is not working, in one pass.

.DESCRIPTION
  Run this when the installer did not reach the end, or when the app is not answering.
  It checks each piece in the order the installer builds them and prints the first thing
  that is wrong, rather than leaving you to read back through a scrolled console.

  It prints your app password, because that is the thing people most often miss when the
  installer window scrolls. Everything here is read-only: it starts nothing, installs
  nothing and changes nothing.

.EXAMPLE
  irm https://raw.githubusercontent.com/onehourbuild/sportsbetting/claude/trusting-ramanujan-ht0xeg/scripts/status-windows.ps1 | iex
#>

[CmdletBinding()]
param(
    [string] $InstallDir = 'C:\apps\sportsbetting',
    [int]    $Port       = 8000
)

$ErrorActionPreference = 'Continue'
$TaskName = 'EdgeFinder'

function Section { param([string] $Text) Write-Host "`n== $Text" -ForegroundColor Cyan }
function Good    { param([string] $Text) Write-Host "   OK    $Text" -ForegroundColor Green }
function Bad     { param([string] $Text) Write-Host "   BAD   $Text" -ForegroundColor Red }
function Info    { param([string] $Text) Write-Host "         $Text" -ForegroundColor DarkGray }

$problems = New-Object System.Collections.Generic.List[string]

Write-Host ''
Write-Host '  Edge Finder - status' -ForegroundColor White
Write-Host "  $InstallDir" -ForegroundColor DarkGray

# ------------------------------------------------------------------ 1. the code
Section 'Code'
if (Test-Path $InstallDir) {
    Good "Install directory exists."
    $marker = [System.IO.Path]::Combine($InstallDir, 'app', 'main.py')
    if (Test-Path $marker) { Good 'Application files are present.' }
    else { Bad 'app\main.py is missing - the clone did not finish.'; $problems.Add('code') }
} else {
    Bad 'Install directory does not exist. The installer never got past fetching the code.'
    $problems.Add('code')
}

# ------------------------------------------------------------- 2. the virtualenv
Section 'Python environment'
$venvPython = [System.IO.Path]::Combine($InstallDir, '.venv', 'Scripts', 'python.exe')
if (Test-Path $venvPython) {
    Good 'Virtualenv exists.'
    $check = & $venvPython -c "import uvicorn, fastapi, sqlalchemy; print('deps ok')" 2>&1 | Out-String
    if ($check -match 'deps ok') {
        Good 'Dependencies are installed.'
    } else {
        Bad 'Dependencies are missing or broken:'
        Info $check.Trim()
        $problems.Add('deps')
    }
} else {
    Bad 'No virtualenv. The installer stopped before or during the dependency install.'
    $problems.Add('deps')
}

# ------------------------------------------------------------------ 3. config
Section 'Configuration'
$envPath = [System.IO.Path]::Combine($InstallDir, '.env')
$password = $null
if (Test-Path $envPath) {
    Good '.env exists.'
    foreach ($line in (Get-Content $envPath)) {
        if ($line -match '^APP_PASSWORD=(.+)$') { $password = $Matches[1].Trim() }
        if ($line -match '^ODDS_API_KEY=(.+)$') { Good 'Odds API key is set.' }
    }
    if (-not $password) { Bad 'APP_PASSWORD is not set in .env.'; $problems.Add('config') }
} else {
    Bad '.env does not exist. The installer stopped before writing the configuration.'
    Info 'That is the step right after the dependency install.'
    $problems.Add('config')
}

# --------------------------------------------------------------- 4. the service
Section 'Startup task'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Good "Task '$TaskName' is registered (State: $($task.State))."
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($info) {
        Info "Last run: $($info.LastRunTime)   Last result: $($info.LastTaskResult)"
        if ($info.LastTaskResult -ne 0 -and $info.LastTaskResult -ne 267009) {
            Bad "The task's last run reported $($info.LastTaskResult)."
        }
    }
} else {
    Bad "Task '$TaskName' is not registered. The installer stopped before this step."
    $problems.Add('task')
}

# ------------------------------------------------------------------ 5. the app
Section 'App'
$answering = $false
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -eq 200) { Good "Answering on 127.0.0.1:$Port."; $answering = $true }
} catch {
    Bad "Nothing is answering on 127.0.0.1:$Port."
    $problems.Add('app')
}

# The single most useful thing when it will not start: run it in the foreground and show
# the real error, rather than leaving it buried in the scheduler.
if (-not $answering -and (Test-Path $venvPython)) {
    Info 'Starting it by hand for a moment to capture the reason ...'
    $out = [System.IO.Path]::Combine($env:TEMP, 'edge-finder-start.log')
    $proc = Start-Process -FilePath $venvPython `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $Port `
        -WorkingDirectory $InstallDir -RedirectStandardError $out -RedirectStandardOutput "$out.out" `
        -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 6
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path $out) {
        $err = (Get-Content $out -Raw)
        if ($err -and $err.Trim()) {
            Write-Host ''
            Write-Host '   Why it will not start:' -ForegroundColor Yellow
            ($err.Trim() -split "`n") | Select-Object -Last 15 | ForEach-Object { Info $_ }
        }
    }
}

# ------------------------------------------------------------------ 6. tailnet
Section 'Tailscale'
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    $candidate = [System.IO.Path]::Combine($env:ProgramFiles, 'Tailscale', 'tailscale.exe')
    if (Test-Path $candidate) { $ts = $candidate } 
}
$hostName = $null
if ($ts) {
    Good 'Tailscale is installed.'
    $exe = if ($ts -is [string]) { $ts } else { $ts.Source }
    $status = & $exe status --json 2>$null | ConvertFrom-Json
    if ($status -and $status.BackendState -eq 'Running') {
        Good 'Tailscale is connected.'
        if ($status.Self -and $status.Self.DNSName) { $hostName = $status.Self.DNSName.TrimEnd('.') }
    } else {
        Bad "Tailscale is not connected (state: $($status.BackendState)). Run: tailscale up"
        $problems.Add('tailscale')
    }
    $serve = & $exe serve status 2>&1 | Out-String
    if ($serve -match "$Port") { Good 'Serving the app on your tailnet.' }
    else { Bad "Not being served yet. Run: tailscale serve --bg $Port"; $problems.Add('serve') }
} else {
    Bad 'Tailscale is not installed.'
    $problems.Add('tailscale')
}

# ------------------------------------------------------------------ summary
Write-Host ''
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
if ($hostName) { Write-Host "  Your app:  https://$hostName/" -ForegroundColor Green }
if ($password) { Write-Host "  Password:  $password" -ForegroundColor Green }
elseif (Test-Path $envPath) { Write-Host '  Password:  see APP_PASSWORD in .env' -ForegroundColor Yellow }

Write-Host ''
if ($problems.Count -eq 0) {
    Write-Host '  Everything is up. Open the URL above in Safari on your phone.' -ForegroundColor Green
} else {
    Write-Host "  Stopped at: $($problems[0])" -ForegroundColor Yellow
    Write-Host '  Re-running the installer is safe - it skips every step already done:' -ForegroundColor DarkGray
    Write-Host '    irm https://raw.githubusercontent.com/onehourbuild/sportsbetting/claude/trusting-ramanujan-ht0xeg/scripts/bootstrap-windows.ps1 | iex' -ForegroundColor DarkGray
}
Write-Host ''
