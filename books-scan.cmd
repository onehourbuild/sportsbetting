@echo off
REM Book refresh: pulls sportsbook lines from The Odds API and re-prices everything.
REM Run by the "EdgeFinder books scan" task. This one COSTS CREDITS -- about 6 a run
REM against a 500/month free tier, so it runs twice a day (360/month) and leaves headroom
REM for manual scans. Check what is left on the Diagnostics page.
REM   schtasks /delete /tn "EdgeFinder books scan" /f
cd /d "%~dp0"
echo ---- %DATE% %TIME% >> "%~dp0data\books-scan.log"
.venv\Scripts\python.exe -m app.cli scan --kind both >> "%~dp0data\books-scan.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0data\books-scan.log"
