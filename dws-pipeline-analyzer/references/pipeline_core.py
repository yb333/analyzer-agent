#!/usr/bin/env python3
"""pipeline_core —— facts / spec 双通道共享的制品装配 + 引擎核（唯一定义）。

为什么存在（docs/17-SPEC列级映射矩阵.md §九）：
    SPEC 通道（人向 xlsx）与 facts 通道（agent 向 JSON）是两个消费方、两套契约与
    演进节奏，但「装配 → 解析 → 增强」必须是同一条链——本文件是这条链的唯一实现，
    两通道 import 之，用共享测试锁同源，防止「两套实现」漂移（口径分叉的源头）。

    通道策略不在此，归各通道自己：
    - facts：DQ 两态披露 + 分拣 warnings + crosscheck 进 warnings_extra（agent 协议）
    - spec：DQ 忽略且不读不告警；两态披露走 xlsx 备注 + 人话 stdout（docs/17 §〇/§七）

消费方:
    facts.py  run_facts()  → include_dq=True
    spec.py   run_spec()   → include_dq=False

Author: 院博
"""

from __future__ import annotations

from pathlib import Path

from analyzer import read_excel
from engine import analyze_pipeline, detect_dialect
from inputs import (ManifestError, assemble_artifact_dir, read_lts_excel,
                    read_dq_excel, stage_ddl_files, discover_i_view_from_ddl,
                    apply_meta_enrichments)


class AssemblyError(Exception):
    """制品/装配问题。通道捕获后打印 stderr + exit(1)（语义同 facts 调用合同 exit 1）。"""


def run_from_dir(dir_path, *, include_dq: bool = True, channel: str = "facts",
                 entry_title: str = "") -> dict:
    """制品目录 → 分拣 → 读取 → 引擎 → 增强（一次装配，双通道共用）。

    Args:
        include_dq: True=读取 DQ（facts 两态语义）；False=DQ 完全忽略（spec 口径，
            不读不告警——目录里出现 DQ excel 时静默跳过）
        channel: 错误消息前缀（"facts" / "spec"）
        entry_title: 入口标题行，原样打印在分拣日志之后（facts 传
            "=== asset_facts（facts 入口）==="，spec 传自己的）

    Returns: dict，键：
        asm             assemble_artifact_dir 的结果（execution_tasks/lts/dq/ddl/unrecognized）
        input_path      术加制品包路径（Path）
        input_sheets    read_excel 弹出的 _source_sheets（provenance 用，可为 None）
        raw             read_excel 结果（rules 之外的键：target_fields/group_variables/
                        rule_group_code/rule_group_en/...）
        rules           RawRule 列表
        schedule_info   read_lts_excel 结果（无 LTS 制品时 None）
        dq_rules        read_dq_excel 结果（include_dq=False 或无 DQ 时 None）
        knowledge       引擎组装的 knowledge dict
        parsed_map      {rule_code: ParsedSQL}（AI summary 等消费方用）
        engine_ctx      analyze_pipeline 的 context_out（table_catalog/final_target_*/ddl_files）
    """
    # ── 分拣（识别职责全在本侧：术加硬闸 / 多义 fail / 其余不阻断）──
    try:
        asm = assemble_artifact_dir(dir_path)
    except ManifestError as e:
        raise AssemblyError(f"[{channel}] 装配失败:\n{e}") from None
    for name, reason in asm["unrecognized"]:
        print(f"  [分拣] 忽略无法识别的文件 {name}（{reason}）")
    if asm["lts"]:
        print(f"  [分拣] LTS 制品: {Path(asm['lts']).name}")
    if include_dq and asm["dq"]:
        print(f"  [分拣] DQ 制品: {Path(asm['dq']).name}")
    if asm["ddl"]:
        print(f"  [分拣] DDL: {len(asm['ddl'])} 个文件")
    if entry_title:
        print(entry_title)

    # ── 主输入：术加制品包（query_sql 唯一来源）──
    input_path = Path(asm["execution_tasks"])
    print("Step 1: 读取术加制品包...")
    raw = read_excel(str(input_path))
    input_sheets = raw.pop("_source_sheets", None)
    rules = raw["rules"]
    if not rules:
        raise AssemblyError("错误: 术加制品包无有效 RULE 行")

    # ── LTS excel（分拣结果；未检出的披露口径归通道）──
    schedule_info = None
    if asm["lts"]:
        schedule_info = read_lts_excel(asm["lts"], raw.get("rule_group_code") or "")
        if schedule_info:
            f_names = [t["task_name"] for t in schedule_info.get("f_tasks", [])]
            i_names = [t["task_name"] for t in schedule_info.get("i_tasks", [])]
            print(f"  [LTS] 调度任务(excel): F={f_names} I={i_names}")

    dq_rules = None
    if include_dq and asm["dq"]:
        dq_rules = read_dq_excel(asm["dq"])
        if not dq_rules:
            raise AssemblyError(
                f"[{channel}] DQ 制品无有效规则行: {asm['dq']}——主 sheet「规则基础信息」"
                "需含带规则Number的数据行")
        print(f"  [DQ] 发现 {len(dq_rules)} 条质量检查规则(excel)")

    # ── DDL（.sql/.ddl 后缀即型）→ stage 目录；provenance 记原路径 ──
    ddl_dir = ""
    ddl_map = {}
    if asm["ddl"]:
        import tempfile
        _stage_root = Path(tempfile.mkdtemp(prefix="ddl_stage_"))
        try:
            ddl_map = stage_ddl_files(asm["ddl"], _stage_root)
        except ManifestError as e:
            raise AssemblyError(f"[{channel}] DDL 装配失败:\n{e}") from None
        ddl_dir = str(_stage_root)

    # ── 引擎 ──
    sql_texts = [r.query_sql for r in rules if r.query_sql]
    dialect = detect_dialect(sql_texts)
    print(f"Step 2: 方言 = {dialect}")
    print("Step 3-7: 解析 + 组装 knowledge...")
    engine_ctx = {}
    knowledge, parsed_map = analyze_pipeline(
        rules, raw["target_fields"], raw["group_variables"], dialect,
        ddl_dir=ddl_dir, source_file=input_path.name,
        rule_group_code=raw.get("rule_group_code") or "",
        context_out=engine_ctx,
    )
    # 目录通道 DQ 两态：未检出 = dq_rules=[]（"无"由闸口人扫兜底，不前置停等）
    apply_meta_enrichments(
        knowledge, None, schedule_info, dq_rules,
        dq_declared_absent=(dq_rules is None))

    # I 视图识别：DDL 清单里的 CREATE VIEW ... FROM 目标表（docs/16 缺口 1）
    # （在 crosscheck 之前注入——crosscheck 不依赖 asset_info，双通道顺序均安全）
    i_view = discover_i_view_from_ddl(
        asm["ddl"], engine_ctx.get("final_target_schema", ""),
        engine_ctx.get("final_target_table", ""))
    if i_view:
        knowledge["meta"]["asset_info"] = {
            "is_view": True, "view_table": i_view["view_name"],
            "view_schema": i_view["view_schema"], "base_table": engine_ctx.get("final_target_table", ""),
            "base_schema": engine_ctx.get("final_target_schema", ""), "view_step": ""}
        print(f"  [DDL] 识别 I 视图: {i_view['view_schema']}.{i_view['view_name']}")

    # DDL provenance：stage 路径翻译回原路径
    if ddl_map:
        engine_ctx["ddl_files"] = [ddl_map.get(f, f)
                                   for f in engine_ctx.get("ddl_files", [])]

    return {
        "asm": asm, "input_path": input_path, "input_sheets": input_sheets,
        "raw": raw, "rules": rules, "schedule_info": schedule_info,
        "dq_rules": dq_rules, "knowledge": knowledge, "engine_ctx": engine_ctx,
        "parsed_map": parsed_map,
    }
