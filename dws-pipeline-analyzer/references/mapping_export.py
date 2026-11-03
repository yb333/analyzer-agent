#!/usr/bin/env python3
"""mapping_export —— mapping.xlsx 投影层（与 SPEC 同源装配，2026-11-03）。

架构：直接消费 spec_export.build_spec_model 的装配结果（Sheet3 实体级行 +
Sheet4 字段行），保证 mapping 与 需求SPEC 逐格一致——不是第二套理解。
mapping 特有：多场景分组（全场景并存）+ mapping 列模板 + DDL 元数据。

Author: 院博
"""
from __future__ import annotations

import re
from pathlib import Path

from viewmodel import build_context, split_schema_table


def build_mapping_model(knowledge: dict, engine_ctx: dict) -> dict:
    """knowledge + engine_ctx → mapping 行模型。

    多场景资产：每场景各调一次 build_spec_model（select_scenario 过滤后），
    字段行带场景标签；实体级行取首个场景（表结构跨场景不变）。
    单场景：直接 build_spec_model。
    """
    from spec_export import build_spec_model, list_scenarios, select_scenario

    ctx = build_context(knowledge)
    scenarios = list_scenarios(knowledge)

    all_fields = []
    entity_rows = None
    if len(scenarios) <= 1:
        model = build_spec_model(knowledge, engine_ctx)
        entity_rows = model["sheet3"]
        sc_label = (scenarios[0].get("partition") or scenarios[0].get("name")) \
            if scenarios else "默认场景"
        for r in model["sheet4"]:
            all_fields.append(_to_attr_row(r, sc_label, ctx))
    else:
        for sc in scenarios:
            k_sc = select_scenario(knowledge, sc)
            model = build_spec_model(k_sc, engine_ctx)
            if entity_rows is None:
                entity_rows = model["sheet3"]
            sc_label = sc.get("partition") or sc.get("name") or ""
            for r in model["sheet4"]:
                all_fields.append(_to_attr_row(r, sc_label, ctx))

    return {
        "entity": _to_entity_rows(entity_rows or [], ctx),
        "attribute": all_fields,
        "warnings": model.get("disclosures", []),
    }


def _to_attr_row(r, scenario: str, ctx) -> dict:
    """SPEC Sheet4 行 → mapping 属性级行。"""
    target_types = ctx.meta.get("target_field_types", {}) or {}
    target_comments = ctx.meta.get("target_field_comments", {}) or {}
    fname = r[10] or ""

    def _tc(n):
        for k in (n, n.lower(), n.upper()):
            if k in target_comments:
                return target_comments[k]
        return ""

    # 源表可能是多表合并（a、b 顿号分隔）——取第一张的 schema
    src_tables = (r[2] or "").split("、")
    src_schema = ""
    for es in ctx.entity_source_tables():
        if es.table == (src_tables[0] if src_tables else ""):
            src_schema = es.schema
            break

    return {
        "scenario": scenario,
        "schema": src_schema,
        "table": r[2] or "",           # 源表（多源顿号合并）
        "alias": r[3] or "",            # 源表别名
        "source_field": r[4] or "",     # 源字段（多源分号合并）
        "type_label": r[5] or "",       # 四枚举
        "expr": r[6] or "",             # 展开态表达式（含说明段）
        "target_field": fname,
        "target_cn": r[11] or _tc(fname),
        "target_type": target_types.get(fname, ""),
    }


def _to_entity_rows(sheet3: list, ctx) -> list:
    """SPEC Sheet3 行 → mapping 实体级行（去重）。

    Sheet3 行格式 [id, left_tbl, left_alias, right_tbl, right_alias, join_type, g]：
    - 主表行（join_type=主表）：表名在 r[2]、别名在 r[3]（tmp 链模型特有布局）
    - 其余行：右表在 r[3]、右别名在 r[4]（标准左/右布局）
    - 虚拟逻辑表行：跳过
    """
    seen = set()
    rows = []
    for r in sheet3:
        if r[5] == "主表":
            table, alias = r[2] or "", r[3] or ""
            left_table, left_alias = "", ""
        else:
            table, alias = r[3] or "", r[4] or ""
            left_table = (r[1] or "").split(".")[-1] if r[1] else ""
            left_alias = r[2] or ""
        if not table or table.startswith("虚拟逻辑表"):
            continue
        key = (table, alias)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "table": table, "alias": alias,
            "join_type": r[5] or "", "on_condition": r[6] or "",
            "left_table": left_table, "left_alias": left_alias,
            "target_schema": ctx.target_schema,
            "target_cn": ctx.target_cn,
            "target_table": ctx.target_table,
        })
    return rows


def render_mapping_xlsx(model: dict, output_path) -> Path:
    """mapping 行模型 → mapping.xlsx（实体级 + 属性级）。"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill(start_color="4472C4", end_color="4472C4",
                              fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)

    def _style(ws, headers):
        ws.append(headers)
        for col in range(1, len(headers) + 1):
            c = ws.cell(row=1, column=col)
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="center", vertical="center",
                                    wrap_text=True)
        ws.row_dimensions[1].height = 32
        ws.freeze_panes = "A2"

    wb = openpyxl.Workbook()

    # Sheet 1: 实体级
    ws1 = wb.active
    ws1.title = "实体级mapping"
    _style(ws1, ["源表schema", "源表物理表名", "源表别名", "源表中文名",
                 "目标表schema", "目标表中文名", "目标表物理表名",
                 "关联&限定条件", "备注"])
    for e in model["entity"]:
        ws1.append([
            "", e["table"], e["alias"], "",
            e["target_schema"], e["target_cn"], e["target_table"],
            e["on_condition"], e["join_type"],
        ])
    for i, w in enumerate([10, 24, 10, 18, 10, 18, 24, 60, 10], 1):
        ws1.column_dimensions[get_column_letter(i)].width = w

    # Sheet 2: 属性级
    ws2 = wb.create_sheet("属性级mapping")
    _style(ws2, ["场景", "源表schema", "源表物理表名", "源表别名", "源字段名",
                 "源字段类型", "映射描述", "映射表达式",
                 "目标字段名", "目标字段中文名", "目标字段类型"])
    for a in model["attribute"]:
        ws2.append([
            a["scenario"], a["schema"], a["table"], a["alias"],
            a["source_field"], "", a["type_label"], a["expr"],
            a["target_field"], a["target_cn"], a["target_type"],
        ])
    for i, w in enumerate([18, 10, 22, 10, 16, 12, 12, 60, 16, 18, 14], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return out


def export_mapping(knowledge: dict, engine_ctx: dict, output_dir) -> Path:
    """编排：同源装配 → 渲染 → mapping.xlsx。"""
    model = build_mapping_model(knowledge, engine_ctx)
    out = render_mapping_xlsx(model, Path(output_dir) / "mapping.xlsx")
    for w in model.get("warnings", []):
        if w.get("severity") == "structural":
            print(f"  [mapping·质量门] {w.get('title', '')}")
    print(f"  [OK] mapping.xlsx: {out}")
    return out
