#!/usr/bin/env python3
"""spec_extract —— SPEC 通道的语义提取器（适配层，docs/17 §三/§四）。

职责（全部只读消费引擎/viewmodel 既有事实，不重新解析 SQL）：
    build_sheet3_rows()     表关联行 + G 列段式重组（ON / 限制 / 写入前限制 /
                            预处理贴码 / 主表链预处理）+ E1 单表行
    decompose_field_logic() Sheet4 加工逻辑列的拆解式（表达式：/ 常量：）
    _strip_as_alias()       表达式去 "AS 别名" 尾巴（纯文本切，保持原文）

铁律（docs/17 §五「拆不出就贴原文，绝不编造」）：提取失败/形态不认识 →
退化为 verbatim 原文，不硬拆。

主表链预处理的数据源 = data_flow step 的 group_by / having_clause（引擎
结构化产出，非二次解析）。

Author: 院博
"""

from __future__ import annotations

import re

from viewmodel import resolve_on_condition_aliases, split_schema_table, norm_name

_NO_JOIN_PLACEHOLDER = "—（无关联）"

# 关联方式标准枚举（docs/17 §三，2026-11-02 实测修订：只保留这几种）
_JOIN_ENUM = ("LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN", "CROSS JOIN")


def _join_kind(jt: str) -> str:
    """引擎 join_type → 标准枚举（LEFT/RIGHT/INNER/FULL/CROSS JOIN）。

    归一规则：大写、去 OUTER 词、裸 JOIN → INNER JOIN；认不出的形态原样大写
    （不编造枚举外的值）。
    """
    import re as _re
    s = _re.sub(r"\s+", " ", (jt or "").strip().upper()).strip()
    if not s:
        return ""
    s = _re.sub(r"\bOUTER\b", "", s).strip()
    s = _re.sub(r"\s+", " ", s)
    if s == "JOIN":
        return "INNER JOIN"
    if s in _JOIN_ENUM:
        return s
    return (jt or "").strip().upper()


# ════════════════════════════════════════════════════════════
# 子查询关联（main-subquery，2026-11-02 嵌套子查询方案）
# ════════════════════════════════════════════════════════════

def _locate_subquery(raw_sql: str, subquery_sql: str) -> int:
    """引擎 subquery_sql 是归一化文本（空白可能不同）→ 用 token 序列空白不敏感
    定位其开头在 raw_sql 中的位置；返回子查询首 "(" 的下标，失败 -1。"""
    if not raw_sql or not subquery_sql:
        return -1
    head = re.sub(r"^\(", "", subquery_sql.strip())[:40].split()
    if not head:
        return -1
    pattern = r"\s+".join(re.escape(t) for t in head)
    m = re.search(pattern, raw_sql, re.S)
    if not m:
        return -1
    open_idx = raw_sql.rfind("(", 0, m.start())
    return open_idx if open_idx != -1 else -1


def _subquery_verbatim(raw_sql: str, subquery_sql: str, alias: str) -> tuple:
    """(外层 join 关键字, 贴码用子查询原文)。

    原文优先从 raw_sql 括号配平提取（贴码纪律：逐字）；定位失败回退引擎
    subquery_sql（归一化文本，信息等价）。join 关键字读不出时兜底 INNER JOIN。
    """
    idx = _locate_subquery(raw_sql, subquery_sql)
    if idx >= 0:
        depth, span_end = 0, -1
        for i in range(idx, len(raw_sql)):
            if raw_sql[i] == "(":
                depth += 1
            elif raw_sql[i] == ")":
                depth -= 1
                if depth == 0:
                    span_end = i
                    break
        sql_text = raw_sql[idx:span_end + 1] if span_end > idx else subquery_sql
        m = re.search(r"(LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\s*$",
                      raw_sql[:idx], re.I | re.S)
        if m:
            kw = (m.group(1) + " JOIN") if m.group(1) else "JOIN"
            return _join_kind(kw), sql_text
        return "INNER JOIN", sql_text
    return "INNER JOIN", subquery_sql


def _subquery_index(ctx) -> dict:
    """join_paths 子查询索引：{step_id: {外层别名: {sql/main_table/tables/on}}}。

    数据源 = data_flow.join_paths[*].subquery_sql / subquery_tables / df joins
    的 ON 条件（引擎结构化产出，零二次解析）。
    """
    out: dict = {}
    for s in ctx.data_flow_steps:
        sid = s.get("step_id", "")
        for alias, info in (s.get("join_paths", {}) or {}).items():
            if not isinstance(info, dict):
                continue
            sql = info.get("subquery_sql", "")
            if not sql:
                continue
            main_tbl, tables = "", set()
            for t in info.get("subquery_tables") or []:
                if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0]:
                    tables.add(t[0].lower())
                    if not main_tbl and (len(t) < 3 or t[2] == "main"):
                        main_tbl, _ma = t[0], t[1]
            on = ""
            for j in s.get("joins", []):
                if j.get("alias") == alias:
                    on = j.get("join_condition", "")
                    break
            kind, sql_text = _subquery_verbatim(s.get("raw_sql", ""), sql, alias)
            out.setdefault(sid, {})[alias] = {
                "sql": sql_text, "main_table": main_tbl, "tables": tables,
                "on": on, "kind": kind,
            }
    return out


def _step_main_table(ctx, sid: str) -> tuple:
    """步骤主表（FROM 驱动表）：df joins 首个非子查询物理表 (表名, 别名)。"""
    for j in ctx.df_step(sid).get("joins", []):
        st = j.get("source_table", "")
        if st and not st.startswith("(subquery:"):
            return st, j.get("alias", "")
    return "", ""


def _squeeze(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _maybe_verbatim(pred_text: str, raw_sqls: list) -> str:
    """子查询谓词保真（贴原文原则）：引擎 where_clause 对子查询会丢形（如内部
    WHERE 关键字被吞，"t WHERE x" → "t x"）。谓词含 EXISTS/IN(SELECT 时，从
    步骤 raw_sql 的顶层 WHERE 原文找回（相似度≥0.6 校验同源）；找不到 → 原样。
    """
    if not pred_text or not raw_sqls:
        return pred_text
    up = pred_text.upper()
    if "EXISTS(" not in up and "IN (SELECT" not in up and "IN(SELECT" not in up:
        return pred_text
    from difflib import SequenceMatcher
    target = _squeeze(pred_text)
    for raw in raw_sqls:
        if not raw:
            continue
        m = re.search(r"\bWHERE\b(.*?)(?=\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b"
                      r"|\bLIMIT\b|\Z)", raw, re.S | re.I)
        if not m:
            continue
        body = m.group(1).strip().rstrip(";").strip()
        if body and SequenceMatcher(None, _squeeze(body), target).ratio() >= 0.6:
            return body
    return pred_text


# ════════════════════════════════════════════════════════════
# Sheet4：拆解式字段逻辑（§四）
# ════════════════════════════════════════════════════════════

def _strip_as_alias(expr_sql: str) -> str:
    """表达式去尾部 "AS 别名"（纯文本切，逐字保真；非 AS 形态原样返回）。"""
    s = (expr_sql or "").strip().rstrip(";").strip()
    m = re.match(r"^(.+?)\s+[Aa][Ss]\s+[A-Za-z_][\w$]*$", s, re.S)
    return m.group(1).strip() if m else s


def decompose_field_logic(raw_sql: str, transform_type: str = "") -> str:
    """raw 表达式 → 拆解式字段逻辑（成值语义，不带表级条件/规则编码，A1+D1）。

    transform_type=value（常量赋值）→ `常量：'CUR'`；其余 → `表达式：SUM(balance)`。
    拆不动（空 raw）→ 空串（直取字段逻辑列本就留空）。
    """
    if not raw_sql:
        return ""
    expr = _strip_as_alias(raw_sql)
    if not expr:
        return ""
    if (transform_type or "").lower() == "value":
        return f"常量：{expr}"
    return f"表达式：{expr}"


# ════════════════════════════════════════════════════════════
# Sheet3：段式重组 + E1 单表行（§三）
# ════════════════════════════════════════════════════════════

def _fmt_preprocess(pre: str) -> str:
    """viewmodel 预处理原文链 → 段式（保留 [CTE x] 贴码原文，只换头）。"""
    body = pre or ""
    if body.startswith("预处理"):
        nl = body.find("\n")
        if nl != -1:
            body = body[nl + 1:]
    return "预处理：" + body.strip()


def _inner_alias(ctx, cte_name: str, right_table: str) -> str:
    """main-cte 行的右别名：物理表在 CTE 查询体内部的别名（无则空，D2）。"""
    cte = ctx.cte_defs.get((cte_name or "").lower(), {})
    for st in cte.get("source_tables", []):
        if norm_name(st.get("name", "")) == norm_name(right_table):
            return st.get("alias", "")
    return ""


def _chain_group_segments(ctx) -> list:
    """主表链预处理段：聚合步骤的 分组 + 分组保留（HAVING），按步骤序。"""
    segs = []
    for s in ctx.steps_list:
        df = ctx.df_step(s.get("step_id", ""))
        gb = df.get("group_by") or []
        if not gb:
            continue
        seg = f"按 {', '.join(gb)} 分组"
        hv = (df.get("having_clause") or "").strip()
        if hv:
            hv_body = re.sub(r"^HAVING\s+", "", hv, flags=re.I)
            seg += f"，保留 {hv_body}"
        segs.append(seg)
    return segs


def build_sheet3_rows(ctx) -> list:
    """ViewContext → Sheet3 行（7 列模型）。

    段式 G 列（docs/17 §三）：{ON}；限制：…；写入前限制：…；预处理：贴码；
    主表链预处理：分组/保留。段按需出现，"；" 分隔。
    E1：无任何关联行时，每物理源表一行（右表=—（无关联）），行级限制落首行。
    """
    base = ctx.logic_join_rows()
    sq_index = _subquery_index(ctx)
    raw_sqls = [s.get("raw_sql", "") for s in ctx.data_flow_steps]

    def _expanded_subquery_alias(row):
        """该行是否为某子查询的引擎展开行（JOIN_SUBQUERY_*，条件为空）——
        其信息将由 main-subquery 行的预处理贴码整体承载。"""
        if row.origin != "main" or "SUBQUERY" not in (row.join_type or ""):
            return None
        for alias, info in sq_index.get(row.step_id, {}).items():
            if norm_name(row.right_table) in info["tables"]:
                return alias
        return None

    survivors = [r for r in base if _expanded_subquery_alias(r) is None]

    rows = []
    attached_wheres = []
    last_pos_by_step: dict = {}

    for row in survivors:
        cond = row.on_condition or ""
        if row.origin == "main":
            rewritten, note = resolve_on_condition_aliases(
                cond, ctx.step_alias_map(row.step_id),
                ctx.df_step(row.step_id).get("join_key_lineage", {}))
            if note:
                rewritten = rewritten + note
            cond = rewritten
        # viewmodel 附着的 "；WHERE xxx" → 「限制：」段，并记账（防重复披露）
        limit_seg = ""
        m = re.search(r"；WHERE (.+)$", cond, re.S)
        if m:
            raw_w = m.group(1).strip()
            limit_seg = _maybe_verbatim(raw_w, raw_sqls)   # 渲染用保真文本
            cond = cond[:m.start()].strip()
            attached_wheres.append(raw_w)                   # 记账用原文（去重口径）
        parts = [cond] if cond else []
        if limit_seg:
            parts.append(f"限制：{limit_seg}")
        if row.preprocess:
            parts.append(_fmt_preprocess(row.preprocess))
        _, l_tbl = split_schema_table(row.left_table)
        _, r_tbl = split_schema_table(row.right_table)
        right_alias = row.right_alias
        if row.origin.startswith("main-cte:"):
            right_alias = _inner_alias(ctx, row.origin.split(":", 1)[1], row.right_table)
        rows.append([None, l_tbl, row.left_alias, r_tbl, right_alias,
                     _join_kind(row.join_type), "；".join(parts)])
        last_pos_by_step[row.step_id] = len(rows) - 1

    # ── main-subquery 行（嵌套子查询，docs/17 §三）：右表=子查询主物理源，
    #    右别名=外层别名（ON 里真实引用的样子），预处理贴子查询原文——
    #    子查询内部的关联/过滤/分组信息全部收进贴码，不单独成行 ──
    for sid, by_alias in sq_index.items():
        for alias, info in by_alias.items():
            main_tbl, main_alias = _step_main_table(ctx, sid)
            if not main_tbl:
                continue
            parts = [info["on"]] if info["on"] else []
            sw = ctx.step_where(sid)
            if sw and sw not in attached_wheres:
                parts.append(f"限制：{sw}")      # 展开行被移除时 WHERE 随主承载行落位
                attached_wheres.append(sw)
            parts.append("预处理：" + info["sql"])
            _, l_tbl = split_schema_table(main_tbl)
            _, r_tbl = split_schema_table(info["main_table"] or "")
            row = [None, l_tbl, main_alias, r_tbl, alias, info["kind"],
                   "；".join(parts)]
            pos = last_pos_by_step.get(sid)
            if pos is None:
                rows.append(row)
            else:
                rows.insert(pos + 1, row)
                last_pos_by_step[sid] = pos + 1

    # 未被附着的步骤 WHERE（logic_join_rows 对无 JOIN 步骤不产出承载行）
    unattached = []
    for s in ctx.steps_list:
        w = ctx.step_where(s.get("step_id", ""))
        if w and w not in attached_wheres:
            unattached.append(_maybe_verbatim(w, raw_sqls))

    chain_group = _chain_group_segments(ctx)

    if rows:
        # 有关联行：未附着 WHERE 全部按「写入前限制」挂主表行（首行左表=驱动表）
        tail = []
        if unattached:
            tail.append("写入前限制：" + "；".join(unattached))
        if chain_group:
            tail.append("主表链预处理：" + "；".join(chain_group))
        if tail:
            rows[0][6] = "；".join(x for x in [rows[0][6]] + tail if x)
        return rows

    # ── E1 单表行：无关联（含全表无 JOIN）→ 每物理源表一行，行级限制落首行 ──
    sources = ctx.entity_source_tables()
    if not sources:
        return []
    limit_first, pre_write_rest = "", []
    for i, w in enumerate(unattached):
        if i == 0:
            limit_first = w            # 首个（源表层）WHERE → 限制
        else:
            pre_write_rest.append(w)   # 链上/写入前 → 写入前限制
    first_parts = []
    if limit_first:
        first_parts.append(f"限制：{limit_first}")
    if pre_write_rest:
        first_parts.append("写入前限制：" + "；".join(pre_write_rest))
    if chain_group:
        first_parts.append("主表链预处理：" + "；".join(chain_group))
    out = []
    for i, es in enumerate(sources):
        out.append([None, es.table, es.alias, _NO_JOIN_PLACEHOLDER, None,
                    "", "；".join(first_parts) if i == 0 else ""])
    return out


# ════════════════════════════════════════════════════════════
# 谓词守恒校验（docs/17 §七）
# ════════════════════════════════════════════════════════════

def predicate_conservation(ctx, sheet3_rows: list, sheet4_rows: list) -> list:
    """每个步骤主查询的 WHERE / HAVING 谓词，必须落在 Sheet3 某段
    （限制 / 写入前限制 / 主表链预处理）或 Sheet4 逻辑列之一。

    缺失 = structural 级 warning（拆解器保真的机器验——渲染层漏放谓词
    直接把 self_check 打成 review，人必须复核）。CTE 内部谓词不在校验面
    （整段贴原文天然保真）。

    Returns: [{"type", "title"}, ...]（severity 由调用方定为 structural）
    """
    raw_sqls = [s.get("raw_sql", "") for s in ctx.data_flow_steps]
    g_blob = _squeeze(" ".join(str(r[6]) for r in sheet3_rows if len(r) > 6)).lower()
    logic_blob = _squeeze(
        " ".join(str(r[6]) for r in sheet4_rows if len(r) > 6)).lower()

    warnings = []
    for s in ctx.steps_list:
        sid = s.get("step_id", "")
        label = s.get("rule_code", "") or sid
        w = ctx.step_where(sid)
        if w:
            # 事实侧与渲染侧同用 _maybe_verbatim（子查询丢形修复），文本口径一致
            w_v = _maybe_verbatim(w, raw_sqls)
            if _squeeze(w_v).lower() not in g_blob:
                warnings.append({
                    "type": "predicate_missing_where",
                    "title": f"谓词守恒：步骤 {label} 的 WHERE 未落入表3"
                             f"（限制/写入前限制）——{w_v[:60]}",
                })
        hv = (ctx.df_step(sid).get("having_clause") or "").strip()
        if hv:
            body = re.sub(r"^HAVING\s+", "", hv, flags=re.I)
            if _squeeze(body).lower() not in g_blob:
                warnings.append({
                    "type": "predicate_missing_having",
                    "title": f"谓词守恒：步骤 {label} 的 HAVING（{body[:60]}）"
                             f"未落入表3主表链预处理段",
                })
    return warnings
