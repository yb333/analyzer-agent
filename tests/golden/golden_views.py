#!/usr/bin/env python3
"""四视图 golden 快照 — 重构安全网。

对 tests/fixtures/analyzer/case_*/execution_tasks.xlsx 走**生产 CLI 路径**：
    analyzer.py --input case.xlsx --output tmp/out/
    view_generator.py --input knowledge_draft.json --output tmp/views/ --views all
把产物规范化成稳定 JSON 快照（剔除时间戳等不稳定字段），用于重构前后对比。

用法:
    python3 tests/golden/golden_views.py --update     # 拍/更新快照
    python3 tests/golden/golden_views.py --check      # 对比快照，输出 diff
    python3 tests/golden/golden_views.py --check --case case_03_cte_nested   # 单个 case
    python3 tests/golden/golden_views.py --check --view mapping              # 只看某视图

快照定位（重要）：
    - knowledge_draft.json / asset_report.html：视为「接近正确」的基准，重构必须零 diff
    - mapping.xlsx / sdd.xlsx / tech_design.md：现状已知有 bug，快照是「变化检测器」，
      每处 diff 需人工判读是修复还是回归
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REF_DIR = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
SNAPSHOT_DIR = PROJECT_ROOT / "tests" / "golden" / "snapshots"

# 序列化时剔除的不稳定字段（时间戳/trace）
VOLATILE_KEYS = {"analysis_time", "generated_at", "trace_id", "analysis_date"}


def _run(cmd: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def _sanitize(obj):
    """递归剔除不稳定键值。"""
    if isinstance(obj, dict):
        return {
            k: ("<TS>" if k in VOLATILE_KEYS and isinstance(v, str) else _sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    return obj


def _snapshot_xlsx(path: Path) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    out = {}
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in row])
        out[ws.title] = rows
    return out


def _snapshot_html(path: Path) -> dict:
    """asset_report.html：全文（模板+注入数据）快照，时间戳正则剔除。"""
    import re
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'("generated_at"\s*:\s*)"[^"]*"', r'\1"<TS>"', text)
    text = re.sub(r'("analysis_time"\s*:\s*)"[^"]*"', r'\1"<TS>"', text)
    return {"sha_lines": text.count("\n"), "text": text}


def generate_case(case_dir: Path, workdir: Path) -> dict | None:
    """跑一个 case 的生产管线，返回规范化快照 dict。"""
    xlsx = case_dir / "execution_tasks.xlsx"
    if not xlsx.exists():
        return None

    out_base = workdir / "out"
    r1 = _run([sys.executable, str(REF_DIR / "analyzer.py"),
               "--input", str(xlsx), "--output", str(out_base)])
    if r1.returncode != 0:
        return {"__error__": f"analyzer 失败: {r1.stderr[-2000:]}"}

    knowledge_files = list(out_base.rglob("knowledge_draft.json"))
    if not knowledge_files:
        return {"__error__": f"未产出 knowledge_draft.json: {r1.stdout[-2000:]}"}

    views_dir = workdir / "views"
    r2 = _run([sys.executable, str(REF_DIR / "view_generator.py"),
               "--input", str(knowledge_files[0]),
               "--output", str(views_dir), "--views", "all"])
    if r2.returncode != 0:
        return {"__error__": f"view_generator 失败: {r2.stderr[-2000:]}"}

    snap = {}
    knowledge = json.loads(knowledge_files[0].read_text(encoding="utf-8"))
    snap["knowledge_draft"] = _sanitize(knowledge)

    import re

    def _snapshot_md(p: Path) -> dict:
        text = p.read_text(encoding="utf-8")
        text = re.sub(r"(生成时间\s*:\s*)\d{4}-\d{2}-\d{2}[ \d:]*", r"\1<TS>", text)
        return {"text": text}

    for name, fn in [
        ("mapping.xlsx", _snapshot_xlsx),
        ("sdd.xlsx", _snapshot_xlsx),
        ("tech_design.md", _snapshot_md),
        ("asset_report.html", _snapshot_html),
    ]:
        f = views_dir / name
        if f.exists():
            snap[name] = fn(f)
        else:
            snap[name] = {"__missing__": True}
    return snap


def diff_values(path: str, old, new, diffs: list, limit: int = 400):
    """递归对比，收集差异路径。"""
    if len(diffs) >= limit:
        return
    if type(old) is not type(new):
        diffs.append(f"{path}: {old!r} -> {new!r}")
        return
    if isinstance(old, dict):
        for k in sorted(set(old) | set(new)):
            if k not in old:
                diffs.append(f"{path}.{k}: <无> -> {new[k]!r}")
            elif k not in new:
                diffs.append(f"{path}.{k}: {old[k]!r} -> <无>")
            else:
                diff_values(f"{path}.{k}", old[k], new[k], diffs, limit)
    elif isinstance(old, list):
        if len(old) != len(new):
            diffs.append(f"{path}: 行数 {len(old)} -> {len(new)}")
        for i, (a, b) in enumerate(zip(old, new)):
            diff_values(f"{path}[{i}]", a, b, diffs, limit)
    elif old != new:
        diffs.append(f"{path}: {old!r} -> {new!r}")


def _shorten(s, limit=220):
    """单条 diff 截断（全文对比的 text 差异可能巨长）。"""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…(共{len(s)}字符)"


def cell_diff_summary(diffs: list) -> list:
    """把 diff 路径压缩成可读摘要（xlsx cell 粒度）。"""
    # mapping.xlsx.属性级mapping[12][3]: 'a' -> 'b'  →  属性级mapping R13 D列
    out = []
    for d in diffs[:200]:
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="拍/更新快照")
    ap.add_argument("--check", action="store_true", help="对比快照")
    ap.add_argument("--case", default="", help="只跑指定 case（如 case_03_cte_nested）")
    ap.add_argument("--view", default="", help="只对比指定视图（如 mapping.xlsx）")
    args = ap.parse_args()

    if not args.update and not args.check:
        ap.error("需要 --update 或 --check")

    case_dirs = sorted(
        d for d in FIXTURES.iterdir()
        if d.is_dir() and d.name.startswith("case_")
        and (not args.case or d.name == args.case)
    )
    if not case_dirs:
        print(f"未找到 case: {args.case}", file=sys.stderr)
        sys.exit(2)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    total_diffs = 0
    cases_with_diff = []

    # case 之间完全独立，并行跑。负载在 CLI 子进程（subprocess.run 阻塞等待），
    # 线程池不吃 GIL，且避免 ProcessPool 在 macOS spawn 下 pickle 局部函数的问题。
    from concurrent.futures import ThreadPoolExecutor

    def _gen(case_dir: Path):
        with tempfile.TemporaryDirectory() as td:
            return case_dir.name, generate_case(case_dir, Path(td))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(_gen, case_dirs))

    for case_dir in case_dirs:
        snap = results.get(case_dir.name)
        if snap is None:
            continue
        snap_file = SNAPSHOT_DIR / f"{case_dir.name}.json"

        if args.update:
            snap_file.write_text(
                json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=False),
                encoding="utf-8")
            print(f"  [SNAP] {case_dir.name}")
            continue

        # --check
        if not snap_file.exists():
            print(f"  [MISSING-SNAP] {case_dir.name}（先跑 --update）")
            total_diffs += 1
            continue
        old = json.loads(snap_file.read_text(encoding="utf-8"))
        diffs = []
        view_filter = args.view or None
        for view in sorted(set(old) | set(snap)):
            if view_filter and view != view_filter:
                continue
            diff_values(view, old.get(view), snap.get(view), diffs)
        if diffs:
            cases_with_diff.append(case_dir.name)
            total_diffs += len(diffs)
            print(f"\n=== {case_dir.name}: {len(diffs)} 处差异 ===")
            for d in diffs[:60]:
                print(f"  {_shorten(d)}")
            if len(diffs) > 60:
                print(f"  ...（共 {len(diffs)} 处，截断）")
        else:
            print(f"  [OK] {case_dir.name}")

    print()
    if args.check:
        if total_diffs == 0:
            print("=== 全部 case 与快照一致 ===")
        else:
            print(f"=== {len(cases_with_diff)} 个 case 有差异，共 {total_diffs} 处 ===")
            sys.exit(1)


if __name__ == "__main__":
    main()
