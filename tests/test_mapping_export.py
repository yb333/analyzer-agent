#!/usr/bin/env python3
"""mapping.xlsx 模板 v2 契约测试（2026-11-04 模板换版）。

锁定四件事：
1. 列头指纹：常量 = 打包模板（模板换版必须三同步——常量/模板文件/测试）
2. 渲染形态：示例行清除、序号/分组（default）/数据库类型（DWS）
3. 口径：关联方式并入「关联&限定条件」前缀；直接复制表达式='-'；
   主表行备注='主表'；调度三列不再承载
4. 同源：与 SPEC 同一装配核（build_spec_model），驱动表/块内表/虚拟段不丢
"""
import sys
from pathlib import Path

import openpyxl
import pytest

# 引用仓内 references（测试从仓根跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "dws-pipeline-analyzer" / "references"))

from mapping_export import (
    MAPPING_TEMPLATE_HEADERS, check_template_fingerprint, export_mapping,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "analyzer"


def _run_case(case: str):
    from pipeline_core import run_from_dir
    return run_from_dir(str(FIXTURES / case), include_dq=False,
                        channel="test", entry_title="")


class TestTemplateFingerprint:
    """列头指纹：常量 = 打包模板（docs/17 §八 同机制）。"""

    def test_pconstant_matches_packaged_template(self):
        assert check_template_fingerprint() == []

    def test_headers_shape(self):
        assert len(MAPPING_TEMPLATE_HEADERS["实体级mapping"]) == 13
        assert len(MAPPING_TEMPLATE_HEADERS["属性级mapping"]) == 15
        # 2026-11-04 口径列在位
        e = MAPPING_TEMPLATE_HEADERS["实体级mapping"]
        assert "取数规则" in e and "关联&限定条件" in e and "数据库类型" in e
        a = MAPPING_TEMPLATE_HEADERS["属性级mapping"]
        assert "分组" in a and "映射规则*" in a and "数据标准" in a


class TestRenderShape:
    """渲染形态：示例行清除 + 固定列填充。"""

    @pytest.fixture(scope="class")
    def rendered(self, tmp_path_factory):
        core = _run_case("case_31_real_nested_chain")
        out = tmp_path_factory.mktemp("mapping")
        path = export_mapping(core["knowledge"], core["engine_ctx"], str(out))
        wb = openpyxl.load_workbook(path)
        yield wb
        wb.close()

    def test_sample_rows_cleared(self, rendered):
        """模板示例行（合同pu样例）必须被清除，不得混入产物。"""
        for name in ("实体级mapping", "属性级mapping"):
            ws = rendered[name]
            for row in ws.iter_rows(min_row=2, values_only=True):
                cells = " ".join(str(c) for c in row if c is not None)
                assert "dwl_con_pu" not in cells, f"{name} 残留模板示例行"
                assert "合同pu" not in cells, f"{name} 残留模板示例行"

    def test_seq_group_dbtype(self, rendered):
        """序号 1..n / 分组 default / 数据库类型 DWS。"""
        ws = rendered["实体级mapping"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        assert [r[0] for r in rows] == [str(i) for i in range(1, len(rows) + 1)]
        assert all(r[1] == "default" for r in rows)
        assert all(r[12] == "DWS" for r in rows)

    def test_no_schedule_columns_content(self, rendered):
        """调度三列已随模板 v2 删除——列头里不应再有调度任务名称/执行路径/依赖参数。"""
        e = MAPPING_TEMPLATE_HEADERS["实体级mapping"]
        for col in ("调度任务名称", "执行路径", "依赖参数"):
            assert col not in e

    def test_join_kind_prefix_in_condition(self, rendered):
        """关联方式并入口径：join 行条件 = "LEFT JOIN: …" 前缀，取数规则列留空。"""
        ws = rendered["实体级mapping"]
        join_rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
                     if (r[10] or "").startswith(("LEFT JOIN", "RIGHT JOIN",
                                                  "INNER JOIN", "FULL JOIN", "CROSS JOIN"))]
        assert join_rows, "应存在带关联方式前缀的行"
        assert all(r[9] in (None, "") for r in join_rows), "取数规则列应留空"


class TestSemantics:
    """口径：主表行备注 / 直接复制 '-' / 驱动表注册 / 块内表合并不丢。"""

    @pytest.fixture(scope="class")
    def case32(self, tmp_path_factory):
        core = _run_case("case_32_emp_real_sql")
        out = tmp_path_factory.mktemp("mapping32")
        path = export_mapping(core["knowledge"], core["engine_ctx"], str(out))
        wb = openpyxl.load_workbook(path)
        yield wb
        wb.close()

    def test_driver_row_remark_main(self, case32):
        """最外层驱动表（eb_account_t）注册为主表行：备注='主表'，行级限制在条件列。"""
        ws = case32["实体级mapping"]
        main_rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
                     if r[11] == "主表"]
        assert main_rows, "应有备注='主表' 的驱动表行"
        assert main_rows[0][4] == "eb_account_t"
        cond = main_rows[0][10] or ""
        assert "限制" in cond, "驱动表行应携带行级限制段（信息不丢）"

    def test_same_table_multi_alias_separate_rows(self, case32):
        """同物理表两处参与（b 包装层 + t2 块内层）= 两行各带各的语义（2026-11-04
        口径：按表×别名分行，嵌套维持 SPEC 既有逻辑）。"""
        ws = case32["实体级mapping"]
        rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
                if str(r[4]) == "eb_account_tag_t"]
        assert len(rows) == 2, f"b、t2 应各一行，实际 {len(rows)} 行"
        aliases = {str(r[5]) for r in rows}
        assert aliases == {"b", "t2"}, f"别名应为 b/t2 各一行，实际 {aliases}"
        b_row = next(r for r in rows if r[5] == "b")
        assert "LEFT JOIN" in str(b_row[10] or ""), "b 行应带包装层 JOIN 条件"
        t2_row = next(r for r in rows if r[5] == "t2")
        assert "【b 内】" in str(t2_row[10] or ""), "t2 行应带块内标记"

    def test_attribute_alias_closed_with_entity(self, case32):
        """闭合校验：属性级（源表, 别名）⊆ 实体级注册行——别名不得凭空出现。"""
        ws1, ws2 = case32["实体级mapping"], case32["属性级mapping"]
        ent = {(str(r[4]).lower(), str(r[5] or "").lower())
               for r in ws1.iter_rows(min_row=2, values_only=True) if r[4]}
        for r in ws2.iter_rows(min_row=2, values_only=True):
            if not r[3] or not r[4]:
                continue  # 赋值/常量行无源表
            key = (str(r[3]).split("、")[0].strip().lower(),
                   str(r[4]).split("、")[0].strip().lower())
            assert key in ent, f"属性级别名断链: {key} 不在实体级 {sorted(ent)}"

    def test_direct_copy_expr_empty(self, case32):
        """直接复制行映射表达式留空（2026-11-04 修订：不写 '-'）。"""
        ws = case32["属性级mapping"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        direct = [r for r in rows if r[8] == "直接复制"]
        assert direct, "应存在直接复制行"
        assert all(r[9] in (None, "") for r in direct), \
            f"直接复制表达式应留空，实际 {[r[9] for r in direct]}"

    def test_transform_expr_present(self, case32):
        """数据加工行映射表达式 = 展开态（含聚合/分组说明段）。"""
        ws = case32["属性级mapping"]
        rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
                if r[8] == "数据加工"]
        assert rows, "case_32 应有数据加工行"
        assert any("SUM" in str(r[9]) for r in rows), \
            f"展开态应含 SUM 聚合，实际 {[str(r[9])[:40] for r in rows]}"


class TestMultiAliasJoin:
    """同表多别名关联（2026-11-04 结构优化）：实体级按别名分行 + 字段别名闭合。"""

    def test_triple_join_per_alias_rows(self, tmp_path):
        """a 以三个条件关联 b（t1/t2/t3）：实体级 4 行、属性级别名各归各。"""
        from engine import RawRule, analyze_pipeline, detect_dialect
        sql = """
SELECT a.id, a.name,
       t1.flag AS flag1, t2.flag AS flag2, t3.amt AS amt3
FROM ods.a_table a
LEFT JOIN dim.b_table t1 ON a.id = t1.id AND t1.tp = '1'
LEFT JOIN dim.b_table t2 ON a.id = t2.id AND t2.tp = '2'
LEFT JOIN dim.b_table t3 ON a.pid = t3.pid
"""
        rules = [RawRule(rule_code="R1", rule_name="s1", rule_type=1, exec_sequence=1,
                         target_schema="dws", target_table="dwb_x_d", delete_mode="1",
                         query_sql=sql)]
        engine_ctx = {}
        knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                        source_file="x.xlsx", context_out=engine_ctx)
        # SPEC Sheet4：字段别名 = 实际引用（引擎链节点 alias 带出）
        from spec_export import build_spec_model
        model = build_spec_model(knowledge, engine_ctx)
        alias_by_field = {r[10]: r[3] for r in model["sheet4"]}  # r[10]=目标字段
        assert alias_by_field["flag1"] == "t1", alias_by_field
        assert alias_by_field["flag2"] == "t2", alias_by_field
        assert alias_by_field["amt3"] == "t3", alias_by_field

        # mapping：实体级 t1/t2/t3 三行 + 属性级闭合
        from mapping_export import build_mapping_model
        m = build_mapping_model(knowledge, engine_ctx)
        b_rows = [e for e in m["entity"] if e["table"] == "b_table"]
        assert [e["alias"] for e in b_rows] == ["t1", "t2", "t3"]
        assert all("LEFT JOIN" in e["on_condition"] for e in b_rows)
        attr = {a["target_field"]: a["alias"] for a in m["attribute"]}
        assert attr["flag2"] == "t2" and attr["amt3"] == "t3"
        ent = {(e["table"].lower(), (e["alias"] or "").lower()) for e in m["entity"]}
        for a in m["attribute"]:
            key = ((a["table"] or "").split("、")[0].lower(),
                   (a["alias"] or "").split("、")[0].lower())
            assert key in ent, f"属性级别名断链: {key}"


class TestDdlMetadata:
    """DDL 元数据填充（2026-11-04 三点修订）：目标字段类型 + 主键/分布键备注。"""

    def test_target_type_and_distribute_key(self, tmp_path):
        """有目标表 DDL 时：目标字段类型逐行填充；DISTRIBUTE BY 字段备注='分布键'。"""
        import shutil
        case_dir = tmp_path / "case"
        shutil.copytree(FIXTURES / "case_32_emp_real_sql", case_dir)
        (case_dir / "target_ddl.sql").write_text(
            "CREATE TABLE dws.dwb_eb_account_d (\n"
            "    id numeric(20) NOT NULL,\n"
            "    account_number nvarchar(50),\n"
            "    is_datacom_flag nvarchar(1),\n"
            "    crt_cycle_id bigint\n"
            ")\n"
            "DISTRIBUTE BY (id);\n"
            "COMMENT ON COLUMN dws.dwb_eb_account_d.id IS '账户ID';\n",
            encoding="utf-8")
        from pipeline_core import run_from_dir
        core = run_from_dir(str(case_dir), include_dq=False,
                            channel="test", entry_title="")
        out = tmp_path / "out"
        export_mapping(core["knowledge"], core["engine_ctx"], str(out))
        import openpyxl
        wb = openpyxl.load_workbook(out / "mapping.xlsx")
        try:
            ws = wb["属性级mapping"]
            rows = {str(r[10]): r for r in ws.iter_rows(min_row=2, values_only=True)
                    if r[10]}
            assert rows["id"][12] == "numeric(20)", \
                f"id 目标类型应填充，实际 {rows['id'][12]}"
            assert rows["account_number"][12] == "nvarchar(50)"
            assert rows["id"][13] == "分布键", \
                f"DISTRIBUTE BY 字段应标'分布键'，实际 {rows['id'][13]}"
            assert rows["account_number"][13] in (None, ""), \
                "非分布键字段备注应留空"
            # 直接复制表达式留空
            assert rows["id"][9] in (None, "")
        finally:
            wb.close()

    def test_pk_and_distribute_both_marked(self, tmp_path):
        """pk + 分布键并存 → 备注顿号合并。"""
        meta = {"fields": {
            "a": {"is_pk": True, "is_distribute_key": True},
            "b": {"is_pk": True, "is_distribute_key": False},
            "c": {"is_pk": False, "is_distribute_key": True},
        }}
        from mapping_export import _field_marks
        marks = _field_marks(meta)
        assert marks == {"a": "主键、分布键", "b": "主键", "c": "分布键"}


class TestFingerprintGate:
    """指纹门：模板漂移必须显式红（不静默降级）。"""

    def test_bad_template_raises(self, tmp_path):
        import shutil
        bad = tmp_path / "bad_template.xlsx"
        shutil.copy(_template_path(), bad)
        wb = openpyxl.load_workbook(bad)
        wb["实体级mapping"]["C1"] = "漂移列"
        wb.save(bad)
        from mapping_export import render_mapping_xlsx, build_mapping_model
        core = _run_case("case_31_real_nested_chain")
        model = build_mapping_model(core["knowledge"], core["engine_ctx"])
        with pytest.raises(Exception, match="指纹"):
            render_mapping_xlsx(model, tmp_path / "out.xlsx", template_path=bad)


def _template_path() -> Path:
    return (Path(__file__).resolve().parent.parent
            / "dws-pipeline-analyzer" / "templates" / "mapping模板.xlsx")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
