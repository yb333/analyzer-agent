@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

REM ============================================================
REM start_telemetry.bat - Analyzer Agent Telemetry Server
REM
REM 双击运行：bat 启动 node server.js 并让其独立于 bat 窗口存活。
REM 关掉这个 bat 窗口（或终端），服务继续运行不停。
REM 停止服务：双击 stop_telemetry.bat
REM
REM 原理：纯 bat 的 start /b 在关窗口时会被 Windows 杀掉整个进程树。
REM       所以 bat 内联生成临时 vbs，用 WshShell.Run "...", 0, False
REM       让 node 进程真正脱离父进程独立存活（False = 不等待，进程独立）。
REM ============================================================

cd /d "%~dp0telemetry-server"

echo ==========================================
echo   Analyzer Agent Telemetry Server
echo ==========================================
echo.

REM --- 检测 Node.js ---
where node >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] Node.js not found. Install v22.5+ from https://nodejs.org/
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('node -v') do set "NODE_VER=%%v"
echo [OK] Node.js: !NODE_VER!

REM --- 检查端口 3000 是否已在运行 ---
netstat -aon | findstr ":3000.*LISTENING" >nul 2>&1
if !errorlevel! equ 0 (
    echo.
    echo [INFO] Port 3000 already in use. Server may already be running.
    echo   Dashboard: http://localhost:3000/
    echo   To stop: double-click stop_telemetry.bat
    echo.
    start "" http://localhost:3000/
    timeout /t 2 /nobreak >nul
    exit /b 0
)

echo Starting server on port 3000...
echo.

REM --- 内联生成临时 vbs（让 node 进程脱离 bat 窗口独立存活）---
REM 日志重定向到 telemetry.log（关闭窗口后 stdout 不可见，需落盘）
set "VBS=%TEMP%\analyzer_telemetry_%RANDOM%.vbs"
(
  echo Set WshShell = CreateObject^("WScript.Shell"^)
  echo WshShell.CurrentDirectory = "%CD%"
  echo WshShell.Run "cmd /c node server.js >> telemetry.log 2>&1", 0, False
) > "%VBS%"

REM 用 wscript 执行 vbs（vbs 立即退出，node 进程独立存活）
wscript "%VBS%"
del "%VBS%" >nul 2>&1

REM --- 等待服务就绪（最多 15 秒）---
set "READY=0"
for /l %%i in (1,1,5) do (
    timeout /t 3 /nobreak >nul
    powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:3000/api/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
    if !errorlevel! equ 0 (
        set "READY=1"
        goto :ready
    )
)

:ready
if "!READY!"=="1" (
    echo.
    echo ==========================================
    echo   Server started (running in background^)
    echo ==========================================
    echo   Dashboard: http://localhost:3000/
    echo   Endpoint:  http://YOUR_IP:3000/api/usage
    echo   Stats:     http://localhost:3000/api/stats
    echo   Log:       telemetry-server\telemetry.log
    echo.
    echo   You can close this window. Server keeps running.
    echo   To stop: double-click stop_telemetry.bat
    echo ==========================================
    echo.
    start "" http://localhost:3000/
) else (
    echo.
    echo [WARN] Server may not be ready. Check telemetry-server\telemetry.log
)

REM bat 退出（node 已脱离，不受影响）
timeout /t 3 /nobreak >nul
