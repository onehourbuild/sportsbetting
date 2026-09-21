@echo off
setlocal
title Edge Finder - Setup

rem ---------------------------------------------------------------------------
rem  Double-click this file to install Edge Finder.
rem
rem  It asks for administrator rights (needed to register the startup task and
rem  stop the machine sleeping), then asks you to paste your Odds API key, then
rem  runs unattended for about ten minutes and prints your URL and password.
rem
rem  A .bat is used rather than a .ps1 because Windows blocks downloaded .ps1
rem  files by execution policy before it reads a line of them. A .bat has no
rem  such restriction and can start PowerShell with the policy relaxed for that
rem  one process, which is what the line near the bottom does.
rem ---------------------------------------------------------------------------

set "BRANCH=claude/trusting-ramanujan-ht0xeg"
set "RAW=https://raw.githubusercontent.com/onehourbuild/sportsbetting/%BRANCH%/scripts/bootstrap-windows.ps1"

rem Are we elevated? "net session" only succeeds for an administrator.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Edge Finder needs administrator rights to finish installing.
    echo   Approve the prompt that is about to appear.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo   ==========================================
echo     Edge Finder - Setup
echo   ==========================================
echo.
echo   This takes about ten minutes. It will ask you
echo   for your Odds API key first, then run on its own.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; irm '%RAW%' | iex"

echo.
echo   ==========================================
echo     Finished. Your URL and password are above.
echo     The password is also in:
echo       C:\apps\sportsbetting\.env
echo   ==========================================
echo.
pause
