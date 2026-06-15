#!/usr/bin/env bash
# DWS Skills 安装器 (macOS/Linux)
# 核心逻辑在 install.py 中，此文件只是找到 python3 然后调用

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 找 Python 3.10+
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    if "$cmd" -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "未找到 Python 3.10+"
  echo "  macOS:  brew install python3"
  echo "  Ubuntu: sudo apt install python3 python3-venv"
  exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/install.py" "$@"
