"""
跨步骤关联键追溯测试

验证 build_join_key_lineage 能从某步骤的关联键出发，沿数据依赖反向追溯，
穿透中间表的直取/加工，追到物理源表的原始字段。

追溯规则：
- 中间表的 direct 字段 → 继续向上追溯
- 中间表的加工字段（拼接/截取/兜底）→ 展示加工，继续追溯每个源字段
- 物理源表字段（ods/dim）→ 停止

运行:
    pytest tests/test_join_key_lineage.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from analyzer import (
    parse_single_sql, build_topology, build_data_flow,
    build_field_mappings, RawRule,
)


def _run_analysis(rules_sql):
    """从 SQL 列表跑完整分析，返回 field_mappings + data_flow + topology。"""
    rules = [RawRule(
        rule_code=f"R{i+1}", rule_name=sql_info["name"], rule_type=1,
        exec_sequence=i + 1, target_schema="dws", target_table=sql_info["target"],
        delete_mode="1", query_sql=sql_info["sql"],
    ) for i, sql_info in enumerate(rules_sql)]
    pm = {r.rule_code: parse_single_sql(r.query_sql, "dws") for r in rules}
    topo = build_topology(rules, pm)
    df = build_data_flow(rules, pm)
    fm = build_field_mappings(rules, pm, {})
    return rules, pm, topo, df, fm


# 场景：拼接→直取→直取→关联
SCENARIO = [
    {"name": "拼接bid", "target": "tmp1",
     "sql": "SELECT a.aid, (a.code || b.seq) AS bid FROM ods.tbl_a a LEFT JOIN ods.tbl_b b ON a.k = b.k"},
    {"name": "直取tmp2", "target": "tmp2",
     "sql": "SELECT t.aid, t.bid FROM dws.tmp1 t"},
    {"name": "直取tmp3", "target": "tmp3",
     "sql": "SELECT t.aid, t.bid FROM dws.tmp2 t"},
    {"name": "关联d", "target": "final_f",
     "sql": "SELECT t.aid, d.dname FROM dws.tmp3 t LEFT JOIN ods.tbl_d d ON t.bid = d.bid"},
]


class TestJoinKeyLineage:
    """跨步骤关联键追溯。"""

    def test_trace_finds_physical_source(self):
        """从 step_4 的 tmp3.bid 追溯，应追到 ods.tbl_a.code 和 ods.tbl_b.seq"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        # 从 step_4 追溯关联键 bid（tmp3.bid）
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        assert chain, "追溯结果不应为空"
        # 应追到物理源表
        leaf_tables = _collect_leaf_tables(chain)
        assert "tbl_a" in leaf_tables or "ods.tbl_a" in leaf_tables, \
            f"应追到 tbl_a，实际叶节点: {leaf_tables}"
        assert "tbl_b" in leaf_tables or "ods.tbl_b" in leaf_tables, \
            f"应追到 tbl_b，实际叶节点: {leaf_tables}"

    def test_trace_shows_processing(self):
        """追溯链应展示拼接加工（a.code || b.seq）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        # 链中应有一跳是 expression（拼接）
        transforms = _collect_transforms(chain)
        assert "expression" in transforms, \
            f"应包含拼接加工 expression，实际: {transforms}"

    def test_trace_includes_intermediate_steps(self):
        """追溯链应包含中间步骤（step_2/step_3 的直取）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        steps = _collect_steps(chain)
        # 至少追溯了 3 跳（step_3→step_2→step_1）
        assert len(steps) >= 3, f"应至少追溯3跳，实际 steps: {steps}"

    def test_trace_terminates_at_physical(self):
        """追溯应在物理源表停止，不无限递归"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SCENARIO)
        chain = build_join_key_lineage("step_4", "bid", "t", rules, pm, topo, df, fm)
        # 叶节点都应是物理源表（非中间表）
        leaves = _collect_leaves(chain)
        for leaf in leaves:
            table = leaf.get("table", "")
            # 物理源表：ods 层或非 dws 中间表
            assert "ods." in table.lower() or not table.lower().startswith("dws.tmp"), \
                f"叶节点应为物理源表，实际 {table}"


# ── 辅助函数：遍历追溯链树 ──

def _walk_chain(chain):
    """递归遍历追溯链。chain 格式见 build_join_key_lineage 返回。"""
    if not chain:
        return
    yield chain
    for child in chain.get("children", []):
        yield from _walk_chain(child)


def _collect_leaf_tables(chain):
    """收集所有叶节点的表名。"""
    result = []
    for node in _walk_chain(chain):
        if not node.get("children"):  # 叶节点
            tbl = node.get("table", "")
            result.append(tbl)
            result.append(tbl.split(".")[-1])  # 短名也加
    return result


def _collect_transforms(chain):
    """收集链中所有 transform_type。"""
    return list({node.get("transform", "") for node in _walk_chain(chain) if node.get("transform")})


def _collect_steps(chain):
    """收集链中所有 step_id。"""
    return list({node.get("step_id", "") for node in _walk_chain(chain) if node.get("step_id")})


# ── 自引用 UPDATE/INSERT 场景（同表多步骤读写）──

SELF_REF_SCENARIO = [
    {"name": "规则1: 从ods建mid表", "target": "mid",
     "sql": "INSERT OVERWRITE TABLE dws.mid SELECT a.order_id, a.orig_amount FROM ods.orders a"},
    {"name": "规则2: mid自引用加工", "target": "mid",
     "sql": "INSERT OVERWRITE TABLE dws.mid SELECT t.order_id, t.orig_amount * 10 AS amount_v2 FROM dws.mid t"},
    {"name": "规则3: mid自引用再加工改名", "target": "mid",
     "sql": "INSERT OVERWRITE TABLE dws.mid SELECT t.order_id, (t.amount_v2 + 1) AS amount_v3 FROM dws.mid t"},
]


class TestSelfReferenceLineage:
    """自引用场景：同一张表被多个规则反复 UPDATE/INSERT。"""

    def test_self_ref_traces_to_physical_source(self):
        """规则3的自引用字段 amount_v3 应追到 ods.orders.orig_amount"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_3", "amount_v3", "t", rules, pm, topo, df, fm)
        assert chain, "追溯结果不应为空"
        leaf_tables = _collect_leaf_tables(chain)
        assert any("orders" in t for t in leaf_tables), \
            f"应追到 ods.orders，实际叶节点: {leaf_tables}"

    def test_self_ref_passes_through_all_steps(self):
        """关联键 order_id 应穿过所有中间步骤（step_3→step_2→step_1）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_3", "order_id", "t", rules, pm, topo, df, fm)
        steps = _collect_steps(chain)
        assert len(steps) >= 3, f"应至少追溯3跳，实际 steps: {steps}"

    def test_self_ref_shows_renames(self):
        """链路中应体现改名（amount_v3 ← amount_v2 ← orig_amount）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_3", "amount_v3", "t", rules, pm, topo, df, fm)
        fields = [n.get("field", "") for n in _walk_chain(chain)]
        # 应同时出现 amount_v3, amount_v2, orig_amount
        assert "amount_v2" in fields, f"链路应含 amount_v2（改名），实际: {fields}"
        assert "orig_amount" in fields, f"链路应含 orig_amount（原始名），实际: {fields}"

    def test_self_ref_no_infinite_loop(self):
        """自引用不应导致无限递归"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_3", "amount_v3", "t", rules, pm, topo, df, fm)
        assert chain, "应正常返回，不应卡死"
        # 叶节点不应都是 mid（说明没追到源头）
        leaves = [n for n in _walk_chain(chain) if not n.get("children")]
        for leaf in leaves:
            assert "mid" not in leaf.get("table", "").lower(), \
                f"叶节点不应是 mid（自引用表本身），实际: {leaf.get('table')}"


def _collect_leaves(chain):
    """收集所有叶节点。"""
    return [node for node in _walk_chain(chain) if not node.get("children")]


# ── CTE 自引用场景（WITH po_tmp AS ... 自引用 UPDATE）──

CTE_SELF_REF_SCENARIO = [
    {"name": "规则8: ods加工a,b,c写入f", "target": "f",
     "sql": "INSERT OVERWRITE TABLE dws.f SELECT x.col_a AS a, x.col_b AS b, y.col_c AS c "
            "FROM ods.orders x LEFT JOIN ods.items y ON x.item_id = y.item_id"},
    {"name": "规则9: CTE加工d UPDATE回f", "target": "f",
     "sql": "INSERT OVERWRITE TABLE dws.f "
            "WITH po_tmp AS (SELECT nvl((po.a - po.b) * po.c, 0) AS d FROM dws.f po) "
            "SELECT t.a, t.b, t.c, sub.d FROM dws.f t LEFT JOIN po_tmp sub ON 1=1"},
    {"name": "规则10: CTE加工e UPDATE回f", "target": "f",
     "sql": "INSERT OVERWRITE TABLE dws.f "
            "WITH po_tmp AS (SELECT h FROM dws.f WHERE xxx = 1 GROUP BY h) "
            "SELECT po.a, po.b, po.c, po.d, sum(decode(po.g, 'Y', 0, po.d)) AS e "
            "FROM dws.f po INNER JOIN po_tmp tmp ON po.h = tmp.h GROUP BY tmp.h"},
]


class TestCTESelfReferenceLineage:
    """CTE + 自引用场景：WITH 内加工字段，UPDATE 回目标表。"""

    def test_cte_field_traces_through_to_physical(self):
        """d 字段通过 CTE 加工，应穿透到 ods.orders / ods.items"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(CTE_SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_2", "d", "sub", rules, pm, topo, df, fm)
        assert chain, "追溯结果不应为空"
        leaf_tables = _collect_leaf_tables(chain)
        assert any("orders" in t for t in leaf_tables), \
            f"d 应追到 ods.orders，实际: {leaf_tables}"
        assert any("items" in t for t in leaf_tables), \
            f"d 应追到 ods.items，实际: {leaf_tables}"

    def test_cte_field_shows_expression(self):
        """链路应体现 CTE 内的加工表达式（NVL((po.a-po.b)*po.c,0)）"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(CTE_SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_2", "d", "sub", rules, pm, topo, df, fm)
        raw_sqls = [n.get("raw_sql", "") for n in _walk_chain(chain)]
        assert any("NVL" in r or "nvl" in r for r in raw_sqls), \
            f"链路应含 CTE 内的 NVL 表达式，实际: {raw_sqls}"

    def test_cte_multi_hop_e_traces_to_physical(self):
        """e 字段（基于 d 加工）应穿透 CTE + 自引用追到 ods 源表"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(CTE_SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_3", "e", "po", rules, pm, topo, df, fm)
        assert chain, "追溯结果不应为空"
        leaf_tables = _collect_leaf_tables(chain)
        assert any("orders" in t for t in leaf_tables), \
            f"e 应追到 ods.orders，实际: {leaf_tables}"

    def test_cte_no_infinite_loop(self):
        """CTE 自引用不应导致无限递归"""
        from analyzer import build_join_key_lineage
        rules, pm, topo, df, fm = _run_analysis(CTE_SELF_REF_SCENARIO)
        chain = build_join_key_lineage("step_3", "e", "po", rules, pm, topo, df, fm)
        assert chain, "应正常返回，不应卡死"


# ═══════════════════════════════════════════════════════════════
# 多场景覆盖
# ═══════════════════════════════════════════════════════════════

class TestJoinKeyLineageMultiScenario:
    """不同追溯模式的多场景覆盖。"""

    def test_single_source_processing_substring(self):
        """场景2：单源加工（截取）→ 直取 → 关联"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "截取key", "target": "tmp1",
             "sql": "SELECT a.id, SUBSTR(a.code, 1, 5) AS join_key FROM ods.src_a a"},
            {"name": "直取", "target": "tmp2",
             "sql": "SELECT t.id, t.join_key FROM dws.tmp1 t"},
            {"name": "关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp2 t LEFT JOIN ods.dim_d d ON t.join_key = d.join_key"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_3", "join_key", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        # 应追到 ods.src_a（单源截取）
        leaves = _collect_leaf_tables(chain)
        assert any("src_a" in t for t in leaves), f"应追到 src_a，实际 {leaves}"
        # 加工类型应有 expression（截取）
        transforms = _collect_transforms(chain)
        assert "expression" in transforms

    def test_multi_hop_pure_direct(self):
        """场景3：纯直取多跳（无加工），关联键一路直取"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "源头", "target": "tmp1",
             "sql": "SELECT a.id, a.k FROM ods.src a"},
            {"name": "直取1", "target": "tmp2",
             "sql": "SELECT t.id, t.k FROM dws.tmp1 t"},
            {"name": "直取2", "target": "tmp3",
             "sql": "SELECT t.id, t.k FROM dws.tmp2 t"},
            {"name": "直取3", "target": "tmp4",
             "sql": "SELECT t.id, t.k FROM dws.tmp3 t"},
            {"name": "关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp4 t LEFT JOIN ods.dim_d d ON t.k = d.k"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_5", "k", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        # 应追到 ods.src
        leaves = _collect_leaf_tables(chain)
        assert any("src" in t for t in leaves), f"应追到 src，实际 {leaves}"
        # 全程直取，无加工
        transforms = _collect_transforms(chain)
        assert "expression" not in transforms, f"纯直取不应有加工，实际 {transforms}"

    def test_no_trace_for_physical_source(self):
        """场景4：物理源表直接关联，无追溯链"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "直接关联", "target": "final_f",
             "sql": "SELECT a.id, b.name FROM ods.src_a a LEFT JOIN ods.dim_b b ON a.k = b.k"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        # src_a.k 是物理源表，不应有追溯链
        chain = build_join_key_lineage("step_1", "k", "a", rules, pm, topo, df, fm)
        assert chain is not None
        assert chain.get("is_physical"), "物理源表的关联键应直接标为物理源表"
        assert not chain.get("children"), "物理源表不应有追溯子节点"

    def test_fallback_processing(self):
        """场景5：兜底加工（COALESCE）作为关联键"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "兜底key", "target": "tmp1",
             "sql": "SELECT a.id, COALESCE(a.code, 'UNK') AS join_key FROM ods.src_a a"},
            {"name": "关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp1 t LEFT JOIN ods.dim_d d ON t.join_key = d.join_key"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_2", "join_key", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        transforms = _collect_transforms(chain)
        # COALESCE 应识别为 fallback
        assert "fallback" in transforms, f"兜底应识别为 fallback，实际 {transforms}"
        leaves = _collect_leaf_tables(chain)
        assert any("src_a" in t for t in leaves)

    def test_case_when_processing(self):
        """场景6：CASE WHEN 加工关联键（条件加工，多条件字段都追溯）"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "条件key", "target": "tmp1",
             "sql": "SELECT a.id, CASE WHEN a.t=1 THEN a.k1 ELSE a.k2 END AS jk FROM ods.src a"},
            {"name": "关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp1 t LEFT JOIN ods.dim_d d ON t.jk = d.jk"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_2", "jk", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        transforms = _collect_transforms(chain)
        assert "case_when" in transforms, f"CASE WHEN 应识别为 case_when，实际 {transforms}"
        leaves = _collect_leaf_tables(chain)
        assert any("src" in t for t in leaves), f"应追到 src，实际 {leaves}"

    def test_multi_step_stacked_processing(self):
        """场景7：多步骤叠加加工（拼接→截取→拼接，三层加工都展示）"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "拼接", "target": "tmp1",
             "sql": "SELECT a.id, (a.x || a.y) AS k FROM ods.src1 a"},
            {"name": "截取", "target": "tmp2",
             "sql": "SELECT t.id, SUBSTR(t.k, 1, 3) AS k FROM dws.tmp1 t"},
            {"name": "再拼接", "target": "tmp3",
             "sql": "SELECT t.id, (t.k || 'X') AS k FROM dws.tmp2 t"},
            {"name": "关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp3 t LEFT JOIN ods.dim_d d ON t.k = d.k"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_4", "k", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        leaves = _collect_leaf_tables(chain)
        assert any("src1" in t for t in leaves), f"应追到 src1，实际 {leaves}"
        transforms = _collect_transforms(chain)
        assert transforms.count("expression") >= 1, f"应有多层加工，实际 {transforms}"
        steps = _collect_steps(chain)
        assert len(steps) >= 3, f"应至少追溯3跳，实际 {steps}"

    def test_inner_join_key(self):
        """场景8：INNER JOIN 关联键追溯（不只 LEFT JOIN）"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "源头", "target": "tmp1",
             "sql": "SELECT a.id, a.k FROM ods.src a"},
            {"name": "INNER关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp1 t INNER JOIN ods.dim_d d ON t.k = d.k"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_2", "k", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        leaves = _collect_leaf_tables(chain)
        assert any("src" in t for t in leaves), f"INNER JOIN 也应追溯，实际 {leaves}"

    def test_subquery_processing_trace(self):
        """场景9：关联键在子查询里加工，追溯应穿透子查询到物理源表"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "子查询加工", "target": "tmp1",
             "sql": "SELECT t.id, t.k FROM (SELECT a.id, (a.x||a.y) AS k FROM ods.src a) t"},
            {"name": "关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM dws.tmp1 t LEFT JOIN ods.dim_d d ON t.k = d.k"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_2", "k", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        leaves = _collect_leaf_tables(chain)
        # 应穿透子查询追到 ods.src
        assert any("src" in t for t in leaves), \
            f"子查询应穿透到 src，实际 {leaves}（可能停在 subquery 假名）"
        transforms = _collect_transforms(chain)
        assert "expression" in transforms, f"应展示拼接加工，实际 {transforms}"

    def test_union_subquery_trace(self):
        """场景10：UNION 子查询的关联键追溯（穿透 UNION 子查询）"""
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "拼接A", "target": "tmp1",
             "sql": "SELECT a.id, (a.x||a.y) AS k FROM ods.src_a a"},
            {"name": "拼接B", "target": "tmp2",
             "sql": "SELECT a.id, (a.x||a.y) AS k FROM ods.src_b a"},
            {"name": "union+关联", "target": "final_f",
             "sql": "SELECT t.id, d.name FROM (SELECT id,k FROM dws.tmp1 UNION ALL SELECT id,k FROM dws.tmp2) t LEFT JOIN ods.dim_d d ON t.k = d.k"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_3", "k", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        leaves = _collect_leaf_tables(chain)
        # 应穿透 UNION 子查询追到至少一个物理源表
        assert any("src_a" in t or "src_b" in t for t in leaves), \
            f"UNION 子查询应穿透到 src_a/src_b，实际 {leaves}"

    def test_real_world_temp_table_naming(self):
        """场景11：生产实际临时表命名格式（xxx_tmp1 后缀，非 tmp 开头）

        Bug: 旧代码 startswith("tmp") 只认 tmp 开头，后缀格式的临时表
        被误判为物理源表，追溯链不穿透。测试用例都用 tmp1（开头），
        掩盖了这个 bug。
        """
        from analyzer import build_join_key_lineage
        scenario = [
            {"name": "源头加工", "target": "dwl_con_pu_any_tmp1",
             "sql": "SELECT a.id, (a.code || b.seq) AS bid FROM ods.tbl_a a LEFT JOIN ods.tbl_b b ON a.k = b.k"},
            {"name": "中间直取", "target": "dwl_con_pu_any_tmp2",
             "sql": "SELECT t.id, t.bid FROM dws.dwl_con_pu_any_tmp1 t"},
            {"name": "最终关联", "target": "dwl_con_pu_any_f",
             "sql": "SELECT t.id, d.name FROM dws.dwl_con_pu_any_tmp2 t LEFT JOIN ods.dim_d d ON t.bid = d.bid"},
        ]
        rules, pm, topo, df, fm = _run_analysis(scenario)
        chain = build_join_key_lineage("step_3", "bid", "t", rules, pm, topo, df, fm)
        assert chain, "追溯链不应为空"
        leaves = _collect_leaf_tables(chain)
        # 应穿透后缀格式的临时表追到物理源表
        assert any("tbl_a" in t for t in leaves), \
            f"后缀格式临时表应穿透到 tbl_a，实际 {leaves}（startswith bug 会停在后缀表名）"
        assert any("tbl_b" in t for t in leaves), \
            f"后缀格式临时表应穿透到 tbl_b，实际 {leaves}"
