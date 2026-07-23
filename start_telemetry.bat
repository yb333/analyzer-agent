@echo off
chcp 65001 >nul 2>&1

REM ============================================================
REM start_telemetry.bat - Analyzer Agent Telemetry Server
REM
REM 双击运行：bat 作为入口，内部调 start_telemetry.vbs 实现静默分离。
REM bat 窗口一闪而过，服务在后台独立运行，关掉任何窗口都不停。
REM 停止服务：双击 stop_telemetry.bat
REM ============================================================

cd /d "%~dp0"

REM 启动 vbs（静默分离：隐藏窗口 + 进程独立）
REM wscript 执行 vbs 后立即返回，bat 窗口随之关闭，node 在后台继续跑
start "" wscript "%~dp0start_telemetry.vbs"

REM bat 自身立即退出（不阻塞，不留窗口）
exit
