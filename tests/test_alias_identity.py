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


class TestSnowflakeMultiJoin:
    """雪花模型（2026-11-11 用户场景）：主表 t 各关联 a/c/d，a/c/d 又各关联 b
    （b 多次参与、别名 b1/b2/b3、b 侧关联键相同）——三个设计关系，三行。

    回归背景：语义去重早期"剥别名前缀"归一把三键全变 k=k（左列同驱动 t、
    右表同 b）→ 误合一行（b1、b2、b3 别名顿号合并），c-b/d-b 两关系丢失。
    修复 = 别名前缀**解析到表**（a.k=b1.k → a_tbl.k=b_tbl.k），表身份保留。
    """

    SQL = """SELECT t.id, a.v1, c.v2, d.v3, b1.bv AS bv1, b2.bv AS bv2, b3.bv AS bv3
FROM fin.t_tbl t
LEFT JOIN fin.a_tbl a ON t.x = a.x
LEFT JOIN fin.c_tbl c ON t.y = c.y
LEFT JOIN fin.d_tbl d ON t.z = d.z
LEFT JOIN fin.b_tbl b1 ON a.k = b1.k
LEFT JOIN fin.b_tbl b2 ON c.k = b2.k
LEFT JOIN fin.b_tbl b3 ON d.k = b3.k"""

    def _knowledge(self):
        from engine import RawRule, analyze_pipeline, detect_dialect
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="fin", target_table="f_x", delete_mode="1",
                         query_sql=self.SQL)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([self.SQL]),
                                        source_file="x.xlsx")
        return knowledge

    def test_sheet3_three_b_relations(self):
        """Sheet3：b_tbl 三行（b1/b2/b3 各带各的条件），关系不丢。"""
        from spec_export import build_spec_model
        model = build_spec_model(self._knowledge(), {})
        rows = [r for r in model["sheet3"] if r[3] == "b_tbl"]
        assert len(rows) == 3, f"b 应三行，实际 {len(rows)}"
        conds = {r[6] for r in rows}
        assert conds == {"a.k = b1.k", "c.k = b2.k", "d.k = b3.k"}, conds
        assert [r[4] for r in rows] == ["b1", "b2", "b3"]

    def test_mapping_entity_three_rows_closed(self):
        """mapping 实体级：b_tbl 三行；属性级别名与实体行闭合。"""
        from mapping_export import build_mapping_model
        m = build_mapping_model(self._knowledge(), {})
        b_rows = [e for e in m["entity"] if e["table"] == "b_tbl"]
        assert len(b_rows) == 3
        assert {e["alias"] for e in b_rows} == {"b1", "b2", "b3"}
        ent = {(e["table"].lower(), (e["alias"] or "").lower()) for e in m["entity"]}
        for a in m["attribute"]:
            if not a["table"]:
                continue
            key = (a["table"].split("、")[0].strip().lower(),
                   (a["alias"] or "").split("、")[0].strip().lower())
            assert key in ent, f"属性级别名断链: {key}"


class TestInstanceMultiplicity:
    """参与实例（2026-11-11 定稿）：同一步骤内同表多别名 = 多个设计实例。

    用户真实结构：t1/t2/t3 是同一张 tab 表的三个别名（s 用不同字段各关联
    一次），每次 tab 参与再各挂一个 emp（emp1/2/3 同表 e、字段逻辑相同
    upper(tN.no)=empN.no）——三个 emp 是并存实例（设计多重性），拆三行；
    与「跨步骤对同一关系的重复书写=实现重复（合一行）」相对照。
    """

    SQL = """select s.a, t1.c1, emp1.e1, t2.c2, emp2.e2, t3.c3, emp3.e3
from dwr.t s
left join dwr.tab t1 on s.f1 = t1.f1 and t1.xx = ${p_cycle}
left join dwr.e emp1 on upper(t1.no) = emp1.no
left join dwr.tab t2 on s.f2 = t2.f2 and t2.xx = ${p_cycle}
left join dwr.e emp2 on upper(t2.no) = emp2.no
left join dwr.tab t3 on s.f3 = t3.f3 and t3.xx = ${p_cycle}
left join dwr.e emp3 on upper(t3.no) = emp3.no"""

    def _knowledge(self):
        from engine import RawRule, analyze_pipeline, detect_dialect
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="f_x", delete_mode="1",
                         query_sql=self.SQL)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([self.SQL]),
                                        source_file="x.xlsx")
        return knowledge

    def test_emp_instances_not_merged(self):
        """emp 三次参与（同表同字段逻辑、挂不同 tab 实例）：三行不合并。"""
        from spec_export import build_spec_model
        model = build_spec_model(self._knowledge(), {})
        e_rows = [r for r in model["sheet3"] if r[3] == "e"]
        assert len(e_rows) == 3, f"e 应三行（三个并存实例），实际 {len(e_rows)}"
        assert [r[4] for r in e_rows] == ["emp1", "emp2", "emp3"]
        conds = {r[6] for r in e_rows}
        assert conds == {"UPPER(t1.no) = emp1.no", "UPPER(t2.no) = emp2.no",
                         "UPPER(t3.no) = emp3.no"}, conds
        # tab 三次参与（不同字段）同样三行
        tab_rows = [r for r in model["sheet3"] if r[3] == "tab"]
        assert len(tab_rows) == 3

    def test_mapping_three_rows_closed(self):
        """mapping：e/tab 各三行，属性级引用闭合。"""
        from mapping_export import build_mapping_model
        m = build_mapping_model(self._knowledge(), {})
        assert sum(1 for e in m["entity"] if e["table"] == "e") == 3
        assert sum(1 for e in m["entity"] if e["table"] == "tab") == 3
        ent = {(e["table"].lower(), (e["alias"] or "").lower()) for e in m["entity"]}
        for a in m["attribute"]:
            if not a["table"]:
                continue
            key = (a["table"].split("、")[0].strip().lower(),
                   (a["alias"] or "").split("、")[0].strip().lower())
            assert key in ent, f"属性级别名断链: {key}"

    def test_same_step_same_cond_selfjoin_splits(self):
        """同步骤同表多别名、条件全同（仅别名不同）的自关联：也拆（行为变化，
        2026-11-11 定稿——并存实例是设计多重性）。"""
        from engine import RawRule, analyze_pipeline, detect_dialect
        from spec_export import build_spec_model
        sql = """select a.x, t1.v AS v1, t2.v AS v2
from ods.a a
left join ods.b t1 on a.k = t1.k
left join ods.b t2 on a.k = t2.k"""
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="f_d", delete_mode="1",
                         query_sql=sql)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                        source_file="x.xlsx")
        model = build_spec_model(knowledge, {})
        b_rows = [r for r in model["sheet3"] if r[3] == "b"]
        assert len(b_rows) == 2, f"同步骤同条件自关联应拆两行，实际 {len(b_rows)}"
        assert {r[4] for r in b_rows} == {"t1", "t2"}
