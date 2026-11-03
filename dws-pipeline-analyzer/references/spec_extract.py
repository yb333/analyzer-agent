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
from engine import _is_intermediate_table

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
    """join_paths 子查询索引：{step_id: {外层别名: {sql/main_table/tables/on/...}}}。

    数据源 = data_flow.join_paths[*].subquery_sql / subquery_tables / df joins
    的 ON 条件（引擎结构化产出，零二次解析）。

    两形态（2026-11-02 实测拆分）：
    - JOIN 子查询（a JOIN (…) b）→ main-subquery 行（右表=主物理源）
    - FROM 包裹（FROM (…) s，整个 FROM 是子查询）→ 虚拟逻辑表行（右表不写
      物理表名——s 是多表加工出的虚拟表；引擎此形态 joins 全是空条件展开行，
      ON 只存在于子查询原文里）
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
            main_tbl, main_alias, tables, table_list = "", "", set(), []
            inner_tables = []      # 子查询内的非驱动物理表 [(表全名, 别名)]
            for t in info.get("subquery_tables") or []:
                if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0]:
                    tables.add(t[0].lower())
                    if t[0] not in table_list:
                        table_list.append(t[0])
                    if not main_tbl and (len(t) < 3 or t[2] == "main"):
                        main_tbl, main_alias = t[0], t[1]
                    else:
                        inner_tables.append((t[0], t[1]))
            on = ""
            join_type = ""
            for j in s.get("joins", []):
                if j.get("alias") == alias:
                    on = j.get("join_condition", "")
                    join_type = j.get("join_type", "")
                    break
            kind, sql_text = _subquery_verbatim(s.get("raw_sql", ""), sql, alias)
            out.setdefault(sid, {})[alias] = {
                "sql": sql_text, "main_table": main_tbl,
                "main_alias": main_alias, "tables": tables,
                "table_list": table_list, "inner_tables": inner_tables,
                "on": on, "kind": kind,
                "is_from_wrap": (join_type or "").upper().startswith("FROM"),
            }
    return out


def _step_main_table(ctx, sid: str) -> tuple:
    """步骤主表（FROM 驱动表）：df joins 首个非子查询物理表 (表名, 别名)。"""
    for j in ctx.df_step(sid).get("joins", []):
        st = j.get("source_table", "")
        if st and not st.startswith("(subquery:"):
            return st, j.get("alias", "")
    return "", ""


def _inner_join_of(fragment: str, alias: str) -> tuple:
    """从子查询片段原文提内部表的 verbatim ON 与 join 关键字。

    Returns: (kind, on)；找不到 → ("", "")（行仍出，ON 由片段兜底）。
    """
    pat = (rf"(LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\s+\S+\s+{re.escape(alias)}"
           rf"\s+ON\s+(.+?)(?=\s+(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b"
           rf"|\bWHERE\b|\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\)|$)")
    m = re.search(pat, fragment or "", re.I | re.S)
    if not m:
        return "", ""
    return _join_kind((m.group(1) or "JOIN") + " JOIN"), m.group(2).strip()


# ════════════════════════════════════════════════════════════
# 包裹片段分解器（W4，2026-11-03 用户案例：FROM (嵌套子查询包裹) s WHERE）
# ════════════════════════════════════════════════════════════

_JOIN_KW_RE = re.compile(r"\b(LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b", re.I)
_FROM_KW_RE = re.compile(r"\bFROM\b", re.I)
_CLAUSE_KW_RE = re.compile(
    r"\b(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\s+BY\b"
    r"|\bHAVING\b|\bORDER\s+BY\b", re.I)
_WORD_RE = re.compile(r"[\w.]+")
_WS_RE = re.compile(r"\s+")


def _depth_map(s: str) -> list:
    """每个字符位置前的括号深度（depth-0 = 顶层）。"""
    d, depth = [0] * len(s), 0
    for i, ch in enumerate(s):
        d[i] = depth
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return d


def _balanced(s: str, open_idx: int) -> int:
    """配平右括号下标；不平衡 → -1。"""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _parse_join_target(body: str, pos: int) -> tuple:
    """解析 JOIN/FROM 目标：表名或 (子查询)，及别名。

    Returns: (end_pos, {"kind"?, "table", "alias", "sub_sql"?})
    """
    m = _WS_RE.match(body, pos)
    if m:
        pos = m.end()
    if pos < len(body) and body[pos] == "(":
        close = _balanced(body, pos)
        if close == -1:
            return pos, {}
        sub_sql = body[pos:close + 1]
        pos = close + 1
        m2 = _WS_RE.match(body, pos)
        if m2:
            pos = m2.end()
        am = _WORD_RE.match(body, pos)
        alias = am.group(0) if am else ""
        if am:
            pos = am.end()
        return pos, {"alias": alias, "sub_sql": sub_sql}
    tm = _WORD_RE.match(body, pos)
    if not tm:
        return pos, {}
    table = tm.group(0)
    pos = tm.end()
    m2 = _WS_RE.match(body, pos)
    if m2:
        pos2 = m2.end()
        am = _WORD_RE.match(body, pos2)
        # 下一词是别名（不是 ON/WHERE 等关键字）
        if am and am.group(0).upper() not in ("ON", "WHERE", "GROUP", "ORDER",
                                              "HAVING", "LEFT", "RIGHT",
                                              "INNER", "FULL", "CROSS", "JOIN"):
            return am.end(), {"table": table, "alias": am.group(0)}
    return pos, {"table": table, "alias": ""}


def _decompose_wrapper(fragment: str) -> dict:
    """包裹片段结构化分解（含 FROM 子查询链递归下探）。

    fragment = "(SELECT … ) s" 逐字原文。Returns:
      {"ok", "from": (表全名, 别名),
       "joins": [{"is_sub", "kind", "alias"?, "table"?, "on"?, "sub_sql"?,
                  "sub_from"?, "sub_inner"?} …按 SQL 出现顺序]}
    """
    body = fragment.strip()
    if body.startswith("("):
        close = _balanced(body, 0)
        if close == -1:
            return {"ok": False}
        body = body[1:close]
    dm = _depth_map(body)
    # FROM 目标
    fm = next((m for m in _FROM_KW_RE.finditer(body) if dm[m.start()] == 0), None)
    if fm is None:
        return {"ok": False}
    pos, target = _parse_join_target(body, fm.end())
    if not target:
        return {"ok": False}
    joins = []
    # 顶层 JOIN 子句（按出现序）
    tops = [m for m in _JOIN_KW_RE.finditer(body) if dm[m.start()] == 0]
    clause_ends = [m.start() for m in _CLAUSE_KW_RE.finditer(body)
                   if dm[m.start()] == 0] + [len(body)]
    for tm_ in tops:
        kind = _join_kind((tm_.group(1) or "JOIN") + " JOIN") if tm_.group(1) \
            else "INNER JOIN"
        p, tgt = _parse_join_target(body, tm_.end())
        if not tgt:
            continue
        # ON 条件：到下一个顶层子句关键字
        om = re.match(r"\s*ON\b", body[p:], re.I)
        on = ""
        if om:
            on_start = p + om.end()
            nxt = min(e for e in clause_ends if e > on_start)
            on = body[on_start:nxt].strip()
        entry = {"is_sub": "sub_sql" in tgt, "kind": kind, "on": on,
                 "alias": tgt.get("alias", "")}
        if entry["is_sub"]:
            entry["sub_sql"] = tgt["sub_sql"]
            sf, si = _slave_expand(tgt["sub_sql"])
            entry["sub_from"], entry["sub_inner"] = sf, si
        else:
            entry["table"] = tgt.get("table", "")
        joins.append(entry)
    # FROM 目标本身是子查询 → 递归下探取驱动表（其内层 JOIN 归入本层 joins 头部）
    if "sub_sql" in target:
        sf, si = _slave_expand(target["sub_sql"])
        from_ref = sf
        for it in reversed(si):
            joins.insert(0, {"is_sub": False, "kind": it["kind"],
                             "table": it["table"], "alias": it["alias"],
                             "on": it["on"]})
    else:
        from_ref = (target.get("table", ""), target.get("alias", ""))
    if not from_ref[0]:
        return {"ok": False}
    return {"ok": True, "from": from_ref, "joins": joins}


def _slave_expand(sub_sql: str, depth: int = 0) -> tuple:
    """子查询（逻辑从表）定义 → (驱动表(表,别名), 内部物理 JOIN 清单)。

    FROM 链上的嵌套子查询递归下探（tt→p/ch 形态）；驱动表 = 最深层 FROM 首表；
    各层直接表 JOIN 全部收进 inner（行标签用最外层锚，docs/14 §四）。
    更深的 JOIN 子查询不展开（整块留在原文）。
    """
    if depth > 5:
        return ("", ""), []
    body = sub_sql.strip()
    if body.startswith("("):
        close = _balanced(body, 0)
        if close == -1:
            return ("", ""), []
        body = body[1:close]
    dm = _depth_map(body)
    fm = next((m for m in _FROM_KW_RE.finditer(body) if dm[m.start()] == 0), None)
    if fm is None:
        return ("", ""), []
    pos, target = _parse_join_target(body, fm.end())
    if not target:
        return ("", ""), []
    inner = []
    tops = [m for m in _JOIN_KW_RE.finditer(body) if dm[m.start()] == 0]
    clause_ends = [m.start() for m in _CLAUSE_KW_RE.finditer(body)
                   if dm[m.start()] == 0] + [len(body)]
    for tm_ in tops:
        kind = _join_kind((tm_.group(1) or "JOIN") + " JOIN") if tm_.group(1) \
            else "INNER JOIN"
        p, tgt = _parse_join_target(body, tm_.end())
        if not tgt or "sub_sql" in tgt:
            continue    # 深层子查询 JOIN 不展开（原文兜底）
        om = re.match(r"\s*ON\b", body[p:], re.I)
        on = ""
        if om:
            on_start = p + om.end()
            nxt = min(e for e in clause_ends if e > on_start)
            on = body[on_start:nxt].strip()
        inner.append({"table": tgt.get("table", ""), "alias": tgt.get("alias", ""),
                      "on": on, "kind": kind})
    if "sub_sql" in target:
        sf, si = _slave_expand(target["sub_sql"], depth + 1)
        inner.extend(si)
        return sf, inner
    return (target.get("table", ""), target.get("alias", "")), inner


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
    """raw 表达式 → 裸成值表达式（展开态本体，docs/14 P3 / C1 拍板）。

    只做 AS 尾巴剥离；前缀（表达式：/常量：）已随展开态取消——说明段、
    物理根别名前缀由组装层（spec_export）加。拆不动（空 raw）→ 空串。
    """
    if not raw_sql:
        return ""
    return _strip_as_alias(raw_sql)


# ════════════════════════════════════════════════════════════
# tmp 链物化无关模型（docs/14 P1 内联等价，C2/C3 拍板 2026-11-02）
# ════════════════════════════════════════════════════════════

def _short_lower(t: str) -> str:
    return split_schema_table(t or "")[1].lower()


def _producer_of(ctx, table_full: str):
    """产出某表的步骤（target_table 匹配；短名比较——生产步登记短名、
    消费侧引用可带 schema，两形态都真实存在）。"""
    for s in ctx.steps_list:
        t = s.get("target_table", "")
        if t and _short_lower(t) == _short_lower(table_full):
            return s
    return None


def _tmp_chain_index(ctx) -> dict:
    """跨规则 tmp 产消链 → 物化无关挂载模型（旗舰形态：终端步骤主 FROM 是 tmp）。

    压平规则（内联等价）：tmp 链呈现为「逻辑从表」——锚 = 消费 SQL 引用它的别名
    （tmp 表名永不进登记值）；定义片段 = 各生产步 query_sql 逐字（多跳 = 嵌套锚，
    深层在前同挂载位）；驱动表 = 最深层生产步的首物理表（定义挂载行）；
    生产步内的物理 JOIN 表 → 每表一行素材（【锚 内】ON 原文 + 指回驱动表行）。

    Returns: {终端step_id: {
        "driving": (表全名, 别名),            # 主表行（含定义挂载）
        "mounts": [{"anchor", "sql"}],       # 深层在前，逐字
        "inner": [{"table","alias","on","anchor"}],
        "producer_ids": [生产步 step_id],
    }}
    """
    out: dict = {}
    for s in ctx.steps_list:
        sid = s.get("step_id", "")
        # 终端 = 写终态表的步骤（自身 target 是中间表的属于链内生产步，
        # 不作为挂载终端——否则整链会被中间消费者重复呈现）
        if _is_intermediate_table(s.get("target_table", "")):
            continue
        joins = ctx.df_step(sid).get("joins") or []
        if not joins:
            continue
        first = joins[0].get("source_table", "")
        if not first or not _is_intermediate_table(first):
            continue
        if _producer_of(ctx, first) is None:
            continue
        mounts, inner, producer_ids, seen = [], [], [], set()
        driving = ["", ""]

        def visit(tmp_full, consumer_alias):
            prod = _producer_of(ctx, tmp_full)
            if prod is None or prod.get("step_id") in seen:
                return
            seen.add(prod.get("step_id"))
            producer_ids.append(prod.get("step_id"))
            pdf = ctx.df_step(prod.get("step_id"))
            pjoins = pdf.get("joins") or []
            deeper = next((j for j in pjoins
                           if j.get("source_table")
                           and _is_intermediate_table(j["source_table"])), None)
            if deeper is not None:
                visit(deeper["source_table"], deeper.get("alias", ""))
            # 深层已入列，自身片段随后（保证 mounts 深层在前）
            mounts.append({"anchor": consumer_alias,
                           "sql": pdf.get("raw_sql", "")})
            for j in pjoins:
                jst = j.get("source_table", "")
                if (not jst or jst.startswith("(subquery:")
                        or _is_intermediate_table(jst)):
                    continue
                if (j.get("join_type") or "FROM") == "FROM":
                    if not driving[0]:
                        driving[0], driving[1] = jst, j.get("alias", "")
                else:
                    inner.append({"table": jst, "alias": j.get("alias", ""),
                                  "on": j.get("join_condition", ""),
                                  "anchor": consumer_alias})

        visit(first, joins[0].get("alias", ""))
        if not driving[0]:
            continue    # 链上无物理驱动表（异常形态）→ 走既有分支
        out[sid] = {"driving": (driving[0], driving[1]), "mounts": mounts,
                    "inner": inner, "producer_ids": producer_ids}
    return out


def strip_alias_prefix(expr: str) -> str:
    """去表达式里的 别名. 前缀（说明段口径：分组键/保留条件用裸字段）。"""
    return re.sub(r"\b[\w]+\.", "", expr or "")


def prefix_root_fields(expr: str, roots: list) -> str:
    """展开态：表达式里裸根字段加 别名. 前缀（已限定/非常量名不动）。"""
    out = expr or ""
    for alias, field in roots:
        if not alias or not field:
            continue
        out = re.sub(rf"(?<![\w.]){re.escape(field)}\b", f"{alias}.{field}", out)
    return out


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


def _chain_group_segments(ctx, exclude=None) -> list:
    """主表链预处理段：聚合步骤的 分组 + 分组保留（HAVING），按步骤序。

    exclude：tmp 链成员步骤（其 GROUP BY/HAVING 已在定义片段内逐字，
    不重复成段——物化无关模型）。
    """
    segs = []
    for s in ctx.steps_list:
        sid = s.get("step_id", "")
        if exclude and sid in exclude:
            continue
        df = ctx.df_step(sid)
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
    tmp_chains = _tmp_chain_index(ctx)
    # 链成员步骤（终端 + 各生产步）：其 base 行由链模型接管（物化无关）
    chain_step_ids = set(tmp_chains)
    for _info in tmp_chains.values():
        chain_step_ids.update(_info["producer_ids"])
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

    survivors = [r for r in base
                 if _expanded_subquery_alias(r) is None
                 and r.step_id not in chain_step_ids]

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

    # ── 子查询关联行（嵌套子查询，docs/17 §三），两形态 ──
    for sid, by_alias in sq_index.items():
        for alias, info in by_alias.items():
            if info["is_from_wrap"]:
                # FROM 包裹（FROM (…) s，W4 分解模型）：片段结构化分解——
                # 直接表 JOIN 行 + JOIN 子查询行（锚+定义+内部表行，指回最外层锚）
                # + 包裹残余行（D-b：右表=虚拟逻辑表，方式空，装外层限定/开窗收敛）
                sw = ctx.step_where(sid)
                dec = _decompose_wrapper(info["sql"])
                if dec.get("ok"):
                    from_tbl, from_alias = dec["from"]
                    _, f_short = split_schema_table(from_tbl)
                    for jn in dec["joins"]:
                        if jn.get("is_sub"):
                            sf_tbl, sf_alias = jn.get("sub_from") or ("", "")
                            if not sf_tbl:
                                continue
                            _, s_short = split_schema_table(sf_tbl)
                            parts = ([jn["on"]] if jn["on"] else []) + [
                                f"【逻辑从表 {jn['alias']} · 定义】{jn['sub_sql']}"]
                            rows.append([None, f_short, from_alias, s_short,
                                         jn["alias"], jn["kind"],
                                         "；".join(parts)])
                            for it in jn.get("sub_inner") or []:
                                _, it_short = split_schema_table(it["table"])
                                g2 = ((f"【{jn['alias']} 内】{it['on']}；")
                                      if it["on"] else "") + \
                                     f"参与逻辑从表 {jn['alias']}，定义见 {s_short} 行"
                                rows.append([None, s_short, sf_alias, it_short,
                                             it["alias"], it["kind"], g2])
                        else:
                            _, j_short = split_schema_table(jn["table"])
                            rows.append([None, f_short, from_alias, j_short,
                                         jn["alias"], jn["kind"], jn["on"]])
                    resid = []
                    if sw and sw not in attached_wheres:
                        note = ("（开窗收敛，窗口定义见原文）"
                                if re.search(r"\brn\s*=", sw) else "")
                        resid.append(f"限定：{sw}{note}")
                        attached_wheres.append(sw)
                    resid.append(f"【{alias} 内】关联见上")
                    rows.append([None, f_short, from_alias,
                                 f"虚拟逻辑表 {alias}", alias, "",
                                 "；".join(resid)])
                    last_pos_by_step[sid] = len(rows) - 1
                    continue
                # 分解失败 → 虚拟逻辑表兜底（拆不出就贴原文）
                if not info["table_list"]:
                    continue
                short_tables = [split_schema_table(t)[1] for t in info["table_list"]]
                parts = [
                    f"预处理（虚拟表 {alias} 由 {'、'.join(short_tables)} "
                    f"关联加工而来）：{info['sql']}"
                ]
                if sw and sw not in attached_wheres:
                    parts.append(f"限制：{sw}")
                    attached_wheres.append(sw)
                _, l_tbl = split_schema_table(info["main_table"] or "")
                rows.append([None, l_tbl, info["main_alias"],
                             f"虚拟逻辑表 {alias}", alias, "",
                             "；".join(parts)])
                last_pos_by_step[sid] = len(rows) - 1
                continue
            # JOIN 子查询（a JOIN (…) b）：右表=子查询主物理源，
            # 右别名=外层别名（ON 里真实引用的样子），预处理贴子查询原文——
            # 子查询内部的关联/过滤/分组信息全部收进贴码，不单独成行
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
            # 子查询内其余物理表：每表一行（【锚 内】ON 原文 + 指回锚行）——
            # 引擎对子查询内 JOIN 不带条件（JOIN_SUBQUERY_* 空条件展开），
            # ON 从片段原文提 verbatim（贴原文原则；提不到由片段兜底）
            for it_tbl, it_alias in info.get("inner_tables", []):
                kind2, on2 = _inner_join_of(info["sql"], it_alias or "")
                _, it_short = split_schema_table(it_tbl)
                g2 = (f"【{alias} 内】{on2}；" if on2 else "") + \
                     f"参与逻辑从表 {alias}，定义见 {r_tbl} 行"
                rows.append([None, r_tbl, info.get("main_alias", ""),
                             it_short, it_alias,
                             kind2 or "INNER JOIN", g2])
                last_pos_by_step[sid] = len(rows) - 1

    # ── tmp 链物化无关行（docs/14 P1，C2/C3 拍板）：主表行（驱动表+定义挂载）
    #    + 生产步内物理表每表一行（【锚 内】ON 原文 + 指回）──
    for sid, chain in tmp_chains.items():
        drive_tbl, drive_alias = chain["driving"]
        parts = []
        # 生产步的表级 WHERE → 主表行限定段（片段内逐字已有，行上可检索）
        prod_wheres = []
        for pid in chain["producer_ids"]:
            w = ctx.step_where(pid)
            if w:
                prod_wheres.append(_maybe_verbatim(w, raw_sqls))
                attached_wheres.append(w)
        if prod_wheres:
            parts.append("限定：" + "；".join(prod_wheres))
        # 定义片段：嵌套锚逐字（深层在前）
        for m in chain["mounts"]:
            if m["sql"]:
                parts.append(f"【逻辑从表 {m['anchor']} · 定义】{m['sql']}")
        # 终端步骤的行级 WHERE → 写入前限制（锚前缀即合法引用）
        tw = ctx.step_where(sid)
        if tw:
            parts.append("写入前限制：" + _maybe_verbatim(tw, raw_sqls))
            attached_wheres.append(tw)
        _, d_short = split_schema_table(drive_tbl)
        rows.append([None, None, d_short, drive_alias, None, "主表",
                     "；".join(parts)])
        last_pos_by_step[sid] = len(rows) - 1
        # 生产步内的物理 JOIN 表：每表一行
        for it in chain["inner"]:
            _, t_short = split_schema_table(it["table"])
            g = (f"【{it['anchor']} 内】{it['on']}；"
                 f"参与逻辑从表 {it['anchor']}，定义见 {drive_alias} 行"
                 if it["on"] else
                 f"参与逻辑从表 {it['anchor']}，定义见 {drive_alias} 行")
            rows.append([None, d_short, drive_alias, t_short, it["alias"],
                         "INNER JOIN", g])
        # 终端步骤自有的直接物理 JOIN（tmp 之外的）照常成行
        for j in (ctx.df_step(sid).get("joins") or [])[1:]:
            jst = j.get("source_table", "")
            if (jst and not jst.startswith("(subquery:")
                    and not _is_intermediate_table(jst)):
                _, r_short = split_schema_table(jst)
                rows.append([None, d_short, drive_alias, r_short,
                             j.get("alias", ""), _join_kind(j.get("join_type", "")),
                             j.get("join_condition", "")])
                last_pos_by_step[sid] = len(rows) - 1

    # 未被附着的步骤 WHERE（logic_join_rows 对无 JOIN 步骤不产出承载行）
    unattached = []
    for s in ctx.steps_list:
        w = ctx.step_where(s.get("step_id", ""))
        if w and w not in attached_wheres:
            unattached.append(_maybe_verbatim(w, raw_sqls))

    chain_group = _chain_group_segments(ctx, exclude=chain_step_ids)

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
