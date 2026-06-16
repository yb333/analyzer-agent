#!/usr/bin/env python3
"""Skill script dispatcher.

Usage:
    python run.py <script_name> [script_args...]

Examples:
    python run.py analyzer --input execution_tasks.xlsx --output output/
    python run.py view_generator --input knowledge_final.json --output output/
"""

import sys
import subprocess
from pathlib import Path

# 命令别名映射（用户/AI 可能用简称）
ALIAS_MAP = {
    "analyze": "analyzer",     # analyze → analyzer.py
    "view": "view_generator",  # view → view_generator.py
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        references_dir = Path(__file__).resolve().parent / "references"
        print("Usage: python run.py <script_name> [script_args...]")
        print(f"\nAvailable scripts in references/:")
        for f in sorted(references_dir.glob("*.py")):
            print(f"  - {f.stem}")
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    script_name = sys.argv[1]
    # 别名映射
    script_name = ALIAS_MAP.get(script_name, script_name)
    if not script_name.endswith(".py"):
        script_name += ".py"

    script_path = Path(__file__).resolve().parent / "references" / script_name
    if not script_path.exists():
        print(f"Error: Script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run([sys.executable, str(script_path)] + sys.argv[2:])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
