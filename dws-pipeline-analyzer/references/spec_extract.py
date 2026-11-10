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



def _subquery_index(ctx) -> dict:
    """join_paths 子查询索引（引擎结构，波2b 起纯检测用）。

    {step_id: {外层别名: {main_table, main_alias, tables, table_list, on,
    is_from_wrap}}}——片段文本/关联方式/内部表 ON 全部由 data_blocks +
    engine_ctx.subquery_sqls 装配（零文本解析）。
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
            for t in info.get("subquery_tables") or []:
                if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0]:
                    tables.add(t[0].lower())
                    if t[0] not in table_list:
                        table_list.append(t[0])
                    if not main_tbl and (len(t) < 3 or t[2] == "main"):
                        main_tbl, main_alias = t[0], t[1]
            on = ""
            join_type = ""
            for j in s.get("joins", []):
                if j.get("alias") == alias:
                    on = j.get("join_condition", "")
                    join_type = j.get("join_type", "")
                    break
            out.setdefault(sid, {})[alias] = {
                "main_table": main_tbl, "main_alias": main_alias,
                "tables": tables, "table_list": table_list, "on": on,
                "is_from_wrap": (join_type or "").upper().startswith("FROM"),
            }
    return out



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


def _iter_blocks(blocks):
    """递归迭代全部块。"""
    for b in blocks or []:
        yield b
        yield from _iter_blocks(b.get("children") or [])


def _descend_main(block) -> tuple:
    """子查询块的驱动表 (表全名, 别名)：子孙里第一个 inner_main。"""
    for c in _iter_blocks((block or {}).get("children") or []):
        if c.get("type") == "inner_main" and c.get("table"):
            return c["table"], c.get("alias", "")
    return "", ""


def _slave_rows(anchor_block, left_tbl: str, left_alias: str,
                frag: str, anchor: str) -> list:
    """逻辑从表块 → 锚行 + 内部表每表一行（嵌套层全部收录，指回最外层锚，
    docs/14 §四整块不拆层：嵌套子查询定义留在渲染 SQL 内）。"""
    rows = []
    drv_tbl, drv_alias = _descend_main(anchor_block)
    if not drv_tbl:
        return rows
    _, l_short = split_schema_table(left_tbl)
    _, d_short = split_schema_table(drv_tbl)
    parts = ([anchor_block.get("on_condition", "")]
             if anchor_block.get("on_condition") else [])
    if frag:
        parts.append(f"【逻辑从表 {anchor} · 定义】{frag}")
    rows.append([None, l_short, left_alias, d_short, anchor,
                 _join_kind(anchor_block.get("join_type", "")),
                 "；".join(parts)])
    for blk in _iter_blocks(anchor_block.get("children") or []):
        if blk.get("type") == "inner_secondary" and blk.get("table"):
            _, b_short = split_schema_table(blk["table"])
            g = ((f"【{anchor} 内】{blk.get('on_condition', '')}；")
                 if blk.get("on_condition") else "") +                 f"参与逻辑从表 {anchor}，定义见 {d_short} 行"
            rows.append([None, d_short, drv_alias, b_short,
                         blk.get("alias", ""),
                         _join_kind(blk.get("join_type", "")), g])
    return rows


# ════════════════════════════════════════════════════════════
# tmp 链物化无关模型（docs/14 P1 内联等价，C2/C3 拍板 2026-11-02）
# ════════════════════════════════════════════════════════════

def _short_lower(t: str) -> str:
    return split_schema_table(t or "")[1].lower()


def _chain_codes(ctx, kind: str) -> set:
    """双链维度里某条链的规则编码集（无双链 → 空集）。"""
    if not ctx.chains.get("has_dual_chain"):
        return set()
    return {c for ch in ctx.chains.get("chains", []) if ch.get("kind") == kind
            for c in ch.get("rule_codes", [])}


def _producer_of(ctx, table_full: str):
    """产出某表的步骤（target_table 匹配；短名比较——生产步登记短名、
    消费侧引用可带 schema，两形态都真实存在）。

    双链资产（2026-11-10）：同表有全量/增量两个生产步时优先**全量链**——
    挂载基底取完整设计（全字段无增量过滤），增量链差异走「增量链限制」段。
    """
    full_codes = _chain_codes(ctx, "full")
    hits = [s for s in ctx.steps_list
            if s.get("target_table", "")
            and _short_lower(s["target_table"]) == _short_lower(table_full)]
    if not hits:
        return None
    for s in hits:
        if s.get("rule_code") in full_codes:
            return s
    return hits[0]


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
    _incr_codes = _chain_codes(ctx, "incremental")
    for s in ctx.steps_list:
        sid = s.get("step_id", "")
        # 终端 = 写终态表的步骤（自身 target 是中间表的属于链内生产步，
        # 不作为挂载终端——否则整链会被中间消费者重复呈现）
        if _is_intermediate_table(s.get("target_table", "")):
            continue
        # 双链重复体（2026-11-10）：增量链终端与全量链共享全部结构，不重复
        # 挂载——其差异（增量谓词/字段差）由 chains 结构承载、经
        # 「增量链限制」段落位（共底座 + 差分标注）
        if _incr_codes and s.get("rule_code") in _incr_codes:
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
                                  "join_type": j.get("join_type", ""),
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


def build_sheet3_rows(ctx, sq_sqls: dict = None) -> list:
    """ViewContext → Sheet3 行（7 列模型）。

    段式 G 列（docs/17 §三）：{ON}；限制：…；写入前限制：…；预处理：贴码；
    主表链预处理：分组/保留。段按需出现，"；" 分隔。
    E1：无任何关联行时，每物理源表一行（右表=—（无关联）），行级限制落首行。
    """
    sq_sqls = sq_sqls or {}
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
            limit_seg = raw_w
            cond = cond[:m.start()].strip()
            attached_wheres.append(raw_w)
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
                prod_wheres.append(w)
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
            parts.append("写入前限制：" + tw)
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
            # 关联方式 = 生产步真实类型（2026-11-10 修正：此前写死 INNER JOIN
            # ——实现泄漏成设计失真）；引擎拿不到时留空不编造
            rows.append([None, d_short, drive_alias, t_short, it["alias"],
                         _join_kind(it.get("join_type", "")), g])
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


    # ── 子查询关联行（嵌套子查询，docs/17 §三，波2b 块装配零文本解析）──
    for sid, by_alias in sq_index.items():
        blocks = ctx.df_step(sid).get("data_blocks") or []
        sqls = sq_sqls.get(sid, {})
        for alias, info in by_alias.items():
            sb = next((b for b in _iter_blocks(blocks)
                       if b.get("alias") == alias
                       and b.get("type") == "subquery"), None)
            if info["is_from_wrap"]:
                # FROM 包裹：子块行（直接 JOIN + 逻辑从表）+ 包裹残余行
                if sb is None:
                    continue
                main_blk = next((c for c in sb.get("children") or []
                                 if c.get("type") == "inner_main"
                                 and c.get("table")), None)
                if main_blk is None:
                    continue
                main_tbl = main_blk["table"]
                main_alias = main_blk.get("alias", "")
                _, m_short = split_schema_table(main_tbl)
                for child in sb.get("children") or []:
                    if child is main_blk:
                        continue
                    if child.get("type") == "inner_secondary" and child.get("table"):
                        _, c_short = split_schema_table(child["table"])
                        rows.append([None, m_short, main_alias, c_short,
                                     child.get("alias", ""),
                                     _join_kind(child.get("join_type", "")),
                                     child.get("on_condition", "")])
                    elif child.get("type") == "subquery":
                        rows.extend(_slave_rows(
                            child, main_tbl, main_alias,
                            sqls.get(child.get("alias", ""), ""),
                            child.get("alias", "")))
                # 残余行（D-b：虚拟逻辑表，方式空）——内层限制 + 外层限定/开窗收敛
                _wu = []
                for w in ctx.df_step(sid).get("where_usage") or []:
                    c = (w.get("condition") or "").strip()
                    if c:
                        c2 = c[7:] if c[:7].upper() == "WHERE S" or c[:6].upper() == "WHERE " else c
                        c2 = re.sub(r"^WHERE\s+", "", c, flags=re.I).strip()
                        if c2 and c2 not in _wu:
                            _wu.append(c2)
                sw = ctx.step_where(sid)
                inner_ws = [w for w in _wu if w != sw] if sw else _wu
                resid = ([f"限制：{'；'.join(inner_ws)}"] if inner_ws else [])
                if sw and sw not in attached_wheres:
                    note = ("（开窗收敛，窗口定义见原文）"
                            if re.search(r"\brn\s*=", sw) else "")
                    resid.append(f"限定：{sw}{note}")
                    attached_wheres.append(sw)
                resid.append(f"【{alias} 内】关联见上")
                rows.append([None, m_short, main_alias,
                             f"虚拟逻辑表 {alias}", alias, "", "；".join(resid)])
                last_pos_by_step[sid] = len(rows) - 1
                continue
            # JOIN 子查询（a JOIN (…) b）：主表 = 步骤 main 块
            main_blk = next((b for b in blocks
                             if b.get("type") == "main" and b.get("table")), None)
            if main_blk is None or sb is None:
                continue
            first_idx = len(rows)
            rows.extend(_slave_rows(sb, main_blk["table"],
                                    main_blk.get("alias", ""),
                                    sqls.get(alias, ""), alias))
            sw = ctx.step_where(sid)
            if sw and sw not in attached_wheres and rows[first_idx:]:
                rows[first_idx][6] = "；".join(
                    x for x in [rows[first_idx][6], f"限制：{sw}"] if x)
                attached_wheres.append(sw)
            last_pos_by_step[sid] = len(rows) - 1

    # 未被附着的步骤 WHERE（logic_join_rows 对无 JOIN 步骤不产出承载行）。
    # 双链资产（2026-11-10）：增量链步骤的 WHERE 不走此通道——增量谓词是
    # 链路级设计（定义增量行集范围），只经「增量链限制」标签段落位，
    # 防同一谓词在写入前限制里无标签泄漏（污染共享行语义）
    _incr_codes = _chain_codes(ctx, "incremental")
    unattached = []
    for s in ctx.steps_list:
        if _incr_codes and s.get("rule_code") in _incr_codes:
            continue
        w = ctx.step_where(s.get("step_id", ""))
        if w and w not in attached_wheres:
            unattached.append(w)

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


def _squeeze(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


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
    # 双链资产（2026-11-10）：增量链步骤的 WHERE 拆原子校验——共享谓词落在
    # 全量链基底段（限定/写入前限制）、增量谓词落在「增量链限制」段，两处
    # 命中即守恒；整段文本比对会把合法的共底座+差分标注误判为缺失
    _incr_codes = _chain_codes(ctx, "incremental")

    def _atoms_conserved(where_text: str) -> bool:
        """WHERE 按顶层 AND 拆原子，逐原子在 Sheet3/Sheet4 段里可寻即守恒。"""
        for atom in re.split(r"(?i)\band\b", where_text):
            atom = atom.strip()
            if not atom or atom.replace(" ", "") in ("1=1", "1 = 1"):
                continue
            if _squeeze(atom).lower() not in g_blob and \
                    _squeeze(atom).lower() not in logic_blob:
                return False
        return True

    for s in ctx.steps_list:
        sid = s.get("step_id", "")
        label = s.get("rule_code", "") or sid
        w = ctx.step_where(sid)
        if w:
            w_v = w
            ok = _squeeze(w_v).lower() in g_blob
            if not ok and _incr_codes and s.get("rule_code") in _incr_codes:
                ok = _atoms_conserved(w_v)
            if not ok:
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
