#!/usr/bin/env python3
"""baseline_v1 契约导出（优化场景存量资产投影）。

定位（docs/baseline_v1-契约.md）：
    knowledge_draft（逆向事实）→ baseline_v1（纯物理事实包），供 design-dev-agent
    优化场景作为外部资产的存量现状输入。纯投影：不改 draft、不做语义推断。

三条铁律（信息守恒）：
    1. 逐字保真——query_sql / delete_condition / raw_expr 逐字节保留解析原文；
    2. 显式空——拿不到的信息 = 显式 null / 空数组，不省略 key，不静默丢弃；
    3. 无假信息——结构化字段只承载确定性文法解析结果；解析不出 = null + warning，
       绝不伪造。结构化与原文并存（原文做比对锚点，结构化做消费输入）。

v1.1（analyzer 侧主导的增量）：
    rules[].write_plan —— delete_mode + delete_condition 按写入类型的确定性翻译。
    kind 映射按 platform_generation 由本模块维护（权威在 analyzer 侧）；
    condition_expr 逐字来自 delete_condition / 交换配置，来自 SQL 解析时为等价
    重排版（condition_source='query_sql' 标记）。

本模块由 analyzer.main 在 --export baseline_v1 时调用（不作为独立 CLI）：
    from baseline_export import export_baseline_v1
    path = export_baseline_v1(knowledge, ctx, output_dir)   # 校验不过抛 BaselineExportError

Author: 院博
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CONTRACT_VERSION = "1.1"
SUPPORTED_VERSIONS = {"1.0", "1.1"}

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "baseline_v1.schema.json"


class BaselineExportError(Exception):
    """baseline_v1 导出失败（schema/语义校验不过）。调用方应 fail loud，不落盘。"""


# ─────────────────────────────────────────────────────────────────
# 写入类型映射（platform_generation = "1.0" 权威表，随 analyzer 维护）
# 平台迁移（2.0）时此表换版，契约事实字段（delete_mode/delete_condition 原文）不变。
# ─────────────────────────────────────────────────────────────────
WRITE_KIND_BY_MODE = {
    # delete_mode → (kind, condition_role)
    "1": ("full_truncate", "none"),            # TRUNCATE TABLE
    "2": ("append", "none"),                   # 追加
    "3": ("subpartition_truncate", "subpartition_expr"),  # TRUNCATE SUBPARTITION
    "4": ("delete_by_condition", "where_predicate"),      # DELETE
    "5": ("partition_truncate", "partition_expr"),        # TRUNCATE PARTITION
    "6": ("merge_upsert", "on_predicate"),     # MERGE INTO
    "7": ("rpt_item", "unclear"),              # RPT_ITEM（平台语义未定）
    "8": ("update_by_condition", "where_predicate"),      # UPDATE
    "9": ("exchange_partition", "exchange_table"),        # EXCHANGE PARTITION
}

# 谓词角色：condition_columns 才有意义
_PREDICATE_ROLES = {"on_predicate", "where_predicate"}

# 限定列引用（alias.col / table.col），用于 condition_columns 提取
_QUALIFIED_REF_RE = re.compile(r"\b([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)\b")

_MERGE_RE = re.compile(r"\bmerge\s+into\b", re.IGNORECASE)


# ═════════════════════════════════════════════════════════════════
# write_plan 构建
# ═════════════════════════════════════════════════════════════════

def _extract_columns(expr: str) -> list:
    """从谓词文本提取限定列引用（原样文本，按出现顺序去重）。确定性提取，不做语义推断。"""
    seen = []
    for m in _QUALIFIED_REF_RE.finditer(expr or ""):
        ref = f"{m.group(1)}.{m.group(2)}"
        if ref not in seen:
            seen.append(ref)
    return seen


def _extract_merge_on_from_sql(sql_text: str):
    """query_sql 是 MERGE 语句时提取 ON 条件（等价重排版，非逐字）。

    返回条件文本；非 MERGE 语句 / 解析失败返回 None。
    """
    if not sql_text or not _MERGE_RE.search(sql_text):
        return None
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql_text)
    except Exception:
        return None
    try:
        if isinstance(tree, exp.Merge):
            # MERGE 的 ON 条件在 Merge.args["on"]（exp.On 是 JOIN 的）
            on_cond = tree.args.get("on")
        else:
            on_node = tree.find(exp.On)
            on_cond = on_node.this if on_node is not None else None
        if on_cond is None:
            return None
        text = on_cond.sql(dialect=None)
        return text or None
    except Exception:
        return None


def build_write_plan(step: dict, query_sql: str = ""):
    """由 topology step 构建 write_plan（+ dm=6 的 merge_on）。

    Returns: (write_plan, merge_on or None, warnings[list[dict]])
    """
    dm = str(step.get("delete_mode") or "")
    cond = str(step.get("delete_condition") or "")
    exchange_table = str(step.get("exchange_temp_table") or "")
    rule_code = str(step.get("rule_code") or "?")
    warnings = []

    if step.get("is_view_step"):
        kind, role = "view", "none"
    else:
        kind, role = WRITE_KIND_BY_MODE.get(dm, ("unknown", "unclear"))

    expr = None
    source = "none"

    if role == "on_predicate":
        # dm=6：优先平台删除条件列（逐字），否则从 MERGE SQL 抽 ON
        if cond.strip():
            expr, source = cond, "delete_condition"
        else:
            extracted = _extract_merge_on_from_sql(query_sql)
            if extracted:
                expr, source = extracted, "query_sql"
        # 仍取不到 → 校验层 fail loud（此处不伪造）
    elif role == "where_predicate":
        if cond.strip():
            expr, source = cond, "delete_condition"
        else:
            warnings.append({
                "type": "write_plan_gap",
                "severity": "medium",
                "rule_code": rule_code,
                "title": f"规则 {rule_code} delete_mode={dm}（{kind}）但删除条件为空——影响范围不可从条件界定",
            })
    elif role in ("partition_expr", "subpartition_expr"):
        if cond.strip():
            expr, source = cond, "delete_condition"
        else:
            warnings.append({
                "type": "write_plan_gap",
                "severity": "info",
                "rule_code": rule_code,
                "title": f"规则 {rule_code} delete_mode={dm}（{kind}）未携带分区条件（可能为整表分区级操作）",
            })
    elif role == "exchange_table":
        if exchange_table:
            expr, source = exchange_table, "exchange_config"
    elif role == "none":
        if cond.strip():
            # truncate/append 却带条件：如实携带 + 告警，不解释平台行为
            role = "unclear"
            expr, source = cond, "delete_condition"
            warnings.append({
                "type": "write_plan_gap",
                "severity": "info",
                "rule_code": rule_code,
                "title": f"规则 {rule_code} delete_mode={dm}（{kind}）携带了非预期删除条件，平台语义未定",
            })
    elif role == "unclear":
        if cond.strip():
            expr, source = cond, "delete_condition"

    if dm and not step.get("is_view_step") and dm not in WRITE_KIND_BY_MODE:
        warnings.append({
            "type": "write_plan_gap",
            "severity": "medium",
            "rule_code": rule_code,
            "title": f"规则 {rule_code} delete_mode={dm!r} 不在平台代次映射表内，write_plan.kind=unknown",
        })

    columns = _extract_columns(expr) if (expr and role in _PREDICATE_ROLES) else []

    write_plan = {
        "kind": kind,
        "condition_role": role,
        "condition_expr": expr,
        "condition_columns": columns,
        "condition_source": source,
    }
    merge_on = expr if (dm == "6" and expr) else None
    return write_plan, merge_on, warnings


# ═════════════════════════════════════════════════════════════════
# knowledge_draft → baseline_v1 投影
# ═════════════════════════════════════════════════════════════════

def _project_joins(df_step: dict) -> list:
    """物理表 join 事实；子查询假名与 CTE 名不进契约（非物理事实）。"""
    cte_names = {c.get("name", "") for c in (df_step.get("ctes") or [])}
    out = []
    for j in (df_step.get("joins") or []):
        st = str(j.get("source_table") or "")
        if not st or st.startswith("(subquery") or st in cte_names:
            continue
        out.append({
            "source_table": st,
            "alias": str(j.get("alias") or ""),
            "join_type": str(j.get("join_type") or ""),
            "join_condition": str(j.get("join_condition") or ""),
        })
    return out


def _project_rules(draft: dict):
    """topology.steps + data_flow.steps + source.raw_sql → rules[]（含 write_plan）。"""
    topo_steps = draft.get("topology", {}).get("steps", [])
    df_by_step = {s.get("step_id"): s for s in draft.get("data_flow", {}).get("steps", [])}
    raw_sql_by_rule = {r.get("rule_code"): r.get("sql", "")
                       for r in draft.get("source", {}).get("raw_sql", [])}

    rules_out = []
    write_plan_warnings = []
    for step in topo_steps:
        rule_code = str(step.get("rule_code") or "")
        df_step = df_by_step.get(step.get("step_id"), {})
        # query_sql 逐字：优先 source.raw_sql（解析原文附录），兜底 data_flow
        query_sql = str(raw_sql_by_rule.get(rule_code) or df_step.get("raw_sql") or "")

        write_plan, merge_on, wp_warns = build_write_plan(step, query_sql)
        write_plan_warnings.extend(wp_warns)

        rule = {
            "rule_code": rule_code,
            "rule_name": str(step.get("rule_name") or ""),
            "exec_sequence": step.get("exec_sequence", 0),
            "scenario_id": str(step.get("scenario_id") or ""),
            "is_common": bool(step.get("is_common_step", False)),
            "target_schema": str(step.get("target_schema") or ""),
            "target_table": str(step.get("target_table") or ""),
            "delete_mode": str(step.get("delete_mode") or ""),
            "delete_condition": str(step.get("delete_condition") or ""),
            "write_plan": write_plan,
            "query_sql": query_sql,
            "source_tables": [str(t) for t in (step.get("source_tables_from_sql") or [])],
            "joins": _project_joins(df_step),
            "where_clause": str(df_step.get("where_clause") or ""),
            "group_by": [str(g) for g in (df_step.get("group_by") or [])],
            "is_view_step": bool(step.get("is_view_step", False)),
        }
        if merge_on:
            rule["merge_on"] = merge_on
        if step.get("exchange_temp_table"):
            rule["exchange_source_table"] = str(step["exchange_temp_table"])
        rules_out.append(rule)
    return rules_out, write_plan_warnings


def _table_fields_from_catalog(catalog_entry: dict) -> list:
    fields = []
    for fname, fmeta in catalog_entry.items():
        if fname == "__table_comment__":
            continue
        if not isinstance(fmeta, dict):
            continue
        fields.append({
            "name": fname,
            "type": str(fmeta.get("type") or ""),
            "comment": str(fmeta.get("comment") or ""),
        })
    return fields


def _project_tables(draft: dict, ctx: dict) -> list:
    """data_flow.tables → tables[]；fields 口径（信息守恒优先级）：
    DDL catalog（全字段）> 最终目标表 meta 兜底（类型/注释）> 血缘回填字段名（type 空）> []。
    """
    meta = draft.get("meta", {})
    catalog = ctx.get("table_catalog") or {}
    final_key = f"{ctx.get('final_target_schema') or ''}.{ctx.get('final_target_table') or ''}".lower()

    # 血缘回填索引：producing_step → 该步目标表 → 字段名列表（保序去重）
    topo_steps = {s.get("step_id"): s for s in draft.get("topology", {}).get("steps", [])}
    fields_by_table = {}
    for f in draft.get("field_mappings", {}).get("fields", []):
        step = topo_steps.get(f.get("producing_step")) or {}
        key = f"{step.get('target_schema') or ''}.{step.get('target_table') or ''}".lower()
        if not key.strip("."):
            continue
        name = str(f.get("target_field") or "")
        if name:
            lst = fields_by_table.setdefault(key, [])
            if name not in lst:
                lst.append(name)

    tables_out = []
    for t in draft.get("data_flow", {}).get("tables", []):
        schema = str(t.get("schema") or "")
        name = str(t.get("name") or "")
        key = f"{schema}.{name}".lower()

        # fields 口径（信息守恒）：DDL catalog（类型/注释事实）∪ 血缘字段名（无类型事实），
        # 大小写不敏感去重；两者皆缺 → 空数组（消费方查库回填）。
        # 最终目标表无 catalog 时用 meta.target_field_types 兜底（同为 DDL 来源）。
        fields = []
        seen_names = set()

        def _add_field(fname: str, ftype: str = "", fcomment: str = ""):
            low = fname.lower()
            if low in seen_names or not fname:
                return
            seen_names.add(low)
            fields.append({"name": fname, "type": ftype, "comment": fcomment})

        if key in catalog:
            for f in _table_fields_from_catalog(catalog[key]):
                _add_field(f["name"], f["type"], f["comment"])
        elif key == final_key:
            types = meta.get("target_field_types") or {}
            comments = meta.get("target_field_comments") or {}
            for fn in types:
                _add_field(fn, str(types.get(fn) or ""), str(comments.get(fn) or ""))
        for fn in fields_by_table.get(key, []):
            _add_field(fn)

        tables_out.append({"schema": schema, "name": name, "fields": fields})
    return tables_out


def _project_lineage(draft: dict) -> list:
    """field_mappings.fields → lineage[]（物理来源穿透结果裁剪到 {table, field}）。"""
    out = []
    for f in draft.get("field_mappings", {}).get("fields", []):
        lineage = f.get("lineage") or []
        raw_expr = str(lineage[0].get("raw_sql") or "") if lineage else ""
        physical_sources = [
            {"table": str(p.get("table") or ""), "field": str(p.get("field") or "")}
            for p in (f.get("physical_source") or [])
        ]
        out.append({
            "rule_code": str(f.get("rule_code") or ""),
            "target_field": str(f.get("target_field") or ""),
            "transform_type": str(f.get("transform_type") or ""),
            "raw_expr": raw_expr,
            "physical_sources": physical_sources,
        })
    return out


def _project_schedule(meta: dict):
    """schedule：f/i/other_tasks 原样（jobs/params 事实），裁冗余 all_tasks 与 lts_file
    （路径已进 provenance）。xlsx 等模式无调度 → 显式 null。"""
    sched = meta.get("schedule")
    if not sched:
        return None
    out = {}
    for key in ("f_tasks", "i_tasks", "other_tasks"):
        tasks = []
        for t in (sched.get(key) or []):
            if not isinstance(t, dict):
                continue
            tasks.append({k: v for k, v in t.items() if k != "lts_file"})
        out[key] = tasks
    return out


def _project_dq_rules(meta: dict):
    dq = meta.get("dq_rules")
    if not dq:
        return None
    keep = ("rule_number", "rule_name", "rule_desc", "check_type", "alert_level", "sql", "target_table")
    # 按 rule_number 排序保证确定性（发现顺序受目录遍历影响）
    items = [{k: d.get(k, "") for k in keep} for d in dq]
    items.sort(key=lambda d: str(d.get("rule_number") or ""))
    return items


def _project_warnings(draft: dict, write_plan_warnings: list) -> list:
    """三类差异事实合流：field_mappings.warnings（原样）+ quality.issues（category→type）
    + write_plan_gap（本模块交叉验证产出）。"""
    out = [dict(w) for w in (draft.get("field_mappings", {}).get("warnings") or [])]
    for iss in (draft.get("quality", {}).get("issues") or []):
        out.append({
            "type": str(iss.get("category") or "quality"),
            "severity": str(iss.get("severity") or "info"),
            "title": str(iss.get("title") or ""),
            "rule_code": str(iss.get("rule_code") or ""),
            "id": str(iss.get("id") or ""),
        })
    out.extend(write_plan_warnings)
    return out


def project_baseline_v1(draft: dict, ctx: dict) -> dict:
    """纯函数投影：knowledge_draft + 导出上下文 → baseline_v1 dict（未校验）。"""
    meta = draft.get("meta", {})
    ctx = ctx or {}

    rules_out, write_plan_warnings = _project_rules(draft)

    return {
        "version": CONTRACT_VERSION,
        "asset": {
            "schema": str(ctx.get("final_target_schema") or ""),
            "table": str(meta.get("target_table") or ""),
            "rule_group_code": str(meta.get("rule_group_code") or ""),
            "rule_group_en": str(ctx.get("rule_group_en") or ""),
            "dialect": str(meta.get("dialect") or ""),
            "source_type": str(ctx.get("source_type") or meta.get("source_type") or ""),
            "analysis_time": str(meta.get("analysis_time") or ""),
            "platform_generation": str(ctx.get("platform_generation") or "1.0"),
        },
        "provenance": [dict(p) for p in (ctx.get("provenance") or [])],
        "rules": rules_out,
        "tables": _project_tables(draft, ctx),
        "lineage": _project_lineage(draft),
        "schedule": _project_schedule(meta),
        "dq_rules": _project_dq_rules(meta),
        "load_strategy": dict(meta.get("load_strategy") or {}),
        "patterns": [dict(p) for p in (meta.get("patterns") or [])],
        "warnings": _project_warnings(draft, write_plan_warnings),
    }


# ═════════════════════════════════════════════════════════════════
# 自校验（schema + 语义条件约束）与落盘
# ═════════════════════════════════════════════════════════════════

def validate_baseline_v1(data: dict, schema: dict = None) -> list:
    """校验产物，返回违规清单（空列表 = 通过）。三层：
    版本支持 → JSON Schema 结构 → 语义条件约束（与消费方 baseline_contract 对齐 + 本侧不变量）。
    """
    errors = []

    version = data.get("version")
    if not version:
        errors.append("[baseline_v1] 缺 version 字段（双端校验锚点）")
    elif version not in SUPPORTED_VERSIONS:
        errors.append(f"[baseline_v1] 不支持的版本 {version!r}（本仓支持: {sorted(SUPPORTED_VERSIONS)}）")

    if schema is not None:
        try:
            from jsonschema import Draft202012Validator
            for err in sorted(Draft202012Validator(schema).iter_errors(data),
                              key=lambda e: [str(p) for p in e.path]):
                loc = "/".join(str(p) for p in err.path) or "<root>"
                errors.append(f"[baseline_v1] {loc}: {err.message}")
        except ImportError:
            errors.append("[baseline_v1] 缺少依赖 jsonschema（pip install jsonschema>=4.0）——自校验无法执行，拒绝落盘")

    # ── 语义条件约束（JSON Schema 表达不了）──
    for rule in data.get("rules") or []:
        rc = rule.get("rule_code", "?")
        dm = str(rule.get("delete_mode") or "")
        # dm=6 必供 merge_on（与消费方 baseline_contract 镜像，消费方同样会拦）
        if dm == "6" and not str(rule.get("merge_on") or "").strip():
            errors.append(
                f"[baseline_v1] 规则 {rc} delete_mode=6（MERGE）但 merge_on 为空——"
                f"删除条件列与 SQL 文本均无法取得 ON 条件，需补平台增量配置后重跑"
            )
        # 本侧不变量：write_plan 若存在，须与 delete_mode 映射一致
        # （v1.0 形态产物不带 write_plan，缺席不视为违规——v1.1 为纯增量）
        wp = rule.get("write_plan") or {}
        if wp:
            expected = WRITE_KIND_BY_MODE.get(dm)
            if rule.get("is_view_step"):
                if wp.get("kind") != "view":
                    errors.append(f"[baseline_v1] 规则 {rc} is_view_step 但 write_plan.kind={wp.get('kind')!r}（应为 'view'）")
            elif expected and wp.get("kind") != expected[0]:
                errors.append(
                    f"[baseline_v1] 规则 {rc} write_plan.kind={wp.get('kind')!r} 与 "
                    f"delete_mode={dm!r} 的平台映射 ({expected[0]!r}) 不一致"
                )

    if not data.get("provenance"):
        errors.append("[baseline_v1] provenance 为空——源文件清单缺失（至少应含输入制品/yml 条目）")

    return errors


def export_baseline_v1(draft: dict, ctx: dict, output_dir) -> Path:
    """投影 → 自校验 → 落盘。校验不过抛 BaselineExportError（fail loud，不写文件）。

    Returns: 产物绝对路径 {output_dir}/baseline_v1.json
    """
    data = project_baseline_v1(draft, ctx)

    schema = None
    if DEFAULT_SCHEMA_PATH.exists():
        schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = validate_baseline_v1(data, schema)
    if errors:
        raise BaselineExportError("\n".join(errors))

    out_path = Path(output_dir) / "baseline_v1.json"
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return out_path
