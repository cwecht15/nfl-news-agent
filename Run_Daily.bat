@echo off
setlocal

cd /d "%~dp0"

REM NFL News Agent - build the daily report on demand.
REM
REM Two different things called "the daily report":
REM   Cloud - GitHub Actions runs the pipeline and commits data/ back to the
REM           repo, which is what nfl-news-agent.streamlit.app serves. This is
REM           the same dispatch scripts\auto_backfill_youtube.bat fires at
REM           5:30 AM. A manual dispatch is never skipped by daily.yml's guard.
REM   Local - runs the pipeline on this PC. Nothing is committed or pushed, so
REM           the live site is unchanged; only the local dashboard sees it.

echo.
echo   NFL News Agent - run the daily report
echo.
echo   [C] Cloud - dispatch daily.yml on GitHub Actions
echo               updates nfl-news-agent.streamlit.app, ~20 min, ~$0.46 tokens
echo   [L] Local - run the pipeline here (local dashboard only, no push)
echo   [Q] Quit
echo.

choice /c CLQ /n /m "Choose C, L or Q: "
if errorlevel 3 goto :done
if errorlevel 2 goto :local
goto :cloud

:cloud
where gh >nul 2>&1
if errorlevel 1 (
    echo.
    echo gh CLI was not found on PATH, so the cloud run cannot be dispatched.
    echo Trigger it by hand instead:
    echo   https://github.com/cwecht15/nfl-news-agent/actions/workflows/daily.yml
    echo.
    pause
    exit /b 1
)

echo.
echo Dispatching the cloud daily pipeline...
gh workflow run daily.yml --ref master
if errorlevel 1 (
    echo.
    echo Dispatch failed - check "gh auth status".
    echo.
    pause
    exit /b 1
)

echo.
echo Dispatched. It takes roughly 20 minutes to land on the site.
echo   Follow it:  gh run watch
echo   Or:         https://github.com/cwecht15/nfl-news-agent/actions
echo.
pause
exit /b 0

:local
echo.
echo Running the pipeline locally. This takes several minutes.
echo Output is also written to data\logs\ for this date.
echo.
"C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe" scripts\run_daily.py %*
set EXITCODE=%ERRORLEVEL%
echo.
if not "%EXITCODE%"=="0" (
    echo Pipeline exited with code %EXITCODE%.
) else (
    echo Done. Open the dashboard with Launch_Dashboard.bat to read it.
)
echo.
pause
exit /b %EXITCODE%

:done
exit /b 0
