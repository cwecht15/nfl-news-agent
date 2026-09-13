@echo off
REM NFL News Agent — Practice-Squad Elevation Check
REM Called by Windows Task Scheduler (NFL_News_Agent_Elevations), Sat 4:15 PM.
REM
REM Standard elevations are declared by 4:00 PM ET the day before a game, and
REM an elevated player is active for it — so this has to be known Saturday
REM night, not Sunday morning. inactives.yml carries Saturday crons already,
REM but GitHub fires this repo's crons a median of four hours late while a
REM workflow_dispatch starts in seconds, so the local machine does the asking.
REM
REM The cloud job is the one that counts: it commits data/roster back to the
REM repo, which is what the dashboard and the report read.

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DATETIME=%%I
set TODAY=%DATETIME:~0,4%-%DATETIME:~4,2%-%DATETIME:~6,2%

if not exist "data\logs" mkdir "data\logs"
set WRAPPERLOG=data\logs\%TODAY%-elevations-task.log

echo [%TODAY% %TIME%] Dispatching the cloud elevation check >> "%WRAPPERLOG%"
gh workflow run inactives.yml --ref master >> "%WRAPPERLOG%" 2>&1
if errorlevel 1 (
    echo [%TODAY% %TIME%] WARNING: gh workflow run failed - the Saturday crons remain as fallback >> "%WRAPPERLOG%"
    exit /b 1
)

echo [%TODAY% %TIME%] Dispatched >> "%WRAPPERLOG%"
exit /b 0
