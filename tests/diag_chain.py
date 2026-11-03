#!/usr/bin/env python3
"""子查询代入链全环节诊断（内网一条命令定位断裂点）。

用法:
    python tests/diag_chain.py {制品目录}
    python tests/diag_chain.py {制品目录} --verbose   # 逐步展开过程
"""
import sys, json, io, contextlib
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dws-pipeline-analyzer" / "references"))

import sqlglot
from sqlglot import exp as exp
print(f"sqlglot = {sqlglot.__version__}")
print(f"python  = {sys.version.split()[0]} ({sys.platform})")
print(f"cwd     = {Path.cwd()}")
print()

verbose = "--verbose" in sys.argv or "-v" in sys.argv
dir_arg = [a for a in sys.argv[1:] if not a.startswith("-")][0] if len(sys.argv) > 1 else "."

from pipeline_core import run_from_dir
with contextlib.redirect_stdout(io.StringIO()):
    core = run_from_dir(dir_arg, include_dq=False, channel="spec", entry_title="")

k = core["knowledge"]
eng = core["engine_ctx"]

# ── Step 1: 引擎 AST 环节 ──
print("=" * 60)
print("Step 1: 引擎 AST（extract_subquery_field_chains 输入）")
print("=" * 60)
from engine import parse_single_sql, _parse_sqlglot_cached
raw = core["knowledge"]["source"]["rules"][0].get("query_sql", "") if core["knowledge"]["source"].get("rules") else ""
# 从 data_flow steps 拿 raw_sql（更可靠）
for s in k["data_flow"]["steps"]:
    parsed = parse_single_sql(s.get("raw_sql", ""), "dws")
    tree = _parse_sqlglot_cached(parsed.clean_sql, "oracle")
    subs = []
    if tree:
        for sq in tree.find_all(exp.Subquery):
            a = (sq.alias or "").lower()
            subs.append(a)
    print(f"  {s.get('rule_code')}: Subquery nodes = {subs}")
    print(f"    clean_sql 前60字 = {parsed.clean_sql[:60]}…")
    print(f"    parse_error = {repr(parsed.parse_error)}")
    if verbose and tree:
        for sq in tree.find_all(exp.Subquery):
            a = (sq.alias or "").lower()
            inner = sq.this if isinstance(sq.this, exp.Select) else sq.find(exp.Select)
            if inner and verbose:
                print(f"    [{a}] SELECT items:")
                for node in inner.expressions[:5]:
                    print(f"      {type(node).__name__:8s} alias={getattr(node, 'alias', ''):20s} sql={node.sql()[:60]}")

# ── Step 2: 引擎链产出 ──
print()
print("=" * 60)
print("Step 2: 引擎链（engine_ctx.subquery_field_chains）")
print("=" * 60)
chains = eng.get("subquery_field_chains", {})
sqls = eng.get("subquery_sqls", {})
print(f"  subquery_sqls 别名: {sorted(set(a for v in sqls.values() for a in v))}")
if not chains:
    print("  ⚠️ chains 为空——引擎未产出任何链（AST 或 clean_sql 问题）")
for sid, fs in chains.items():
    for fname, chain in fs.items():
        hops = [e["alias"] for e in chain]
        print(f"  {fname}: {len(hops)} 跳 [{' → '.join(hops)}]")
        for i, e in enumerate(chain):
            print(f"    [{i}] {e['alias']}.{e['field']} = {e['expression'][:60]}…")
            print(f"        refs = {[(r['alias'], r['field']) for r in e['refs']]}")

# ── Step 3: SPEC 侧消费 ──
print()
print("=" * 60)
print("Step 3: SPEC 侧（is_sq_alias 判定 + 代入）")
print("=" * 60)
from spec_export import build_spec_model
model = build_spec_model(k, eng)
for r in model["sheet4"]:
    fname = r[10]
    logic = r[6] or ""
    if "tt." in logic or "b." in logic or fname == "is_datacom_flag":
        status = "✅ 全链展开" if "tt." not in logic else "❌ 断在 b 层（tt.is_datacom_flag 未代入）"
        print(f"  {fname}: {status}")
        print(f"    logic = {logic[:100]}…")
        print(f"    源表 = {r[2]} | 别名 = {r[3]} | 源字段 = {r[4]}")

print()
print("=" * 60)
print("结论")
print("=" * 60)
if not chains:
    print("  Step 2 空 → 引擎 AST 环节断裂（查 sqlglot 版本 / clean_sql / parse_error）")
elif any(len(c) < 2 for fs in chains.values() for c in fs.values()):
    print("  Step 2 有单跳链 → 引擎 BFS 未到达 tt 层（查 sqlglot Subquery 识别）")
else:
    print("  Step 2 全部多跳 → 引擎侧正常；如 Step 3 仍断链，查 SPEC 消费路径")
