#Requires -RunAsAdministrator
<#
.SYNOPSIS
  One-shot install of Edge Finder on a Windows desktop, reachable from your phone.

.DESCRIPTION
  Installs git, Python 3.11 and Tailscale (skipping whatever is already there), clones
  the repo, builds the virtualenv, generates the secrets, writes .env, registers a
  scheduled task so the app starts with the machine, stops the machine sleeping, and
  publishes it on your tailnet over HTTPS.

  Safe to re-run: every step checks before it acts, and an existing .env is never
  overwritten.

  Three things it cannot do for you, because they are your identity, not configuration:
  signing in to Tailscale (a browser login), enabling HTTPS Certificates in the Tailscale
  admin console (one toggle), and installing Tailscale on the phone. It tells you when
  each is due.

.PARAMETER InstallDir
  Where to put the app. Default C:\apps\sportsbetting. Avoid OneDrive-synced folders:
  sync and an open SQLite file corrupt each other.

.PARAMETER Branch
  Branch to check out. Defaults to the PR branch until it is merged to main.

.PARAMETER OddsApiKey
  Your key from the-odds-api.com. Optional — without it the app falls back to the ESPN
  scoreboard, which is one soft source rather than a sharp consensus.

.PARAMETER Port
  Local port. Only bound on 127.0.0.1; Tailscale fronts it. Default 8000.

.EXAMPLE
  .\setup-windows.ps1
.EXAMPLE
  .\setup-windows.ps1 -OddsApiKey 'abc123' -InstallDir 'D:\apps\edges'
#>

[CmdletBinding()]
param(
    [string] $InstallDir = 'C:\apps\sportsbetting',
    [string] $Branch     = 'claude/trusting-ramanujan-ht0xeg',
    [string] $OddsApiKey = '',
    [int]    $Port       = 8000
)

$ErrorActionPreference = 'Stop'
$RepoUrl  = 'https://github.com/onehourbuild/sportsbetting'
$TaskName = 'EdgeFinder'

function Write-Step { param([string] $Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string] $Message) Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Win  { param([string] $Message) Write-Host "    $Message" -ForegroundColor Green }

# winget installs edit the machine PATH, but not the PATH of this already-running process.
# Re-reading both scopes after each install is what stops "git is not recognized" one line
# after git was installed successfully.
function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Test-Command { param([string] $Name) [bool] (Get-Command $Name -ErrorAction SilentlyContinue) }

function Install-IfMissing {
    param([string] $Command, [string] $WingetId, [string] $Label)
    if (Test-Command $Command) {
        Write-Note "$Label already installed."
        return
    }
    Write-Note "Installing $Label ..."
    & winget install --id $WingetId --exact --silent --accept-source-agreements --accept-package-agreements
    Update-PathFromRegistry
    if (-not (Test-Command $Command)) {
        throw "$Label installed but '$Command' is still not on PATH. Open a new admin PowerShell and re-run this script."
    }
    Write-Win "$Label installed."
}

# A passphrase you can actually type on a phone keyboard beats 16 random characters that
# you will get wrong twice in a bar. Four words from this list clear the 12-character
# minimum with room to spare.
$Words = @(
    'amber','anchor','apple','arrow','autumn','bamboo','beacon','birch','bishop','bramble',
    'cactus','canyon','cedar','cobalt','copper','coral','cotton','crimson','crystal','dahlia',
    'delta','denim','ember','fable','falcon','fern','flint','forest','garnet','ginger',
    'granite','harbor','hazel','heron','indigo','ivory','jasper','juniper','kestrel','lagoon',
    'lantern','laurel','lilac','linen','lotus','lumber','maple','marble','meadow','mesa',
    'mint','oak','ocean','onyx','opal','orchid','osprey','otter','pebble','pepper',
    'pewter','pine','plum','prairie','quartz','quill','raven','ribbon','ridge','river',
    'saffron','sage','sable','scarlet','sequoia','shale','silver','slate','sparrow','spruce',
    'summit','sunset','tamarind','teal','thistle','timber','topaz','tundra','velvet','violet',
    'walnut','willow','winter','yarrow'
)

function New-Passphrase {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $picked = for ($i = 0; $i -lt 4; $i++) {
        $bytes = New-Object byte[] 4
        $rng.GetBytes($bytes)
        $Words[[BitConverter]::ToUInt32($bytes, 0) % $Words.Count]
    }
    $picked -join '-'
}

function New-HexKey {
    param([int] $Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    ($buffer | ForEach-Object { $_.ToString('x2') }) -join ''
}

# PowerShell 5.1's Out-File -Encoding utf8 writes a byte-order mark, and a BOM in front of
# the first key makes pydantic read "\ufeffAPP_ENV" instead of "APP_ENV" — the app then
# starts in dev with no password. Always write .env BOM-free.
function Write-TextNoBom {
    param([string] $Path, [string] $Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Find-Python311 {
    if (Test-Command 'py') {
        & py -3.11 --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return @('py', '-3.11') }
    }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "${env:ProgramFiles(x86)}\Python311\python.exe"
    )) {
        if (Test-Path $candidate) { return @($candidate) }
    }
    throw 'Python 3.11 is installed but I cannot find it. Open a new admin PowerShell and re-run.'
}

# ----------------------------------------------------------------------------- preflight

Write-Step 'Checking prerequisites'
if (-not (Test-Command 'winget')) {
    throw 'winget is missing. Install "App Installer" from the Microsoft Store, then re-run this script.'
}
Write-Note "Installing to $InstallDir"

Install-IfMissing -Command 'git'       -WingetId 'Git.Git'              -Label 'Git'
Install-IfMissing -Command 'python'    -WingetId 'Python.Python.3.11'   -Label 'Python 3.11'
Install-IfMissing -Command 'tailscale' -WingetId 'tailscale.tailscale'  -Label 'Tailscale'

# ----------------------------------------------------------------------------- the code

Write-Step 'Fetching the app'
if (Test-Path ([System.IO.Path]::Combine($InstallDir, '.git'))) {
    Write-Note 'Already cloned; updating.'
    & git -C $InstallDir fetch --quiet origin $Branch
    & git -C $InstallDir checkout --quiet $Branch
    & git -C $InstallDir pull --quiet origin $Branch
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
    & git clone --quiet --branch $Branch $RepoUrl $InstallDir
}
if ($LASTEXITCODE -ne 0) { throw "git failed against $RepoUrl (branch $Branch)." }
Write-Win 'Code is in place.'

Write-Step 'Building the Python environment (a minute or two)'
$venvPython = [System.IO.Path]::Combine($InstallDir, '.venv', 'Scripts', 'python.exe')
if (-not (Test-Path $venvPython)) {
    $python = Find-Python311
    & $python[0] @($python[1..($python.Count - 1)]) -m venv ([System.IO.Path]::Combine($InstallDir, '.venv'))
    if (-not (Test-Path $venvPython)) { throw 'Failed to create the virtualenv.' }
}
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet --require-hashes -r ([System.IO.Path]::Combine($InstallDir, 'requirements.lock'))
if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed.' }
Write-Win 'Dependencies installed from the hashed lockfile.'

# ----------------------------------------------------------------------------- config

Write-Step 'Configuring'
$envPath = [System.IO.Path]::Combine($InstallDir, '.env')
$passphrase = $null
if (Test-Path $envPath) {
    Write-Note '.env already exists — leaving it exactly as it is.'
} else {
    $passphrase = New-Passphrase
    $dbPath = [System.IO.Path]::Combine($InstallDir, 'data', 'app.db') -replace '\\', '/'
    $settings = @"
# Written by scripts/setup-windows.ps1. See .env.example for every option.
APP_ENV=prod
APP_PASSWORD=$passphrase
SECRET_KEY=$(New-HexKey)
ODDS_API_KEY=$OddsApiKey
DATABASE_URL=sqlite:///$dbPath
DEMO_MODE=false

# Tailscale proxies over loopback, so without this every request looks like it came from
# 127.0.0.1 and the login lockout would count attempts globally instead of per device.
TRUSTED_PROXY_HEADER=x-forwarded-for

# Polymarket refreshes are free. Book refreshes spend Odds API credits (9 per three-league
# scan, 500/month on the free tier), so that one stays manual by default.
SCHEDULER_POLY_MINUTES=15
SCHEDULER_BOOKS_HOURS=0

LOG_LEVEL=INFO
"@
    Write-TextNoBom -Path $envPath -Text $settings
    Write-Win 'Wrote .env with a generated password and secret key.'
}
if (-not $OddsApiKey -and -not (Select-String -Path $envPath -Pattern '^ODDS_API_KEY=.+' -Quiet)) {
    Write-Note 'No Odds API key set. The app will fall back to the ESPN scoreboard (one soft'
    Write-Note 'source). Get a free key at https://the-odds-api.com and put it in .env later.'
}

# ----------------------------------------------------------------------------- service

Write-Step 'Starting it with Windows'
$runScript = [System.IO.Path]::Combine($InstallDir, 'scripts', 'run-windows.ps1')
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $runScript)
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'   # let Tailscale come up first
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settingsSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settingsSet -Force | Out-Null
Write-Win "Registered the '$TaskName' scheduled task (runs at startup, as SYSTEM)."

Write-Step 'Keeping the machine awake'
& powercfg /change standby-timeout-ac 0
& powercfg /change hibernate-timeout-ac 0
Write-Note 'Sleep and hibernate disabled on AC. The monitor can still sleep.'

Write-Step 'Launching'
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName

$healthy = $false
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 2
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        # still starting
    }
}
if (-not $healthy) {
    throw ("The app did not answer on 127.0.0.1:$Port within a minute. Run it by hand to see why:`n" +
           "    powershell -NoProfile -ExecutionPolicy Bypass -File `"$runScript`"")
}
Write-Win "App is answering on 127.0.0.1:$Port."

# ----------------------------------------------------------------------------- tailnet

Write-Step 'Publishing it on your tailnet'
$tsStatus = & tailscale status --json 2>$null | ConvertFrom-Json
if (-not $tsStatus -or $tsStatus.BackendState -ne 'Running') {
    Write-Note 'Signing in to Tailscale — a browser window will open. Use the same account'
    Write-Note 'you will sign in with on your phone.'
    & tailscale up
    $tsStatus = & tailscale status --json 2>$null | ConvertFrom-Json
}
if (-not $tsStatus -or $tsStatus.BackendState -ne 'Running') {
    throw 'Tailscale is not connected. Sign in with `tailscale up`, then re-run this script.'
}

& tailscale serve --bg $Port 2>&1 | Out-String | Write-Note
$hostName = $null
if ($tsStatus.Self -and $tsStatus.Self.DNSName) { $hostName = $tsStatus.Self.DNSName.TrimEnd('.') }

Write-Host ''
if ($hostName) {
    Write-Host "  Your app:  https://$hostName/" -ForegroundColor Green
} else {
    Write-Host '  Run `tailscale serve status` to see the URL.' -ForegroundColor Yellow
}
if ($passphrase) {
    Write-Host "  Password:  $passphrase" -ForegroundColor Green
    Write-Host '  (Also in .env. Save it to your password manager now.)' -ForegroundColor DarkGray
} else {
    Write-Host '  Password:  unchanged (see APP_PASSWORD in .env)' -ForegroundColor DarkGray
}

Write-Host @"

  Three things only you can do:

  1. Tailscale admin console -> DNS -> enable MagicDNS and HTTPS Certificates.
     https://login.tailscale.com/admin/dns
     Without the HTTPS toggle there is no valid certificate, and without that
     iOS will not offer "Add to Home Screen".

  2. Install Tailscale on your iPhone and sign in with the same account.

  3. In Safari on the phone (not Chrome), open the URL above, log in, then
     Share -> Add to Home Screen.

  Then tap Refresh Books and check the Diagnostics page.

"@ -ForegroundColor White
