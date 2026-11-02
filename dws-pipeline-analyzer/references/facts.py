#!/usr/bin/env python3
"""facts —— 面向 agent 的存量资产事实包入口（asset_facts.json）。

定位（消费方 docs/specs/opt/12 v3 / 本仓 docs/14 使用说明）：
    输入一个制品目录 + 一个产物落盘路径，一次调用一次交卷。全程零 AI、
    零判断分支——制品类型识别（sheet 指纹 / 后缀即型）、DQ 两态、DDL 收录
    全部由脚本消化，入口不因输入模糊而模糊。

调用合同（退出码语义，agent 据此分流）：
    exit 0   成功交卷：产物已落盘且过权威 schema 全绿；stdout 单独一行
             `asset_facts: {产物绝对路径}` —— 按行前缀解析定位
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
from inputs import build_baseline_export_ctx
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


def run_facts(dir_path, output_path, fmt: str = "asset_facts") -> Path:
    """制品目录 → 分析 → 产物精确路径落盘（一次调用一次交卷）。

    fmt: asset_facts（v1 物理事实包，默认）| ts_draft（v2 ts 草稿=ViewContext JSON 渲染，
    对齐正向 ts 形态；双支持期按消费方节奏切换）。两格式共用装配/分析/分拣/交叉链路，
    只在投影层分叉——同源保证内容一致。
    """
    # ── 共享装配核（docs/17 §九：facts/spec 同一条链，唯一定义在 pipeline_core）──
    try:
        core = run_from_dir(dir_path, include_dq=True, channel="facts",
                            entry_title="=== asset_facts（facts 入口）===")
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
    # 制品间一致性交叉（v3.1 §十：披露不阻断，经同一 warnings 通道）
    knowledge["meta"]["warnings_extra"].extend(
        run_crosscheck(knowledge, engine_ctx, asm,
                       dq_rules=dq_rules, schedule_info=schedule_info))

    # ── 投影 → 校验 → 落盘（精确路径）──
    from baseline_export import BaselineExportError, export_baseline_v1
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

    if fmt == "ts_draft":
        # v2：ViewContext JSON 渲染（与 tech_design 同源）；调度还原 + 进门验 vendor schema
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
        locator = "_reverse"
    else:
        export_ctx["extra_warnings"] = extra_warnings
        try:
            out = export_baseline_v1(knowledge, export_ctx,
                                     output_dir=Path(output_path).parent,
                                     output_file=output_path)
        except BaselineExportError as e:
            print("[asset_facts] 导出失败，产物未落盘：", file=sys.stderr)
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"asset_facts: {out.resolve()}")
        locator = None
    # 自检结论行（v1.4 交付门，机器按行前缀判读；与定位行同风格）
    import json as _json
    product = _json.loads(out.read_text(encoding="utf-8"))
    holder = product.get(locator, {}) if locator else product
    print(f"self_check: {holder.get('self_check', {}).get('verdict', '')}")
    return out


def main():
    parser = argparse.ArgumentParser(
        description="asset_facts —— 存量资产事实包入口（面向 agent：目录直入，一次调用一次交卷）",
    )
    parser.add_argument("--dir", required=True,
                        help="制品目录（该资产全部原始制品；类型自动识别：excel 按 sheet 指纹、"
                             ".sql/.ddl 后缀即型；术加制品包必需，其余可选）")
    parser.add_argument("--output", required=True,
                        help="产物落盘路径（父目录自动创建）")
    parser.add_argument("--format", default="asset_facts", choices=("asset_facts", "ts_draft"),
                        help="产物形态：asset_facts（v1 物理事实包，默认）| ts_draft"
                             "（v2 ts 草稿，对齐消费方正向 ts 形态，vendor schema 进门验）")
    args = parser.parse_args()
    run_facts(args.dir, args.output, fmt=args.format)


if __name__ == "__main__":
    main()
