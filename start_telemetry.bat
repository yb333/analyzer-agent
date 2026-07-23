@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

REM ============================================================
REM start_telemetry.bat - Analyzer Agent Telemetry Server
REM
REM Double-click to run. Auto:
REM   1. Check Node.js
REM   2. npm install (fallback: copy from ETL_opencode_ai if compile fails)
REM   3. Start server on port 3000
REM   4. Open browser
REM ============================================================

cd /d "%~dp0telemetry-server"

echo ==========================================
echo   Analyzer Agent Telemetry Server
echo ==========================================
echo.

REM --- Check Node.js ---
where node >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org/
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('node -v') do set "NODE_VER=%%v"
echo [OK] Node.js: !NODE_VER!
echo.

REM --- Install deps if missing ---
if not exist "node_modules" (
    echo Installing dependencies...
    echo.
    call npm install 2>&1
    if not exist "node_modules" (
        echo.
        echo [WARN] npm install failed. Trying to copy from ETL_opencode_ai...
        if exist "%USERPROFILE%\ETL_opencode_ai\telemetry-server\node_modules" (
            xcopy "%USERPROFILE%\ETL_opencode_ai\telemetry-server\node_modules" "node_modules\" /E /I /Q /Y >nul 2>&1
            if exist "node_modules\better-sqlite3" (
                echo [OK] Copied node_modules from ETL_opencode_ai
            ) else (
                echo [ERROR] Copy failed. Please manually run: npm install
                echo.
                pause
                exit /b 1
            )
        ) else (
            echo [ERROR] ETL_opencode_ai node_modules not found either.
            echo Please manually run in telemetry-server: npm install
            echo.
            pause
            exit /b 1
        )
    )
    echo.
    echo [OK] Dependencies ready
    echo.
)

REM --- Start server ---
echo Starting server on port 3000...
echo.
echo   Dashboard: http://localhost:3000/
echo   Endpoint:  http://YOUR_IP:3000/api/usage
echo   Stats:     http://localhost:3000/api/stats
echo.
echo   Press Ctrl+C to stop.
echo ==========================================
echo.

REM Open browser after 3s (background, non-blocking)
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:3000/"

REM Start node (foreground)
node server.js

echo.
echo Server stopped.
pause
