"""viewmodel（视图模型层）回归测试。

锁定本次架构收敛的四个行为修复/统一项：
1. 嵌套 CTE 递归穿透（旧 _resolve_physical_sources 只展开一层，深层嵌套丢来源）
2. 实体级源表清单包含 UNION 分支（旧 tech_design 血缘图不遍历 union_branches → 丢源表）
3. 实体级源表清单过滤中间表（旧 mapping 的 UNION 分支/CTE 内部行不过滤 → tmp 混入）
4. final_fields 场景分组口径（mapping 按场景、sdd 全局）

运行:
    pytest tests/test_viewmodel.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import (  # noqa: E402
    parse_single_sql, build_topology, build_data_flow, build_field_mappings,
    enrich_join_key_lineage, enrich_field_physical_sources, RawRule,
)
from viewmodel import build_context, resolve_physical_sources, build_cte_index  # noqa: E402


def _make_rule(sql, rule_code="R1", target="final_f"):
    return RawRule(rule_code=rule_code, rule_name="test", rule_type=1, exec_sequence=1,
                   target_schema="dws", target_table=target, delete_mode="1", query_sql=sql)


def _build_knowledge(sql, target="final_f"):
    rule = _make_rule(sql, target=target)
    pm = {rule.rule_code: parse_single_sql(sql, "dws")}
    topo = build_topology([rule], pm)
    df = build_data_flow([rule], pm)
    fm = build_field_mappings([rule], pm, {})
    enrich_join_key_lineage(df, [rule], pm, topo, fm)
    enrich_field_physical_sources(fm, df, [rule], pm, topo)
    return {
        "meta": {"source_type": "t", "analysis_time": "", "dialect": "dws",
                 "total_rules": 1, "target_table": target, "table_comment": "",
                 "patterns": [], "target_field_types": {}, "target_field_comments": {}},
        "topology": topo, "data_flow": df, "field_mappings": fm, "quality": {},
        "business_logic": {"summary": "", "step_descriptions": [], "key_transforms": []},
        "source": {},
    }


NESTED_CTE_SQL = """
WITH cte_outer AS (
    SELECT c.pcode AS product_code, c.qty
    FROM cte_inner c
),
cte_inner AS (
    SELECT b.pcode, b.qty
    FROM ods.tbl_biz_order b
    WHERE b.del_flag = 'N'
)
SELECT o.product_code, o.qty
FROM cte_outer o
"""

UNION_SQL = """
SELECT a.order_id, a.amount FROM ods.tbl_a a
UNION ALL
SELECT b.order_id, b.amount FROM ods.tbl_b b
"""

UNION_WITH_TMP_SQL = """
SELECT a.order_id, a.amount FROM ods.tbl_a a
UNION ALL
SELECT t.order_id, t.amount FROM dws.tmp_stage t
"""

CTE5_LOGIC_SQL = """
WITH base_orders AS (
    SELECT o.order_id, o.cust_id, o.amount, o.order_date
    FROM ods.orders o
    WHERE o.del_flag = 'N'
),
cust_info AS (
    SELECT c.cust_id, c.cust_name, i.id_no
    FROM dim.cust c
    LEFT JOIN dim.cust_id i ON c.cust_id = i.cust_id AND i.id_type = '0'
),
order_agg AS (
    SELECT b.cust_id, SUM(b.amount) AS total_amount
    FROM base_orders b
    GROUP BY b.cust_id
),
recent_orders AS (
    SELECT r.order_id, r.cust_id, r.amount
    FROM (
        SELECT bo.order_id, bo.cust_id, bo.amount
        FROM base_orders bo
        WHERE bo.order_date >= '2026-01-01'
    ) r
    WHERE r.amount > 0
)
SELECT m.cust_id, ci.cust_name, oa.total_amount, ro.amount AS recent_amount
FROM dim.cust_main m
LEFT JOIN cust_info ci ON m.cust_id = ci.cust_id
LEFT JOIN order_agg oa ON m.cust_id = oa.cust_id
LEFT JOIN recent_orders ro ON m.cust_id = ro.cust_id
"""


class TestLogicJoinRows:
    """物理表关联清单（SDD 3表关联口径）：左右表永远物理表，CTE 坍缩为预处理。"""

    def test_rows_cover_main_cte_and_inner(self):
        ctx = build_context(_build_knowledge(CTE5_LOGIC_SQL))
        rows = ctx.logic_join_rows()
        origins = [r.origin for r in rows]
        # 3 个主查询 JOIN CTE + 1 个 CTE 内部 JOIN（cust↔cust_id）
        assert sum(1 for o in origins if o.startswith("main-cte:")) == 3
        assert sum(1 for o in origins if o.startswith("cte-inner:")) == 1
        # 左右表全是物理表（无 CTE 名）
        for r in rows:
            for t in (r.left_table, r.right_table):
                assert t.lower() not in ctx.cte_defs, f"表列出现 CTE 名: {t}"

    def test_on_and_preprocess_raw_sql(self):
        ctx = build_context(_build_knowledge(CTE5_LOGIC_SQL))
        rows = [r for r in ctx.logic_join_rows() if r.origin == "main-cte:order_agg"]
        assert len(rows) == 1
        r = rows[0]
        assert r.right_table == "ods.orders"      # CTE 主源解析到物理表
        assert r.on_condition == "m.cust_id = oa.cust_id"
        # 预处理链：基础 CTE（含 WHERE）在前，聚合 CTE（含 GROUP BY）在后
        assert "[CTE base_orders]" in r.preprocess and "[CTE order_agg]" in r.preprocess
        assert r.preprocess.index("[CTE base_orders]") < r.preprocess.index("[CTE order_agg]")
        assert "WHERE o.del_flag = 'N'" in r.preprocess
        assert "GROUP BY b.cust_id" in r.preprocess

    def test_subquery_cte_field_penetration(self):
        """CTE 内 FROM 子查询：字段穿透到物理表（曾断链在子查询别名 r）。"""
        k = _build_knowledge(CTE5_LOGIC_SQL)
        f = next(x for x in k["field_mappings"]["fields"]
                 if x["target_field"] == "recent_amount")
        ps = f.get("physical_source") or []
        assert any("orders" in p.get("table", "") for p in ps), \
            f"子查询穿透断链: {ps}"


class TestNestedCtePenetration:
    def test_recursive_penetration_reaches_physical(self):
        """嵌套 CTE（outer→inner→物理表）穿透必须到达 ods 物理表。"""
        k = _build_knowledge(NESTED_CTE_SQL)
        fields = k["field_mappings"]["fields"]
        f = next(x for x in fields if x["target_field"] == "product_code")
        ctx = build_context(k)
        sources = ctx.resolve_field_sources(f)
        tables = {s.get("table", "") for s in sources}
        assert any("tbl_biz_order" in t for t in tables), \
            f"嵌套 CTE 穿透未到达物理表，实际: {tables}"

    def test_raw_resolve_physical_sources_recursion(self):
        """回退穿透函数本身的递归能力（不经 physical_source）。"""
        k = _build_knowledge(NESTED_CTE_SQL)
        cte_index = build_cte_index(k["data_flow"]["steps"])
        cte_names = set(cte_index.keys())
        f = next(x for x in k["field_mappings"]["fields"]
                 if x["target_field"] == "product_code")
        results = resolve_physical_sources(f.get("lineage", []), cte_index, cte_names, set())
        tables = {r["table"] for r in results}
        assert any("tbl_biz_order" in t for t in tables), \
            f"递归穿透失败，实际: {tables}"


class TestEntitySourceTables:
    def test_union_branches_collected(self):
        """UNION 步骤两个分支的物理源表都要进实体级清单。"""
        k = _build_knowledge(UNION_SQL)
        ctx = build_context(k)
        tables = {f"{es.schema}.{es.table}" for es in ctx.entity_source_tables()}
        assert any("tbl_a" in t for t in tables), f"分支1 源表丢失: {tables}"
        assert any("tbl_b" in t for t in tables), f"分支2 源表丢失: {tables}"
        origins = [es.origin for es in ctx.entity_source_tables()]
        assert all(o.startswith("union:") for o in origins), f"UNION 步骤 origin 应为 union:N: {origins}"

    def test_union_branch_intermediate_filtered(self):
        """UNION 分支里的中间表（tmp_stage）不得进入实体级清单。"""
        k = _build_knowledge(UNION_WITH_TMP_SQL)
        ctx = build_context(k)
        tables = {es.table for es in ctx.entity_source_tables()}
        assert "tmp_stage" not in tables, f"中间表混入实体级清单: {tables}"
        assert "tbl_a" in tables

    def test_main_join_intermediate_filtered(self):
        """主查询 JOIN 的中间表同样过滤。"""
        sql = ("SELECT o.order_id, t.amt FROM ods.tbl_order o "
               "JOIN dws.tmp_calc t ON o.order_id = t.order_id")
        k = _build_knowledge(sql)
        ctx = build_context(k)
        tables = {es.table for es in ctx.entity_source_tables()}
        assert "tmp_calc" not in tables, f"中间表混入: {tables}"
        assert "tbl_order" in tables


class TestFinalFields:
    def test_returns_list_without_scenario(self):
        k = _build_knowledge(UNION_SQL)
        ctx = build_context(k)
        ff = ctx.final_fields(group_by_scenario=False)
        assert isinstance(ff, list)
        names = {f["target_field"] for f in ff}
        assert "order_id" in names

    def test_returns_groups_with_scenario(self):
        k = _build_knowledge(UNION_SQL)
        ctx = build_context(k)
        ff = ctx.final_fields(group_by_scenario=True)
        assert isinstance(ff, dict)
        # 单场景 fixture：默认场景组
        assert any("order_id" in {f["target_field"] for f in fields} for fields in ff.values())


class TestTargetCaliber:
    def test_target_table_from_max_sequence(self):
        k = _build_knowledge(UNION_SQL)
        ctx = build_context(k)
        assert ctx.target_table == "final_f"
        assert ctx.target_table_full == "dws.final_f"

    def test_target_cn_prefers_table_comment(self):
        k = _build_knowledge(UNION_SQL)
        k["meta"]["table_comment"] = "订单宽表"
        k["business_logic"]["summary"] = "业务描述首句，第二句"
        ctx = build_context(k)
        assert ctx.target_cn == "订单宽表"

    def test_target_cn_falls_back_to_summary(self):
        k = _build_knowledge(UNION_SQL)
        k["business_logic"]["summary"] = "业务描述首句，第二句"
        ctx = build_context(k)
        assert ctx.target_cn == "业务描述首句"
