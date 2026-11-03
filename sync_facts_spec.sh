#!/usr/bin/env bash
# sync_facts_spec.sh — 薄入口（开发环境测试；内网 Windows 实际运行用 sync_facts_spec.bat）
# 核心逻辑在 sync_facts_spec.py。不带参数 = 直接同步；带参数 = 原样透传。
SCRIPT="$(cd "$(dirname "$0")" && pwd)/sync_facts_spec.py"

if [ $# -eq 0 ]; then
    python3 "$SCRIPT"
else
    python3 "$SCRIPT" "$@"
fi
