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
exit /b %EXITCODE%
