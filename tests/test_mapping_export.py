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

    def test_same_physical_table_merged(self, case32):
        """同物理表两处参与（b 包装层 + t2 块内层）合并一行：别名顿号合并。"""
        ws = case32["实体级mapping"]
        tables = [str(r[4]) for r in ws.iter_rows(min_row=2, values_only=True)]
        assert tables.count("eb_account_tag_t") == 1, \
            f"同物理表应合并为一行，实际出现 {tables.count('eb_account_tag_t')} 次"
        row = next(r for r in ws.iter_rows(min_row=2, values_only=True)
                   if r[4] == "eb_account_tag_t")
        assert "b" in str(row[5]) and "t2" in str(row[5]), \
            f"别名应合并 b、t2，实际 {row[5]}"
        cond = str(row[10] or "")
        assert "【b 内】" in cond, "块内参与段应保留在条件列"

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
