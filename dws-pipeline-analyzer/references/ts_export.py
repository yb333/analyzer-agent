#!/usr/bin/env python3
"""ts 草稿投影（ts_export）—— ViewContext 的 JSON 渲染，与 tech_design.md 同源。

定位（docs/16 映射矿阵 / 消费方 docs/specs/opt/12 v4 §十一）：
    从 knowledge 富表示 + ViewContext（v1.7 三视图公共取数层）**直接还原** ts 形态
    ——不从 asset_facts 二次装配（信息损耗源头）、不迁移消费方装配器。
    对齐靶 = vendor/ts_draft/ts_sample_forward.json（正向设计链路真实产出，全貌）
    进门验 = vendor/ts_draft/ts_draft_v2.schema.json（契约必填面，消费方权威）

本模块**只渲染不解析**：事实来自 knowledge（引擎）/ engine_ctx（catalog）/
ViewContext（取数口径）/ schedule_recovery（调度还原）/ baseline_export（write_plan、
warnings 分级、self_check）——单一权威生产点，这里只摆位。

处理方式五标（docs/16）：R 恢复 / D 派生 / E 留空 / P 带源回填 / N 不产。
纪律：恢复声明进本体；推断不冒充声明（判断位留合法空结构）；显式空；
词表外 kind 禁硬映射（load_mode="" + write_plan_gap 进 _reverse）。

Author: 院博
"""

from __future__ import annotations

import json
from pathlib import Path

from viewmodel import build_context
from baseline_export import (build_write_plan, _project_warnings, _derive_self_check)

TS_VERSION = "2.1"  # v2.1：rules[].query_sql 逐字必填（契约波次随 vendor 同步）
GENERATED_BY = "dws-pipeline-analyzer"
VENDOR_SCHEMA_PATH = (Path(__file__).resolve().parent.parent
                      / "vendor" / "ts_draft" / "ts_draft_v2.schema.json")

# write_plan.kind → ts load_mode（ts 域词汇，形态随 vendor 同步；词表外禁硬映射）
LOAD_MODE_BY_KIND = {
    "full_truncate": "truncate_table",
    "append": "no_delete",
    "delete_by_condition": "delete",
    "partition_truncate": "truncate_partition",
    "merge_upsert": "merge_into",
    "update_by_condition": "update",
}

# 消费方标准审计列名——只用于**计数实际存在的**（field_count.audit），不补造
STANDARD_AUDIT_FIELDS = ("del_flag", "crt_cycle_id", "last_upd_cycle_id", "dw_last_update_date")


class TsDraftError(Exception):
    """ts 草稿校验不过（schema / 语义不变量）。调用方 fail loud，不落盘。"""


def _short(full: str) -> str:
    return full.split(".")[-1] if full else ""


# ═══════════════════════════════════════════════════════════════
# 拓扑（D 派生）：规则读写关系 / 表角色
# ═══════════════════════════════════════════════════════════════

def _topology(knowledge: dict) -> dict:
    """rule_code → {step_id, target_full, target_short, reads, produces_for, role, is_view}
    + "_written": {表全名小写: rule_code}。中间表 = 被其他规则读的产出表。"""
    steps = knowledge.get("topology", {}).get("steps", [])
    written = {str(s.get("target_table_full") or "").lower(): s["rule_code"]
               for s in steps if s.get("target_table_full")}
    info: dict = {}
    for s in steps:
        code = s["rule_code"]
        full = str(s.get("target_table_full") or "")
        reads = [t for t in (s.get("source_tables_from_sql") or [])
                 if t.lower() in written and written[t.lower()] != code]
        info[code] = {"step_id": s["step_id"], "target_full": full, "target_short": _short(full),
                      "schema": str(s.get("target_schema") or ""),
                      "reads": reads, "produces_for": [], "is_view": bool(s.get("is_view_step"))}
    for code, d in info.items():
        for t in d["reads"]:
            info[written[t.lower()]]["produces_for"].append(code)
    read_tables = {t.lower() for d in info.values() for t in d["reads"]}
    for d in info.values():
        d["produces_for"] = sorted(set(d["produces_for"]))
        d["role"] = "intermediate" if d["target_full"].lower() in read_tables else "target"
    info["_written"] = written
    return info


# ═══════════════════════════════════════════════════════════════
# write_plan（复用 baseline_export，R）
# ═══════════════════════════════════════════════════════════════

def _write_plans(knowledge: dict, ctx) -> tuple[dict, list]:
    plans, warnings = {}, []
    for s in knowledge.get("topology", {}).get("steps", []):
        raw_sql = str(ctx.df_step(s["step_id"]).get("raw_sql") or "")
        plan, _merge_on, w = build_write_plan(s, raw_sql)
        plans[s["rule_code"]] = plan
        warnings.extend(w)
    return plans, warnings


# ═══════════════════════════════════════════════════════════════
# 字段三桶（R）：direct / assign / processed
# ═══════════════════════════════════════════════════════════════

def _field_refs(ctx, field: dict) -> list:
    """refs = 完整血缘串 alias.field（lineage 的 source_table 即 SQL 别名；回退穿透来源）。"""
    refs, seen = [], set()
    for ln in field.get("lineage") or []:
        a = str(ln.get("source_table") or "").strip()
        c = str(ln.get("source_field") or "").strip()
        if c:
            r = f"{a}.{c}" if a else c
            if r not in seen:
                seen.add(r)
                refs.append(r)
    if refs:
        return refs
    for s in ctx.resolve_field_sources(field):
        a = str(s.get("alias") or s.get("table") or "").strip()
        c = str(s.get("field") or "").strip()
        r = f"{a}.{c}" if a and c else (c or a)
        if r and r not in seen:
            seen.add(r)
            refs.append(r)
    return refs


def _bucket_fields(ctx, step_id: str) -> tuple[dict, list]:
    """步骤产出字段 → 三桶 + field_targets（派生只读）。

    桶判据（coder 粘贴 vs 写表达式）：direct=有源直取 → "alias.col AS target"（一律带 AS）；
    value（常量赋值）→ assign {target, value}；其余加工 → processed {target, logic, refs}。
    logic/value 取 lineage raw_sql 逐字原文（口径表达式恢复）。
    """
    direct, processed, assign, targets = [], [], [], []
    for f in ctx.fields_list:
        if f.get("producing_step") != step_id:
            continue
        target = str(f.get("target_field") or "")
        if not target:
            continue
        targets.append(target)
        tt = str(f.get("transform_type") or "expression")
        lineage = f.get("lineage") or []
        raw_expr = str((lineage[0].get("raw_sql") if lineage else "") or "").strip()
        refs = _field_refs(ctx, f)
        if tt == "direct":
            expr = raw_expr or (refs[0] if refs else "")
            if expr and " as " not in expr.lower():
                direct.append(f"{expr} AS {target}")
            elif expr:
                direct.append(expr)
            else:
                processed.append({"target": target, "logic": "", "refs": refs})
        elif tt == "value":
            assign.append({"target": target, "value": raw_expr})
        else:
            processed.append({"target": target, "logic": raw_expr, "refs": refs})
    return {"processed": processed, "assign": assign, "direct": direct}, sorted(set(targets))


# ═══════════════════════════════════════════════════════════════
# rules 段
# ═══════════════════════════════════════════════════════════════

def _ts_rules(ctx, knowledge: dict, topo: dict, plans: dict) -> dict:
    written = topo["_written"]
    # query_sql 逐字（v2.1 必填）：source.raw_sql=解析原文附录（与 asset_facts 同源），
    # 兜底 data_flow.raw_sql——双通道同一优先级口径
    raw_sql_by_rule = {r.get("rule_code"): r.get("sql", "")
                       for r in knowledge.get("source", {}).get("raw_sql", [])}
    steps = sorted(knowledge.get("topology", {}).get("steps", []),
                   key=lambda s: (s.get("exec_sequence", 0), s["step_id"]))
    out: dict = {}
    for s in steps:
        code = s["rule_code"]
        info = topo[code]
        plan = plans.get(code) or {}
        joins_raw = ctx.df_step(info["step_id"]).get("joins") or []
        alias_of = {str(j.get("source_table") or "").lower(): str(j.get("alias") or "")
                    for j in joins_raw if j.get("source_table")}
        src = []
        for full in (s.get("source_tables_from_sql") or []):
            if full.lower() in written and written[full.lower()] != code:
                continue  # 本资产中间表 → reads
            src.append({"schema": full.split(".")[0] if "." in full else "",
                        "table": _short(full), "alias": alias_of.get(full.lower(), "")})
        joins = [{"alias": str(j.get("alias") or ""), "type": str(j.get("join_type") or ""),
                  "condition": str(j.get("join_condition") or ""), "filter": ""}
                 for j in joins_raw if str(j.get("join_type") or "").upper() != "FROM"]
        buckets, targets = _bucket_fields(ctx, info["step_id"])
        out[code] = {
            "rule_name": str(s.get("rule_name") or ""),
            "query_sql": str(raw_sql_by_rule.get(code)
                             or ctx.df_step(info["step_id"]).get("raw_sql") or ""),  # 逐字
            "scenario": str(s.get("scenario_id") or ""),
            "exec_sequence": s.get("exec_sequence", 0),
            "target_table": info["target_full"],
            "design_intent": "",                                  # E
            "load_mode": LOAD_MODE_BY_KIND.get(str(plan.get("kind") or ""), ""),  # R（词表外 ""）
            "write_condition": str(plan.get("condition_expr") or ""),             # R
            "step_type": "",                                      # E：加工路径是设计判断
            "target_role": info["role"],                          # D
            "produces_for": info["produces_for"],                 # D
            "reads": info["reads"],                               # D
            "incremental": {},                                    # E
            "filter": ctx.step_where(info["step_id"]),            # R
            "source_tables": src,                                 # R（实际别名）
            "source_aliases": [],                                 # 形态对齐正向（当前 []）
            "derived_fields": {},                                 # E
            "grain": {},                                          # E
            "joins": joins,                                       # R
            "join_safety": [],                                    # E
            "dedup_strategy": {},                                 # E
            "field_targets": targets,                             # D
            "fields": buckets,                                    # R
            "field_count": sum(len(v) for v in buckets.values()),  # D
        }
    return out


# ═══════════════════════════════════════════════════════════════
# tables 段（规则产出表；三键纯元数据 + DDL 物理属性）
# ═══════════════════════════════════════════════════════════════

def _storage_from_with(with_options: str) -> str:
    low = (with_options or "").lower()
    if "orientation" in low:
        if "column" in low:
            return "column"
        if "row" in low:
            return "row"
    return ""


def _ts_tables(knowledge: dict, engine_ctx: dict, topo: dict, rules: dict) -> dict:
    catalog = engine_ctx.get("table_catalog") or {}
    out: dict = {}
    for code, info in topo.items():
        if code.startswith("_") or info["is_view"]:
            continue
        full = info["target_full"]
        short = info["target_short"]
        if short in out:
            continue  # 多规则共写同表：一张表一个条目
        entry = catalog.get(full.lower()) or {}
        fields, seen = [], set()
        for fname, fmeta in entry.items():
            if fname.startswith("__") or not isinstance(fmeta, dict):
                continue
            seen.add(fname.lower())
            fields.append({"target_field": fname, "field_type": str(fmeta.get("type") or ""),
                           "field_comment": str(fmeta.get("comment") or "")})
        # 血缘补名（DDL 缺的字段，type/comment 留空——显式空）
        for c, i2 in topo.items():
            if c.startswith("_") or i2["target_full"].lower() != full.lower():
                continue
            for t in rules.get(c, {}).get("field_targets", []):
                if t.lower() not in seen:
                    seen.add(t.lower())
                    fields.append({"target_field": t, "field_type": "", "field_comment": ""})
        props = entry.get("__table_props__") or {}
        dist_key = [fn for fn, m in entry.items()
                    if not fn.startswith("__") and isinstance(m, dict) and m.get("is_distribute_key")]
        by = str(props.get("distribute_by") or "").strip()
        dist_type = (by.split("(")[0].split()[0].upper() if by
                     else ("HASH" if dist_key else "ROUNDROBIN"))
        out[short] = {
            "type": info["role"],                                  # D
            "business_key": [],                                    # E
            "distribution_key": dist_key,                          # R
            "distribute_type": dist_type,                          # R/D
            "partition": str(props.get("partition_by") or ""),     # R
            "storage": _storage_from_with(props.get("with_options") or ""),  # R（不硬编码）
            "logical_group": str(props.get("to_group") or ""),     # R
            "build_mode": "",                                      # E
            "fields": fields,                                      # R（三键）
        }
    return out


# ═══════════════════════════════════════════════════════════════
# meta 段
# ═══════════════════════════════════════════════════════════════

def _ts_schedule(rec: dict | None) -> tuple[dict, list]:
    """schedule_recovery 结果 → meta.schedule 形态。未匹配上游不进本体（披露为 warning）。"""
    empty = {"schedule_type": "", "cron": "", "upstream_added": [],
             "exec_params": {}, "lts_params": [], "tasks": {}}
    if not rec:
        return empty, []
    warnings = []
    tasks = {}
    for slot, t in (rec.get("tasks") or {}).items():
        ups = []
        for u in t.get("upstream") or []:
            if not u.get("resolved") or not u.get("task"):
                warnings.append({
                    "type": "lts_upstream_unresolved",
                    "title": f"LTS 任务 {t.get('task_name')} 的依赖 job {u.get('job')} 未在制品内匹配到上游任务"
                             f"（表名线索 {u.get('table')}）——原文见 _reverse.schedule_evidence，请人确认上游",
                })
                continue
            ups.append({"table": u["table"], "task": u["task"], "dep_type": u["dep_type"],
                        "project": u.get("project", ""), "group": u.get("group", "")})
        tasks[slot] = {"task_name": t.get("task_name", ""), "job_name": t.get("job_name", ""),
                       "cron": t.get("cron", ""), "upstream": ups,
                       "project_name": t.get("project_name", ""), "task_group": t.get("task_group", "")}
    exec_params = {name: {"value_type": v.get("value_type", ""), "desc": v.get("desc", ""),
                          "standard": False,
                          "default_value": {"type": "static", "expr": v.get("default_value", "")}}
                   for name, v in (rec.get("exec_params") or {}).items()}
    return {
        "schedule_type": rec.get("schedule_type", ""),
        "cron": rec.get("cron", ""),
        "upstream_added": [u["task"] for u in tasks.get("f", {}).get("upstream", [])],
        "exec_params": exec_params,
        "lts_params": list(rec.get("lts_params") or []),
        "tasks": tasks,
    }, warnings


def _ts_meta(ctx, knowledge: dict, engine_ctx: dict, tables: dict, topo: dict,
             schedule: dict, dq_detected: bool) -> dict:
    meta = knowledge.get("meta", {})
    catalog = engine_ctx.get("table_catalog") or {}
    f_schema = str(engine_ctx.get("final_target_schema") or "")
    f_table = str(engine_ctx.get("final_target_table") or meta.get("target_table") or "")
    f_entry = catalog.get(f"{f_schema}.{f_table}".lower()) or {}
    f_cn = str(f_entry.get("__table_comment__") or meta.get("table_comment") or "")
    asset_info = meta.get("asset_info") or {}
    i_view = {"schema": str(asset_info.get("view_schema") or ""),
              "table": str(asset_info.get("view_table") or ""), "cn": ""}

    def _cn(schema: str, table: str) -> str:
        e = catalog.get(f"{schema}.{table}".lower()) or {}
        return str(e.get("__table_comment__") or "")

    source_tables, seen = [], set()
    for es in ctx.entity_source_tables():
        key = f"{es.schema}.{es.table}".lower()
        if key in seen:
            continue
        seen.add(key)
        source_tables.append({"schema": es.schema, "table": es.table,
                              "table_cn": _cn(es.schema, es.table), "alias": es.alias})

    final_fields = tables.get(_short(f_table), {}).get("fields", [])
    audit = sum(1 for f in final_fields if f["target_field"].lower() in STANDARD_AUDIT_FIELDS)
    ls = meta.get("load_strategy") or {}
    return {
        "target": {"f_table": {"schema": f_schema, "table": f_table, "cn": f_cn},
                   "i_view": i_view},
        "dq_required": 1 if dq_detected else 0,                           # R
        "grain": "",                                                       # E
        "business_key": [],                                                # E（dq_pk_check 后续 P）
        "load_strategy": {"strategy": str(ls.get("strategy") or ""),      # R（本仓标准枚举）
                          "label": str(ls.get("label") or ""),
                          "delete_mode": str(ls.get("delete_mode") or "")},
        "field_count": {"business": len(final_fields) - audit, "audit": audit,
                        "total": len(final_fields)},                      # D
        "source_tables": source_tables,                                    # R
        "schedule": schedule,                                              # R
    }


# ═══════════════════════════════════════════════════════════════
# data_flow / _reverse
# ═══════════════════════════════════════════════════════════════

def _ts_data_flow(knowledge: dict, topo: dict) -> dict:
    step_rule = {v["step_id"]: k for k, v in topo.items() if not k.startswith("_")}
    view_tables = {v["target_short"].lower() for k, v in topo.items()
                   if not k.startswith("_") and v["is_view"]}
    tables = [{"schema": t.get("schema", ""), "name": t.get("name", ""), "role": t.get("role", ""),
               "layer": t.get("layer", ""), "is_view": t.get("name", "").lower() in view_tables}
              for t in knowledge.get("data_flow", {}).get("tables", [])]
    deps = [{"from": step_rule.get(d.get("from"), d.get("from")),
             "to": step_rule.get(d.get("to"), d.get("to")),
             "type": d.get("type", "data_flow"),
             "intermediate_table": d.get("intermediate_table", "")}
            for d in knowledge.get("topology", {}).get("data_dependencies", [])]
    groups = [{"sequence": g.get("sequence", 0),
               "rules": sorted(step_rule.get(sid, sid) for sid in g.get("parallel_steps", []))}
              for g in knowledge.get("topology", {}).get("schedule_plan", [])]
    return {"tables": tables, "dependencies": deps, "schedule_groups": groups}


def _ts_reverse(knowledge: dict, ctx, topo: dict, plans: dict, wp_warnings: list,
                extra_warnings: list, schedule_rec: dict | None) -> dict:
    warnings_out = _project_warnings(knowledge, wp_warnings, extra_warnings)
    write_plan = []
    bk_evidence = []
    for code, plan in plans.items():
        info = topo.get(code) or {}
        write_plan.append({
            "rule_code": code, "kind": plan.get("kind", ""),
            "condition_role": plan.get("condition_role", ""),
            "condition_expr": plan.get("condition_expr"),
            "condition_columns": list(plan.get("condition_columns") or []),
            "condition_source": plan.get("condition_source"),
            "where_clause": ctx.step_where(info.get("step_id", "")) if info else "",
        })
        if plan.get("kind") == "merge_upsert" and plan.get("condition_expr"):
            bk_evidence.append({"source": "merge_on", "expr": str(plan["condition_expr"]),
                                "origin": code})
    return {
        "self_check": _derive_self_check(warnings_out),
        "warnings": warnings_out,
        "write_plan": write_plan,
        "schedule_evidence": list((schedule_rec or {}).get("evidence") or []),
        "evidence": {"business_key": bk_evidence},
    }


# ═══════════════════════════════════════════════════════════════
# 总装 / 校验 / 落盘
# ═══════════════════════════════════════════════════════════════

def render_ts_draft(knowledge: dict, engine_ctx: dict, *,
                    schedule_rec: dict | None = None,
                    dq_detected: bool = False,
                    provenance: list | None = None,
                    extra_warnings: list | None = None) -> dict:
    """knowledge + engine_ctx（+ 调度还原/分拣披露）→ ts 草稿 dict（未校验）。"""
    ctx = build_context(knowledge)
    topo = _topology(knowledge)
    plans, wp_warnings = _write_plans(knowledge, ctx)
    rules = _ts_rules(ctx, knowledge, topo, plans)
    tables = _ts_tables(knowledge, engine_ctx, topo, rules)
    schedule, sched_warnings = _ts_schedule(schedule_rec)
    extra = list(extra_warnings or []) + sched_warnings
    meta = _ts_meta(ctx, knowledge, engine_ctx, tables, topo, schedule, dq_detected)
    return {
        "version": TS_VERSION,
        "spec_type": "ts",
        "generated_at": str(knowledge.get("meta", {}).get("analysis_time") or ""),
        "generated_by": GENERATED_BY,
        "meta": meta,
        "tables": tables,
        "rules": rules,
        "data_flow": _ts_data_flow(knowledge, topo),
        "_baseline": {"source": "reverse", "provenance": [dict(p) for p in (provenance or [])]},
        "_reverse": _ts_reverse(knowledge, ctx, topo, plans, wp_warnings, extra, schedule_rec),
    }


def validate_ts_draft(data: dict, schema: dict | None = None) -> list:
    """进门验（vendor schema）+ 语义不变量。返回违规清单（空=通过）。"""
    errors = []
    if schema is None and VENDOR_SCHEMA_PATH.exists():
        schema = json.loads(VENDOR_SCHEMA_PATH.read_text(encoding="utf-8"))
    if schema is not None:
        try:
            from jsonschema import Draft202012Validator
            for err in sorted(Draft202012Validator(schema).iter_errors(data),
                              key=lambda e: [str(p) for p in e.path]):
                loc = "/".join(str(p) for p in err.path) or "<root>"
                errors.append(f"[ts_draft] {loc}: {err.message}")
        except ImportError:
            errors.append("[ts_draft] 缺 jsonschema 依赖，无法做 schema 校验（pip install jsonschema）")
    # 语义不变量：merge_into 必须有 write_condition（与 asset_facts dm=6 纪律镜像）
    for code, r in (data.get("rules") or {}).items():
        if r.get("load_mode") == "merge_into" and not r.get("write_condition"):
            errors.append(f"[ts_draft] rules/{code}: load_mode=merge_into 但 write_condition 为空"
                          "——MERGE ON 条件缺失（制品删除条件列漏填），请补料重跑")
    return errors


def export_ts_draft(knowledge: dict, engine_ctx: dict, output_file, **kw) -> Path:
    """渲染 → 进门验 → 落盘（校验不过抛 TsDraftError，不落盘）。"""
    data = render_ts_draft(knowledge, engine_ctx, **kw)
    errors = validate_ts_draft(data)
    if errors:
        raise TsDraftError("\n".join(errors))
    out = Path(output_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    return out
