#!/bin/bash
# ============================================================
# start_telemetry.sh - Analyzer Agent Telemetry Server (macOS/Linux)
#
# Usage: bash start_telemetry.sh
# Auto: check node -> npm install (fallback copy) -> start -> open browser
# ============================================================

set -e
cd "$(dirname "$0")/telemetry-server"

echo "═══════════════════════════════════════════════"
echo "  Analyzer Agent Telemetry Server"
echo "═══════════════════════════════════════════════"
echo

# --- Check Node.js ---
if ! command -v node &>/dev/null; then
    echo "[ERROR] Node.js not found. Install from https://nodejs.org/"
    exit 1
fi
echo "[OK] Node.js: $(node -v)"
echo

# --- Install deps if missing ---
if [ ! -d node_modules ]; then
    echo "Installing dependencies..."
    npm install 2>&1 || true
    if [ ! -d node_modules/better-sqlite3 ]; then
        echo
        echo "[WARN] npm install failed. Trying to copy from ETL_opencode_ai..."
        REF="$HOME/ETL_opencode_ai/telemetry-server/node_modules"
        if [ -d "$REF/better-sqlite3" ]; then
            cp -R "$REF" ./node_modules
            echo "[OK] Copied node_modules from ETL_opencode_ai"
        else
            echo "[ERROR] Cannot find prebuilt node_modules."
            echo "Please manually run in telemetry-server: npm install"
            exit 1
        fi
    fi
    echo
    echo "[OK] Dependencies ready"
    echo
fi

# --- Start server ---
echo "Starting server on port 3000..."
echo
echo "  Dashboard: http://localhost:3000/"
echo "  Endpoint:  http://YOUR_IP:3000/api/usage"
echo "  Stats:     http://localhost:3000/api/stats"
echo
echo "  Press Ctrl+C to stop."
echo "═══════════════════════════════════════════════"
echo

# Open browser after 3s (background)
(sleep 3 && (open http://localhost:3000/ 2>/dev/null || xdg-open http://localhost:3000/ 2>/dev/null)) &

node server.js
