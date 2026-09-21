@echo off
REM Forward test: one scan, appended to a log. Run by the "EdgeFinder forward scan" task.
REM Polymarket and ESPN are both free, so this costs no Odds API credits while
REM ODDS_API_KEY is empty. Remove the task with:
REM   schtasks /delete /tn "EdgeFinder forward scan" /f
cd /d "%~dp0"
echo ---- %DATE% %TIME% >> "%~dp0data\forward-scan.log"
.venv\Scripts\python.exe -m app.cli scan --kind both >> "%~dp0data\forward-scan.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0data\forward-scan.log"
