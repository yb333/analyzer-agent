#!/usr/bin/env python3
"""spec_export —— SPEC 通道投影层：ViewContext + engine_ctx → 需求SPEC.xlsx。

定位（docs/17-SPEC列级映射矩阵.md）：
    人向交付通道的投影器，与 asset_facts / ts_draft 同源（共享 pipeline_core 装配 +
    解析引擎 + ViewContext 取数层），但契约自成一体：
    - 进门验 = 模板列头指纹（§八，非 JSON schema）；
    - 列级口径 = docs/17 §一~§五（R/D/E/N 逐列标注）；
    - 两态披露（没给 vs 给了没匹配上）进 xlsx 备注 + 返回值，交付门打印人话。

分层：
    build_spec_model()  取数 → SpecModel（纯行模型 dict，不碰 openpyxl，可单测）
    render_spec_xlsx()  SpecModel → xlsx（模板复制 + 追加行）
    export_spec()       编排：指纹门 → model → render → self_check

Author: 院博
"""

from __future__ import annotations

import re
from pathlib import Path

from viewmodel import (build_context, resolve_on_condition_aliases, norm_name,
                       split_schema_table)
from spec_extract import (build_sheet3_rows, decompose_field_logic,
                          predicate_conservation, _subquery_index,
                          _tmp_chain_index, prefix_root_fields,
                          strip_alias_prefix)

# 打包基准（docs/需求SPEC.xlsx 的入库副本；列头指纹测试锁两者一致）
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "需求SPEC模板.xlsx"

# 列头指纹基准（docs/17 §八）：模板换版 → 此处 + 模板文件同步更新 + 测试红。
# 逐字保真（含全角括号/星号），不要手敲——从模板文件复制。
SPEC_TEMPLATE_HEADERS = {
    "1业务背景": [
        "*序号", "*US编码", "*业务背景", "使用场景", "使用角色", "下游消费方", "需求价值",
    ],
    "2数据源清单": [
        "记录ID", "*源系统", "*源表（格式：数据库类型//数据库/schema/表名）",
        "*源表中文说明（逻辑实体名称）", "*主键字段",
        "*数据源调度任务名", "*数据源调度应用", "*数据源调度项目", "*数据源调度任务组",
        "*数据源调度Pjob名称", "*数据源调度方式", "*数据源调度频率", "备注",
    ],
    "3表关联": [
        "记录ID", "*左表", "左表别名", "*右表", "右表别名", "*关联方式",
        "*关联条件&限制条件",
    ],
    "4需求字段": [
        "记录ID", "*业务需求字段(源表字段)", "*源表", "源表别名", "*源表字段",
        "*类型(直接复制/数据加工/赋值/序列)", "非直取字段加工逻辑",
        "*资产设计参考_新增优化判断",
        "*资产表名(schema.表名)", "*资产中文名称", "*资产字段", "*资产字段中文名称", "*备注",
    ],
    "5业务样例数据": [],
}

# 列宽（可读性；不构成契约）
_SHEET_WIDTHS = {
    "2数据源清单": [8, 10, 24, 20, 12, 24, 12, 14, 12, 24, 10, 14, 60],
    "3表关联": [8, 22, 10, 22, 10, 12, 60],
    "4需求字段": [8, 16, 22, 10, 14, 12, 70, 14, 22, 18, 14, 16, 60],
}


class SpecExportError(Exception):
    """SPEC 导出问题（指纹不符等）。通道捕获后 → stderr + exit 1，产物不落盘。"""


# ════════════════════════════════════════════════════════════
# 模板指纹（§八）
# ════════════════════════════════════════════════════════════

def check_template_fingerprint(template_path=DEFAULT_TEMPLATE) -> list:
    """模板列头 vs SPEC_TEMPLATE_HEADERS 逐字比对。返回差异清单（空 = 通过）。"""
    import openpyxl
    diffs = []
    wb = openpyxl.load_workbook(template_path, read_only=True)
    try:
        if wb.sheetnames != list(SPEC_TEMPLATE_HEADERS.keys()):
            diffs.append(f"sheet 名不一致: 模板={wb.sheetnames} "
                         f"基准={list(SPEC_TEMPLATE_HEADERS.keys())}")
        for name, expect in SPEC_TEMPLATE_HEADERS.items():
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


# ════════════════════════════════════════════════════════════
# 场景（§〇 方案 A：单场景口径，多场景由入口 --scenario 指定后过滤）
# ════════════════════════════════════════════════════════════

def _scenario_index(knowledge: dict) -> list:
    """拓扑 scenarios → 非公共场景清单（公共步骤不构成可选场景）。

    数据源 = knowledge.topology.scenarios（engine build_scenarios 的结构化产出，
    {id, name, partition, is_common}；单场景资产也有一个"默认场景"条目）。
    """
    out = []
    for sc in knowledge.get("topology", {}).get("scenarios", []):
        if sc.get("is_common"):
            continue
        out.append({"id": sc.get("id", ""), "name": sc.get("name", ""),
                    "partition": sc.get("partition", "")})
    return out


def list_scenarios(knowledge: dict) -> list:
    """可选场景清单 [{id, name, partition}]；len>1 即多场景资产。"""
    return _scenario_index(knowledge)


def find_scenario(knowledge: dict, token: str):
    """--scenario 词法：partition（P_CUR）> 场景名 > 场景 id，三者任一匹配即中。"""
    for sc in _scenario_index(knowledge):
        if token and token in (sc["partition"], sc["name"], sc["id"]):
            return sc
    return None


def select_scenario(knowledge: dict, chosen: dict) -> dict:
    """多场景 knowledge → 单场景 knowledge（保留公共步骤；不改入参）。

    过滤面：topology.steps / data_flow.steps / field_mappings.fields 三处按
    step_id 一致收敛；meta 原样（final_target 等事实不随场景变）。
    """
    keep_name = chosen.get("name", "")

    def _keep(s):
        return (s.get("scenario_name", "") == keep_name
                or s.get("is_common_step", False)
                or s.get("scenario_id", "") == "scenario_common"
                or s.get("scenario_name", "") in ("", "公共步骤"))

    topo_steps = [s for s in knowledge.get("topology", {}).get("steps", []) if _keep(s)]
    keep_ids = {s.get("step_id", "") for s in topo_steps}
    df_steps = [s for s in knowledge.get("data_flow", {}).get("steps", [])
                if s.get("step_id", "") in keep_ids]
    fields = [f for f in knowledge.get("field_mappings", {}).get("fields", [])
              if f.get("producing_step", "") in keep_ids]
    return {
        **knowledge,
        "topology": {**knowledge.get("topology", {}), "steps": topo_steps},
        "data_flow": {**knowledge.get("data_flow", {}), "steps": df_steps},
        "field_mappings": {**knowledge.get("field_mappings", {}), "fields": fields},
    }


# ════════════════════════════════════════════════════════════
# SpecModel 构建（取数层，纯 dict；列口径 = docs/17 §二~§四）
# ════════════════════════════════════════════════════════════

def _catalog_for(catalog: dict, schema: str, table: str) -> dict:
    """table_catalog 查表（"schema.table" 小写 → 裸表名小写兜底）。无 → {}。"""
    key = f"{schema}.{table}".lower() if schema else table.lower()
    if key in catalog:
        return catalog[key]
    return catalog.get(table.lower(), {})


def _source_remark(dist_cols, has_catalog, lts_remark):
    """Sheet2 备注拼装（分布键标识 / DDL 缺失 / LTS 两态，docs/17 §二）。"""
    parts = []
    if dist_cols:
        parts.append("主键字段列为分布键（DDL无主键约束，取DISTRIBUTE BY）")
    if not has_catalog:
        parts.append("DDL未覆盖该表，中文说明/主键留空")
    if lts_remark:
        parts.append(lts_remark)
    return "；".join(parts)


def build_spec_model(knowledge: dict, engine_ctx: dict,
                     schedule_lookup=None, schedule_unmatched=None,
                     lts_remark: str = "") -> dict:
    """knowledge + engine_ctx → SpecModel（五个 sheet 的行模型 + 披露清单）。

    Args:
        schedule_lookup: {查找键: 调度列 dict} schedule_match 匹配器输出
            （查找键 = "schema.table".lower() 与裸表名小写双写）
        schedule_unmatched: 未匹配 LTS 任务行（尾部列举，docs/17 §二）；
            None=无 LTS 制品，[]=有 LTS 全命中
        lts_remark: LTS 两态备注之「没给」（有 LTS 时传空，行级备注走匹配器标注）
    """
    ctx = build_context(knowledge)
    catalog = engine_ctx.get("table_catalog", {}) or {}
    schedule_lookup = schedule_lookup or {}
    disclosures = []

    def _sched_for(schema, table):
        if schema:
            key = f"{schema}.{table}".lower()
            if key in schedule_lookup:
                return schedule_lookup[key]
        return schedule_lookup.get(table.lower(), {})

    # ── Sheet2 数据源清单：物理源表（穿透口径 entity_source_tables，§二）──
    sheet2 = []
    for es in ctx.entity_source_tables():
        cat = _catalog_for(catalog, es.schema, es.table)
        table_comment = cat.get("__table_comment__", "") if cat else ""
        props = cat.get("__table_props__", {}) if cat else {}
        dist_cols = list(props.get("distribute_columns") or [])
        dist_cols.sort()
        sched = _sched_for(es.schema, es.table)
        sheet2.append([
            None,                                   # 记录ID（N 不填）
            es.schema.upper() if es.schema else "",  # *源系统
            es.table,                               # *源表（仅表名，Q3 不编造前缀）
            table_comment,                          # *源表中文说明（DDL 表注释）
            ", ".join(dist_cols),                   # *主键字段（分布键，Q4）
            sched.get("task_name", ""),             # *调度任务名
            sched.get("app", ""),                   # *调度应用（E 留空，缺口1）
            sched.get("project", ""),               # *调度项目
            sched.get("group", ""),                 # *调度任务组
            sched.get("pjob", ""),                  # *调度Pjob名称
            sched.get("schedule_type", ""),         # *调度方式
            sched.get("cron", ""),                  # *调度频率
            _source_remark(dist_cols, bool(cat), lts_remark or sched.get("remark", "")),
        ])
    # LTS 未匹配任务行：尾部列举（源表五列留空，调度列照填，docs/17 §二）
    for u in (schedule_unmatched or []):
        sheet2.append([
            None, None, None, None, None,
            u.get("task_name", ""), u.get("app", ""), u.get("project", ""),
            u.get("group", ""), u.get("pjob", ""), u.get("schedule_type", ""),
            u.get("cron", ""), u.get("remark", ""),
        ])
    if not catalog:
        disclosures.append({
            "severity": "noise", "type": "ddl_not_provided",
            "title": "DDL未提供：源表中文说明/主键字段/资产字段中文名称列留空",
        })
    if schedule_unmatched is not None and not schedule_lookup:
        disclosures.append({
            "severity": "noise", "type": "lts_no_match",
            "title": "LTS制品已提供但未匹配到任何源表：任务全部列于清单尾部，请人工核对"
                     "（两态之「给了没匹配上」）",
        })

    # ── Sheet3 表关联：行集语义段式重组 + E1 单表行（§三，口径在 spec_extract）──
    sheet3 = build_sheet3_rows(ctx)

    # ── Sheet4 需求字段：终态字段 × 物理源穿透（§四；逻辑列=拆解式）──
    sheet4 = []
    target_field_comments = knowledge.get("meta", {}).get("target_field_comments", {}) or {}

    def _field_cn(fname):
        for k in (fname, fname.lower(), fname.upper()):
            if k in target_field_comments:
                return target_field_comments[k]
        return ""

    # 主查询 CTE 别名 → CTE 名 映射（cte_joins：{name: cte3, alias: c3}）
    cte_alias_to_name = {}
    for s in ctx.data_flow_steps:
        for cj in s.get("cte_joins", []):
            a, n = cj.get("alias", ""), cj.get("name", "")
            if a and n:
                cte_alias_to_name[a.lower()] = n

    # 子查询外层别名集合（包裹形态下穿透会停在子查询别名，如 b.total_amt）
    subq_alias_set = set()
    for _sid, _by in _subquery_index(ctx).items():
        subq_alias_set.update(a.lower() for a in _by)

    def _resolve_cte_physical(name_or_alias: str):
        """CTE 名/主查询 CTE 别名 → 主物理源表名（解析不出 → ""）。"""
        key = (name_or_alias or "").lower()
        if not key:
            return ""
        if name_or_alias.upper() in ctx.cte_names_upper:
            cte = name_or_alias
        else:
            cte = cte_alias_to_name.get(key, "")
        if not cte:
            return ""
        t, _a = ctx._cte_primary_physical(cte)
        return t

    def _spec_type_label(tt, ps_transforms):
        """类型列四枚举（模板 v2，2026-11-02）：直接复制/数据加工/赋值/序列。

        direct→直接复制；value→赋值；其余（含穿透 transform≠direct 的跨步加工）
        →数据加工。序列 = DDL 层生成列，静态分析不产（模板枚举占位）。
        """
        if tt == "value":
            return "赋值"
        if tt == "direct" and all((t or "direct") == "direct" for t in ps_transforms):
            return "直接复制"
        return "数据加工"

    # tmp 链物化无关上下文（展开态说明段素材：外层锚 + 分组/保留口径）
    chain_by_step: dict = {}
    for _sid, _ch in _tmp_chain_index(ctx).items():
        outer_anchor = _ch["mounts"][-1]["anchor"] if _ch["mounts"] else ""
        notes = []
        for _pid in _ch["producer_ids"]:
            _df = ctx.df_step(_pid)
            _gb = _df.get("group_by") or []
            if _gb:
                _seg = "按 " + ", ".join(
                    strip_alias_prefix(g) for g in _gb) + " 分组"
                _hv = (_df.get("having_clause") or "").strip()
                if _hv:
                    _hv_body = re.sub(r"^HAVING\s+", "", _hv, flags=re.I)
                    _seg += f"，保留 {strip_alias_prefix(_hv_body)}"
                notes.append(_seg)
        entry = {"anchor": outer_anchor, "note": "；".join(notes)}
        chain_by_step[_sid] = entry
        for _pid in _ch["producer_ids"]:
            chain_by_step.setdefault(_pid, entry)

    def _step_partition(step_id):
        """步骤场景分区名（"场景1 (分区: P_CUR)" → P_CUR；默认场景无分区）。"""
        name = ctx.topo_step(step_id).get("scenario_name", "") or ""
        m = re.match(r"场景\d+\s*\(分区:\s*(.+?)\)", name)
        return m.group(1) if m else ""

    for f in ctx.final_fields(group_by_scenario=False):
        tt = f.get("transform_type", "expression")
        fname = f.get("target_field", "")
        phys = ctx.resolve_field_sources(f)
        common_tail = [None,                      # *资产设计参考（E 留空，154c435）
                       ctx.target_table_full,     # *资产表名(schema.表名)
                       ctx.target_cn,             # *资产中文名称
                       fname,                     # *资产字段
                       _field_cn(fname)]          # *资产字段中文名称（DDL 列注释）

        # ── 多源合并为一行（资产字段列不可重复，2026-11-02 实测修订）──
        # CASE WHEN a,b 之类多来源字段：源表/别名/源字段列内 "、" 合并，逻辑列一条
        tables, aliases, src_ref_parts, remarks = [], [], [], []
        ps_transforms = []
        best_raw = ""
        seen_tf = set()
        for ps in phys:
            ps_table_full = ps.get("table", "") or ""
            remark = ""
            # CTE 内加工字段的穿透缺口（docs/17 §十缺口3）：table 为空 / CTE 名 /
            # 主查询 CTE 别名 → 解析 CTE 主物理源回填；解析不出 → 留空+备注
            resolved = _resolve_cte_physical(ps_table_full)
            if not resolved and ps_table_full.upper() not in ctx.cte_names_upper:
                resolved = _resolve_cte_physical(ps.get("alias", ""))
            is_sq_alias = bool(ps_table_full) and ps_table_full.lower() in subq_alias_set
            if (not is_sq_alias and not resolved and ps_table_full
                    and "." not in ps_table_full and ps.get("step_id")):
                # 包裹形态下嵌套子查询别名可能不在任何引擎结构里（只在 SQL 文本）
                # → 按 ") 别名 ON" 形态从步骤原文识别
                raw = ctx.df_step(ps["step_id"]).get("raw_sql", "")
                if re.search(rf"\)\s*{re.escape(ps_table_full)}\s+ON", raw):
                    is_sq_alias = True
            if not resolved and is_sq_alias:
                # 包裹形态穿透缺口（docs/17 §十缺口6）：引擎穿透停在子查询别名
                # （b.total_amt），底层物理源/加工只在子查询原文里 → 保守呈现：
                # 源表标虚拟表+指路表3，类型保守判数据加工，不编造物理源
                marker = f"（虚拟表 {ps_table_full}）"
                if marker not in tables:
                    tables.append(marker)
                remark = "字段经子查询加工，物理源与加工逻辑见表3预处理贴码"
                src_ref_parts.append((ps.get("alias", ""),
                                      ps.get("field", fname) or ""))
                ps_transforms.append("subquery")
                if remark not in remarks:
                    remarks.append(remark)
                continue
            if resolved:
                ps_table_full = resolved
                remark = "源表经CTE预处理，物理源见表3预处理段"
            elif (not ps_table_full
                  or ps_table_full.upper() in ctx.cte_names_upper
                  or ps.get("alias", "").lower() in cte_alias_to_name):
                remark = "无物理源表（经CTE加工，见表3预处理段）"
                if ps_table_full.upper() in ctx.cte_names_upper:
                    ps_table_full = ""
            _, ps_table = split_schema_table(ps_table_full)
            ps_field = ps.get("field", fname) or ""
            key = (ps_table, ps_field)
            if key not in seen_tf:
                seen_tf.add(key)
                if ps_table and ps_table not in tables:
                    tables.append(ps_table)
                # 别名：穿透项自带；缺失（pivot 等形态）用步骤别名表反查兜底
                a = ps.get("alias", "")
                if not a and ps.get("step_id"):
                    amap = ctx.step_alias_map(ps["step_id"])
                    for _al, _tbl in amap.items():
                        if norm_name(_tbl) == norm_name(ps_table_full):
                            a = _al.lower()   # 步骤别名表键为大写，呈现贴原文小写
                            break
                src_ref_parts.append((a, ps_field))
            a = ps.get("alias", "")
            if a and a not in aliases:
                aliases.append(a)
            if remark and remark not in remarks:
                remarks.append(remark)
            t_ps = ps.get("transform", "")
            ps_transforms.append(t_ps)
            if (t_ps and t_ps != "direct" and ps.get("raw_sql") and not best_raw):
                best_raw = ps["raw_sql"]
        # 逻辑原文优先级：穿透加工段 raw → 字段 raw_expr → lineage 首条（拆不出就贴原文）
        if not best_raw:
            best_raw = f.get("raw_expr") or ""
        if not best_raw and tt != "direct" and f.get("lineage"):
            best_raw = f["lineage"][0].get("raw_sql", "")

        type_label = _spec_type_label(tt, ps_transforms)
        # 展开态表达式（C1 拍板，2026-11-02）：裸表达式 + 物理根别名.字段前缀
        # + （说明段：分组/保留/锚/场景）——引用位只允许物理根别名
        expr_bare = (decompose_field_logic(best_raw, tt)
                     if type_label != "直接复制" else "")
        roots = [(a, fl) for a, fl in src_ref_parts if a and fl]
        expr = prefix_root_fields(expr_bare, roots) if expr_bare else ""
        _chain = chain_by_step.get(f.get("producing_step", ""))
        note_parts = []
        if _chain:
            # 分组/保留口径只挂数据加工（聚合/表达式）——常量与分组行集无关
            if _chain["note"] and type_label == "数据加工":
                note_parts.append(_chain["note"])
            if _chain["anchor"]:
                note_parts.append(f"经逻辑从表 {_chain['anchor']}")
        if type_label == "赋值":
            _part = _step_partition(f.get("producing_step", ""))
            if _part:
                note_parts.append(f"场景 {_part} 常量")
        logic = f"{expr}（{'；'.join(note_parts)}）" if (expr and note_parts) else expr
        # 多字段形态（2026-11-02 实测修订⑥）：英文分号分隔，且每字段带 别名.字段
        # （多源时物理来源不唯一，裸字段名有歧义）；单字段保持裸字段名
        if len(src_ref_parts) > 1:
            merged_sources = ";".join(
                f"{a}.{fl}" if a else fl for a, fl in src_ref_parts)
        else:
            merged_sources = src_ref_parts[0][1] if src_ref_parts else None
        remark_out = "；".join(remarks)
        # 直取字段穿过逻辑从表 → 备注指锚（说明段只挂逻辑列）
        if (_chain and _chain["anchor"] and type_label == "直接复制"
                and "经逻辑从表" not in remark_out):
            remark_out = "；".join(x for x in [remark_out,
                                               f"经逻辑从表 {_chain['anchor']}"] if x)
        if not phys and tt != "direct" and not remark_out:
            remark_out = "常量字段，无物理源"
        sheet4.append([
            None,
            merged_sources,                          # *业务需求字段 = copy 源字段（Q5）
            "、".join(tables) if tables else None,   # *源表（多源合并）
            "、".join(aliases) if aliases else None, # 源表别名
            merged_sources,                          # *源表字段（多源合并）
            type_label, logic,
            *common_tail,
            remark_out,
        ])

    # ── 记录ID（2026-11-02 实测修订：生成，每 sheet 顺序编号 1..n）──
    for rows_ in (sheet2, sheet3, sheet4):
        for i, r in enumerate(rows_, 1):
            r[0] = i

    # 谓词守恒（docs/17 §七）：漏放任一 WHERE/HAVING → structural → verdict review
    for warn in predicate_conservation(ctx, sheet3, sheet4):
        disclosures.append({"severity": "structural",
                            "type": warn["type"], "title": warn["title"]})

    # 引用闭包 + 物化零痕迹（docs/14 §六性质1/7，四轮修订⑫）
    for warn in _reference_closure(ctx, sheet2, sheet3, sheet4):
        disclosures.append({"severity": "structural",
                            "type": warn["type"], "title": warn["title"]})
    for warn in _materialization_trace_check(ctx, sheet2, sheet3, sheet4):
        disclosures.append({"severity": "structural",
                            "type": warn["type"], "title": warn["title"]})

    return {
        "sheet1": [],       # 业务背景全人工（E 留空）
        "sheet2": sheet2,
        "sheet3": sheet3,
        "sheet4": sheet4,
        "sheet5": [],       # 业务样例数据（E 留空）
        "disclosures": disclosures,
        "target": {"schema": ctx.target_schema, "table": ctx.target_table,
                   "table_full": ctx.target_table_full, "cn": ctx.target_cn},
    }


# ════════════════════════════════════════════════════════════
# 渲染 + 编排
# ════════════════════════════════════════════════════════════

# SpecModel 键 → xlsx sheet 标题（渲染时一一对应）
_SHEET_ORDER = [
    ("sheet1", "1业务背景"),
    ("sheet2", "2数据源清单"),
    ("sheet3", "3表关联"),
    ("sheet4", "4需求字段"),
    ("sheet5", "5业务样例数据"),
]


def _reference_closure(ctx, sheet2: list, sheet3: list, sheet4: list) -> list:
    """引用闭包（docs/14 §六性质1）：Sheet4 表达式本体（剥除（说明段））里的
    全部 别名.字段 引用 ⊆ 登记别名集（数据源清单 + 表关联左右别名，含锚）。
    违规 → structural（读者按表达式找不到引用行 = 断链）。
    """
    aliases = set()
    for es in ctx.entity_source_tables():
        if es.alias:
            aliases.add(es.alias.lower())
    for r in sheet3:
        for cell in (r[2], r[4]):
            if cell:
                aliases.add(str(cell).strip().lower())
    warnings = []
    for r in sheet4:
        logic = r[6] if len(r) > 6 else None
        if not logic:
            continue
        body = re.sub(r"（[^）]*）", "", str(logic))   # 剥说明段
        for m in re.finditer(r"([A-Za-z_]\w*)\.(\w+)", body):
            alias = m.group(1).lower()
            if alias not in aliases:
                warnings.append({
                    "type": "reference_closure",
                    "title": f"引用闭包：表达式引用 {m.group(0)}（资产字段 "
                             f"{r[10]}）未在数据源清单/表关联的别名集中——断链，"
                             f"请人工核对",
                })
    return warnings


def _materialization_trace_check(ctx, sheet2: list, sheet3: list,
                                 sheet4: list) -> list:
    """物化零痕迹（docs/14 §六性质7）：登记值里不出现 tmp 表名与规则编号
    （片段原文除外——逐字性的必然豁免位：Sheet3 G 列）。
    """
    from engine import _is_intermediate_table
    tmp_names, rule_codes = set(), set()
    for s in ctx.steps_list:
        t = s.get("target_table", "")
        if t and _is_intermediate_table(t):
            sch, tbl = split_schema_table(t)
            tmp_names.add(tbl.lower())
            if sch:
                tmp_names.add(f"{sch}.{tbl}".lower())
        rc = s.get("rule_code", "")
        if rc:
            rule_codes.add(rc)
    warnings = []

    def _scan(cells, where):
        for c in cells:
            if not c:
                continue
            cv = str(c)
            for tn in tmp_names:
                if tn and tn in cv.lower():
                    warnings.append({
                        "type": "materialization_trace",
                        "title": f"物化零痕迹违规：{where} 出现中间表名 "
                                 f"「{tn}」——tmp 应压平为逻辑从表（锚+片段）",
                    })
            for rc in rule_codes:
                if rc in cv:
                    warnings.append({
                        "type": "materialization_trace",
                        "title": f"物化零痕迹违规：{where} 出现规则编号 "
                                 f"「{rc}」——SPEC 不携带物化实现事实",
                    })

    for r in sheet2:
        _scan(r, "数据源清单")
    for r in sheet3:
        _scan(r[:6], "表关联登记值")     # G 列（片段原文）豁免
    for r in sheet4:
        _scan(r, "需求字段")
    return warnings


def render_spec_xlsx(model: dict, template_path, output_path) -> Path:
    """SpecModel → xlsx（复制模板 + 追加行；模板列头/样式原样保留）。"""
    import openpyxl
    from openpyxl.utils import get_column_letter
    wb = openpyxl.load_workbook(template_path)
    for key, title in _SHEET_ORDER:
        ws = wb[title]
        for r in model.get(key, []):
            ws.append(r)
        widths = _SHEET_WIDTHS.get(title)
        if widths:
            for i, w in enumerate(widths, 1):
                if i <= ws.max_column:
                    ws.column_dimensions[get_column_letter(i)].width = w
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return out


def export_spec(knowledge: dict, engine_ctx: dict, output_path,
                template_path=None, schedule_lookup=None, schedule_unmatched=None,
                lts_remark: str = "") -> tuple:
    """编排：指纹门 → SpecModel → 渲染 → (产物路径, self_check, 披露清单)。

    self_check verdict 沿用本仓词表（pass / pass_with_noise / review）。
    """
    template_path = Path(template_path) if template_path else DEFAULT_TEMPLATE
    diffs = check_template_fingerprint(template_path)
    if diffs:
        raise SpecExportError(
            "模板列头指纹不符（docs/17 §八——模板换版须同步基准+测试）:\n"
            + "\n".join(diffs))
    model = build_spec_model(knowledge, engine_ctx,
                             schedule_lookup=schedule_lookup,
                             schedule_unmatched=schedule_unmatched,
                             lts_remark=lts_remark)
    out = render_spec_xlsx(model, template_path, output_path)
    disclosures = model["disclosures"]
    verdict = ("review" if any(d["severity"] == "structural" for d in disclosures)
               else ("pass_with_noise" if any(d["severity"] == "noise" for d in disclosures)
                     else "pass"))
    return out, {"verdict": verdict}, disclosures
