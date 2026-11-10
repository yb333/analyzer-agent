#!/usr/bin/env python3
"""别名身份统一口径契约测试（2026-11-10）。

背景（用户实测缺陷）：同表多别名（t/t1/t2 三次独立关联）时三处源表清单
各自为政——ts rules[].source_tables last-wins 反查（只剩一个别名）、
meta.source_tables keep-first（另一个别名）、joins 逐条（三个别名），
同一文件内互相打架。

统一口径：**（步骤内 表×别名 一次参与）= 别名身份的唯一定义**，引擎
data_flow.steps[].joins 是逐次参与的完整真相，所有清单投影从它派生：
- ts rules[].source_tables：逐参与一行（与 joins 同基数）
- ts meta.source_tables：表级一行，alias = 别名全集（顿号合并）
- SPEC/mapping 实体级：按（表×别名）分行（test_mapping_export 已锁）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "dws-pipeline-analyzer" / "references"))

from engine import RawRule, analyze_pipeline, detect_dialect

SQL = """
SELECT a.id, t1.flag AS flag1, t2.flag AS flag2, t3.amt AS amt3
FROM ods.a_table a
LEFT JOIN dim.b_table t1 ON a.id = t1.id
LEFT JOIN dim.b_table t2 ON a.id = t2.id
LEFT JOIN dim.b_table t3 ON a.pid = t3.pid
"""


def _knowledge():
    rules = [RawRule(rule_code="R1", rule_name="s1", rule_type=1, exec_sequence=1,
                     target_schema="dws", target_table="dwb_x_d", delete_mode="1",
                     query_sql=SQL)]
    knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([SQL]),
                                    source_file="x.xlsx")
    return knowledge


def test_ts_source_tables_per_participation():
    """rules[].source_tables 逐参与一行：b_table 三行各带各的别名，与 joins 同基数。"""
    import ts_export
    from viewmodel import build_context
    knowledge = _knowledge()
    ctx = build_context(knowledge)
    ts_rules = ts_export._ts_rules(ctx, knowledge, ts_export._topology(knowledge),
                                   ts_export._write_plans(knowledge, ctx)[0])
    r = ts_rules["R1"]
    pairs = [(x["table"], x["alias"]) for x in r["source_tables"]]
    assert pairs == [("a_table", "a"), ("b_table", "t1"),
                     ("b_table", "t2"), ("b_table", "t3")], pairs
    join_aliases = [j["alias"] for j in r["joins"]]
    assert join_aliases == ["t1", "t2", "t3"]
    # source_tables 与 joins 基数一致（同表多参与不合并、不打架）
    b_rows = [x for x in r["source_tables"] if x["table"] == "b_table"]
    assert len(b_rows) == len([j for j in r["joins"]
                               if j["alias"] in ("t1", "t2", "t3")])


def test_meta_source_tables_alias_full_set():
    """meta.source_tables 表级一行，alias = 别名全集顿号合并（不再 keep-first 单别名）。"""
    import ts_export
    from viewmodel import build_context
    knowledge = _knowledge()
    ctx = build_context(knowledge)
    tables = {}  # _ts_meta 的 tables 参数仅用于字段桶，别名断言不依赖
    meta = ts_export._ts_meta(ctx, knowledge, {}, tables,
                              ts_export._topology(knowledge), {}, dq_detected=False)
    b = next(t for t in meta["source_tables"] if t["table"] == "b_table")
    assert b["alias"] == "t1、t2、t3", b
    a = next(t for t in meta["source_tables"] if t["table"] == "a_table")
    assert a["alias"] == "a"


def test_entity_source_aliases_registry():
    """viewmodel EntitySource.aliases = 出现序全集（身份登记先于表级去重）。"""
    from viewmodel import build_context
    ctx = build_context(_knowledge())
    by_table = {es.table: es for es in ctx.entity_source_tables()}
    assert by_table["b_table"].aliases == ["t1", "t2", "t3"]
    assert by_table["b_table"].alias == "t1"   # 兼容字段 = 首现别名
    assert len([es for es in ctx.entity_source_tables()
                if es.table == "b_table"]) == 1  # 行仍是表级去重


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))


class TestSemanticJoinIdentity:
    """同语义关联行身份（2026-11-10 拍板：一行）。

    行身份 = 左表×右表×关联类型×归一化条件；别名降级为引用名。
    场景：从表 s 在生产步与终端步以相同条件各关联一次（跨步重复关联），
    tmp 链内外两处呈现 → 设计层同一关联关系 → SPEC/mapping 各一行，
    且关联方式为生产步真实类型（此前写死 INNER JOIN 的实现失真已修）。
    """

    SQL1 = """SELECT a.cust_id, a.name, s.acct_no
FROM fin.a_table a
LEFT JOIN fin.s_table s ON a.cust_id = s.cust_id"""
    SQL2 = """SELECT t.cust_id, t.name, t.acct_no, s.phone
FROM fin.tmp1 t
LEFT JOIN fin.s_table s ON t.cust_id = s.cust_id"""

    def _rules(self):
        from engine import RawRule
        return [
            RawRule(rule_code="R1", rule_name="s1", rule_type=1, exec_sequence=1,
                    target_schema="fin", target_table="tmp1", delete_mode="1",
                    query_sql=self.SQL1),
            RawRule(rule_code="R2", rule_name="s2", rule_type=1, exec_sequence=2,
                    target_schema="fin", target_table="final_f", delete_mode="1",
                    query_sql=self.SQL2),
        ]

    def _knowledge(self):
        from engine import analyze_pipeline, detect_dialect
        knowledge, _ = analyze_pipeline(self._rules(), {}, {},
                                        detect_dialect([self.SQL1, self.SQL2]),
                                        source_file="x.xlsx")
        return knowledge

    def test_sheet3_one_row_real_type(self):
        """SPEC Sheet3：s_table 一行、LEFT JOIN（生产步真实类型，非写死 INNER）。"""
        from spec_export import build_spec_model
        knowledge = self._knowledge()
        model = build_spec_model(knowledge, {})
        s_rows = [r for r in model["sheet3"] if r[3] == "s_table"]
        assert len(s_rows) == 1, f"同语义应一行，实际 {len(s_rows)} 行"
        assert s_rows[0][5] == "LEFT JOIN", s_rows[0][5]
        # 条件贴物理别名（物化无关：a.cust_id，不是 tmp 别名 t.cust_id 独占）
        assert "a.cust_id = s.cust_id" in (s_rows[0][6] or "")

    def test_mapping_entity_one_row(self):
        """mapping 实体级：s_table 一行 LEFT JOIN，条件含锚贴别名。"""
        from mapping_export import build_mapping_model
        m = build_mapping_model(self._knowledge(), {})
        s_rows = [e for e in m["entity"] if e["table"] == "s_table"]
        assert len(s_rows) == 1
        assert s_rows[0]["on_condition"].startswith("LEFT JOIN:")
        assert "a.cust_id = s.cust_id" in s_rows[0]["on_condition"]

    def test_different_condition_not_merged(self):
        """条件不同的同表参与（t1/t2/t3）不合并——语义不同=不同设计关系。"""
        sql = """SELECT a.id, t1.f AS f1, t2.f AS f2
FROM ods.a a
LEFT JOIN ods.b t1 ON a.id = t1.id AND t1.tp = '1'
LEFT JOIN ods.b t2 ON a.id = t2.id AND t2.tp = '2'"""
        from engine import RawRule, analyze_pipeline, detect_dialect
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="f_d", delete_mode="1",
                         query_sql=sql)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                        source_file="x.xlsx")
        from mapping_export import build_mapping_model
        m = build_mapping_model(knowledge, {})
        b_rows = [e for e in m["entity"] if e["table"] == "b"]
        assert len(b_rows) == 2, "不同条件不合并"
