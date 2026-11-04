#!/usr/bin/env python3
"""mapping_export —— mapping.xlsx 投影层（与 SPEC 同源装配，模板 v2 2026-11-04）。

架构：直接消费 spec_export.build_spec_model 的装配结果（Sheet3 实体级行 +
Sheet4 字段行），保证 mapping 与 需求SPEC 逐格一致——不是第二套理解。
mapping 特有：多场景分组（分组列）+ mapping模板.xlsx 列型 + DDL 元数据填充
（源表/字段中文名、字段类型、主键备注）。

模板 v2 口径（docs/17 同源，2026-11-04 拍板）：
- 实体级 13 列：取数规则留空；关联方式并入「关联&限定条件」前缀（LEFT JOIN: …）；
  调度三列（调度任务名称/执行路径/依赖参数）不再承载——SPEC Sheet2 独占。
- 属性级 15 列：分组=场景（单场景 default）；映射规则=四枚举；直接复制表达式='-'；
  备注=主键（DDL distribute key/pk）；数据标准留空（平台侧补）。

Author: 院博
"""
from __future__ import annotations

import re
from pathlib import Path

from viewmodel import build_context, split_schema_table

# 打包基准（docs/templates/mapping模板.xlsx 的入库副本；列头指纹测试锁两者一致）
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "mapping模板.xlsx"

# 列头指纹基准（机制同 SPEC §八）：模板换版 → 此处 + 模板文件同步更新 + 测试红。
# 逐字保真（含全角括号/星号），不要手敲——从模板文件复制。
MAPPING_TEMPLATE_HEADERS = {
    "实体级mapping": [
        "序号", "分组", "源表schema*", "源表中文名", "源表物理表名*", "源表别名*",
        "目标表逻辑schema*", "目标表中文名", "目标表物理名称*", "取数规则",
        "关联&限定条件", "备注", "数据库类型",
    ],
    "属性级mapping": [
        "序号", "分组", "源Schema", "源表物理表名", "源表别名", "源表字段中文名",
        "源表字段名", "源表字段类型", "映射规则*", "映射表达式", "目标字段名*",
        "目标字段中文名", "目标字段类型", "备注", "数据标准",
    ],
}

# 列宽（可读性；不构成契约）
_SHEET_WIDTHS = {
    "实体级mapping": [6, 10, 14, 20, 24, 10, 14, 20, 24, 12, 60, 12, 10],
    "属性级mapping": [6, 10, 14, 24, 10, 18, 18, 14, 10, 60, 18, 18, 14, 10, 10],
}


class MappingExportError(Exception):
    """mapping 导出问题（指纹不符等）。调用方捕获后 → stderr + 产物不落盘。"""


def check_template_fingerprint(template_path=DEFAULT_TEMPLATE) -> list:
    """模板列头 vs MAPPING_TEMPLATE_HEADERS 逐字比对。返回差异清单（空 = 通过）。"""
    import openpyxl
    diffs = []
    wb = openpyxl.load_workbook(template_path, read_only=True)
    try:
        if wb.sheetnames != list(MAPPING_TEMPLATE_HEADERS.keys()):
            diffs.append(f"sheet 名不一致: 模板={wb.sheetnames} "
                         f"基准={list(MAPPING_TEMPLATE_HEADERS.keys())}")
        for name, expect in MAPPING_TEMPLATE_HEADERS.items():
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
            actual = [str(c) if c is not None else "" for c in row]
            while actual and actual[-1] == "":
                actual.pop()   # 模板里 straggler 空单元格不构成列头
            if actual != expect:
                diffs.append(f"[{name}] 列头不一致:\n  模板={actual}\n  基准={expect}")
    finally:
        wb.close()
    return diffs


def _ensure_subquery_chains(knowledge: dict, engine_ctx: dict) -> dict:
    """engine_ctx 缺子查询链时从 knowledge data_flow 重建（独立路径自足）。

    view_generator --input knowledge_draft.json 独立路径没有 engine_ctx
    （subquery_field_chains / subquery_sqls 只在 pipeline_core 装配时产出）。
    data_flow.steps[].raw_sql 在 knowledge 里，重新跑 extract_subquery_
    field_chains 即可还原——与引擎同源，不是二次理解。
    """
    if engine_ctx.get("subquery_field_chains") is not None:
        return engine_ctx  # 已有，不重建

    from engine import parse_single_sql, extract_subquery_field_chains
    chains, sqls = {}, {}
    dialect = knowledge.get("meta", {}).get("dialect", "dws")
    for step in knowledge.get("data_flow", {}).get("steps", []):
        raw_sql = step.get("raw_sql", "")
        if not raw_sql:
            continue
        try:
            parsed = parse_single_sql(raw_sql, dialect)
            ch, sq = extract_subquery_field_chains(parsed)
            sid = step.get("step_id", "")
            if ch:
                chains[sid] = ch
            if sq:
                sqls[sid] = sq
        except Exception:
            continue
    engine_ctx = dict(engine_ctx or {})
    if chains:
        engine_ctx["subquery_field_chains"] = chains
    if sqls:
        engine_ctx["subquery_sqls"] = sqls
    return engine_ctx


# ════════════════════════════════════════════════════════════
# DDL 元数据（模板 v2 新增列的数据源）
# ════════════════════════════════════════════════════════════

def _catalog_index(engine_ctx: dict) -> dict:
    """engine_ctx.table_catalog → {(schema,table 小写): {comment, fields}}。

    table_catalog 键 = "schema.table"（小写），值 = {field: {type, comment,
    is_pk, is_distribute_key}, "__table_comment__": 表级中文名}。
    独立路径（无 engine_ctx）→ 空 dict，新列留空人工补——DDL 是可选增强不阻塞。
    """
    cat = (engine_ctx or {}).get("table_catalog") or {}
    out = {}
    for key, fields in cat.items():
        schema, _, table = str(key).rpartition(".")
        fields = fields or {}
        out[(schema, table)] = {
            "comment": fields.get("__table_comment__", ""),
            "fields": {k: v for k, v in fields.items()
                       if not str(k).startswith("__")},
        }
    return out


def _table_meta(idx: dict, schema: str, table: str) -> dict | None:
    """按 (schema, table) 查 catalog；schema 一侧缺失时按表名兜底。"""
    if not table:
        return None
    key = ((schema or "").lower(), table.lower())
    if key in idx:
        return idx[key]
    for (s, t), v in idx.items():
        if t == key[1] and (not key[0] or not s):
            return v
    return None


def _field_meta(table_meta: dict | None, field: str) -> dict:
    """表元数据里查单字段（小写对齐）；查不到返回空 dict。"""
    if not table_meta or not field:
        return {}
    fld = re.sub(r"^\w+\.", "", field.strip()).lower()   # 去别名前缀
    return table_meta.get("fields", {}).get(fld, {})


def _pk_fields(meta: dict | None) -> set:
    """表的主键字段集（DDL pk / distribute key，user 口径二者皆主键）。"""
    if not meta:
        return set()
    return {f for f, v in meta.get("fields", {}).items()
            if v.get("is_pk") or v.get("is_distribute_key")}


# ════════════════════════════════════════════════════════════
# 装配（build_spec_model 同源）
# ════════════════════════════════════════════════════════════

def build_mapping_model(knowledge: dict, engine_ctx: dict) -> dict:
    """knowledge + engine_ctx → mapping 行模型（模板 v2 列型）。

    多场景资产：每场景各调一次 build_spec_model（select_scenario 过滤后），
    属性级行分组列带场景标签；实体级行取首个场景（表结构跨场景不变）。
    单场景：直接 build_spec_model，分组列 = 'default'（模板约定）。
    """
    from spec_export import build_spec_model, list_scenarios, select_scenario

    engine_ctx = _ensure_subquery_chains(knowledge, engine_ctx or {})
    ctx = build_context(knowledge)
    scenarios = list_scenarios(knowledge)
    catalog_idx = _catalog_index(engine_ctx)

    # 目标表主键集（属性级 备注='主键' 的数据源）
    tgt_meta = _table_meta(catalog_idx, ctx.target_schema, ctx.target_table)
    target_pk = _pk_fields(tgt_meta)

    all_fields = []
    entity_rows = None
    if len(scenarios) <= 1:
        model = build_spec_model(knowledge, engine_ctx)
        entity_rows = model["sheet3"]
        for r in model["sheet4"]:
            all_fields.append(_to_attr_row(r, "default", ctx, catalog_idx, target_pk))
    else:
        for sc in scenarios:
            k_sc = select_scenario(knowledge, sc)
            model = build_spec_model(k_sc, engine_ctx)
            if entity_rows is None:
                entity_rows = model["sheet3"]
            sc_label = sc.get("partition") or sc.get("name") or ""
            for r in model["sheet4"]:
                all_fields.append(_to_attr_row(r, sc_label, ctx, catalog_idx, target_pk))

    return {
        "entity": _to_entity_rows(entity_rows or [], ctx, catalog_idx),
        "attribute": all_fields,
        "warnings": model.get("disclosures", []),
    }


def _to_attr_row(r, group: str, ctx, catalog_idx: dict, target_pk: set) -> dict:
    """SPEC Sheet4 行 → mapping 属性级行（模板 v2 15 列）。"""
    target_types = ctx.meta.get("target_field_types", {}) or {}
    target_comments = ctx.meta.get("target_field_comments", {}) or {}
    fname = r[10] or ""
    type_label = r[5] or ""

    def _tc(n):
        for k in (n, n.lower(), n.upper()):
            if k in target_comments:
                return target_comments[k]
        return ""

    # 源表可能是多表合并（a、b 顿号分隔）——取第一张的 schema + DDL 元数据
    src_tables = (r[2] or "").split("、")
    src_table = src_tables[0] if src_tables else ""
    src_schema = ""
    for es in ctx.entity_source_tables():
        if es.table == src_table:
            src_schema = es.schema
            break
    src_alias = r[3] or ""

    # 源字段中文名/类型：单字段且 DDL 收录时填充；多源合并留空（人工补）
    src_field = r[4] or ""
    src_field_cn, src_field_type = "", ""
    if "；" not in src_field:
        fm = _field_meta(_table_meta(catalog_idx, src_schema, src_table), src_field)
        src_field_cn = fm.get("comment", "")
        src_field_type = fm.get("type", "")

    # 映射表达式：直接复制 = '-'（模板约定）；赋值/数据加工 = 展开态表达式
    expr = "-" if type_label == "直接复制" else (r[6] or "-")

    # 备注：目标字段命中 DDL 主键 → '主键'；其余留空（审计字段等人工补）
    remark = "主键" if fname.lower() in target_pk else ""

    return {
        "group": group,
        "schema": src_schema,
        "table": r[2] or "",            # 源表（多源顿号合并）
        "alias": src_alias,             # 源表别名
        "src_field_cn": src_field_cn,   # 源表字段中文名（DDL）
        "src_field": src_field,         # 源字段（多源分号合并）
        "src_field_type": src_field_type,  # 源字段类型（DDL）
        "type_label": type_label,       # 四枚举（映射规则）
        "expr": expr,                   # 展开态表达式（含说明段）
        "target_field": fname,
        "target_cn": r[11] or _tc(fname),
        "target_type": target_types.get(fname, ""),
        "remark": remark,
    }


def _to_entity_rows(sheet3: list, ctx, catalog_idx: dict) -> list:
    """SPEC Sheet3 行 → mapping 实体级行（模板 v2 13 列，每物理表一行·表级合并）。

    同物理表多处参与（子查询内外两层别名、UNION 分支、大小写混写）合并为
    一行：别名顿号合并、关联/限定段全保留——表不重复，信息不丢。

    Sheet3 行格式 [id, left_tbl, left_alias, right_tbl, right_alias, join_type, g]：
    - 主表行（join_type=主表）：表名在 r[2]、别名在 r[3]（tmp 链模型特有布局）
    - E1 单表行（右表是占位符）：表名在 r[1]、别名在 r[2]
    - 其余行：右表在 r[3]、右别名在 r[4]（标准左/右布局）
    - 虚拟逻辑表行：不注册（C3 物理表才注册），但其限制/收敛段并入驱动表
      主表行——物化无关铁律：表不出现，信息不丢。

    驱动表补注册（C3）：包装形态下驱动表只作为 JOIN 行左表出现、没有显式
    主表行——左表首次被引用时注册为主表行（模板首行=主表行约定）。

    关联方式并入口径（2026-11-04 拍板）：join 行「关联&限定条件」= "LEFT JOIN: {g}"；
    主表/E1 行 g 原样（段式前缀已含 限制/写入前限制），备注='主表'。
    """
    # schema 查找表（从 entity_source_tables 建 {短表名→schema}）
    schema_of = {}
    for es in ctx.entity_source_tables():
        if es.table:
            schema_of[es.table.lower()] = es.schema

    # 预扫：虚拟逻辑表行（包装层 限制/窗口收敛段）→ 按驱动表归集
    virtual_segs = {}
    for r in sheet3:
        if (r[3] or "").startswith("虚拟逻辑表"):
            virtual_segs.setdefault((r[1] or "", r[2] or ""), []).append(r[6] or "")

    # 表级 upsert：key = 表名小写（同物理表多处参与——别名 b、t2 / UNION 分支
    # a、b / 大小写混写——合并为一行：别名顿号合并，条件段全保留，信息不丢）
    by_table = {}
    order = []

    def _upsert(table, alias, on_cell, main=False):
        if not table or table.startswith("虚拟逻辑表"):
            return
        key = table.lower()
        row = by_table.get(key)
        if row is not None:
            if alias and alias not in row["_aliases"]:
                row["_aliases"].append(alias)
            if on_cell:
                row["_on"].append(on_cell)
            if main:
                row["remark"] = "主表"
            return
        schema = schema_of.get(key, "")
        meta = _table_meta(catalog_idx, schema, table)
        by_table[key] = {
            "schema": schema,
            "table_cn": (meta or {}).get("comment", ""),
            "table": table,
            "_aliases": [alias] if alias else [],
            "target_schema": ctx.target_schema,
            "target_cn": ctx.target_cn,
            "target_table": ctx.target_table,
            "rule": "",                    # 取数规则（模板留空，2026-11-04 口径）
            "_on": [on_cell] if on_cell else [],
            "remark": "主表" if main else "",
            "db_type": "DWS",
        }
        order.append(key)

    for r in sheet3:
        if r[5] == "主表":
            # tmp 链主表行：r[2]=表名、r[3]=别名（SPEC 特有布局）
            _upsert(r[2] or "", r[3] or "", r[6] or "", main=True)
        elif (r[3] or "").startswith("—"):
            # E1 单表行：r[1]=表名、r[2]=别名（右表是占位符）
            _upsert(r[1] or "", r[2] or "", r[6] or "", main=True)
        elif (r[3] or "").startswith("虚拟逻辑表"):
            continue  # 预扫已归集，段随后并入驱动表主表行
        else:
            # 标准关联行：左表首引用 → 先注册（每物理表一行）。
            # 【b 内】标记 = 子查询块内行：其左表是块内驱动表，不是目标表主表
            # ——main=False（块内语义在该块首行 ON 上，备注不占主表位）。
            left_tbl = (r[1] or "").split(".")[-1]
            m_block = re.match(r"【(\w+)\s+内】", r[6] or "")
            if m_block:
                _upsert(left_tbl, r[2] or "", f"【{m_block.group(1)} 内】")
            else:
                _upsert(left_tbl, r[2] or "", "", main=True)
            kind = r[5] or ""
            on_cell = f"{kind}: {r[6] or ''}" if r[6] else kind
            _upsert(r[3] or "", r[4] or "", on_cell)

    # 虚拟逻辑表段并入驱动表行（驱动表未注册过则此时补注册为主表）；
    # 纯交叉引用段（"【x 内】关联见上"，SPEC 行间导航语）不进 mapping
    for (tbl, alias), segs in virtual_segs.items():
        for seg in segs:
            if seg and not re.fullmatch(r"【\w+\s+内】关联见上", seg.strip()):
                _upsert(tbl, alias, seg, main=True)

    for key in order:
        row = by_table[key]
        row["alias"] = "、".join(row.pop("_aliases"))
        row["on_condition"] = "；".join(row.pop("_on"))
    return [by_table[k] for k in order]


# ════════════════════════════════════════════════════════════
# 渲染（复制模板 + 清示例行 + 追加）
# ════════════════════════════════════════════════════════════

def render_mapping_xlsx(model: dict, output_path, template_path=None) -> Path:
    """mapping 行模型 → mapping.xlsx（复制模板 + 追加行；模板列头/样式原样保留）。

    模板自带示例数据行（第 2 行起）——渲染前先清空，只留表头样式。
    """
    import openpyxl
    from openpyxl.utils import get_column_letter

    template_path = Path(template_path) if template_path else DEFAULT_TEMPLATE
    diffs = check_template_fingerprint(template_path)
    if diffs:
        raise MappingExportError(
            "mapping 模板列头指纹不符（模板换版须同步基准+测试）:\n"
            + "\n".join(diffs))

    entity_rows = [
        [str(i), "default", e["schema"], e["table_cn"],
         e["table"], e["alias"], e["target_schema"], e["target_cn"], e["target_table"],
         e["rule"], e["on_condition"], e["remark"], e["db_type"]]
        # 分组列固定 default——表关联结构跨场景不变（属性级才分场景）
        for i, e in enumerate(model["entity"], 1)
    ]
    attr_rows = [
        [str(i), a["group"], a["schema"], a["table"], a["alias"], a["src_field_cn"],
         a["src_field"], a["src_field_type"], a["type_label"], a["expr"],
         a["target_field"], a["target_cn"], a["target_type"], a["remark"], ""]
        for i, a in enumerate(model["attribute"], 1)
    ]

    wb = openpyxl.load_workbook(template_path)
    for title, rows in (("实体级mapping", entity_rows), ("属性级mapping", attr_rows)):
        ws = wb[title]
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row - 1)   # 清模板示例行
        for row in rows:
            ws.append(row)
        widths = _SHEET_WIDTHS.get(title)
        if widths:
            for i, w in enumerate(widths, 1):
                if i <= ws.max_column:
                    ws.column_dimensions[get_column_letter(i)].width = w

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return out


def export_mapping(knowledge: dict, engine_ctx: dict, output_dir,
                   template_path=None) -> Path:
    """编排：同源装配 → 指纹门 → 渲染 → mapping.xlsx。

    指纹不符 / 装配异常向上抛（不静默降级——旧 12 列回退逻辑已删除，
    漂移必须显式红）。
    """
    model = build_mapping_model(knowledge, engine_ctx)
    out = render_mapping_xlsx(model, Path(output_dir) / "mapping.xlsx",
                              template_path=template_path)
    for w in model.get("warnings", []):
        if w.get("severity") == "structural":
            print(f"  [mapping·质量门] {w.get('title', '')}")
    print(f"  [OK] mapping.xlsx: {out}")
    return out
