"""视图模型层 — mapping / sdd / tech_design 三视图的公共数据构建层。

职责边界：
    knowledge_draft.json ──build_context()──> ViewContext ──> 渲染层(generate_*)
    渲染层只从 ctx 取数，不允许直接访问 lineage/ctes/joins/union_branches 等原始结构。

为什么存在（重构背景）：
    此前三视图各自在 generate_* 函数体内复制了一套取数/穿透逻辑，能力分叉：
    - 字段血缘穿透有 4 套实现（HTML 的 _resolve_sources/_build_penetration_chain、
      _resolve_physical_sources、generate_mapping 内联），嵌套 CTE 只有 HTML 那套会递归；
    - 实体级源表清单三个口径：mapping（UNION 分支/CTE 内部不过滤中间表）、
      sdd（全部过滤）、tech_design（不遍历 UNION 分支 → 血缘图丢源表）；
    - 目标表推导、字段去重、视图步骤过滤，每个视图手写一份。
    本文件是收敛后的唯一定义处。asset_report.html 走 build_report_data（HTML 专用
    视图模型，行为已稳定），暂不并入。

字段来源的取数优先级（全视图统一）：
    1. engine 注入的 field.physical_source（跨步骤追溯，含 transform）
    2. 回退：resolve_physical_sources() 单步 CTE 递归穿透（本文件实现）
"""

from __future__ import annotations

from engine import _is_intermediate_table

# 写入方式标签（与 view_generator 的 _DELETE_MODE_LABEL 同源，见 delete-mode-vs-rule-type 口径）
_DELETE_MODE_LABEL = {
    "0": "追加写入", "1": "覆盖写入", "2": "清空后写入",
    "3": "按条件删除后写入", "4": "增量写入",
}

# 变换类型优先级（同名字段去重时保留「更重」的加工版本）— 唯一定义
TRANSFORM_PRIORITY = {
    "unknown": -1, "direct": 0, "value": 1, "fallback": 2,
    "case_when": 3, "expression": 4, "aggregate": 5, "pivot": 6, "window": 7,
}


def resolve_on_condition_aliases(on_condition: str, alias_map: dict, join_key_lineage: dict):
    """把 ON 条件里的中间表别名替换成物理源表，并标注传递路径。

    例: "t.bid = d.bid"，t 是中间表(tmp1)，bid 追溯到 tbl_b
        → "tbl_b.bid = d.bid（经tmp1传递）"

    Args:
        on_condition: ON 条件字符串（如 "t.bid = d.bid"）
        alias_map: {别名(UPPER): 物理表名}，本步骤的 FROM/JOIN 别名映射
        join_key_lineage: {field_lower: [追溯链]}，关联键追溯数据
    Returns: (改写后的ON条件, 传递路径标注)
    """
    import re as _re
    if not on_condition:
        return on_condition, ""

    rewritten = on_condition
    transfers = []  # 传递路径标注

    # 提取所有 alias.field 模式
    for m in _re.finditer(r'(\w+)\.(\w+)', on_condition):
        alias = m.group(1)
        field = m.group(2)
        table = alias_map.get(alias.upper(), "")
        if not table or not _is_intermediate_table(table):
            continue
        # 这个别名是中间表，追溯它的 field 到物理源表
        trace_chains = join_key_lineage.get(field.lower(), [])
        if not trace_chains:
            continue
        chain = trace_chains[0]
        # 找物理源表（叶节点）
        leaves = []

        def _find_leaves(n):
            if not n.get("children"):
                leaves.append(n)
            for c in n.get("children", []):
                _find_leaves(c)

        _find_leaves(chain)
        if not leaves:
            continue
        # 取第一个叶节点的物理表短名
        phys_table = leaves[0].get("table", "").split(".")[-1]
        phys_field = leaves[0].get("field", field)
        # 替换别名：t.bid → tbl_b.bid
        old_ref = f"{alias}.{field}"
        new_ref = f"{phys_table}.{phys_field}"
        rewritten = rewritten.replace(old_ref, new_ref)
        transfers.append(f"{old_ref}经{table.split('.')[-1]}来自{new_ref}")

    transfer_note = "（" + "；".join(transfers) + "）" if transfers else ""
    return rewritten, transfer_note


# ════════════════════════════════════════════════════════════
# 基础工具（唯一定义，渲染层从这里 import）
# ════════════════════════════════════════════════════════════

def _clean(s):
    """清洗字符串（与 view_generator._clean 同口径）。"""
    if s is None:
        return ""
    return str(s).strip()


def norm_name(name: str) -> str:
    """表名归一化（统一小写）。视图层表名比较的唯一口径。

    注意：engine._norm_table 是大写口径，两者不可混用；本函数保持
    收编前 view_generator._norm 的行为（lower）。
    """
    if not name:
        return ""
    return name.strip().lower()


def schema_table(schema: str, table: str) -> str:
    """schema + table 拼全名。"""
    s = _clean(schema)
    t = _clean(table)
    if s and t:
        return f"{s}.{t}"
    return t or s


def split_schema_table(full: str) -> tuple:
    """全名 → (schema, table)。无 schema 时 schema 为空串。"""
    if "." in full:
        parts = full.split(".", 1)
        return parts[0].strip(), parts[1].strip()
    return "", full.strip()


def join_type_label(jt: str, join_cond: str = "") -> str:
    """join_type → 实体级 mapping 的关联描述。"""
    labels = {
        "FROM": "主表",
        "FROM_SUBQUERY_MAIN": "子查询内部主表",
        "FROM_SUBQUERY": "子查询内部从表",
        "JOIN_SUBQUERY_INNER": "JOIN子查询内部从表",
        "JOIN_SUBQUERY": "JOIN子查询",
    }
    if jt in labels:
        return labels[jt]
    return f"{jt} ON {join_cond}" if join_cond else jt


def build_cte_index(data_flow_steps: list) -> dict:
    """CTE 索引：{CTE名(UPPER): {alias_to_table, fields_map, source_tables}}。

    供穿透回退与「CTE 名过滤」使用。曾经 generate_mapping 内部手写过一份，
    现在唯一定义在此。
    """
    cte_index = {}
    for s in data_flow_steps:
        for cte in s.get("ctes", []):
            cte_key = cte.get("name", "").upper()
            alias_to_table = {}
            for st in cte.get("source_tables", []):
                talias = st.get("alias", "").upper()
                tname = st.get("name", "")
                if talias:
                    alias_to_table[talias] = tname
            fields_map = {}
            for cf in cte.get("fields", []):
                if isinstance(cf, dict) and cf.get("name"):
                    fields_map[cf["name"].upper()] = cf
            cte_index[cte_key] = {
                "alias_to_table": alias_to_table,
                "fields_map": fields_map,
                "source_tables": cte.get("source_tables", []),
            }
    return cte_index


# ════════════════════════════════════════════════════════════
# 字段血缘穿透（回退路径 — 唯一实现）
# ════════════════════════════════════════════════════════════

def resolve_physical_sources(lineages: list, cte_index: dict, cte_names_upper: set,
                             visited: set, depth: int = 0) -> list:
    """穿透 CTE 到物理源表字段（递归）。

    lineages: field.lineage 列表
    返回: [{schema, table, field, alias, raw_sql}, ...]

    与旧版 _resolve_physical_sources 的差异（修复项）：
    - 嵌套 CTE（CTE1→CTE2→物理表）递归穿透到底；旧版只手写展开一层，
      更深嵌套会静默丢来源。
    """
    if depth > 10 or not lineages:
        return []

    results = []
    for l in lineages:
        src_table = l.get("source_table", "")
        src_field = l.get("source_field", "")
        cte_name = l.get("cte_name", "")
        raw_sql = l.get("raw_sql", "")

        if cte_name and cte_name.upper() in cte_index:
            if cte_name.upper() in visited:
                continue
            visited.add(cte_name.upper())
            cte_info = cte_index[cte_name.upper()]
            cte_field_info = cte_info["fields_map"].get(src_field.upper(), {})
            cte_source_fields = cte_field_info.get("source_fields", l.get("cte_source_fields", []))
            cte_alias_to_table = cte_info["alias_to_table"]

            for csf in cte_source_fields:
                csf_alias = csf.get("alias", "").upper()
                csf_field = csf.get("field", "")
                physical_table = cte_alias_to_table.get(csf_alias, "")

                if physical_table and physical_table.upper() in cte_names_upper:
                    # 别名映射到的"表"本身是嵌套 CTE（如 cte_outer 引用 cte_inner，
                    # alias_to_table['C'] = 'cte_inner'）→ 递归穿透到真物理表
                    nested = resolve_physical_sources(
                        [{"cte_name": physical_table, "source_field": csf_field}],
                        cte_index, cte_names_upper, visited, depth + 1)
                    for n in nested:
                        if not n.get("raw_sql"):
                            n["raw_sql"] = raw_sql
                    results.extend(nested)
                elif physical_table:
                    sch, tbl = split_schema_table(physical_table)
                    results.append({
                        "schema": sch, "table": tbl, "field": csf_field,
                        "alias": csf.get("alias", ""), "raw_sql": raw_sql,
                    })
                elif csf_alias in cte_names_upper:
                    nested = resolve_physical_sources(
                        [{"source_table": csf_alias, "source_field": csf_field,
                          "cte_name": csf_alias}],
                        cte_index, cte_names_upper, visited, depth + 1)
                    for n in nested:
                        if not n.get("raw_sql"):
                            n["raw_sql"] = raw_sql
                    results.extend(nested)

            visited.discard(cte_name.upper())
        elif src_table and src_table.upper() not in cte_names_upper:
            sch, tbl = split_schema_table(src_table)
            results.append({
                "schema": sch, "table": tbl, "field": src_field,
                "alias": l.get("alias", ""), "raw_sql": raw_sql,
            })

    return results


# ════════════════════════════════════════════════════════════
# 视图上下文
# ════════════════════════════════════════════════════════════

class EntitySource:
    """实体级物理源表行（统一口径的产出物）。

    origin: "main"（主查询 JOIN）/ "union:N"（UNION 分支 N）/ "cte:CNAME"（CTE 内部）
    """
    __slots__ = ("schema", "table", "alias", "origin", "join_type",
                 "join_condition", "rewritten_condition", "transfer_note", "step_id")

    def __init__(self, schema, table, alias, origin, join_type="", join_condition="",
                 rewritten_condition="", transfer_note="", step_id=""):
        self.schema = schema
        self.table = table
        self.alias = alias
        self.origin = origin
        self.join_type = join_type
        self.join_condition = join_condition
        self.rewritten_condition = rewritten_condition
        self.transfer_note = transfer_note
        self.step_id = step_id


class ViewContext:
    """三视图（mapping/sdd/tech_design）共享的视图上下文。

    用 build_context(knowledge) 构建，不要直接实例化。
    """

    def __init__(self, knowledge: dict):
        self.knowledge = knowledge
        self.meta = knowledge.get("meta", {})
        self.topo = knowledge.get("topology", {})
        self.df = knowledge.get("data_flow", {})
        self.fm = knowledge.get("field_mappings", {})
        self.bl = knowledge.get("business_logic", {})
        self.quality = knowledge.get("quality", {})
        self.source = knowledge.get("source", {})

        self.steps_list = self.topo.get("steps", [])
        self.data_flow_steps = self.df.get("steps", [])
        self.fields_list = self.fm.get("fields", [])

        # CTE 索引（唯一定义）
        self.cte_index = build_cte_index(self.data_flow_steps)
        self.cte_names_upper = set(self.cte_index.keys())

        # ── 最终目标表（exec_sequence 最大步骤；唯一定义）──
        self.final_step = None
        if self.steps_list:
            _max_seq = max(s.get("exec_sequence", 0) for s in self.steps_list)
            self.final_step = next(
                (s for s in self.steps_list if s.get("exec_sequence", 0) == _max_seq),
                self.steps_list[0])
        self.target_schema = self.final_step.get("target_schema", "") if self.final_step else ""
        self.target_table = self.final_step.get("target_table", "") if self.final_step else ""
        self.target_table_full = schema_table(self.target_schema, self.target_table)
        # 资产中文名统一口径（与 build_report_data 一致）：
        # DDL 表级注释（COMMENT ON TABLE）优先，兜底业务描述首句。
        # 历史三口径：HTML=注释||描述首句、mapping=描述首句||表名、sdd=仅注释，收编于此。
        self.target_cn = (self.meta.get("table_comment", "")
                          or (self.bl.get("summary", "").split("，")[0]
                              if self.bl.get("summary") else ""))

        # CTE 定义索引：{CTE名(lower): cte dict}（含 raw_sql / where_clause /
        # source_tables[].join_condition — 还原预处理逻辑用，与穿透用 cte_index 互补）
        self.cte_defs = {}
        for s in self.data_flow_steps:
            for cte in s.get("ctes", []):
                if cte.get("name"):
                    self.cte_defs[cte["name"].lower()] = cte

        # 目标表集合（过滤实体级源表用）
        self.target_tables_set = set()
        for s in self.steps_list:
            self.target_tables_set.add(
                norm_name(schema_table(s.get("target_schema", ""), s.get("target_table", ""))))

        # step_id → 拓扑步骤 / data_flow 步骤 的索引（O(1) 查找）
        self._step_by_id = {s.get("step_id", ""): s for s in self.steps_list}
        self._df_by_id = {s.get("step_id", ""): s for s in self.data_flow_steps}

        # 实体级源表清单（懒构建）
        self._entity_sources = None

    # ── 步骤查询 ──────────────────────────────────────────

    def topo_step(self, step_id: str) -> dict:
        return self._step_by_id.get(step_id, {})

    def df_step(self, step_id: str) -> dict:
        return self._df_by_id.get(step_id, {})

    def is_view_step(self, step_id: str) -> bool:
        """CREATE VIEW 步骤（属性级字段展示时跳过）。"""
        raw_sql = (self.df_step(step_id).get("raw_sql", "") or "").upper()
        return "CREATE VIEW" in raw_sql or "CREATE OR REPLACE VIEW" in raw_sql

    def step_alias_map(self, step_id: str) -> dict:
        """步骤内 {别名(UPPER): 物理表名}。"""
        amap = {}
        for j in self.df_step(step_id).get("joins", []):
            if j.get("alias") and j.get("source_table"):
                amap[j["alias"].upper()] = j["source_table"]
        return amap

    def step_where(self, step_id: str) -> str:
        """步骤 WHERE 子句（去掉 WHERE 前缀）。"""
        return (self.df_step(step_id).get("where_clause", "") or "").replace("WHERE ", "")

    # ── 字段来源解析（统一优先级）─────────────────────────

    def resolve_field_sources(self, field: dict) -> list:
        """字段物理来源：physical_source 优先，回退递归 CTE 穿透。

        引擎层（build_join_key_lineage）已修复嵌套 CTE 穿透缺口；
        下方对「physical_source 表名命中 CTE 名」的二次穿透保留为防御层
        （旧版 knowledge JSON 回放/引擎回退时仍能拿到正确结果）。

        返回: [{schema?, table, field, alias, transform?, raw_sql}, ...]
        """
        ps = field.get("physical_source") or []
        if ps:
            out = []
            for p in ps:
                t = p.get("table", "")
                if t.upper() in self.cte_names_upper:
                    nested = resolve_physical_sources(
                        [{"cte_name": t, "source_field": p.get("field", "")}],
                        self.cte_index, self.cte_names_upper, set())
                    if nested:
                        for n in nested:
                            # 保留引擎追溯到的加工信息（transform/raw_sql）
                            if p.get("transform") and p.get("transform") != "direct":
                                n["transform"] = p["transform"]
                            if not n.get("raw_sql"):
                                n["raw_sql"] = p.get("raw_sql", "")
                        out.extend(nested)
                        continue
                out.append(p)
            return out
        return resolve_physical_sources(
            field.get("lineage", []), self.cte_index, self.cte_names_upper, set())

    # ── 字段筛选（终态字段清单）───────────────────────────

    def final_fields(self, group_by_scenario: bool = False):
        """最终目标表字段（过滤视图步骤/中间表步骤，同名字段保留加工版本）。

        group_by_scenario=True: 返回 {scenario: [fields]}（mapping 口径）
        group_by_scenario=False: 返回 [fields]（sdd 口径）
        tech_design 按步骤全量展示字段是有意设计，不走本方法。
        """
        step_scenario = {s.get("step_id", ""): s.get("scenario_name", "")
                         for s in self.steps_list}
        groups: dict = {}
        seen: dict = {}
        for f in self.fields_list:
            fname = f.get("target_field", "")
            step_id = f.get("producing_step", "")
            if not fname or self.is_view_step(step_id):
                continue
            step_target = (self.topo_step(step_id).get("target_table_full", "")
                           or self.topo_step(step_id).get("target_table", ""))
            if _is_intermediate_table(step_target):
                continue
            if group_by_scenario:
                key = step_scenario.get(step_id, "") or "默认场景"
            else:
                key = "__all__"
            bucket = groups.setdefault(key, [])
            bucket_seen = seen.setdefault(key, set())
            if fname in bucket_seen:
                for i, existing in enumerate(bucket):
                    if existing.get("target_field") == fname:
                        existing_tt = existing.get("transform_type", "expression")
                        new_tt = f.get("transform_type", "expression")
                        if TRANSFORM_PRIORITY.get(new_tt, 0) > TRANSFORM_PRIORITY.get(existing_tt, 0):
                            bucket[i] = f
                        break
            else:
                bucket_seen.add(fname)
                bucket.append(f)
        if group_by_scenario:
            return groups
        return groups.get("__all__", [])

    # ── 实体级物理源表清单（统一口径）─────────────────────

    def entity_source_tables(self) -> list:
        """全步骤物理源表清单（EntitySource 列表，按出现顺序全局去重）。

        统一口径（收编三个视图的分叉行为）：
        - 遍历：每步骤 UNION 分支（有 UNION 时替代主查询 joins）/ 主查询 joins / CTE 内部；
        - 过滤：子查询假名、中间表（含 UNION 分支与 CTE 内部 — 修复 mapping 的遗漏）、
         目标表自身、CTE 名；
        - 主查询 JOIN 的 ON 条件做中间表别名改写（join_key_lineage），
          UNION 分支/CTE 内部不做改写（与旧 mapping 行为一致）。
        """
        if self._entity_sources is not None:
            return self._entity_sources

        # ON 条件改写器（主查询路径专用；定义于本模块 resolve_on_condition_aliases）
        _resolve_on_cond = resolve_on_condition_aliases

        seen_global = set()
        seen_branches = set()
        out = []

        for s in self.steps_list:
            sid = s.get("step_id", "")
            df_step = self._df_by_id.get(sid, {})
            joins = df_step.get("joins", [])
            ctes = df_step.get("ctes", [])
            ub_list = df_step.get("union_branches", [])
            has_union = len(ub_list) >= 1

            if has_union:
                for b in ub_list:
                    bidx = b.get("branch_index", 0)
                    for j in b.get("source_tables", []):
                        src_full = j.get("source_table", "")
                        if not self._is_entity_eligible(src_full, seen_global):
                            continue
                        dedup_key = (bidx, norm_name(src_full))
                        if dedup_key in seen_branches:
                            continue
                        seen_branches.add(dedup_key)
                        seen_global.add(norm_name(src_full))
                        sch, tbl = split_schema_table(src_full)
                        out.append(EntitySource(
                            schema=sch, table=tbl, alias=j.get("alias", ""),
                            origin=f"union:{bidx}", join_type=j.get("join_type", ""),
                            join_condition=j.get("join_condition", ""), step_id=sid))
            else:
                step_alias_map = self.step_alias_map(sid)
                step_jkl = df_step.get("join_key_lineage", {})
                for j in joins:
                    src_full = j.get("source_table", "")
                    if not self._is_entity_eligible(src_full, seen_global):
                        continue
                    seen_global.add(norm_name(src_full))
                    sch, tbl = split_schema_table(src_full)
                    rewritten, note = _resolve_on_cond(
                        j.get("join_condition", ""), step_alias_map, step_jkl)
                    out.append(EntitySource(
                        schema=sch, table=tbl, alias=j.get("alias", ""),
                        origin="main", join_type=j.get("join_type", ""),
                        join_condition=j.get("join_condition", ""),
                        rewritten_condition=rewritten, transfer_note=note, step_id=sid))

            for cte in ctes:
                for st in cte.get("source_tables", []):
                    tname = st.get("name", "")
                    if not self._is_entity_eligible(tname, seen_global):
                        continue
                    seen_global.add(norm_name(tname))
                    sch, tbl = split_schema_table(tname)
                    out.append(EntitySource(
                        schema=sch, table=tbl, alias=st.get("alias", ""),
                        origin=f"cte:{cte.get('name', '')}",
                        join_type=st.get("join_type", "FROM"),
                        join_condition=st.get("join_condition", ""), step_id=sid))

        self._entity_sources = out
        return out

    def _is_entity_eligible(self, src_full: str, seen_global: set) -> bool:
        """实体级源表准入：真物理表 + 非中间表 + 非目标表 + 非重复。"""
        if not src_full or src_full.startswith("(subquery:"):
            return False
        if _is_intermediate_table(src_full):
            return False
        if norm_name(src_full) in self.target_tables_set:
            return False
        if src_full.upper() in self.cte_names_upper:
            return False
        if norm_name(src_full) in seen_global:
            return False
        return True

    # ── 逻辑关联模型（SDD「3表关联」/ mapping 条件列共用）─────────

    def cte_chain_sqls(self, cte_name: str) -> list:
        """某 CTE 的查询体依赖链（基础在前）：[{name, raw_sql}, ...]。

        例 order_agg 基于 base_orders →
        [{base_orders, SELECT ... WHERE del_flag='N'},
         {order_agg, SELECT ... GROUP BY cust_id}]
        """
        ordered = []

        def visit(name, seen):
            key = (name or "").lower()
            if not key or key in seen:
                return
            seen.add(key)
            cte = self.cte_defs.get(key)
            if not cte:
                return
            for st in cte.get("source_tables", []):
                n = st.get("name", "")
                if n.lower() in self.cte_defs:
                    visit(n, seen)
            ordered.append({"name": cte.get("name", name),
                            "raw_sql": cte.get("raw_sql", "")})

        visit(cte_name, set())
        return ordered

    def _cte_primary_physical(self, cte_name: str) -> tuple:
        """CTE 的主物理源表（主源仍是 CTE 时递归下探）。返回 (表名, 别名)。"""
        key = (cte_name or "").lower()
        seen = set()
        while key in self.cte_defs and key not in seen:
            seen.add(key)
            cte = self.cte_defs[key]
            main_src = None
            # 优先 FROM 的源，其次任意物理源（排除 CTE 引用）
            for st in cte.get("source_tables", []):
                if (st.get("join_type") or "FROM") == "FROM":
                    main_src = st
                    if main_src.get("name", "").lower() not in self.cte_defs:
                        break
            if main_src is None:
                for st in cte.get("source_tables", []):
                    if st.get("name", "").lower() not in self.cte_defs:
                        main_src = st
                        break
            if main_src is None:
                return "", ""
            name = main_src.get("name", "")
            if name.lower() in self.cte_defs:
                key = name.lower()
                continue
            return name, main_src.get("alias", "")
        return "", ""

    def logic_join_rows(self) -> list:
        """物理表关联清单（SDD「3表关联」主口径）。

        设计立场（还原为可解释回代码的基线）：
        - 左右表永远写真实物理表；CTE 坍缩为「关联时的预处理」属性；
        - 有多少个关联就有多少行（同一物理表被多种口径预处理后多次关联 → 多行）;
        - 预处理说明用 CTE 查询体原文链（准确性优先，WHERE/GROUP BY/子查询天然全含）。

        行顺序：主查询物理 JOIN → 主查询 JOIN CTE（右表解析为 CTE 主物理源）
        → CTE 内部物理 JOIN。
        """
        rows = []
        where_attached = False
        for s in self.steps_list:
            sid = s.get("step_id", "")
            df_step = self._df_by_id.get(sid, {})
            joins = df_step.get("joins", [])
            where_clause = self.step_where(sid)
            main_table, main_alias = "", ""
            for j in joins:
                st = j.get("source_table", "")
                if st and not st.startswith("(subquery:"):
                    main_table, main_alias = st, j.get("alias", "")
                    break
            if not main_table:
                # 主表是 CTE/子查询 → 从 cte_joins 的 FROM 项下探物理主表
                for cj in df_step.get("cte_joins", []):
                    if cj.get("join_type") == "FROM":
                        t, a = self._cte_primary_physical(cj.get("name", ""))
                        if t:
                            main_table, main_alias = t, a or cj.get("alias", "")
                        break
            if not main_table:
                continue

            # 1. 主查询物理 JOIN（含子查询展开的物理表；跳过主 FROM 行，FROM 不是关联）
            for j in joins[1:]:
                rt = j.get("source_table", "")
                if not rt or rt.startswith("(subquery:"):
                    continue
                cond = j.get("join_condition", "")
                if where_clause and not where_attached:
                    cond = (cond + "；" if cond else "") + f"WHERE {where_clause}"
                    where_attached = True
                rows.append(LogicJoinRow(
                    left_table=main_table, left_alias=main_alias,
                    right_table=rt, right_alias=j.get("alias", ""),
                    join_type=j.get("join_type", ""),
                    on_condition=cond, preprocess="", origin="main", step_id=sid))

            # 2. 主查询 JOIN CTE：右表解析为 CTE 主物理源 + 预处理原文链
            for cj in df_step.get("cte_joins", []):
                if cj.get("join_type") == "FROM":
                    continue  # 作为主表已在上面处理
                rt, ra = self._cte_primary_physical(cj.get("name", ""))
                if not rt:
                    continue
                chain = self.cte_chain_sqls(cj.get("name", ""))
                pre = ""
                if chain:
                    pre = "预处理（依次执行）:\n" + "\n".join(
                        f"[CTE {c['name']}] {c['raw_sql']}" for c in chain if c["raw_sql"])
                cond = cj.get("join_condition", "")
                if where_clause and not where_attached:
                    cond = (cond + "；" if cond else "") + f"WHERE {where_clause}"
                    where_attached = True
                rows.append(LogicJoinRow(
                    left_table=main_table, left_alias=main_alias,
                    # 右别名用主查询里的 CTE 别名（ci/oa/bg/ro），与 ON 里的引用一致；
                    # 物理源在 CTE 内的别名（o/c）放进预处理原文里已可追溯
                    right_table=rt, right_alias=cj.get("alias", "") or ra,
                    join_type=cj.get("join_type", ""),
                    on_condition=cond, preprocess=pre,
                    origin=f"main-cte:{cj.get('name', '')}", step_id=sid))

            # 3. CTE 内部物理 JOIN（如 cust_info 内 cust JOIN cust_id）
            for cte in df_step.get("ctes", []):
                cte_main, cte_main_alias = self._cte_primary_physical(cte.get("name", ""))
                for st in cte.get("source_tables", []):
                    n = st.get("name", "")
                    jt = st.get("join_type", "FROM")
                    if (not n or n.lower() in self.cte_defs
                            or (jt or "FROM") == "FROM"
                            or n.startswith("(subquery:")):
                        continue
                    if not cte_main or cte_main == n:
                        continue
                    rows.append(LogicJoinRow(
                        left_table=cte_main, left_alias=cte_main_alias,
                        right_table=n, right_alias=st.get("alias", ""),
                        join_type=jt,
                        on_condition=st.get("join_condition", ""),
                        preprocess=f"构成 CTE {cte.get('name', '')}（该 CTE 再与主查询关联，见上）",
                        origin=f"cte-inner:{cte.get('name', '')}", step_id=sid))
        return rows


class LogicJoinRow:
    """物理表关联行（SDD「3表关联」与 mapping 条件列的统一行模型）。

    左右表永远写真实物理表；CTE 坍缩为 preprocess 属性（查询体原文链）。
    origin: "main"（主查询物理 JOIN）/ "main-cte:NAME"（主查询 JOIN 该 CTE，
    右表已解析为其主物理源）/ "cte-inner:NAME"（CTE 内部物理 JOIN）。
    """
    __slots__ = ("left_table", "left_alias", "right_table", "right_alias",
                 "join_type", "on_condition", "preprocess", "origin", "step_id")

    def __init__(self, left_table, left_alias, right_table, right_alias,
                 join_type, on_condition, preprocess, origin, step_id=""):
        self.left_table = left_table
        self.left_alias = left_alias
        self.right_table = right_table
        self.right_alias = right_alias
        self.join_type = join_type
        self.on_condition = on_condition
        self.preprocess = preprocess
        self.origin = origin
        self.step_id = step_id


def build_context(knowledge: dict) -> ViewContext:
    """knowledge_draft.json → ViewContext（三视图共用入口）。"""
    return ViewContext(knowledge)
