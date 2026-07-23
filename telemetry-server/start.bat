@echo off
REM Analyzer Agent 运营埋点服务端启动脚本（Windows）
cd /d "%~dp0"
if not exist node_modules (
  echo 首次运行，安装依赖...
  call npm install
)
node server.js
pause
