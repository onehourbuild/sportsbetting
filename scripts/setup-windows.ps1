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
  Your key from the-odds-api.com. Optional - without it the app falls back to the ESPN
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
    [string] $ChosenPassword = '',
    [int]    $Port       = 8000
)

$ErrorActionPreference = 'Stop'
$RepoUrl  = 'https://github.com/onehourbuild/sportsbetting'
$TaskName = 'EdgeFinder'

# Windows checks the execution policy when it loads a .ps1, which happens before it reads
# #Requires - so a #Requires -RunAsAdministrator here would never get to print anything
# useful on a default machine. Registering a scheduled task and changing the power plan
# both need administrator rights, so ask for them now rather than failing halfway through
# with a permission error and a half-configured machine.
#
# The key is deliberately not forwarded to the elevated window: arguments are visible in
# the process list to every user on the machine. The elevated run prompts for it instead.
$principalCheck = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'This needs administrator rights. Opening an elevated window ...' -ForegroundColor Yellow
    $relaunch = @(
        '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath),
        '-InstallDir', ('"{0}"' -f $InstallDir),
        '-Branch', ('"{0}"' -f $Branch),
        '-Port', $Port
    )
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $relaunch
    } catch {
        throw ('Could not elevate. Right-click Windows PowerShell, choose "Run as ' +
               'administrator", and run this script again.')
    }
    Write-Host 'Carry on in the new window; this one is done.' -ForegroundColor Yellow
    return
}

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

function Join-ToolPath {
    param([string] $Root, [string[]] $Parts)
    if (-not $Root) { return $null }   # ProgramFiles(x86) is absent on some installs
    $path = $Root
    foreach ($part in $Parts) { $path = [System.IO.Path]::Combine($path, $part) }
    return $path
}

# Where these land when their installer does not put them on this process's PATH - which
# is most of the time, because a PATH written by an installer only reaches a NEW session.
# Checking here first also means an existing install is found rather than reinstalled.
$KnownToolPaths = @{
    'tailscale' = @(
        (Join-ToolPath $env:ProgramFiles        @('Tailscale', 'tailscale.exe')),
        (Join-ToolPath ${env:ProgramFiles(x86)} @('Tailscale', 'tailscale.exe'))
    )
    'git' = @(
        (Join-ToolPath $env:ProgramFiles        @('Git', 'cmd', 'git.exe')),
        (Join-ToolPath ${env:ProgramFiles(x86)} @('Git', 'cmd', 'git.exe'))
    )
    'python' = @(
        (Join-ToolPath $env:LOCALAPPDATA @('Programs', 'Python', 'Python311', 'python.exe')),
        (Join-ToolPath $env:ProgramFiles @('Python311', 'python.exe'))
    )
}

# Look on PATH, then in the known locations. Finding it off-PATH also puts its folder on
# this process's PATH, so the plain-name calls further down this script keep working.
function Resolve-Tool {
    param([string] $Name)
    $onPath = Get-Command $Name -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($candidate in @($KnownToolPaths[$Name])) {
        if ($candidate -and (Test-Path $candidate)) {
            $dir = Split-Path -Parent $candidate
            if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$dir;$env:Path" }
            return $candidate
        }
    }
    return $null
}

$InstallTailscaleMsi = {
    $msi = [System.IO.Path]::Combine($env:TEMP, 'tailscale-setup.msi')
    $url = 'https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi'
    Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
    Start-Process -FilePath 'msiexec.exe' `
        -ArgumentList '/i', ('"{0}"' -f $msi), '/quiet', '/norestart' -Wait
}

function Install-IfMissing {
    param(
        [string] $Command,
        [string] $WingetId,
        [string] $Label,
        [scriptblock] $Fallback
    )
    if (Resolve-Tool $Command) {
        Write-Note "$Label already installed."
        return
    }

    Write-Note "Installing $Label ..."
    # --exact makes winget match the id case-sensitively, so it has to be spelled exactly
    # as the manifest spells it: 'tailscale.tailscale' returns "No package found matching
    # input criteria" while 'Tailscale.Tailscale' installs. Exit codes are not a reliable
    # signal either - an upgrade of an already-present package reports failure - so
    # success is decided by finding the executable afterwards, not by $LASTEXITCODE.
    $output = & winget install --id $WingetId --exact --silent `
        --accept-source-agreements --accept-package-agreements 2>&1 | Out-String
    if ($output.Trim()) { Write-Note $output.Trim() }
    Update-PathFromRegistry
    if (Resolve-Tool $Command) { Write-Win "$Label installed."; return }

    if ($Fallback) {
        Write-Note "winget did not produce a working $Label. Downloading the installer directly ..."
        try { & $Fallback } catch { Write-Note "Direct download failed: $($_.Exception.Message)" }
        Update-PathFromRegistry
        if (Resolve-Tool $Command) { Write-Win "$Label installed."; return }
    }

    throw ("Could not install $Label. Install it by hand and re-run this script - it " +
           "skips every step that is already done, so nothing is lost.")
}

# A passphrase you can actually type on a phone keyboard beats 16 random characters that
# you will get wrong twice in a bar. Two words plus three digits clears the app's
# 12-character minimum without being a sentence to thumb in one-handed.
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
    $pick = {
        $bytes = New-Object byte[] 4
        $rng.GetBytes($bytes)
        [BitConverter]::ToUInt32($bytes, 0)
    }
    $first  = $Words[(& $pick) % $Words.Count]
    $second = $Words[(& $pick) % $Words.Count]
    $digits = (& $pick) % 1000
    $candidate = '{0}-{1}{2:D3}' -f $first, $second, $digits
    # Two short words could land under the minimum; pad rather than hand back something
    # the app will refuse at startup.
    while ($candidate.Length -lt 12) {
        $candidate = '{0}-{1}' -f $candidate, $Words[(& $pick) % $Words.Count]
    }
    $candidate
}

function New-HexKey {
    param([int] $Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    ($buffer | ForEach-Object { $_.ToString('x2') }) -join ''
}

# PowerShell 5.1's Out-File -Encoding utf8 writes a byte-order mark, and a BOM in front of
# the first key makes pydantic read "\ufeffAPP_ENV" instead of "APP_ENV" - the app then
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

# Everything after this runs unattended, so collect the one thing only you can supply
# before it starts rather than five minutes in with you already out of the room.
$envPath = [System.IO.Path]::Combine($InstallDir, '.env')
$keyAlreadyStored = (Test-Path $envPath) -and
    (Select-String -Path $envPath -Pattern '^ODDS_API_KEY=.+' -Quiet)
if (-not $OddsApiKey -and -not $keyAlreadyStored) {
    Write-Step 'Your Odds API key'
    Write-Note 'Paste the key from https://the-odds-api.com (free, 500 credits a month),'
    Write-Note 'then press Enter. The rest of the install needs nothing else from you.'
    Write-Note ''
    Write-Note 'Press Enter on its own to skip: fair value then comes from the ESPN'
    Write-Note 'scoreboard alone, which is one soft source rather than a sharp consensus.'
    $OddsApiKey = (Read-Host '    Odds API key').Trim()
    if ($OddsApiKey) { Write-Win 'Key captured.' }
}

if (-not $keyAlreadyStored -and -not (Test-Path $envPath)) {
    Write-Step 'Choose your app password'
    Write-Note 'This is what you type on your phone to open the app, so pick something'
    Write-Note 'you can actually thumb in. At least 12 characters.'
    Write-Note ''
    Write-Note 'Press Enter on its own and one will be generated for you.'
    while ($true) {
        $chosen = (Read-Host '    Password').Trim()
        if (-not $chosen) { break }
        if ($chosen.Length -ge 12) { $ChosenPassword = $chosen; Write-Win 'Password set.'; break }
        Write-Note "That is $($chosen.Length) characters; the app requires at least 12."
    }
}

Write-Step 'Checking prerequisites'
if (-not (Test-Command 'winget')) {
    throw 'winget is missing. Install "App Installer" from the Microsoft Store, then re-run this script.'
}
Write-Note "Installing to $InstallDir"

Install-IfMissing -Command 'git'       -WingetId 'Git.Git'             -Label 'Git'
Install-IfMissing -Command 'python'    -WingetId 'Python.Python.3.11'  -Label 'Python 3.11'
Install-IfMissing -Command 'tailscale' -WingetId 'Tailscale.Tailscale' -Label 'Tailscale' `
    -Fallback $InstallTailscaleMsi

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
$passphrase = $null
if (Test-Path $envPath) {
    Write-Note '.env already exists - leaving it exactly as it is.'
} else {
    $passphrase = if ($ChosenPassword) { $ChosenPassword } else { New-Passphrase }
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
if (-not $OddsApiKey -and -not $keyAlreadyStored) {
    Write-Note 'Running without an Odds API key: fair value will come from the ESPN'
    Write-Note 'scoreboard alone. Add ODDS_API_KEY to .env and restart to fix that.'
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
    Write-Note 'Signing in to Tailscale - a browser window will open. Use the same account'
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
