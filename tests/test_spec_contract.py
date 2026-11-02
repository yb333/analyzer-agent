"""SPEC 通道契约测试 —— 模板列头指纹（docs/17 §八）。

指纹基准三位一体：
    SPEC_TEMPLATE_HEADERS 常量（代码内基准）
    = dws-pipeline-analyzer/templates/需求SPEC模板.xlsx（打包基准）
    = docs/需求SPEC.xlsx（人侧源头基准）
任何一方漂移（模板换版 / 常量手误）→ 本文件红。

运行:
    pytest tests/test_spec_contract.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from spec_export import (DEFAULT_TEMPLATE, SPEC_TEMPLATE_HEADERS,
                         check_template_fingerprint)


class TestTemplateFingerprint:
    """列头指纹：模板 ↔ 常量 ↔ docs 基准三方一致。"""

    def test_package_template_matches_constant(self):
        """打包模板（templates/需求SPEC模板.xlsx）列头 = 常量基准。"""
        assert DEFAULT_TEMPLATE.exists(), f"打包模板缺失: {DEFAULT_TEMPLATE}"
        assert check_template_fingerprint(DEFAULT_TEMPLATE) == []

    def test_docs_baseline_matches_constant(self):
        """docs 人侧源头基准（docs/需求SPEC.xlsx）= 常量基准。

        模板换版流程（docs/17 §八）：新模板入库 docs → 复制进 templates/ →
        同步常量 → 本测试绿。跳过条件仅限 docs 基准不在仓内。
        """
        docs = PROJECT_ROOT / "docs" / "需求SPEC.xlsx"
        if not docs.exists():
            import pytest
            pytest.skip("docs/需求SPEC.xlsx 不在仓内")
        assert check_template_fingerprint(docs) == []

    def test_tampered_header_detected(self, tmp_path):
        """列头被改（哪怕一字/全角半角）→ 指纹差异清单非空。"""
        import openpyxl
        wb = openpyxl.load_workbook(DEFAULT_TEMPLATE)
        ws = wb["4需求字段"]
        ws.cell(row=1, column=9).value = "*资产表名（schema.表名）"  # 全角括号污染
        bad = tmp_path / "bad_template.xlsx"
        wb.save(str(bad))
        diffs = check_template_fingerprint(bad)
        assert any("4需求字段" in d for d in diffs), f"应检出列头篡改，实际 {diffs}"

    def test_header_constant_shapes(self):
        """常量形态自查：5 sheet、列数与模板一致（13/13/7）。"""
        assert list(SPEC_TEMPLATE_HEADERS.keys()) == [
            "1业务背景", "2数据源清单", "3表关联", "4需求字段", "5业务样例数据"]
        assert len(SPEC_TEMPLATE_HEADERS["2数据源清单"]) == 13
        assert len(SPEC_TEMPLATE_HEADERS["4需求字段"]) == 13
        assert len(SPEC_TEMPLATE_HEADERS["3表关联"]) == 7
        assert SPEC_TEMPLATE_HEADERS["5业务样例数据"] == []
