#!/usr/bin/env python3
"""多来源（多场景）分组呈现契约测试（2026-11-12，docs/17 §十四）。

理论定案（用户拍板）：
- 分组 = 行的产出路径：UNION 每分支一条行路径 → 来源N；分区场景每链一条
  → 场景标签（同一概念两种叫法）；步骤/表/双链/公共加工不拆组（负面清单）
- 渲染两轴判据：逐字相同 → 公共一行；有差异（源表/字段/条件/键）→ 按组重复
- 阅读契约：某组完整设计 = 分组∈{该组, 公共} 的行；组合视图内目标字段唯一
- 键异名：合并集关联左键逐组解析回本地字段，有差异 → 关系按组展开
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "dws-pipeline-analyzer" / "references"))

from engine import RawRule, analyze_pipeline, detect_dialect


# ════════════════════════════════════════════════════════════
# 用户真实案例：前三步并行加工字典 → 第四步 10 来源 UNION ALL +
# 关联 3 tmp 带出 → 第五步维度扩维（case_36 同形）
# ════════════════════════════════════════════════════════════

def _user_case_rules():
    br = []
    for c in "abcdefghij":
        br.append(f"SELECT {c}.bill_id, {c}.cust_id, {c}.amt, '{c}' AS src_flag\n"
                  f"FROM ods.src_{c} {c} WHERE {c}.dr = '0'")
    union_sql = "\nUNION ALL\n".join(br)
    sql4 = f"""SELECT u.bill_id, u.cust_id, u.amt, u.src_flag,
       d1.tag_a, d1.tag_b, d2.score_x, d3.level_y
FROM ({union_sql}) u
LEFT JOIN fin.tmp_dict1 d1 ON u.cust_id = d1.cust_id
LEFT JOIN fin.tmp_dict2 d2 ON u.cust_id = d2.cust_id
LEFT JOIN fin.tmp_dict3 d3 ON u.cust_id = d3.cust_id"""
    sql5 = """SELECT t.bill_id, t.cust_id, t.amt, t.src_flag, t.tag_a, t.tag_b,
       t.score_x, t.level_y, c.cust_name, ar.area_name
FROM fin.tmp_union4 t
LEFT JOIN dim.dim_cust c ON t.cust_id = c.cust_id
LEFT JOIN dim.dim_area ar ON c.area_id = ar.area_id"""
    return [
        RawRule(rule_code="R1", rule_name="加工字典1", rule_type=1, exec_sequence=1,
                target_schema="fin", target_table="tmp_dict1", delete_mode="1",
                query_sql="SELECT k1.cust_id, k1.tag_a, k1.tag_b FROM ods.dict_k1 k1 WHERE k1.dr = '0'"),
        RawRule(rule_code="R2", rule_name="加工字典2", rule_type=1, exec_sequence=2,
                target_schema="fin", target_table="tmp_dict2", delete_mode="1",
                query_sql="SELECT k2.cust_id, k2.score_x FROM ods.dict_k2 k2 WHERE k2.status = '1'"),
        RawRule(rule_code="R3", rule_name="加工字典3", rule_type=1, exec_sequence=3,
                target_schema="fin", target_table="tmp_dict3", delete_mode="1",
                query_sql="SELECT k3.cust_id, k3.level_y FROM ods.dict_k3 k3 WHERE k3.flag = 'Y'"),
        RawRule(rule_code="R4", rule_name="多来源整合", rule_type=1, exec_sequence=4,
                target_schema="fin", target_table="tmp_union4", delete_mode="1", query_sql=sql4),
        RawRule(rule_code="R5", rule_name="维度扩维", rule_type=1, exec_sequence=5,
                target_schema="dwb", target_table="final_f", delete_mode="1", query_sql=sql5),
    ]


@pytest.fixture(scope="module")
def user_model():
    from spec_export import build_spec_model
    rules = _user_case_rules()
    knowledge, _ = analyze_pipeline(
        rules, {}, {}, detect_dialect([r.query_sql for r in rules]),
        source_file="x.xlsx")
    return build_spec_model(knowledge, {})


class TestUserCaseSheet3:
    """表关联：来源组行 + 公共行（8 列，分组垫尾）。"""

    def test_source_rows_with_branch_filters(self, user_model):
        """10 张来源表各一行：多来源(UNION ALL) + 分支行筛选限制段 + 来源N。
        布局 = E1 式（驱动表在左位 r[1]，右位占位符）。"""
        src_rows = [r for r in user_model["sheet3"]
                    if r[5] == "多来源(UNION ALL)"]
        assert len(src_rows) == 10, f"应 10 来源行，实际 {len(src_rows)}"
        assert [r[1] for r in src_rows] == [f"src_{c}" for c in "abcdefghij"]
        assert [r[7] for r in src_rows] == [f"来源{i}" for i in range(1, 11)]
        assert all("dr = '0'" in (r[6] or "") for r in src_rows)

    def test_common_dict_rows_penetrated(self, user_model):
        """3 条 tmp 链各自穿透成物理行（多 tmp 链不再只挂 joins[0]），
        ON 贴物理别名 + 生产步限定段；不再挂巨型 SQL 原文。"""
        d_rows = [r for r in user_model["sheet3"] if r[3].startswith("dict_k")]
        assert len(d_rows) == 3, f"dict_k1/2/3 应各一行，实际 {len(d_rows)}"
        for r in d_rows:
            assert r[7] == "公共"
            assert r[1] == "合并集"
            assert "u.cust_id = " in (r[6] or "") and "d1." not in (r[6] or "")
        conds = {r[3]: r[6] for r in d_rows}
        assert "k1.dr = '0'" in conds["dict_k1"]
        assert "k2.status = '1'" in conds["dict_k2"]
        assert "k3.flag = 'Y'" in conds["dict_k3"]
        blob = " ".join(str(r[6]) for r in user_model["sheet3"])
        assert "【逻辑从表" not in blob, "多来源呈现不挂 SQL 原文（实现细节）"

    def test_common_dim_rows(self, user_model):
        """维度关联公共：合并集左表（键同名零信息）；dim_area 左表 = dim_cust。"""
        dim1 = next(r for r in user_model["sheet3"] if r[3] == "dim_cust")
        dim2 = next(r for r in user_model["sheet3"] if r[3] == "dim_area")
        assert dim1[1] == "合并集" and dim1[7] == "公共"
        assert "t.cust_id = c.cust_id" == dim1[6]
        assert dim2[1] == "dim_cust" and dim2[7] == "公共"

    def test_no_predicate_warnings(self, user_model):
        """谓词守恒：分支/生产步 WHERE 全部落段（多来源行限制段承载）。"""
        types = [w["type"] for w in user_model["disclosures"]]
        assert "predicate_missing_where" not in types


class TestUserCaseSheet4:
    """属性级：union 供数按组展开 + 公共加工字段公共行。"""

    def test_union_fed_fields_per_group(self, user_model):
        """bill_id/cust_id/amt 十组各一行（各组源表不同=有信息）。"""
        for fld in ("bill_id", "cust_id", "amt"):
            rows = [r for r in user_model["sheet4"] if r[10] == fld]
            assert len(rows) == 10, f"{fld} 应 10 行，实际 {len(rows)}"
            assert {r[2] for r in rows} == {f"src_{c}" for c in "abcdefghij"}
            assert all(r[5] == "直接复制" for r in rows)

    def test_branch_constant_per_group(self, user_model):
        """分支常量 src_flag：按组赋值行（'a'~'j' 有信息），源表回填组驱动表。"""
        rows = [r for r in user_model["sheet4"] if r[10] == "src_flag"]
        assert len(rows) == 10
        assert all(r[5] == "赋值" for r in rows)
        exprs = [r[6] for r in rows]
        assert exprs[0].startswith("'a'") and exprs[-1].startswith("'j'")
        assert rows[0][2] == "src_a" and rows[0][3] == "a"

    def test_common_fields_once(self, user_model):
        """公共加工字段一行（dict/dim 带出，逐字同文零信息）。"""
        for fld in ("tag_a", "tag_b", "score_x", "level_y", "cust_name", "area_name"):
            rows = [r for r in user_model["sheet4"] if r[10] == fld]
            assert len(rows) == 1, f"{fld} 应公共一行，实际 {len(rows)}"
            assert rows[0][13] == "公共"

    def test_composed_view_field_unique(self, user_model):
        """阅读契约：任一「组+公共」组合视图内目标字段恰好出现一次。"""
        from collections import Counter
        for g in [f"来源{i}" for i in range(1, 11)]:
            view = [r[10] for r in user_model["sheet4"]
                    if r[13] in (g, "公共")]
            dup = [k for k, v in Counter(view).items() if v > 1]
            assert not dup, f"{g}+公共 视图字段重复: {dup}"
            assert len(view) == 10, f"{g} 完整切片应 10 字段"


class TestUserCaseMapping:
    """mapping：分组列接来源语义；实体级闭合；不注册合并集伪行。"""

    def test_entity_groups_and_no_pseudo(self):
        from mapping_export import build_mapping_model
        rules = _user_case_rules()
        knowledge, _ = analyze_pipeline(
            rules, {}, {}, detect_dialect([r.query_sql for r in rules]),
            source_file="x.xlsx")
        m = build_mapping_model(knowledge, {})
        tables = {e["table"] for e in m["entity"]}
        assert "合并集" not in tables, "合并集是呈现名，不进实体级"
        src_rows = [e for e in m["entity"] if e["table"].startswith("src_")]
        assert len(src_rows) == 10
        assert {e["group"] for e in src_rows} == {f"来源{i}" for i in range(1, 11)}
        assert all(e["remark"] != "主表" for e in m["entity"]), \
            "多来源没有单一主表，不标主表"
        # 属性级引用闭合：来源行组别与实体行组别一致
        ent = {(e["table"].lower(), e["alias"].lower(), e["group"])
               for e in m["entity"]}
        for a in m["attribute"]:
            if a["table"] and a["alias"] and a["group"] != "公共":
                assert (a["table"].split("、")[0].strip().lower(),
                        a["alias"].split("、")[0].strip().lower(),
                        a["group"]) in ent, f"属性级组行断链: {a}"


# ════════════════════════════════════════════════════════════
# 形态2/3：顶层 UNION、分支自带关联、键异名（case_37 同形）
# ════════════════════════════════════════════════════════════

_KEY_VARIANT_SQL = """SELECT a.bill_id, a.acct_no AS join_key, c.cust_name, c.cust_level
FROM ods.src_a a
LEFT JOIN dim.dim_cust c ON a.acct_no = c.cust_no
WHERE a.dr = '0'
UNION ALL
SELECT b.bill_id, b.cust_no AS join_key, c2.cust_name, c2.cust_level
FROM ods.src_b b
LEFT JOIN dim.dim_cust c2 ON b.cust_no = c2.cust_no
WHERE b.status = '1'"""


def _variant_model():
    from spec_export import build_spec_model
    rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                     target_schema="dwb", target_table="f", delete_mode="1",
                     query_sql=_KEY_VARIANT_SQL)]
    knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([_KEY_VARIANT_SQL]),
                                    source_file="x.xlsx")
    return build_spec_model(knowledge, {})


class TestBranchJoinAndKeyVariant:
    """分支内关联按组（形态2）+ 键异名展开（形态3）。"""

    def test_branch_joins_labeled_by_group(self):
        model = _variant_model()
        dim_rows = [r for r in model["sheet3"] if r[3] == "dim_cust"]
        assert len(dim_rows) == 2, "dim_cust 两分支各关联一次，按组两行"
        assert {r[7] for r in dim_rows} == {"来源1", "来源2"}
        assert dim_rows[0][6] == "a.acct_no = c.cust_no"
        assert dim_rows[1][6] == "b.cust_no = c2.cust_no"

    def test_key_field_per_group(self):
        """join_key 按组拆行（本地字段名不同=有信息）。"""
        model = _variant_model()
        rows = [r for r in model["sheet4"] if r[10] == "join_key"]
        assert {r[4] for r in rows} == {"acct_no", "cust_no"}
        assert {r[13] for r in rows} == {"来源1", "来源2"}

    def test_dim_fields_common_with_alias_union(self):
        """维表带出字段：映射逻辑逐字同文 → 公共一行，别名并集。"""
        model = _variant_model()
        for fld in ("cust_name", "cust_level"):
            rows = [r for r in model["sheet4"] if r[10] == fld]
            assert len(rows) == 1 and rows[0][13] == "公共"
            assert rows[0][3] == "c、c2"


# ════════════════════════════════════════════════════════════
# 回归保护：标准资产分组=默认/default；引擎分支 where
# ════════════════════════════════════════════════════════════

class TestStandardAssetGuard:
    """非多来源资产：分组列=默认（SPEC）/default（mapping），行为不变。"""

    SQL = """SELECT a.id, b.v FROM ods.a a LEFT JOIN ods.b b ON a.k = b.k"""

    def test_spec_group_default(self):
        from spec_export import build_spec_model
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="f_d", delete_mode="1",
                         query_sql=self.SQL)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([self.SQL]),
                                        source_file="x.xlsx")
        model = build_spec_model(knowledge, {})
        assert all(r[7] == "默认" for r in model["sheet3"])
        assert all(r[13] == "默认" for r in model["sheet4"])

    def test_mapping_group_default(self):
        from mapping_export import build_mapping_model
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="f_d", delete_mode="1",
                         query_sql=self.SQL)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([self.SQL]),
                                        source_file="x.xlsx")
        m = build_mapping_model(knowledge, {})
        assert all(a["group"] == "default" for a in m["attribute"])


class TestEngineBranchWhere:
    """引擎：union_branches[].where 逐分支携带行筛选（合并文本之外）。"""

    def test_branch_where_per_branch(self):
        sql = """SELECT a.x FROM ods.t1 a WHERE a.f = '1'
UNION ALL
SELECT b.x FROM ods.t2 b WHERE b.g = '2'"""
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="f_u", delete_mode="1",
                         query_sql=sql)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                        source_file="x.xlsx")
        ubs = knowledge["data_flow"]["steps"][0]["union_branches"]
        assert ubs[0]["where"] == "WHERE a.f = '1'", ubs[0].get("where")
        assert ubs[1]["where"] == "WHERE b.g = '2'", ubs[1].get("where")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
