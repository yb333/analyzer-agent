#!/usr/bin/env python3
"""DWS Pipeline Analyzer — 自包含入口脚本。

自动检测 venv，无需手动指定 Python 路径。
跨平台支持 macOS / Linux / Windows。

用法:
    python dws_analyzer.py analyze --input execution_tasks.xlsx --output output/
    python dws_analyzer.py view_generator --input knowledge_final.json --output output/

安装后也可通过 dws-run 调用:
    dws-run analyzer analyze --input execution_tasks.xlsx --output output/
"""

import sys
import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_venv_python():
    """查找 venv 中的 Python（跨平台）。

    优先级：
    1. 同级 .venv/（local 模式）
    2. ~/.config/opencode/venv/（global 模式）
    3. sys.executable（fallback 到当前 Python）
    """
    candidates = [
        SCRIPT_DIR / ".venv",                          # local venv
        Path.home() / ".config" / "opencode" / "venv",  # global venv
    ]

    for venv_dir in candidates:
        if os.name == "nt":
            py = venv_dir / "Scripts" / "python.exe"
        else:
            py = venv_dir / "bin" / "python"
        if py.exists():
            return str(py)

    return sys.executable


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("\n可用命令:")
        print("  analyze         分析制品包")
        print("  view_generator  生成视图")
        sys.exit(0)

    action = sys.argv[1]
    args = sys.argv[2:]

    # 映射 action → references/ 脚本
    SCRIPT_MAP = {
        "analyze": "analyzer.py",
        "view_generator": "view_generator.py",
        "analyzer": "analyzer.py",
        "view": "view_generator.py",
    }

    script_file = SCRIPT_MAP.get(action)
    if not script_file:
        script_file = action if action.endswith(".py") else f"{action}.py"

    script_path = SCRIPT_DIR / "references" / script_file
    if not script_path.exists():
        print(f"Error: Script not found: {script_path}", file=sys.stderr)
        print(f"Available: analyze, view_generator", file=sys.stderr)
        sys.exit(1)

    # 用 venv Python 执行（确保有 openpyxl/sqlglot）
    venv_python = _find_venv_python()
    result = subprocess.run([venv_python, str(script_path)] + args)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
