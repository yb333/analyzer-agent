#!/usr/bin/env python3
"""内网诊断：一条命令定位子查询代入链各环节是否正常。

用法（内网 Windows 同款）:
    python tests/diag_chain.py {制品目录}
"""
import sys, json
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dws-pipeline-analyzer" / "references"))

import sqlglot
print(f"sqlglot version: {sqlglot.__version__}")
if sqlglot.__version__ != "28.10.1":
    print(f"  ⚠️ 版本非 28.10.1（本地已验证版本），嵌套子查询 AST 可能有差异")

from pipeline_core import run_from_dir
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    core = run_from_dir(sys.argv[1], include_dq=False, channel="spec", entry_title="")
eng = core["engine_ctx"]
chains = eng.get("subquery_field_chains", {})
sqls = eng.get("subquery_sqls", {})
print(f"subquery_sqls 别名: {sorted(set(a for v in sqls.values() for a in v))}")
for sid, fs in chains.items():
    for fname, chain in fs.items():
        hops = [e["alias"] for e in chain]
        expr = chain[0]["expression"][:60]
        print(f"  {fname}: {len(hops)} 跳 [{' → '.join(hops)}]  首跳={expr}…")
        if len(hops) < 2:
            print(f"    ⚠️ 单跳——tt 层代入断裂（检查 sqlglot 版本 / 上面别名列表是否含 tt）")
print(f"\n无链字段 = 穿透成功或保守呈现；单跳 = 版本问题")
