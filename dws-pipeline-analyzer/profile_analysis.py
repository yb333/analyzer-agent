#!/usr/bin/env python3
"""分析性能诊断脚本。

用法（在代码仓根目录或任意位置运行）：
    python3 dws-pipeline-analyzer/profile_analysis.py <规则组目录>

例如：
    python3 dws-pipeline-analyzer/profile_analysis.py BFT/BftWideTable/P_TRADE/SUB_TRADE/DWB_TRADE_ORDER_D/

会分阶段计时，输出哪个阶段慢。把输出贴给开发者定位瓶颈。
"""

import sys
import time
from pathlib import Path

# 加载引擎
SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR / "references"))

import engine
from analyzer import read_yml, detect_dialect


# ── 计时包装器 ──
_timings = {}
_call_counts = {}

def _wrap(name, fn):
    def wrapper(*args, **kwargs):
        t0 = time.time()
        try:
            return fn(*args, **kwargs)
        finally:
            elapsed = time.time() - t0
            _timings[name] = _timings.get(name, 0) + elapsed
            _call_counts[name] = _call_counts.get(name, 0) + 1
    return wrapper


# 包装所有关键函数
for fn_name in [
    "parse_single_sql", "build_topology", "build_data_flow",
    "build_table_catalog", "build_field_mappings",
    "enrich_join_key_lineage", "enrich_field_physical_sources",
    "build_structured_step_summary", "build_data_blocks",
    "analyze_quality", "check_type_consistency",
    # DDL 相关
    "parse_ddl_for_metadata",
    # 子查询/分支穿透（可能重复解析）
    "_resolve_branch_columns_physical", "_trace_subquery_sources",
    "_penetrate_subquery_columns", "_extract_ctes",
    "_apply_cte_penetration", "_extract_field_usage",
    "_enhance_with_query_unit",
]:
    if hasattr(engine, fn_name):
        setattr(engine, fn_name, _wrap(fn_name, getattr(engine, fn_name)))


def main():
    if len(sys.argv) < 2:
        print("用法: python profile_analysis.py <规则组目录>")
        print("例如: python profile_analysis.py BFT/BftWideTable/.../DWB_TRADE_F/")
        sys.exit(1)

    group_dir = sys.argv[1]
    group_path = Path(group_dir)
    if not group_path.is_dir():
        print(f"[ERROR] 目录不存在: {group_dir}")
        sys.exit(1)

    print(f"分析目录: {group_dir}")
    print(f"=" * 60)

    # ── 阶段1: read_yml ──
    t0 = time.time()
    raw = read_yml(group_dir)
    t_yml = time.time() - t0
    rules = raw.get("rules", [])
    tf = raw.get("target_fields", {})
    gv = raw.get("group_variables", {})
    print(f"[read_yml]                {t_yml:.3f}s  ({len(rules)}规则, {sum(len(v) for v in tf.values())}目标字段)")

    if not rules:
        print("[ERROR] 未读到规则")
        sys.exit(1)

    # ── 阶段2: detect_dialect ──
    t0 = time.time()
    dialect = detect_dialect(rules)
    t_dialect = time.time() - t0
    print(f"[detect_dialect]          {t_dialect:.3f}s  (dialect={dialect})")

    # ── 阶段3: DDL 发现 ──
    _timings.clear()
    _call_counts.clear()
    engine.clear_sql_ast_cache()

    t0 = time.time()
    # 复刻 analyzer main 的 DDL 发现逻辑
    ddl_dir = ""
    try:
        repo_root = None
        current = Path(group_dir).resolve()
        for parent in [current] + list(current.parents):
            if (parent / "BFT").is_dir() and (parent / "DDL").is_dir():
                repo_root = parent
                break
        if repo_root:
            from analyzer import _auto_discover_ddl_from_repo
            ddl_dir = _auto_discover_ddl_from_repo(rules, Path(group_dir)) or ""
    except Exception as e:
        print(f"[DDL发现]                 失败: {e}")
    t_ddl = time.time() - t0
    print(f"[DDL发现]                 {t_ddl:.3f}s  (ddl_dir={ddl_dir or '无'})")

    # ── 阶段4: analyze_pipeline（核心，分阶段计时）──
    _timings.clear()
    _call_counts.clear()
    engine.clear_sql_ast_cache()

    t0 = time.time()
    kj, pm = engine.analyze_pipeline(rules, tf, gv, dialect, ddl_dir=ddl_dir)
    t_pipeline = time.time() - t0

    field_count = len(kj.get("field_mappings", {}).get("fields", []))

    print(f"\n[analyze_pipeline 总计]   {t_pipeline:.3f}s  ({field_count}字段)")
    print(f"\n{'=' * 60}")
    print(f"阶段分解（降序）:")
    print(f"{'─' * 60}")
    for name, t in sorted(_timings.items(), key=lambda x: -x[1]):
        if t < 0.001:
            continue
        count = _call_counts.get(name, 0)
        pct = t / t_pipeline * 100 if t_pipeline > 0 else 0
        per_call = t / count * 1000 if count > 0 else 0  # ms
        flag = " ←← 瓶颈!" if t > 1.0 else (" ← 关注" if t > 0.3 else "")
        print(f"  {name:35s} {t:7.3f}s ({pct:4.0f}%)  调用{count:5d}次  均{per_call:6.1f}ms{flag}")

    print(f"\n{'=' * 60}")
    print(f"SQL AST 缓存条目: {len(engine._SQL_AST_CACHE)}")
    print(f"\n把以上输出贴给开发者，即可定位瓶颈。")


if __name__ == "__main__":
    main()
