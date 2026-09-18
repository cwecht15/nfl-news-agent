@echo off
REM NFL News Agent - Injury Report Refresh
REM Called by Windows Task Scheduler (NFL_News_Agent_Injuries): Wed/Thu 5:00 PM,
REM Fri every 45 minutes 3:45-6:45 PM, Sat 4:30 PM.
REM
REM Clubs post practice reports ~3:30-5 PM ET and Friday's carries the game
REM designations. The afternoon cron starts hours late, so the local machine
REM dispatches the cloud refresh (injuries.yml), which commits data/injuries,
REM the audit and today's report - what the dashboard reads.

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"

REM Not wmic: Windows 11 dropped it (see auto_backfill_youtube.bat).
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%I
if not defined TODAY set TODAY=undated

if not exist "data\logs" mkdir "data\logs"
set WRAPPERLOG=data\logs\%TODAY%-injuries-task.log

echo [%TODAY% %TIME%] Dispatching the cloud injury report refresh >> "%WRAPPERLOG%"
gh workflow run injuries.yml --ref master >> "%WRAPPERLOG%" 2>&1
if errorlevel 1 (
    echo [%TODAY% %TIME%] WARNING: gh workflow run failed - the injuries.yml crons remain as fallback >> "%WRAPPERLOG%"
    exit /b 1
)

echo [%TODAY% %TIME%] Dispatched >> "%WRAPPERLOG%"
exit /b 0
