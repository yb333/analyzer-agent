#!/bin/bash
# Analyzer Agent 运营埋点服务端启动脚本（macOS/Linux）
# 用法: bash start.sh  或  PORT=3000 bash start.sh
cd "$(dirname "$0")"
if [ ! -d node_modules ]; then
  echo "首次运行，安装依赖..."
  npm install
fi
exec node server.js
