@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python or open this project from a shell where Python is available.
    echo.
    pause
    exit /b 1
)

echo Starting NFL News Agent dashboard...
echo.
echo Dashboard URL: http://localhost:8501
echo Leave this window open while the app is running.
echo Close it to stop the dashboard.
echo.

start "" cmd /c "timeout /t 5 /nobreak >nul && start http://localhost:8501"

python -m streamlit run dashboard\app.py --server.address localhost --server.port 8501

if errorlevel 1 (
    echo.
    echo Streamlit stopped with an error.
    pause
)
