#!/usr/bin/env python3
"""制品间一致性交叉校验（自检层，消费方 docs/specs/opt/12 v3.1 §十）。

定位与边界：
    输入 = 引擎产物（draft / engine_ctx）+ 装配产物（asm / dq_rules / schedule_info）
    ——两边事实都在手，交叉归这里；引擎不知道多制品，装配层在分析前跑（还没有
    draft），谁都不合适。本模块**只读不写**：消费既有事实、不产生新事实。
    输出 = warnings 列表（带 severity），经 extra_warnings 通道进产物——
    差异=存量既成事实，**披露不阻断**（v3 基线纪律不变）。

严重度两级（§九，分级口径=本仓）：
    structural —— 指向"料可能给错 / 资产级对不上"（驱动待审：人审换料或放行）
    noise      —— 平台常态差异（登记滞后等，披露即可）
    分级映射的权威定义在 baseline_export._SEVERITY_BY_TYPE（投影层统一归一化）；
    本模块产出的每条 warning 自带 severity，口径与之一致。

交叉范围（五类中四类在此实现；TargetFields vs RULE SQL 由引擎既有
field_mappings.warnings 覆盖，见 _SEVERITY_BY_TYPE 定级）：
    术加 vs DQ   —— DQ 规则目标表 vs 制品目标表（不符=料可能属其他资产）
    术加 vs DDL  —— 已给 DDL 但不含目标表；DDL 内部多文件重复定义同表
    术加 vs LTS  —— 任务 V_GROUP_CODE vs 制品规则组编码（混组兜底）

Author: 院博
"""

from __future__ import annotations

import re
from pathlib import Path

STRUCTURAL = "structural"
NOISE = "noise"


def run_crosscheck(draft: dict, engine_ctx: dict, asm: dict,
                   dq_rules: list | None = None,
                   schedule_info: dict | None = None) -> list:
    """执行全部交叉校验，返回 warnings 列表（每条含 type/severity/title/定位信息）。

    防御纪律：单个校验器异常不挡产出（交叉逻辑 bug 不该拦截交卷）——
    捕获后以 internal warning 披露。
    """
    checks = (
        lambda: _check_dq_target(draft, dq_rules),
        lambda: _check_ddl_target(draft, engine_ctx, asm),
        lambda: _check_lts_group(draft, schedule_info),
    )
    out: list = []
    for check in checks:
        try:
            out.extend(check())
        except Exception as e:  # noqa: BLE001 —— 自检失败自身不能阻断交卷
            out.append({
                "type": "crosscheck_internal",
                "severity": NOISE,
                "title": f"一致性交叉校验部分失败（{e}）——不影响产物事实，请反馈生产方",
            })
    return out


def _final_target(draft: dict, engine_ctx: dict) -> tuple[str, str]:
    """制品最终目标表（schema, table，小写）——引擎产物权威。"""
    meta = draft.get("meta") or {}
    table = str(engine_ctx.get("final_target_table") or meta.get("target_table") or "")
    schema = str(engine_ctx.get("final_target_schema") or "")
    return schema.lower(), table.lower()


def _check_dq_target(draft: dict, dq_rules: list | None) -> list:
    """术加 vs DQ：DQ 规则的目标表应与制品目标表一致——不符=料可能属其他资产。"""
    if not dq_rules:
        return []
    _, final_table = _final_target(draft, {})
    if not final_table:
        return []
    out = []
    for r in dq_rules:
        target = str(r.get("target_table") or "").strip().lower()
        if target and target != final_table:
            out.append({
                "type": "dq_target_mismatch",
                "severity": STRUCTURAL,
                "rule_number": str(r.get("rule_number") or ""),
                "title": (f"DQ 规则 {r.get('rule_number') or '?'} 的目标表 "
                          f"{r.get('target_table')} 与制品目标表 {final_table} 不符"
                          "——DQ 制品可能属其他资产"),
            })
    return out


def _check_ddl_target(draft: dict, engine_ctx: dict, asm: dict) -> list:
    """术加 vs DDL + DDL 内部。

    - 给了 DDL 但 catalog 未覆盖制品目标表 → 料可能不属本资产（structural）
    - 多个 .sql 定义同一张表 → 结构可能冲突（structural）
    """
    out: list = []
    ddl_files = asm.get("ddl") or []
    if ddl_files:
        final_schema, final_table = _final_target(draft, engine_ctx)
        catalog = engine_ctx.get("table_catalog") or {}
        if final_table and f"{final_schema}.{final_table}" not in catalog:
            # 大小写容错：catalog key 归一小写后兜底再找一次
            if not any(k.split(".")[-1] == final_table for k in catalog):
                out.append({
                    "type": "ddl_target_missing",
                    "severity": STRUCTURAL,
                    "title": (f"已提供 {len(ddl_files)} 个 DDL 文件但未覆盖制品目标表 "
                              f"{final_schema}.{final_table}——DDL 料可能不属本资产"),
                })

        # DDL 内部：同表多文件定义（检测用轻解析，不替代 engine 的 DDL 解析权威）
        by_table: dict[str, list[str]] = {}
        for f in ddl_files:
            try:
                content = Path(f).read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                continue
            for m in re.finditer(
                    r'create\s+table\s+(?:if\s+not\s+exists\s+)?[\w.]*?(\w+)\s*[(\[cC]',
                    content):
                by_table.setdefault(m.group(1), []).append(Path(f).name)
        for table, files in sorted(by_table.items()):
            if len(files) > 1:
                out.append({
                    "type": "ddl_duplicate_table",
                    "severity": STRUCTURAL,
                    "title": f"表 {table} 在多个 DDL 文件中重复定义（{', '.join(files)}）"
                             "——结构可能冲突，请人工确认保留哪个",
                })
    return out


def _check_lts_group(draft: dict, schedule_info: dict | None) -> list:
    """术加 vs LTS：任务的 V_GROUP_CODE 应与制品规则组编码一致。

    facts 目录通道按 V_GROUP_CODE 精确匹配、失败回退全收——混组 excel 里
    非本组任务会进 all_tasks，这道交叉兜住（不符=混入他组任务）。
    """
    if not schedule_info:
        return []
    rgc = str((draft.get("meta") or {}).get("rule_group_code") or "").strip().lower()
    if not rgc:
        return []
    out = []
    seen = set()
    for key in ("f_tasks", "i_tasks", "other_tasks", "all_tasks"):
        for t in (schedule_info.get(key) or []):
            if not isinstance(t, dict):
                continue
            name = t.get("task_name") or ""
            gc = str(t.get("group_code") or "").strip().lower()
            if gc and gc != rgc and (name, gc) not in seen:
                seen.add((name, gc))
                out.append({
                    "type": "lts_group_mismatch",
                    "severity": STRUCTURAL,
                    "title": (f"LTS 任务 {name} 的 V_GROUP_CODE={t.get('group_code')} "
                              f"与制品规则组编码 {draft['meta'].get('rule_group_code')} 不符"
                              "——LTS 制品可能混入他组任务"),
                })
    return out
