<#
.SYNOPSIS
  One-command install of Edge Finder on a Windows desktop.

.DESCRIPTION
  Fetches the app and hands off to scripts/setup-windows.ps1, which does the real work.

  This exists because of two things Windows does to a first-time user of a downloaded
  script, both of which look like the script is broken when it is not:

    * The execution policy blocks .ps1 files from running at all ("running scripts is
      disabled on this system"). Piping this file straight into Invoke-Expression runs it
      from memory, where the policy does not apply, and the handoff below re-launches the
      installer with -ExecutionPolicy Bypass so the same wall is not hit twice.

    * Installing git through winget on a machine that already has git tries an upgrade,
      and a failed upgrade reports "Installer failed with exit code: 1" even though the
      working git is untouched. Downloading a zip avoids needing git at all.

.PARAMETER InstallDir
  Where to put the app. Default C:\apps\sportsbetting. Avoid OneDrive-synced folders:
  sync and an open SQLite file corrupt each other.

.PARAMETER Branch
  Branch to install. Defaults to the PR branch until it is merged to main.

.PARAMETER Port
  Local port. Only bound on 127.0.0.1; Tailscale fronts it. Default 8000.

.EXAMPLE
  # From any PowerShell window, elevated or not:
  irm https://raw.githubusercontent.com/onehourbuild/sportsbetting/claude/trusting-ramanujan-ht0xeg/scripts/bootstrap-windows.ps1 | iex
#>

[CmdletBinding()]
param(
    [string] $InstallDir = 'C:\apps\sportsbetting',
    [string] $Branch     = 'claude/trusting-ramanujan-ht0xeg',
    [int]    $Port       = 8000
)

$ErrorActionPreference = 'Stop'

# PowerShell 5.1 still negotiates TLS 1.0 by default, which GitHub refuses.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11

$Repo = 'onehourbuild/sportsbetting'

Write-Host ''
Write-Host '==> Fetching Edge Finder' -ForegroundColor Cyan

$staging = Join-Path $env:TEMP 'edge-finder-bootstrap'
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $staging | Out-Null

$zipPath = Join-Path $staging 'app.zip'
$zipUrl  = 'https://codeload.github.com/{0}/zip/refs/heads/{1}' -f $Repo, $Branch
try {
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
} catch {
    throw ("Could not download $zipUrl`n" +
           "Check the machine is online and that the branch name is right.`n" +
           $_.Exception.Message)
}

Expand-Archive -LiteralPath $zipPath -DestinationPath $staging -Force

# GitHub names the folder after the branch with slashes flattened, so find it rather than
# guessing at the transformation.
$extracted = Get-ChildItem -Path $staging -Directory | Select-Object -First 1
if (-not $extracted) { throw "The downloaded archive was empty." }

$setup = Join-Path $extracted.FullName 'scripts\setup-windows.ps1'
if (-not (Test-Path $setup)) { throw "setup-windows.ps1 is missing from the download." }

# Files that came from the internet carry a mark-of-the-web that blocks them even under a
# relaxed policy. Strip it from the scripts we are about to run.
Get-ChildItem -Path (Join-Path $extracted.FullName 'scripts') -Filter *.ps1 |
    Unblock-File -ErrorAction SilentlyContinue

Write-Host '    Downloaded.' -ForegroundColor DarkGray
Write-Host ''
Write-Host '==> Starting the installer' -ForegroundColor Cyan

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$arguments = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $setup),
    '-InstallDir', ('"{0}"' -f $InstallDir),
    '-Branch', ('"{0}"' -f $Branch),
    '-Port', $Port
)

if ($isAdmin) {
    # Already elevated: run it here, in the window the user is watching. No -NoExit —
    # this window was already open and stays open on its own.
    & powershell.exe @arguments
} else {
    # A new elevated window replaces this one, so it has to stay open afterwards or the
    # URL and password scroll past and vanish with it.
    $arguments = @('-NoExit') + $arguments
    Write-Host '    Asking for administrator rights — approve the prompt.' -ForegroundColor Yellow
    Write-Host '    The install continues in the window that opens.' -ForegroundColor Yellow
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments
}
