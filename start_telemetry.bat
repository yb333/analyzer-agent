@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM ============================================================
REM start_telemetry.bat — Analyzer Agent 运营埋点服务端一键启动
REM
REM 双击即可运行。自动完成：
REM   1. 检测 Node.js 是否安装
REM   2. 首次运行自动 npm install 装依赖
REM   3. 启动服务端（监听 3000 端口）
REM   4. 浏览器自动打开看板
REM
REM 服务端代码在 telemetry-server/ 目录下，不随内网同步分发。
REM ============================================================

REM 切到本脚本所在目录的 telemetry-server 子目录
cd /d "%~dp0telemetry-server"

echo ═══════════════════════════════════════════════
echo   Analyzer Agent 运营埋点服务端
echo ═══════════════════════════════════════════════
echo.

REM ── 检测 Node.js ──
where node >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] 未检测到 Node.js，请先安装：https://nodejs.org/
    echo         装完重开命令行再双击本脚本。
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('node -v') do set "NODE_VER=%%v"
echo [OK] Node.js 版本: !NODE_VER!
echo.

REM ── 检测依赖是否已装 ──
if not exist "node_modules" (
    echo 首次运行，安装依赖（better-sqlite3）...
    echo.
    call npm install
    if !errorlevel! neq 0 (
        echo.
        echo [ERROR] 依赖安装失败。请手动在 telemetry-server 目录执行 npm install
        echo.
        pause
        exit /b 1
    )
    echo.
    echo [OK] 依赖安装完成
    echo.
)

REM ── 启动服务 ──
echo 启动服务端（端口 3000）...
echo.
echo   看板地址: http://localhost:3000/
echo   上报地址: http://本机IP:3000/api/usage
echo   统计接口: http://localhost:3000/api/stats
echo.
echo   按 Ctrl+C 停止服务。
echo ═══════════════════════════════════════════════
echo.

REM 3 秒后自动打开浏览器（后台启动，不阻塞 node）
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:3000/"

REM 启动 node（前台，Ctrl+C 退出）
node server.js

echo.
echo 服务已停止。
pause
