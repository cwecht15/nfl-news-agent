@echo off
REM NFL News Agent — Daily Run Wrapper
REM Called by Windows Task Scheduler
REM Uses conda env Python directly so activate.bat isn't needed.

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"

REM Capture today's date as YYYY-MM-DD for the wrapper log filename.
REM Wrapper log is separate from the pipeline's own log so Task Scheduler
REM crashes (before/around the pipeline) are still recoverable.
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DATETIME=%%I
set TODAY=%DATETIME:~0,4%-%DATETIME:~4,2%-%DATETIME:~6,2%

if not exist "data\logs" mkdir "data\logs"
set WRAPPERLOG=data\logs\%TODAY%-task.log

echo [%TODAY% %TIME%] Task Scheduler triggered run_daily.bat >> "%WRAPPERLOG%"
"C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe" scripts\run_daily.py >> "%WRAPPERLOG%" 2>&1
set EXITCODE=%ERRORLEVEL%
echo [%TODAY% %TIME%] Pipeline exited with code %EXITCODE% >> "%WRAPPERLOG%"
exit /b %EXITCODE%
