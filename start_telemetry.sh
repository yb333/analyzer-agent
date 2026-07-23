#!/bin/bash
# ============================================================
# start_telemetry.sh — Analyzer Agent 运营埋点服务端一键启动（macOS/Linux）
#
# 用法: bash start_telemetry.sh
# 双击或终端运行。自动装依赖 + 启动 + 打开浏览器。
# ============================================================

set -e
cd "$(dirname "$0")/telemetry-server"

echo "═══════════════════════════════════════════════"
echo "  Analyzer Agent 运营埋点服务端"
echo "═══════════════════════════════════════════════"
echo

# 检测 Node.js
if ! command -v node &>/dev/null; then
    echo "[ERROR] 未检测到 Node.js，请先安装：https://nodejs.org/"
    exit 1
fi
echo "[OK] Node.js 版本: $(node -v)"
echo

# 装依赖
if [ ! -d node_modules ]; then
    echo "首次运行，安装依赖（better-sqlite3）..."
    npm install
    echo "[OK] 依赖安装完成"
    echo
fi

# 启动
echo "启动服务端（端口 3000）..."
echo
echo "  看板地址: http://localhost:3000/"
echo "  上报地址: http://本机IP:3000/api/usage"
echo
echo "  按 Ctrl+C 停止服务。"
echo "═══════════════════════════════════════════════"
echo

# 3 秒后打开浏览器
(sleep 3 && (open http://localhost:3000/ 2>/dev/null || xdg-open http://localhost:3000/ 2>/dev/null)) &

node server.js
