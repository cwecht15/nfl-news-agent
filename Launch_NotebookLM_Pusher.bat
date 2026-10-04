@echo off
setlocal

cd /d "%~dp0"

rem Use the project's conda env python explicitly (has streamlit/pandas/yaml).
set "PY=C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe"
if not exist "%PY%" set "PY=python"

where node >nul 2>&1
if errorlevel 1 (
    echo Node.js was not found on PATH.
    echo Install Node.js 20+ from https://nodejs.org/  ^(the app runs the
    echo NotebookLM engine itself via tools\notebooklm-mcp^).
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo  NotebookLM Team Pusher
echo ============================================================
echo.
echo  Python: %PY%
echo  App URL: http://localhost:8503
echo.
echo  Self-contained: it starts the NotebookLM engine itself
echo  (tools\notebooklm-mcp, stdio) - no separate server, no :3000.
echo  First time only, authenticate once:
echo      cd tools\notebooklm-mcp ^&^& npm run setup-auth
echo.
echo  Starting Streamlit... leave this window open. Close it to stop.
echo.

start "" cmd /c "timeout /t 7 /nobreak >nul && start http://localhost:8503"

"%PY%" -m streamlit run notebooklm_pusher\app.py --server.address localhost --server.port 8503 --server.headless true

echo.
echo Streamlit exited. Press any key to close this window.
pause >nul
