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
        """10 张来源表各一行：与单一场景无关联表同构（E1 式，关联方式空，
        2026-11-16 拍板——「多来源(UNION ALL)」标签退役，行路径归分组列）。
        布局 = E1 式（驱动表在左位 r[1]，右位占位符）。"""
        src_rows = [r for r in user_model["sheet3"]
                    if r[7] and r[7].startswith("来源") and (r[3] or "").startswith("—")]
        assert len(src_rows) == 10, f"应 10 来源行，实际 {len(src_rows)}"
        assert all(r[5] == "" for r in src_rows), "E1 同构：关联方式列空"
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
    """分支内关联按组（形态2）+ 键异名展开（形态3）。

    根行省略拍板（2026-11-16）：分支有关联行时根行不产出——分组列已表达
    行路径，驱动表由首关联行左表承载，分支行筛选并入其限制段；无关联行
    时根行保留（唯一承载位：实体注册 + 谓词守恒）。
    """

    def test_branch_joins_labeled_by_group(self):
        model = _variant_model()
        dim_rows = [r for r in model["sheet3"] if r[3] == "dim_cust"]
        assert len(dim_rows) == 2, "dim_cust 两分支各关联一次，按组两行"
        assert {r[7] for r in dim_rows} == {"来源1", "来源2"}
        assert dim_rows[0][6] == "a.acct_no = c.cust_no；限制：a.dr = '0'"
        assert dim_rows[1][6] == "b.cust_no = c2.cust_no；限制：b.status = '1'"
        # 根行省略：无 多来源(UNION ALL) 行（分支都有关联行）
        assert not [r for r in model["sheet3"]
                    if r[5] == "多来源(UNION ALL)"]

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

    def test_root_row_kept_when_no_branch_join(self, user_model):
        """分支无关联行（用户 10 来源案例形态）→ E1 式表行保留：
        实体注册 + 谓词守恒唯一承载位（关联方式空，分组=来源N）。"""
        src_rows = [r for r in user_model["sheet3"]
                    if r[7] and r[7].startswith("来源") and (r[3] or "").startswith("—")]
        assert len(src_rows) == 10


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


# ════════════════════════════════════════════════════════════
# 用户复测三问题（2026-11-16）：函数开头 ON / 逗号 join 子查询 / 根行省略
# ════════════════════════════════════════════════════════════

_COMMA_SUBQ_SQL = """SELECT s.cust_id, sub1.type_name, sub1.mc_name
FROM (SELECT t1.cust_id, t1.p FROM ods.t1 t1 WHERE t1.dr = '0') s
LEFT JOIN (SELECT ct.type_name, mc.mc_name, ct.d
           FROM dim.dim_type ct, dim.dim_mc mc
           WHERE ct.mc_id = mc.mc_id) sub1
  ON upper(s.p) = sub1.d"""


def _model_of(sql):
    from spec_export import build_spec_model
    rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                     target_schema="dwb", target_table="f_x", delete_mode="1",
                     query_sql=sql)]
    knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                    source_file="x.xlsx")
    return build_spec_model(knowledge, {})


class TestCommaJoinSubquery:
    """问题2（用户真实形态）：Oracle 旧式逗号 join 子查询。

    此前缺陷：主查询 FROM 是子查询包裹 → 无 main 块 → sub1 关系整体丢失，
    逗号 join 条件错位挤进包裹限制段；且 JOIN_SUBQUERY_INNER 空行可能漏出。
    """

    def test_subq_relation_rendered_with_real_type(self):
        model = _model_of(_COMMA_SUBQ_SQL)
        row = next(r for r in model["sheet3"] if r[3] == "dim_type")
        assert row[5] == "LEFT JOIN", "真实类型（引擎 real_join_type），非空非伪型"
        assert row[6] == "UPPER(s.p) = sub1.d"
        assert row[1] == "t1", "左表 = FROM 包裹主物理（main 块缺失兜底）"

    def test_comma_join_carried_as_inner_row(self):
        """逗号 join（from a,b where …）→ 内层关联行：INNER JOIN + 块 where 条件。"""
        model = _model_of(_COMMA_SUBQ_SQL)
        mc = next(r for r in model["sheet3"] if r[3] == "dim_mc")
        assert mc[5] == "INNER JOIN"
        assert "ct.mc_id = mc.mc_id" in (mc[6] or "")

    def test_no_subquery_artifact_rows(self):
        """JOIN_SUBQUERY_INNER 空行（左表/条件全空）不存在——信息由块渲染承载。"""
        model = _model_of(_COMMA_SUBQ_SQL)
        for r in model["sheet3"]:
            assert "SUBQUERY" not in str(r[5]).upper(), r
            assert not (str(r[5]).upper().startswith("JOIN") and not (r[6] or "").strip()
                        and not r[1]), f"空壳关联行: {r}"

    def test_no_misplaced_where(self):
        """逗号 join 条件不再错位进包裹（s）限制段。"""
        model = _model_of(_COMMA_SUBQ_SQL)
        s_row = next(r for r in model["sheet3"] if str(r[3]).startswith("虚拟逻辑表"))
        assert "mc.mc_id" not in (s_row[6] or ""), s_row[6]


_FUNC_ON_SQL = """SELECT u.bill_id, u.colx, c1.code_name
FROM (SELECT a.bill_id, a.colx FROM ods.src_a a WHERE a.dr = '0'
      UNION ALL
      SELECT b.bill_id, b.colx FROM ods.src_b b WHERE b.status = '1') u
LEFT JOIN dim.dim_a c1 ON upper(u.colx) = c1.code AND c1.type = 'xxx'"""


class TestFunctionOnLeftResolution:
    """问题1：ON 以函数开头（upper(s.colx)=c1.code）时左表解析不再为空。"""

    def test_merged_left_with_func_on(self):
        model = _model_of(_FUNC_ON_SQL)
        dim = next(r for r in model["sheet3"] if r[3] == "dim_a")
        assert dim[1] == "合并集", f"函数开头 ON 应仍解析出合并集左表，实际 {dim[1]!r}"
        assert dim[6] == "UPPER(u.colx) = c1.code AND c1.type = 'xxx'"

    def test_downstream_left_with_func_on(self):
        """下游步骤（合并 tmp 承载别名）函数开头 ON：左表=合并集。"""
        sql = """SELECT u.bill_id, u.colx FROM (SELECT a.bill_id, a.colx FROM ods.src_a a
      UNION ALL SELECT b.bill_id, b.colx FROM ods.src_b b) u"""
        sql5 = ("SELECT t.bill_id, t.colx, c1.code_name FROM fin.tmp_u t "
                "LEFT JOIN dim.dim_a c1 ON upper(t.colx) = c1.code AND c1.type = 'xxx'")
        from spec_export import build_spec_model
        rules = [
            RawRule(rule_code="R1", rule_name="s1", rule_type=1, exec_sequence=1,
                    target_schema="fin", target_table="tmp_u", delete_mode="1",
                    query_sql=sql),
            RawRule(rule_code="R2", rule_name="s2", rule_type=1, exec_sequence=2,
                    target_schema="dwb", target_table="f_f", delete_mode="1",
                    query_sql=sql5),
        ]
        knowledge, _ = analyze_pipeline(
            rules, {}, {}, detect_dialect([sql, sql5]), source_file="x.xlsx")
        model = build_spec_model(knowledge, {})
        dim = next(r for r in model["sheet3"] if r[3] == "dim_a")
        assert dim[1] == "合并集", f"实际左表 {dim[1]!r}"
        assert dim[2] == "t"


class TestCommaJoinQuality:
    """逗号 join 质量提示（2026-11-16 用户需求）：旧式写法是质量问题，
    质量评估须体现并提醒优化；对逗号表不再误报「JOIN 缺少 ON 条件」。"""

    def _quality(self, sql):
        rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                         target_schema="dwb", target_table="f_cj", delete_mode="1",
                         query_sql=sql)]
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                        source_file="x.xlsx")
        return knowledge["quality"]["issues"]

    def test_top_level_comma_join_flagged(self):
        iss = self._quality(
            "SELECT x.cust_id, y.tag FROM ods.t1 x, ods.t2 y "
            "WHERE x.k = y.k AND x.dr = '0'")
        cj = [i for i in iss if "逗号 join" in i["title"]]
        assert len(cj) == 1
        assert cj[0]["severity"] == "medium"
        assert cj[0]["category"] == "code_quality"
        assert "ANSI JOIN" in cj[0]["title"]
        assert "ods.t2" in (cj[0].get("description") or "")
        assert not any("缺少 ON" in i["title"] for i in iss), "逗号表不误报缺 ON"

    def test_subquery_comma_join_flagged(self):
        iss = self._quality(
            "SELECT s.cust_id, sub1.type_name FROM (SELECT t1.cust_id, t1.p FROM ods.t1 t1) s "
            "LEFT JOIN (SELECT ct.type_name, ct.d FROM dim.dim_type ct, dim.dim_mc mc "
            "WHERE ct.mc_id = mc.mc_id) sub1 ON upper(s.p) = sub1.d")
        assert any("逗号 join" in i["title"] for i in iss)

    def test_ansi_join_not_flagged(self):
        iss = self._quality(
            "SELECT a.cust_id, b.tag FROM ods.t1 a LEFT JOIN ods.t2 b ON a.k = b.k")
        assert not any("逗号 join" in i["title"] for i in iss)
        assert not any("缺少 ON" in i["title"] for i in iss)


# ════════════════════════════════════════════════════════════
# 嵌套窗口字典（2026-11-16 用户案例）：JOIN 子查询(嵌套 FROM 子查询开窗
# + 逗号 join)——谓词原子按"离谓词的表近"归位
# ════════════════════════════════════════════════════════════

_NESTED_WINDOW_SQL = """SELECT s.col1, sub1.k1 FROM dwr.t_main s
LEFT JOIN (SELECT t.k1, t.k2
           FROM (SELECT b.k1, b.k2,
                        ROW_NUMBER() OVER(PARTITION BY b.k2 ORDER BY b.dt) rn
                 FROM dim.t_dim_a a, dim.t_dim_b b
                 WHERE a.type = 'x' AND a.id = b.id) t
           WHERE rn = 1) sub1
  ON upper(s.col1) = sub1.k2"""


class TestNestedWindowDict:
    """三层嵌套字典：空壳行已修 + 嵌套块谓词归位（用户拍板）。

    归位口径：双别名原子（a.id=b.id）→ 逗号关联行 ON；自身单别名 →
    该行限制段；未消费原子（rn=1 收敛 / a.type='x' 主表过滤）→ 锚行
    限定段，开窗沿用「窗口定义见定义原文」标注（定义片段逐字承载）。
    """

    def test_no_artifact_rows(self):
        model = _model_of(_NESTED_WINDOW_SQL)
        for r in model["sheet3"]:
            assert "SUBQUERY" not in str(r[5]).upper(), r
            assert not (not r[1] and not (r[6] or "").strip()), f"空壳行: {r}"

    def test_anchor_row_carry_window_and_filter(self):
        model = _model_of(_NESTED_WINDOW_SQL)
        anchor = next(r for r in model["sheet3"] if r[3] == "t_dim_a")
        g = anchor[6]
        assert "UPPER(s.col1) = sub1.k2" in g
        assert "rn = 1（开窗收敛，窗口定义见定义原文）" in g
        assert "a.type = 'x'" in g, "内层主表过滤不再丢失"

    def test_comma_join_atom_on_secondary(self):
        model = _model_of(_NESTED_WINDOW_SQL)
        b = next(r for r in model["sheet3"] if r[3] == "t_dim_b")
        assert b[5] == "INNER JOIN"
        assert "a.id = b.id" in (b[6] or ""), "逗号关联条件归位到 b 行"
        assert "rn" not in (b[6] or ""), "开窗收敛条件不再张冠李戴到 b 行"


# ════════════════════════════════════════════════════════════
# 多来源 × 嵌套子查询（2026-11-16 用户真实形态）：union 之后的公共规则
# 里 LEFT JOIN 嵌套窗口字典——多来源路径复用标准路径的子查询承载
# ════════════════════════════════════════════════════════════

_MS_SUBQ_SQL1 = """SELECT a.bill_id, a.col1 FROM ods.src_a a WHERE a.dr = '0'
UNION ALL
SELECT b.bill_id, b.col1 FROM ods.src_b b WHERE b.status = '1'"""
_MS_SUBQ_SQL2 = """SELECT t.bill_id, t.col1, sub1.k1, c.cust_name
FROM fin.tmp_u t
LEFT JOIN (SELECT tk.k1, tk.k2
           FROM (SELECT b2.k1, b2.k2,
                        ROW_NUMBER() OVER(PARTITION BY b2.k2 ORDER BY b2.dt) rn
                 FROM dim.t_dim_a a2, dim.t_dim_b b2
                 WHERE a2.type = 'x' AND a2.id = b2.id) tk
           WHERE rn = 1) sub1
  ON upper(t.col1) = sub1.k2
LEFT JOIN dim.dim_cust c ON t.bill_id = c.bill_id"""


def _ms_subq_model():
    from spec_export import build_spec_model
    rules = [
        RawRule(rule_code="R1", rule_name="多来源", rule_type=1, exec_sequence=1,
                target_schema="fin", target_table="tmp_u", delete_mode="1",
                query_sql=_MS_SUBQ_SQL1),
        RawRule(rule_code="R2", rule_name="公共字典关联", rule_type=1, exec_sequence=2,
                target_schema="dwb", target_table="f_ms_sq", delete_mode="1",
                query_sql=_MS_SUBQ_SQL2),
    ]
    knowledge, _ = analyze_pipeline(
        rules, {}, {}, detect_dialect([_MS_SUBQ_SQL1, _MS_SUBQ_SQL2]),
        source_file="x.xlsx")
    return build_spec_model(knowledge, {})


class TestMultiSourceWithSubquery:
    """多来源路径的公共规则里 JOIN 子查询：空壳行不再漏出、关系与谓词
    全量承载（标准路径同款块装配复用）、from_alias 不被子查询覆盖。"""

    def test_no_artifact_and_from_alias_intact(self):
        model = _ms_subq_model()
        for r in model["sheet3"]:
            assert "SUBQUERY" not in str(r[5]).upper(), f"空壳/伪型行: {r}"
        # dim_cust 左表 = 合并集（from_alias 未被 (subquery:) 覆盖）
        dim = next(r for r in model["sheet3"] if r[3] == "dim_cust")
        assert dim[1] == "合并集" and dim[2] == "t"

    def test_subq_carried_on_common_rows(self):
        """sub1 字典：锚行（ON+开窗收敛+主表过滤）+ 逗号关联行，组=公共。"""
        model = _ms_subq_model()
        anchor = next(r for r in model["sheet3"] if r[3] == "t_dim_a")
        assert anchor[7] == "公共" and anchor[5] == "LEFT JOIN"
        g = anchor[6]
        assert "UPPER(t.col1) = sub1.k2" in g
        assert "rn = 1（开窗收敛，窗口定义见定义原文）" in g
        assert "a2.type = 'x'" in g
        b2 = next(r for r in model["sheet3"] if r[3] == "t_dim_b")
        assert b2[5] == "INNER JOIN" and "a2.id = b2.id" in (b2[6] or "")
        assert b2[7] == "公共"

    def test_no_predicate_or_materialization_warnings(self):
        model = _ms_subq_model()
        types = [w["type"] for w in model["disclosures"]]
        assert "predicate_missing_where" not in types
        assert "materialization_trace" not in types


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
