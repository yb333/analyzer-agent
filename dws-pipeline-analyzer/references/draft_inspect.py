#!/usr/bin/env python3
"""knowledge_draft.json 只读探查入口（AI 定向下钻通道，无副作用）。

契约背景（2026-11-11 渐进式披露升级）：AI 增强从 knowledge_summary.md 起步
（自足 80%），需要细节（某步完整 SQL / 某字段血缘 / 某表达式）时用本入口
定向取，**不要自己写 python/jq 读 draft JSON**（Windows PowerShell 编码/
引号不稳，且容易读错结构）。

用法（经 run.py 分发，别名 inspect；纯 stdlib，无需 openpyxl/sqlglot）:
    python run.py inspect --draft {draft路径}                       # 概览
    python run.py inspect --draft ... --step step_2 [--part P]      # 步骤细节
    python run.py inspect --draft ... --field amount                # 字段血缘
    python run.py inspect --draft ... --table ods.ods_trade_order_di # 表使用位置
    python run.py inspect --draft ... --search "SUM("               # 全文搜索
    python run.py inspect --draft ... --schema                      # 结构字典
    （--json 叠加任意选择器 = 原始 JSON 切片输出）

--part 取值: summary | sql | blocks | fields | where | joins | ctes（默认 summary）
输出: markdown 到 stdout（默认 200 行截断，超限提示收窄选择器）
退出码: 0=查到 / 1=没查到或参数错误
"""

import argparse
import json
import sys
from pathlib import Path

MAX_OUT_LINES = 200   # 输出总量控制（防大 draft 刷屏）
SQL_MAX_CHARS = 4000  # 单段 SQL 上限（再大就提示 --json 拿全量）

# ═══════════════════════════════════════════════════════════════
# draft 结构字典（单一事实源）
# - `--schema` 出口直接打印本字典；tests/test_summary_inspect.py 双向锁：
#   ① 字典里每个路径必须在真实 draft 中存在（防文档腐烂）
#   ② draft 每个顶层键必须被字典覆盖（防漏文档）
# - key = draft 内路径，"[]" 表示取 list 首元素后继续下钻
# - value = 形状 + 用途（键名不用猜）
# ═══════════════════════════════════════════════════════════════
DRAFT_SCHEMA = {
    "meta": "dict 基本信息",
    "meta.target_table": "str 目标表名",
    "meta.table_comment": "str 目标表注释（DDL）",
    "meta.patterns": "list[{label}] 加工模式标签",
    "meta.load_strategy": "dict {strategy,label,detail,delete_mode,delete_mode_label} 加载策略",
    "meta.target_field_types": "dict {字段名: 类型} 目标表字段类型（DDL）",
    "meta.target_field_comments": "dict {字段名: 中文注释} 目标表字段注释（DDL COMMENT）",
    "meta.schedule": "dict {f_tasks,i_tasks,other_tasks,all_tasks} LTS 调度任务（可选：制品含 LTS 时才有）",
    "meta.dq_rules": "list[{rule_number,rule_name,rule_desc,check_type,alert_level,sql,target_table,owner}] DQ 质量规则（可选：制品含 DQ excel 时才有）",
    "meta.total_rules": "int 规则数",
    "meta.total_sql_columns": "int SQL 解析列数",
    "meta.total_target_fields": "int Excel 目标字段数",
    "topology": "dict 步骤拓扑",
    "topology.steps": "list[{step_id,rule_code,rule_type,exec_sequence,target_table_full,delete_mode,delete_condition,source_tables_from_sql,is_exchange,is_view_step}] 步骤清单",
    "topology.scenarios": "list[{name,is_common,rule_count,rule_codes}] 场景分组",
    "topology.schedule_plan": "list 调度执行计划",
    "topology.data_dependencies": "list 步骤间数据依赖",
    "topology.self_references": "list 自引用",
    "topology.implicit_dependencies": "list 隐式依赖",
    "topology.target_write_groups": "list 同表多写分组",
    "topology.over_constraints": "list 过约束预警",
    "data_flow": "dict 步骤级 SQL 事实",
    "data_flow.tables": "list[{schema,name,role,layer}] 全部表（目标/中间/源）",
    "data_flow.steps": "list[{step_id,rule_code,target_table,write_mode,joins,where_clause,group_by,having_clause,ctes,raw_sql,join_usage,where_usage,groupby_usage,join_paths,union_branches,structured_summary,data_blocks}] 步骤细节",
    "data_flow.steps[].joins": "list[{source_table,alias,join_type,join_condition,subquery_sql}] 来源表与关联",
    "data_flow.steps[].ctes": "list[{name,source_tables,fields,raw_sql,where_clause}] CTE 定义",
    "data_flow.steps[].ctes[].fields": "list[{name,expression,transform_type,source_fields}] CTE 内部字段",
    "data_flow.steps[].join_usage": "list[{field,alias,table,condition}] 关联键使用",
    "data_flow.steps[].where_usage": "list[{field,alias,condition}] 过滤字段使用",
    "data_flow.steps[].groupby_usage": "list 分组字段使用",
    "data_flow.steps[].union_branches": "list UNION/集合操作分支",
    "data_flow.steps[].raw_sql": "str 该步完整 SQL 原文（保真未改写）",
    "data_flow.steps[].data_blocks": "list 递归逻辑块：{type,table,alias,role,join_type,on_condition,brought_fields[{name,type}],ops,group_by,children}",
    "data_flow.steps[].structured_summary": "步骤结构化摘要（脚本兜底描述素材）",
    "field_mappings": "dict 字段级血缘",
    "field_mappings.fields": "list[{target_field,producing_step,rule_code,in_target_fields,transform_type,lineage,physical_source,validation}] 字段映射明细",
    "field_mappings.fields[].lineage": "list[{step,source_table,source_field,transform,raw_sql,cte_name,cte_transform_type,cte_expression}] 血缘链（穿透 CTE/中间表）",
    "field_mappings.fields[].physical_source": "list[{table,field,step_id,alias,transform,raw_sql}] 物理源表终点",
    "field_mappings.statistics": "dict {total_in_sql,total_in_excel,match_count,only_in_sql,only_in_excel} 双源交叉验证统计",
    "field_mappings.differences": "list 双源差异明细",
    "field_mappings.warnings": "list 预警",
    "quality": "dict 质量分析",
    "quality.complexity_metrics": "dict 复杂度指标（max_join_count/max_cte_count 等）",
    "quality.issues": "list[{severity,title,...}] 质量问题清单",
    "quality.issue_statistics": "dict 问题统计",
    "quality.ai_insights": "list AI 洞察提示",
    "business_logic": "dict 脚本自动兜底描述",
    "business_logic.summary": "str 整体兜底摘要",
    "business_logic.step_descriptions": "list[{step_id,rule_code,purpose,logic,is_auto_generated}] 步骤兜底描述",
    "business_logic.key_transforms": "list 关键加工点",
    "source": "dict 输入来源回溯 {rule_sheet_raw:[...]} 规则 sheet 原始行（审计用）",
}


def _hardening():
    """stdout/stderr 强制 utf-8（Windows GBK 控制台打印 SQL/中文不炸）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _short(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def _cond(s, n):
    """剥掉行首 WHERE/HAVING 关键字再截断。"""
    import re
    s = re.sub(r"^\s*(WHERE|HAVING)\s+", "", (s or "").strip(), flags=re.IGNORECASE)
    return _short(s, n)


def _emit(lines, as_json=False, payload=None):
    """统一出口：行数截断 + 打印。as_json 时输出原始 JSON 切片。"""
    if as_json:
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        lines = text.splitlines()
    if len(lines) <= MAX_OUT_LINES:
        print("\n".join(lines))
        return
    print("\n".join(lines[:MAX_OUT_LINES]))
    print(f"\n[…输出截断：共 {len(lines)} 行，请收窄选择器"
          f"（--step/--part/--field/--search）或 --json 自行切…]")


def _find_step(draft, ref):
    """按 step_id 或 rule_code 找 data_flow 步骤（大小写不敏感）。"""
    ref_l = (ref or "").strip().lower()
    for s in draft.get("data_flow", {}).get("steps", []):
        if str(s.get("step_id", "")).lower() == ref_l or str(s.get("rule_code", "")).lower() == ref_l:
            return s
    return None


def _walk_blocks(blocks):
    """递归产出 (块, 层级) 序列。"""
    for blk in blocks or []:
        yield blk, 0
        for child, depth in _walk_blocks(blk.get("children")):
            yield child, depth + 1


# ── 渲染器：每个选择器一个 ──────────────────────────────────────

def render_stats(draft):
    meta = draft.get("meta", {})
    lines = ["# draft 概览", ""]
    lines.append(f"- 目标表: {meta.get('target_table', '')}"
                 + (f"（{meta.get('table_comment', '')}）" if meta.get("table_comment") else ""))
    lines.append(f"- 规则数: {meta.get('total_rules', '?')}"
                 f" | SQL列数: {meta.get('total_sql_columns', '?')}"
                 f" | Excel目标字段数: {meta.get('total_target_fields', '?')}")
    pats = ", ".join(p.get("label", "") for p in meta.get("patterns", []))
    ls = meta.get("load_strategy", {})
    if pats:
        lines.append(f"- 加工模式: {pats}")
    if ls:
        lines.append(f"- 加载策略: {ls.get('label', '')}（{ls.get('detail', '')}）")
    tfc = meta.get("target_field_comments", {})
    tft = meta.get("target_field_types", {})
    if tfc or tft:
        names = list(tfc.keys()) or list(tft.keys())
        lines.append(f"- 目标字段({len(names)}): " + ", ".join(
            n + (f"[{tfc[n]}]" if tfc.get(n) else "") for n in names[:30])
            + ("…" if len(names) > 30 else ""))
    dq = meta.get("dq_rules", [])
    if dq:
        lines.append(f"- DQ规则({len(dq)}): " + ", ".join(
            r.get("rule_number", "") for r in dq[:10]))

    topo = draft.get("topology", {})
    lines += ["", "## 步骤拓扑"]
    for st in topo.get("steps", []):
        lines.append(f"- {st.get('step_id')} ({st.get('rule_code')}) → {st.get('target_table_full', '')}")
    for sc in topo.get("scenarios", []):
        label = "公共步骤" if sc.get("is_common") else sc.get("name", "")
        lines.append(f"- 场景 {label}: {sc.get('rule_count')} 规则 ({', '.join(sc.get('rule_codes', []))})")

    q = draft.get("quality", {})
    issues = q.get("issues", [])
    lines += ["", f"## 质量（{len(issues)} 个 issue）"]
    for iss in issues[:10]:
        lines.append(f"- [{iss.get('severity', '')}] {iss.get('title', '')}")
    if len(issues) > 10:
        lines.append(f"- …共 {len(issues)} 个")
    lines += ["", "提示: --step X --part ... / --field / --table / --search / --schema 逐层下钻"]
    return lines, {"meta": meta, "topology": topo, "quality_issues": issues}


def render_schema():
    lines = ["# knowledge_draft.json 结构字典", "",
             f"顶层 {len([k for k in DRAFT_SCHEMA if '.' not in k])} 段；"
             "「[]」= 取列表首元素后继续下钻。", ""]
    for path, desc in DRAFT_SCHEMA.items():
        lines.append(f"- {path}: {desc}")
    return lines, dict(DRAFT_SCHEMA)


def render_step(draft, ref, part):
    step = _find_step(draft, ref)
    if not step:
        return None, None
    sid = step.get("step_id", "")
    joins = step.get("joins", [])
    ctes = step.get("ctes", [])
    blocks = step.get("data_blocks", [])

    if part == "summary":
        lines = [f"# {sid} ({step.get('rule_code')}) → {step.get('target_table', '')}", ""]
        lines.append(f"- 写入方式: {step.get('write_mode', '')}")
        lines.append(f"- 来源表: {', '.join(j.get('source_table', '') for j in joins)}")
        if ctes:
            lines.append(f"- CTE: {', '.join(c.get('name', '') for c in ctes)}")
        if step.get("where_clause"):
            lines.append(f"- WHERE: {_cond(step.get('where_clause'), 240)}")
        if step.get("group_by"):
            lines.append(f"- GROUP BY: {_short(', '.join(step.get('group_by', [])), 200)}")
        if step.get("having_clause"):
            lines.append(f"- HAVING: {_cond(step.get('having_clause'), 160)}")
        if step.get("union_branches"):
            lines.append(f"- UNION 分支: {len(step['union_branches'])} 个")
        lines.append(f"- 逻辑块: {len(blocks)} 个（--part blocks 递归展开）")
        lines.append(f"- SQL 原文: {len(step.get('raw_sql', ''))} 字符（--part sql）")
        lines += ["", "可用 part: sql | blocks | fields | where | joins | ctes"]
        return lines, step

    if part == "sql":
        lines = [f"# {sid} SQL 原文", ""]
        sql = step.get("raw_sql", "")
        if len(sql) > SQL_MAX_CHARS:
            lines.append(f"(SQL {len(sql)} 字符，截断展示前 {SQL_MAX_CHARS}；--json 拿全量)")
            sql = sql[:SQL_MAX_CHARS]
        lines += ["```sql", sql, "```"]
        for c in ctes:
            csql = c.get("raw_sql", "")
            if len(csql) > SQL_MAX_CHARS:
                csql = csql[:SQL_MAX_CHARS] + "…"
            lines += ["", f"## CTE {c.get('name', '')}", "", "```sql", csql, "```"]
        return lines, step

    if part == "blocks":
        lines = [f"# {sid} 逻辑块（递归）", ""]
        for blk, depth in _walk_blocks(blocks):
            pad = "  " * depth
            seg = f"{blk.get('role', '')} {blk.get('table', '')}"
            if blk.get("alias"):
                seg += f"({blk['alias']})"
            jt = blk.get("join_type", "")
            if jt and jt != "FROM":
                seg += f" {jt}"
            if blk.get("on_condition"):
                seg += f" ON {blk['on_condition']}"
            if blk.get("ops"):
                seg += f" [{', '.join(blk['ops'])}]"
            lines.append(f"{pad}- {seg}")
            brought = [f.get("name", "") if isinstance(f, dict) else f
                       for f in blk.get("brought_fields", [])]
            if brought:
                lines.append(f"{pad}  带出: {', '.join(brought)}")
            if blk.get("group_by"):
                lines.append(f"{pad}  分组: {', '.join(blk['group_by'])}")
        return lines, {"step_id": sid, "data_blocks": blocks}

    if part == "fields":
        lines = [f"# {sid} 字段清单（field_mappings 口径）", ""]
        rows = [f for f in draft.get("field_mappings", {}).get("fields", [])
                if f.get("producing_step") == sid or f.get("rule_code") == step.get("rule_code")]
        for f in rows:
            phys = ", ".join(f"{p.get('table', '')}.{p.get('field', '')}"
                             for p in f.get("physical_source", [])[:4])
            lines.append(f"- {f.get('target_field', '')} [{f.get('transform_type', '')}]"
                         + (f" ← {phys}" if phys else ""))
            for lg in f.get("lineage", [])[:3]:
                expr = lg.get("raw_sql", "")
                cte_expr = lg.get("cte_expression", "")
                if cte_expr and cte_expr != expr:
                    expr = f"{expr}（CTE内: {_short(cte_expr, 80)}）"
                if expr:
                    lines.append(f"    {lg.get('step', '')}: {lg.get('source_table', '')}."
                                 f"{lg.get('source_field', '')} = {_short(expr, 140)}")
        if not rows:
            lines.append("(该步骤无 field_mappings 记录)")
        return lines, rows

    if part == "where":
        lines = [f"# {sid} 过滤/分组条件", ""]
        lines.append(f"- WHERE 原文: {step.get('where_clause', '') or '(无)'}")
        for w in step.get("where_usage", []):
            lines.append(f"  - 字段 {w.get('field', '')}"
                         + (f"（{w.get('alias', '')}）" if w.get("alias") else "")
                         + (f" 条件: {w.get('condition', '')}" if w.get("condition") else ""))
        if step.get("group_by"):
            lines.append(f"- GROUP BY: {', '.join(step['group_by'])}")
        if step.get("having_clause"):
            lines.append(f"- HAVING: {step['having_clause']}")
        return lines, {k: step.get(k) for k in
                       ("where_clause", "where_usage", "group_by", "having_clause")}

    if part == "joins":
        lines = [f"# {sid} 关联", ""]
        for j in joins:
            seg = f"- {j.get('join_type', '')} {j.get('source_table', '')}"
            if j.get("alias"):
                seg += f"({j['alias']})"
            if j.get("join_condition"):
                seg += f" ON {j['join_condition']}"
            if j.get("subquery_sql"):
                seg += f" 子查询: {_short(j['subquery_sql'], 120)}"
            lines.append(seg)
        for u in step.get("join_usage", []):
            lines.append(f"- 关联键 {u.get('field', '')}"
                         + (f"（{u.get('table', '')}）" if u.get("table") else "")
                         + (f" 条件: {u.get('condition', '')}" if u.get("condition") else ""))
        return lines, {"joins": joins, "join_usage": step.get("join_usage", [])}

    if part == "ctes":
        lines = [f"# {sid} CTE（{len(ctes)} 个）", ""]
        for c in ctes:
            srcs = ", ".join(s.get("name", "") if isinstance(s, dict) else str(s)
                             for s in c.get("source_tables", []))
            lines.append(f"## {c.get('name', '')} ← {srcs}")
            for f in c.get("fields", []):
                lines.append(f"- {f.get('name', '')} = {f.get('expression', '')}"
                             f" [{f.get('transform_type', '')}]")
            if c.get("where_clause"):
                lines.append(f"- WHERE: {c['where_clause']}")
            lines.append(f"```sql\n{c.get('raw_sql', '')}\n```")
            lines.append("")
        if not ctes:
            lines.append("(无 CTE)")
        return lines, ctes

    return None, None  # 不可达（argparse choices 兜底）


def render_field(draft, name):
    name_l = name.strip().lower()
    fields = draft.get("field_mappings", {}).get("fields", [])
    exact = [f for f in fields if str(f.get("target_field", "")).lower() == name_l]
    rows = exact or [f for f in fields if name_l in str(f.get("target_field", "")).lower()]
    if not rows:
        return None, None
    lines = [f"# 字段血缘: {name}（{len(rows)} 条记录）", ""]
    for f in rows:
        lines.append(f"## {f.get('target_field', '')} [{f.get('transform_type', '')}]"
                     f" 产出步骤 {f.get('producing_step', '')} ({f.get('rule_code', '')})")
        if f.get("in_target_fields") is False:
            lines.append("- ⚠ 不在 Excel 目标字段清单中")
        for lg in f.get("lineage", []):
            seg = f"- {lg.get('step', '')}: {lg.get('source_table', '')}.{lg.get('source_field', '')}"
            expr = lg.get("raw_sql", "")
            cte_expr = lg.get("cte_expression", "")
            if cte_expr and cte_expr != expr:
                expr = f"{expr}（CTE内: {_short(cte_expr, 90)}）"
            if expr:
                seg += f" = {_short(expr, 160)}"
            if lg.get("cte_name"):
                seg += f"（经 CTE {lg['cte_name']}）"
            lines.append(seg)
        for p in f.get("physical_source", []):
            tbl = p.get("table", "") or "（表未解析）"
            lines.append(f"- 物理源: {tbl}.{p.get('field', '')}"
                         f"（{p.get('step_id', '')}，{p.get('transform', '')}）")
        lines.append("")
    return lines, rows


def render_table(draft, table):
    t_l = table.strip().lower()
    t_bare = t_l.split(".")[-1]
    lines = [f"# 表使用位置: {table}", ""]
    payload = {"joins": [], "blocks": [], "fields": []}
    for s in draft.get("data_flow", {}).get("steps", []):
        sid = s.get("step_id", "")
        hit = False
        for j in s.get("joins", []):
            jt = str(j.get("source_table", "")).lower()
            if t_l in jt or t_bare == jt.split(".")[-1]:
                seg = f"- {sid} ({s.get('rule_code', '')}): {j.get('join_type', '')} {j.get('source_table', '')}"
                if j.get("alias"):
                    seg += f"({j['alias']})"
                if j.get("join_condition"):
                    seg += f" ON {j['join_condition']}"
                lines.append(seg)
                payload["joins"].append({"step": sid, **{k: j.get(k) for k in ("source_table", "join_type", "join_condition")}})
                hit = True
        for blk, depth in _walk_blocks(s.get("data_blocks")):
            bt = str(blk.get("table", "")).lower()
            if bt and (t_l in bt or t_bare == bt.split(".")[-1]):
                lines.append(f"- {sid} 逻辑块: {blk.get('role', '')} {blk.get('table', '')}"
                             + (f" ON {blk['on_condition']}" if blk.get("on_condition") else ""))
                payload["blocks"].append({"step": sid, "role": blk.get("role"), "table": blk.get("table")})
                hit = True
        for c in s.get("ctes", []):
            for st in c.get("source_tables", []):
                stn = str(st.get("name", "") if isinstance(st, dict) else st).lower()
                if stn and (t_l in stn or t_bare == stn.split(".")[-1]):
                    lines.append(f"- {sid} CTE {c.get('name', '')} 源表")
                    hit = True
    for f in draft.get("field_mappings", {}).get("fields", []):
        for p in f.get("physical_source", []):
            pt = str(p.get("table", "")).lower()
            if pt and (t_l in pt or t_bare == pt.split(".")[-1]):
                lines.append(f"- 字段 {f.get('target_field', '')} 物理源 {p.get('table', '')}.{p.get('field', '')}"
                             f"（{p.get('step_id', '')}）")
                payload["fields"].append({"field": f.get("target_field"), "table": p.get("table")})
    if len(lines) == 2:
        return None, None
    return lines, payload


def _ctx(text, kw):
    """命中上下文片段（前后各 50 字符）。"""
    i = text.lower().find(kw.lower())
    if i < 0:
        return None
    lo, hi = max(0, i - 50), min(len(text), i + len(kw) + 50)
    return ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def render_search(draft, kw):
    lines = [f"# 搜索 “{kw}”（大小写不敏感）", ""]
    payload = {"sql": [], "fields": [], "dq": []}
    for s in draft.get("data_flow", {}).get("steps", []):
        sid = s.get("step_id", "")
        for key in ("raw_sql", "where_clause", "having_clause"):
            frag = _ctx(str(s.get(key, "")), kw)
            if frag:
                lines.append(f"- {sid} {key}: …{frag}…")
                payload["sql"].append({"step": sid, "where": key, "context": frag})
        for c in s.get("ctes", []):
            frag = _ctx(str(c.get("raw_sql", "")), kw)
            if frag:
                lines.append(f"- {sid} CTE {c.get('name', '')}: …{frag}…")
                payload["sql"].append({"step": sid, "where": f"cte:{c.get('name', '')}", "context": frag})
        for blk, _ in _walk_blocks(s.get("data_blocks")):
            frag = _ctx(str(blk.get("on_condition", "")), kw)
            if frag:
                lines.append(f"- {sid} 块 ON: …{frag}…")
    for f in draft.get("field_mappings", {}).get("fields", []):
        for lg in f.get("lineage", []):
            frag = _ctx(str(lg.get("raw_sql", "") or lg.get("cte_expression", "")), kw)
            if frag:
                lines.append(f"- 字段 {f.get('target_field', '')} ({lg.get('step', '')}): …{frag}…")
                payload["fields"].append({"field": f.get("target_field"), "context": frag})
    for r in draft.get("meta", {}).get("dq_rules", []):
        frag = _ctx(str(r.get("sql", "")), kw)
        if frag:
            lines.append(f"- DQ {r.get('rule_number', '')}: …{frag}…")
            payload["dq"].append({"rule": r.get("rule_number"), "context": frag})
    if len(lines) == 2:
        return None, None
    return lines, payload


# ── CLI ────────────────────────────────────────────────────────

def main():
    _hardening()
    ap = argparse.ArgumentParser(
        description="knowledge_draft.json 只读探查（AI 定向下钻通道）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="选择器优先级: --schema > --step > --field > --table > --search > 默认概览")
    ap.add_argument("--draft", required=True, help="knowledge_draft.json 路径")
    ap.add_argument("--schema", action="store_true", help="打印 draft 结构字典")
    ap.add_argument("--step", default="", help="步骤 step_id 或 rule_code")
    ap.add_argument("--part", default="summary",
                    choices=["summary", "sql", "blocks", "fields", "where", "joins", "ctes"],
                    help="--step 的细节切面（默认 summary）")
    ap.add_argument("--field", default="", help="字段名（血缘+表达式+物理源）")
    ap.add_argument("--table", default="", help="表名（哪些步骤/块/字段用到它）")
    ap.add_argument("--search", default="", help="关键字（SQL/表达式/条件全文搜索）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON 切片")
    args = ap.parse_args()

    draft_path = Path(args.draft)
    if not draft_path.exists():
        print(f"[ERROR] draft 不存在: {draft_path}", file=sys.stderr)
        sys.exit(1)
    try:
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[ERROR] draft 解析失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 单选择器分发（优先级见 epilog）
    if args.schema:
        lines, payload = render_schema()
    elif args.step:
        lines, payload = render_step(draft, args.step, args.part)
        if lines is None:
            print(f"[ERROR] 未找到步骤: {args.step}（可用 step_id 或 rule_code）", file=sys.stderr)
            sys.exit(1)
    elif args.field:
        lines, payload = render_field(draft, args.field)
        if lines is None:
            print(f"[ERROR] 未找到字段: {args.field}", file=sys.stderr)
            sys.exit(1)
    elif args.table:
        lines, payload = render_table(draft, args.table)
        if lines is None:
            print(f"[ERROR] 未找到表: {args.table}", file=sys.stderr)
            sys.exit(1)
    elif args.search:
        lines, payload = render_search(draft, args.search)
        if lines is None:
            print(f"[ERROR] 无命中: {args.search}", file=sys.stderr)
            sys.exit(1)
    else:
        lines, payload = render_stats(draft)

    _emit(lines, as_json=args.json, payload=payload)
    sys.exit(0)


if __name__ == "__main__":
    main()
