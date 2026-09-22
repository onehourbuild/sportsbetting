@echo off
REM Forward test: one scan, appended to a log. Run by the "EdgeFinder forward scan" task.
REM --kind poly costs NO Odds API credits: Polymarket and ESPN are both free, and the
REM forward test only needs a price every hour. The paid book refresh is a separate task,
REM "EdgeFinder books scan", because --kind both costs 6 credits a run and the free tier is
REM 500 a month -- hourly would drain it in three and a half days. Remove the task with:
REM   schtasks /delete /tn "EdgeFinder forward scan" /f
cd /d "%~dp0"
echo ---- %DATE% %TIME% >> "%~dp0data\forward-scan.log"
.venv\Scripts\python.exe -m app.cli scan --kind poly >> "%~dp0data\forward-scan.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0data\forward-scan.log"
