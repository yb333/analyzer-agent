#!/usr/bin/env python3
"""spec —— 面向人的需求 SPEC 交付入口（需求SPEC.xlsx）。

定位（docs/17-SPEC列级映射矩阵.md）：
    与 facts（agent 向 JSON）是两个消费方、两套契约，但共用同一条装配/解析链
    （pipeline_core 唯一定义，共享测试锁同源）。本通道投影为人向 xlsx：
    - 输入 = 制品目录（术加必选 + LTS/DDL 可选；DQ 忽略不告警，docs/17 §〇）；
    - 多场景资产按单场景口径产出，须 --scenario 指定（§〇 方案 A）；
    - 交付门 = 模板列头指纹 + 两态披露（人话 stdout，非 agent 协议）。

调用合同（退出码语义与 facts 对齐）：
    exit 0   成功交卷：产物已落盘；stdout 单独一行 `spec: {产物绝对路径}`
             与 `self_check: {verdict}`（pass / pass_with_noise / review）
    exit 1   制品/数据问题：stderr 人话错误清单；产物不落盘
    exit 2   调用参数错误

用法:
    python run.py spec --dir {制品目录} --output {产物路径} [--scenario {场景名}]

Author: 院博
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from pipeline_core import AssemblyError, run_from_dir
from schedule_match import match_schedules
from spec_export import (SpecExportError, export_spec, find_scenario,
                         list_scenarios, select_scenario)


def _scenario_label(sc: dict) -> str:
    """场景展示：分区名优先（P_CUR），无分区用场景名。"""
    return sc.get("partition") or sc.get("name") or sc.get("id")


def _record_usage(payload: dict) -> None:
    """运营埋点（全程吞异常——绝不影响交付）。"""
    try:
        from usage import record as _rec
        _rec(payload)
    except Exception:
        pass


def run_spec(dir_path, output_path, scenario: str = None) -> Path:
    """制品目录 → 需求SPEC.xlsx（一次调用一次交卷）。"""
    import time as _t
    t0 = _t.time()
    # ── 共享装配核（DQ 忽略：include_dq=False，docs/17 §〇）──
    try:
        core = run_from_dir(dir_path, include_dq=False, channel="spec",
                            entry_title="=== 需求SPEC（spec 入口）===")
    except AssemblyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    knowledge = core["knowledge"]
    engine_ctx = core["engine_ctx"]
    asm = core["asm"]

    # ── 场景门（§〇 方案 A：单场景口径）──
    scenarios = list_scenarios(knowledge)
    if len(scenarios) > 1:
        labels = ", ".join(_scenario_label(s) for s in scenarios)
        if not scenario:
            print(f"[spec] 多场景资产检测到 {len(scenarios)} 个场景: {labels}",
                  file=sys.stderr)
            print("  SPEC 按单场景口径产出（文档零场景概念），请用 --scenario 指定其一后重跑",
                  file=sys.stderr)
            # 机器可解析行（/export-spec 封装读它弹场景选择；与 locator 同风格）
            print(f"scenarios: {labels}", file=sys.stderr)
            sys.exit(1)
        chosen = find_scenario(knowledge, scenario)
        if not chosen:
            print(f"[spec] 场景 {scenario} 不存在；可选: {labels}", file=sys.stderr)
            print(f"scenarios: {labels}", file=sys.stderr)
            sys.exit(1)
        knowledge = select_scenario(knowledge, chosen)
        print(f"  [场景] 按场景 {_scenario_label(chosen)} 单场景口径产出")
    elif scenario:
        chosen = find_scenario(knowledge, scenario)
        if not chosen:
            only = _scenario_label(scenarios[0]) if scenarios else "（无场景）"
            print(f"[spec] 场景 {scenario} 不存在；该资产仅有场景: {only}",
                  file=sys.stderr)
            sys.exit(1)

    # ── 输出路径默认（面向人：不传 --output 时按约定落盘）──
    if not output_path:
        group_en = (core["raw"].get("rule_group_en") or "").strip()
        output_path = str(Path("./output") /
                          f"需求SPEC-{group_en or '未命名'}.xlsx")

    # ── LTS 两态（§二/§七：没给 ≠ 给了没匹配上，措辞必须区分）──
    schedule_lookup, schedule_unmatched = {}, None
    if core["schedule_info"]:
        # 匹配器：资产 F 任务的 tskdep 依赖 × 物理源表（docs/17 §六实测修订⑧）
        from viewmodel import build_context as _build_ctx
        _ctx = _build_ctx(knowledge)
        _sources = [{"schema": e.schema, "table": e.table}
                    for e in _ctx.entity_source_tables()]
        schedule_lookup, schedule_unmatched, mstatus = match_schedules(
            core["schedule_info"], _sources)
        if mstatus != "ok":
            reason = ("LTS制品未含资产F任务（命名需 *_F），依赖job无法提取"
                      if mstatus == "no_f_task"
                      else "资产F任务无 tskdep 依赖job，依赖无法提取")
            lts_remark = f"{reason}，调度信息留空"
            print(f"  [调度匹配] {lts_remark}")
        else:
            hit = sum(1 for s in _sources
                      if (f"{s['schema']}.{s['table']}".lower() in schedule_lookup
                          if s["schema"] else s["table"].lower() in schedule_lookup))
            print(f"  [调度匹配] 依赖job × 源表 {len(_sources)} 张：命中 {hit}；"
                  f"未匹配依赖 {len(schedule_unmatched)} 个列于清单尾部")
            lts_remark = ""
    elif asm["lts"]:
        schedule_unmatched = []
        lts_remark = ""
    else:
        lts_remark = "LTS制品未提供，调度信息留空"
        print("  [披露] LTS制品未提供：调度 7 列留空（两态之「没给」）")

    # ── 投影（指纹门在 export_spec 内，不符不落盘）──
    try:
        out, self_check, disclosures = export_spec(
            knowledge, engine_ctx, output_path,
            schedule_lookup=schedule_lookup,
            schedule_unmatched=schedule_unmatched,
            lts_remark=lts_remark)
    except SpecExportError as e:
        print("[spec] 导出失败，产物未落盘：", file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(1)
    for d in disclosures:
        print(f"  [披露] {d['title']}")
    print(f"spec: {out.resolve()}")
    print(f"self_check: {self_check['verdict']}")
    # ── 运营埋点：成功记全字段（失败由 main 拦 SystemExit 粗记，不会重复）──
    _record_usage({
        "command": "spec",
        "input_type": "artifact_dir",
        "asset": core["raw"].get("rule_group_en") or "",
        "elapsed_sec": round(_t.time() - t0, 2),
        "status": "ok",
        "scenario": scenario or "",  # 多场景口径时随行（自动进 extra）
        "self_check": self_check["verdict"],  # 自检结论随行（自动进 extra）
    })
    return out


def main():
    parser = argparse.ArgumentParser(
        description="spec —— 需求SPEC.xlsx 交付入口（面向人：目录直入，"
                    "一次调用一次交卷；多场景资产需 --scenario）",
    )
    parser.add_argument("--dir", required=True,
                        help="制品目录（该资产全部原始制品；类型自动识别：excel 按 "
                             "sheet 指纹、.sql/.ddl 后缀即型；术加制品包必需，"
                             "LTS/DDL 可选，DQ 忽略）")
    parser.add_argument("--output", default=None,
                        help="产物落盘路径（可省略：默认 ./output/需求SPEC-{规则组英文名}.xlsx）")
    parser.add_argument("--scenario", default=None,
                        help="多场景资产必填：按该场景单场景口径产出（场景名 = "
                             "分区分组名，如 P_CUR）")
    args = parser.parse_args()
    import time as _t
    t0 = _t.time()
    try:
        run_spec(args.dir, args.output, scenario=args.scenario)
    except SystemExit as e:
        # 失败统一粗记（成功已由 run_spec 内部记过，这里只拦非零退出；
        # exit 1 含多场景未指定 --scenario 等可修正调用）
        if e.code not in (0, None):
            _record_usage({
                "command": "spec",
                "input_type": "artifact_dir",
                "asset": Path(args.dir).name,
                "elapsed_sec": round(_t.time() - t0, 2),
                "status": "error",
                "error_type": "input_error",
                "error_message": "non-zero exit (见 stderr 错误清单)",
            })
        raise


if __name__ == "__main__":
    main()
