#!/usr/bin/env python3
"""facts —— 面向 agent 的存量资产逆向入口（ts 草稿，契约 v2.1）。

定位（消费方 docs/specs/opt/12 / 本仓 docs/14 使用说明）：
    输入一个制品目录 + 一个产物落盘路径，一次调用一次交卷。全程零 AI、
    零判断分支——制品类型识别（sheet 指纹 / 后缀即型）、DQ 两态、DDL 收录
    全部由脚本消化，入口不因输入模糊而模糊。

调用合同（退出码语义，agent 据此分流）：
    exit 0   成功交卷：产物已落盘且过 vendor schema 全绿；stdout 两行
             `ts_draft: {产物绝对路径}` + `self_check: {结论}`——按行前缀解析
    exit 1   制品/数据问题：stderr 逐条可行动错误清单（哪个文件、什么问题、
             缺什么料）；产物不落盘。原样上报流程要料或退回，不重试不代填
    exit 2   调用参数错误（本方问题）：修参数后重调

用法:
    python run.py facts --dir {制品目录} --output {产物绝对路径}

Author: 院博
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from crosscheck import run_crosscheck
from schedule_recovery import recover_schedule
from ts_export import TsDraftError, export_ts_draft
from inputs import build_baseline_export_ctx, scan_unnamed_view_bodies
from pipeline_core import AssemblyError, run_from_dir


def _sorting_warnings(unrecognized: list, dq_detected: bool, lts_detected: bool) -> list:
    """分拣通道 warnings（docs/12 v3 §二：无法识别文件不阻断、未检出 DQ/LTS 披露）。

    unrecognized 进产物随交付，由消费方闸口人扫兜底——不参与失败判定。
    """
    out = []
    for name, reason in unrecognized:
        out.append({
            "type": "unrecognized_artifact",
            "title": f"无法识别的文件 {name}（{reason}）——不阻断产出，请人工确认是否遗漏制品",
        })
    if not dq_detected:
        out.append({
            "type": "dq_not_detected",
            "title": "未检出 DQ 制品（目录即用户所给全部）——若该资产确有存量 DQ，请补料重跑",
        })
    if not lts_detected:
        out.append({
            "type": "lts_not_detected",
            "title": "未检出 LTS 制品——增量资产的回刷地基（增量变量还原），若该资产为增量请补料重跑",
        })
    return out


def _record_usage(payload: dict) -> None:
    """运营埋点（全程吞异常——绝不影响交付）。"""
    try:
        from usage import record as _rec
        _rec(payload)
    except Exception:
        pass


def run_facts(dir_path, output_path) -> Path:
    """制品目录 → 分析 → ts 草稿精确路径落盘（一次调用一次交卷）。

    产物 = ts_draft（契约 v2.1，ViewContext JSON 渲染、对齐正向 ts 形态）——
    v1 asset_facts 投影已于 2026-11-04 退役（消费方 v1 入料路径同日删除，
    一波换彻底不留双版本）。
    """
    import time as _t
    t0 = _t.time()
    # ── 共享装配核（docs/17 §九：facts/spec 同一条链，唯一定义在 pipeline_core）──
    try:
        core = run_from_dir(dir_path, include_dq=True, channel="facts",
                            entry_title="=== ts_draft（facts 入口）===")
    except AssemblyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    asm = core["asm"]
    input_path = core["input_path"]
    input_sheets = core["input_sheets"]
    raw = core["raw"]
    schedule_info = core["schedule_info"]
    dq_rules = core["dq_rules"]
    knowledge = core["knowledge"]
    engine_ctx = core["engine_ctx"]

    # ── facts 通道策略：分拣披露 + 制品间一致性交叉 → warnings 随产物交付 ──
    # 分拣披露（无法识别文件/未检出 DQ/未检出 LTS）
    knowledge.setdefault("meta", {}).setdefault("warnings_extra", []).extend(
        _sorting_warnings(asm["unrecognized"], dq_detected=asm["dq"] is not None,
                          lts_detected=asm["lts"] is not None))
    # 无壳视图体披露（纯 SELECT 无 CREATE VIEW：视图名无从恢复，i_view 将为空——
    # 不静默跳过，要求补完整 DDL 重跑）
    for vb in scan_unnamed_view_bodies(asm["ddl"]):
        knowledge["meta"]["warnings_extra"].append({
            "type": "ddl_view_without_name",
            "title": f"DDL 文件 {vb['file']} 为纯 SELECT 查询体（无 CREATE VIEW 定义壳）——"
                     f"疑似 I 视图逻辑体但视图名不在 DDL 内容中、无从恢复"
                     f"（meta.target.i_view 将为空）。本仓口径：视图名必须在 DDL 内容里、"
                     f"不从文件名推断。请补完整视图 DDL"
                     f"（CREATE [OR REPLACE] VIEW <schema>.<视图名> AS <原 SELECT>）重跑",
            "detail": f"内容开头：{vb['head']}",
        })
    # 制品间一致性交叉（v3.1 §十：披露不阻断，经同一 warnings 通道）
    knowledge["meta"]["warnings_extra"].extend(
        run_crosscheck(knowledge, engine_ctx, asm,
                       dq_rules=dq_rules, schedule_info=schedule_info))

    # ── 投影 → 校验 → 落盘（精确路径）──
    export_ctx = build_baseline_export_ctx(
        knowledge, engine_ctx,
        is_yml_mode=False, consumed_yml_files=[],
        input_path=input_path,
        platform_generation="1.0",
        rule_group_en=raw.get("rule_group_en") or "",
        group_en_fallback="",
        input_sheets=input_sheets,
    )
    # warnings 合流：分拣披露 + 交叉（投影层三源合流之外的第 4 源）
    extra_warnings = knowledge["meta"].pop("warnings_extra", [])

    # ts 草稿：ViewContext JSON 渲染（与 tech_design 同源）；调度还原 + 进门验 vendor schema
    group_vars = {}
    for lst in (raw.get("group_variables") or {}).values():
        for gv in lst:
            name = getattr(gv, "var_name", "") or ""
            if name and name not in group_vars:
                group_vars[name] = getattr(gv, "default_value", "") or ""
    schedule_rec = recover_schedule(schedule_info, group_vars)
    try:
        out = export_ts_draft(knowledge, engine_ctx, output_path,
                              schedule_rec=schedule_rec,
                              dq_detected=bool(dq_rules),
                              provenance=export_ctx["provenance"],
                              extra_warnings=extra_warnings)
    except TsDraftError as e:
        print("[ts_draft] 导出失败，产物未落盘：", file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(f"ts_draft: {out.resolve()}")
    # 自检结论行（交付门，机器按行前缀判读；与定位行同风格）
    import json as _json
    product = _json.loads(out.read_text(encoding="utf-8"))
    verdict = product.get('_reverse', {}).get('self_check', {}).get('verdict', '')
    print(f"self_check: {verdict}")
    # ── 运营埋点：成功记全字段（失败由 main 拦 SystemExit 粗记，不会重复）──
    _record_usage({
        "command": "facts",
        "input_type": "artifact_dir",
        "asset": raw.get("rule_group_en") or "",
        "elapsed_sec": round(_t.time() - t0, 2),
        "status": "ok",
        "self_check": verdict,  # 自检结论随行（自动进 extra）
    })
    return out


def main():
    parser = argparse.ArgumentParser(
        description="asset_facts —— 存量资产事实包入口（面向 agent：目录直入，一次调用一次交卷）",
    )
    parser.add_argument("--dir", required=True,
                        help="制品目录（该资产全部原始制品；类型自动识别：excel 按 sheet 指纹、"
                             ".sql/.ddl 后缀即型；术加制品包必需，其余可选）")
    parser.add_argument("--output", required=True,
                        help="产物落盘路径（ts 草稿 json，父目录自动创建）")
    args = parser.parse_args()
    import time as _t
    t0 = _t.time()
    try:
        run_facts(args.dir, args.output)
    except SystemExit as e:
        # 失败统一粗记（成功已由 run_facts 内部记过，这里只拦非零退出）
        if e.code not in (0, None):
            _record_usage({
                "command": "facts",
                "input_type": "artifact_dir",
                "asset": Path(args.dir).name,
                "elapsed_sec": round(_t.time() - t0, 2),
                "status": "error",
                "error_type": "input_error",  # exit 1 = 制品/数据问题（exit 2 在 parse 阶段，到不了这里）
                "error_message": "non-zero exit (见 stderr 错误清单)",
            })
        raise


if __name__ == "__main__":
    main()
