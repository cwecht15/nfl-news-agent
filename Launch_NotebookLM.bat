@echo off
setlocal

cd /d "%~dp0\tools\notebooklm-mcp"

where node >nul 2>&1
if errorlevel 1 (
    echo Node.js was not found on PATH.
    echo Install Node.js 20+ from https://nodejs.org/
    echo.
    pause
    exit /b 1
)

if not exist "dist" (
    echo First run: building notebooklm-mcp...
    call npm run build
    if errorlevel 1 (
        echo Build failed. Try: cd tools\notebooklm-mcp ^&^& npm install
        pause
        exit /b 1
    )
)

echo Starting NotebookLM MCP HTTP server on http://localhost:3000
echo Leave this window open while you push videos from the dashboard.
echo Close it to stop the server.
echo.
echo If this is your first run, you need to authenticate first:
echo   1. Close this window (Ctrl+C)
echo   2. cd tools\notebooklm-mcp
echo   3. npm run setup-auth   ^(opens Chrome, log into Google^)
echo   4. Re-run this .bat
echo.

call npm run start:http

if errorlevel 1 (
    echo.
    echo Server stopped with an error.
    pause
)
