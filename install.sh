#!/usr/bin/env bash
set -euo pipefail

# DWS Skills 安装器 — macOS / Linux
#
# 用法:
#   bash install.sh                          # 安装所有 skill（全局）
#   bash install.sh dws-pipeline-analyzer     # 只装指定的 skill
#   bash install.sh -l                        # 安装到当前项目 .opencode/（默认全局）
#
# 用户场景: 下载仓库 zip → 解压 → bash install.sh → 完事

MODE="global"
SKILL_FILTER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -l|--local)  MODE="local"; shift ;;
    -h|--help)
      echo "用法: bash install.sh [skill名称] [-l]"
      echo ""
      echo "  无参数       安装所有 skill"
      echo "  指定名称     只装该 skill（如 dws-pipeline-analyzer）"
      echo "  -l          装到当前项目 .opencode/ 而非全局"
      exit 0 ;;
    *)  SKILL_FILTER="$1"; shift ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "  DWS Skills 安装器 (macOS/Linux)"
echo "=========================================="
echo ""

# ── 1. 扫描可用 skill ──
ALL_SKILLS=()
for d in "$SCRIPT_DIR"/*/; do
  name=$(basename "$d")
  # 跳过非 skill 目录（没有 SKILL.md 的不算）
  if [ -f "$d/SKILL.md" ]; then
    ALL_SKILLS+=("$name")
  fi
done

if [ ${#ALL_SKILLS[@]} -eq 0 ]; then
  echo "未找到任何 skill（需要 SKILL.md 文件）"
  exit 1
fi

# 过滤
if [ -n "$SKILL_FILTER" ]; then
  SKILLS=()
  for s in "${ALL_SKILLS[@]}"; do
    if [[ "$s" == *"$SKILL_FILTER"* ]]; then
      SKILLS+=("$s")
    fi
  done
  if [ ${#SKILLS[@]} -eq 0 ]; then
    echo "未找到匹配 '$SKILL_FILTER' 的 skill"
    echo "可用: ${ALL_SKILLS[*]}"
    exit 1
  fi
else
  SKILLS=("${ALL_SKILLS[@]}")
fi

echo "将安装 ${#SKILLS[@]} 个 skill: ${SKILLS[*]}"
[ "$MODE" = "local" ] && echo "模式: 项目级 (.opencode/)" || echo "模式: 全局 (~/.config/opencode/)"
echo ""

# ── 2. 检测 Python ──
echo "[1/4] 检测 Python..."
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    PY_OK=$("$cmd" -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>/dev/null || echo "0")
    if [ "$PY_OK" = "1" ]; then
      PYTHON="$cmd"
      break
    fi
  fi
done
if [ -z "$PYTHON" ]; then
  echo "  未找到 Python 3.10+，请先安装"
  echo "    macOS:  brew install python3"
  echo "    Ubuntu: sudo apt install python3 python3-venv"
  echo "    CentOS: sudo yum install python3"
  exit 1
fi
echo "  $PYTHON ($($PYTHON --version 2>&1))"

# ── 3. venv + 依赖 ──
echo ""
echo "[2/4] 安装 Python 依赖..."
if [ "$MODE" = "global" ]; then
  VENV_DIR="$HOME/.config/opencode/venv"
else
  VENV_DIR="$SCRIPT_DIR/.venv"
fi
[ ! -d "$VENV_DIR" ] && "$PYTHON" -m venv "$VENV_DIR"
VENV_PY="$VENV_DIR/bin/python"

# 汇总所有 skill 的 requirements.txt
ALL_REQS=""
for s in "${SKILLS[@]}"; do
  req="$SCRIPT_DIR/$s/requirements.txt"
  [ -f "$req" ] && ALL_REQS="$ALL_REQS $(cat "$req" | grep -v '^#' | grep -v '^$' | tr '\n' ' ')"
done
# 去重
UNIQUE_REQS=$(echo "$ALL_REQS" | tr ' ' '\n' | sort -u | grep -v '^$' | tr '\n' ' ')

if [ -n "$UNIQUE_REQS" ]; then
  "$VENV_PY" -m pip install --upgrade pip --quiet 2>/dev/null || true
  "$VENV_PY" -m pip install $UNIQUE_REQS --quiet
  echo "  已安装: $UNIQUE_REQS"
fi

# ── 4. 复制 skill 文件 ──
echo ""
echo "[3/4] 安装 skill 文件..."
if [ "$MODE" = "global" ]; then
  DEST_BASE="$HOME/.config/opencode"
else
  DEST_BASE=".opencode"
fi
SKILLS_DIR="$DEST_BASE/skills"
COMMANDS_DIR="$DEST_BASE/commands"
mkdir -p "$SKILLS_DIR" "$COMMANDS_DIR"

for s in "${SKILLS[@]}"; do
  src="$SCRIPT_DIR/$s"
  dst="$SKILLS_DIR/$s"
  mkdir -p "$dst"
  # 复制所有文件（排除 .git, __pycache__, .venv）
  rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.venv' --exclude='.DS_Store' "$src/" "$dst/" 2>/dev/null || \
    cp -R "$src/." "$dst/" 2>/dev/null
  # 清理 __pycache__
  find "$dst" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  echo "  ✓ $s"
done

# 复制命令文件（仓库根 commands/ 目录）
if [ -d "$SCRIPT_DIR/commands" ]; then
  for cmd_file in "$SCRIPT_DIR"/commands/*.md; do
    [ -f "$cmd_file" ] || continue
    fname=$(basename "$cmd_file")
    cp "$cmd_file" "$COMMANDS_DIR/$fname" 2>/dev/null && echo "  ✓ 命令: $fname"
  done
fi

# ── 5. dws-run（如果有 dws-run.py 就更新 SKILL_MAP）──
echo ""
echo "[4/4] 检查 dws-run..."
DWS_RUN="$SKILLS_DIR/dws-run.py"
if [ -f "$DWS_RUN" ]; then
  for s in "${SKILLS[@]}"; do
    if ! grep -q "\"$s\"" "$DWS_RUN" 2>/dev/null; then
      # 尝试注册短名
      short="${s#dws-pipeline-}"
      if grep -q "\"$short\":" "$DWS_RUN" 2>/dev/null; then
        sed -i.bak "s|\"$short\": \"$s-sql-analyzer\"|\"$short\": \"$s\"|" "$DWS_RUN" 2>/dev/null || \
        sed -i '' "s|\"$short\": \"$s-sql-analyzer\"|\"$short\": \"$s\"|" "$DWS_RUN" 2>/dev/null || true
        rm -f "$DWS_RUN.bak" 2>/dev/null || true
      fi
    fi
  done
  echo "  ✓ dws-run 已就绪"
else
  echo "  (无 dws-run.py，skill 仍可通过 python run.py 直接调用)"
fi

# ── 完成 ──
echo ""
echo "=========================================="
echo "  安装完成！"
echo "=========================================="
echo ""
echo "Python 依赖: $UNIQUE_REQS"
echo "安装位置: $SKILLS_DIR"
echo ""
echo "在 opencode 中使用:"
echo "  /analyze @execution_tasks.xlsx"
echo ""
