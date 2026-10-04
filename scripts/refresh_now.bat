@echo off
REM NFL News Agent - Refresh Now
REM One double-click (or Ctrl+Alt+N with the desktop shortcut) pulls the two
REM time-sensitive things on demand: the injury report / game designations and
REM the practice-squad elevation + inactives check. Both run in the cloud and
REM commit back to the repo, which is what the dashboard reads.
REM
REM Create the desktop shortcut with:
REM   python scripts\setup_scheduler.py shortcut

cd /d "C:\Users\cwech\Documents\Claude\Projects\NFL_News_Agent"

REM Not wmic: Windows 11 dropped it (see auto_backfill_youtube.bat).
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%I
if not defined TODAY set TODAY=undated
if not exist "data\logs" mkdir "data\logs"
set WRAPPERLOG=data\logs\%TODAY%-refresh-task.log

echo.
echo   NFL News Agent - refreshing injury report and elevations...
echo.
echo [%TODAY% %TIME%] Refresh Now: dispatching injuries.yml + inactives.yml >> "%WRAPPERLOG%"

set FAILED=0
call :dispatch injuries.yml "Injury report / designations"
call :dispatch inactives.yml "Elevations + inactives"

if "%FAILED%"=="0" (
    echo.
    echo   Both started. They take about 3 minutes; the dashboard updates when they finish.
) else (
    echo.
    echo   Something did not start - see %WRAPPERLOG%.
)
REM Keeps the window readable on a double-click; skipped when stdin is redirected.
timeout /t 8 >nul 2>nul
exit /b %FAILED%

:dispatch
gh workflow run %~1 --ref master >> "%WRAPPERLOG%" 2>&1
if errorlevel 1 (
    echo   [FAILED]  %~2
    echo [%TODAY% %TIME%] WARNING: gh workflow run %~1 failed >> "%WRAPPERLOG%"
    set FAILED=1
) else (
    echo   [started] %~2
    echo [%TODAY% %TIME%] Dispatched %~1 >> "%WRAPPERLOG%"
)
exit /b 0
