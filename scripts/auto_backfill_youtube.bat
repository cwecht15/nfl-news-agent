@echo off
REM NFL News Agent — Auto YouTube Backfill Wrapper
REM Called by Windows Task Scheduler
REM Captions-only catch-up + git push to master.

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DATETIME=%%I
set TODAY=%DATETIME:~0,4%-%DATETIME:~4,2%-%DATETIME:~6,2%

if not exist "data\logs" mkdir "data\logs"
set WRAPPERLOG=data\logs\%TODAY%-yt-backfill-task.log

echo [%TODAY% %TIME%] Task Scheduler triggered auto_backfill_youtube.bat >> "%WRAPPERLOG%"
"C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe" scripts\auto_backfill_youtube.py >> "%WRAPPERLOG%" 2>&1
set EXITCODE=%ERRORLEVEL%
echo [%TODAY% %TIME%] Auto-backfill exited with code %EXITCODE% >> "%WRAPPERLOG%"

REM Kick the cloud daily pipeline now. GitHub's cron scheduler has been firing
REM this repo 3-5 hours late every day (median 242 min over 9 runs), while a
REM workflow_dispatch starts in seconds. daily.yml keeps a 10:41 UTC cron as a
REM fallback for days this machine is off, and skips itself when this dispatch
REM already produced the report.
REM
REM Run this unconditionally: the cloud report ignores transcripts entirely, so
REM a backfill failure must not also cost us the day's report.
echo [%TODAY% %TIME%] Dispatching cloud daily pipeline >> "%WRAPPERLOG%"
gh workflow run daily.yml --ref master >> "%WRAPPERLOG%" 2>&1
if errorlevel 1 (
    echo [%TODAY% %TIME%] WARNING: gh workflow run failed - the 10:41 UTC cron fallback will cover it >> "%WRAPPERLOG%"
) else (
    echo [%TODAY% %TIME%] Cloud daily pipeline dispatched >> "%WRAPPERLOG%"
)

exit /b %EXITCODE%
