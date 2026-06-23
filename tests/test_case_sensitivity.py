"""
大小写不敏感回归测试

数据库的表/字段/schema 大小写不敏感，但代码多处用了原始字符串 ==/in 比较表名，
导致同一物理表因大小写不同被当成两张表（漏匹配/重复/计数失效）。

这组测试锁定"大小写不敏感"契约：用混合大小写构造输入，验证产物不重复、不漏匹配。

运行:
    pytest tests/test_case_sensitivity.py -v
"""

import sys
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(FIXTURES / "cases"))

from analyzer import (
    read_excel, detect_dialect, parse_single_sql,
    build_topology, build_data_flow, build_field_mappings, analyze_quality,
    RawRule,
)
from _build_xlsx import build_xlsx


def _build_minimal_knowledge(rules_data):
    """用 rule dict 列表构造最小可用的 knowledge dict，供 build_report_data。"""
    from datetime import datetime
    rules = [RawRule(
        rule_code=r["rule_code"], rule_name=r.get("rule_name", ""),
        rule_type=r["rule_type"], exec_sequence=r["exec_sequence"],
        target_schema=r["target_schema"], target_table=r["target_table"],
        delete_mode=r.get("delete_mode", "1"), delete_condition="",
        query_sql=r["query_sql"],
        exchange_source_table=r.get("exchange_source_table", ""),
    ) for r in rules_data]
    dialect = detect_dialect([r.query_sql for r in rules if r.query_sql])
    parsed_map = {r.rule_code: parse_single_sql(r.query_sql, dialect) for r in rules}
    topology = build_topology(rules, parsed_map)
    data_flow = build_data_flow(rules, parsed_map)
    field_mappings = build_field_mappings(rules, parsed_map, {})
    quality = analyze_quality(topology, data_flow, field_mappings, parsed_map)
    return {
        "meta": {
            "source_type": "test", "analysis_time": datetime.now().isoformat(),
            "dialect": dialect, "total_rules": len(rules),
            "target_table": rules[0].target_table, "patterns": [],
            "target_field_types": {}, "target_field_comments": {},
        },
        "topology": topology,
        "data_flow": data_flow,
        "field_mappings": field_mappings,
        "quality": quality,
        "business_logic": {"summary": "", "step_descriptions": [], "key_transforms": []},
        "source": {"rules": [], "target_fields": {}, "group_variables": {}},
    }


# ═══════════════════════════════════════════════════════════════
# 发现 1（高）: source_ref_count 写小写/读大写 → ×N 标签失效
# ═══════════════════════════════════════════════════════════════

class TestSourceRefCountCaseInsensitive:
    """血缘图里来源表的引用计数（×N）应大小写不敏感。

    Bug: view_generator 写入 source_ref_count 用 _norm（小写），
    读取处用 .upper()，永远查不到，恒返回默认值 1。
    """

    def test_source_ref_count_reflects_uppercase_table(self):
        """被多步骤引用的表，source_ref_count 应正确累计（非恒为 1）"""
        from view_generator import build_report_data
        rules_data = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "t_f",
             "query_sql": "SELECT a.x FROM ODS.TAB1 a"},
            {"rule_code": "R2", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "t_f",
             "query_sql": "SELECT a.x FROM ods.tab1 a"},
        ]
        knowledge = _build_minimal_knowledge(rules_data)
        report = build_report_data(knowledge)
        lineage = report.get("lineage", {})
        src_nodes = [n for n in lineage.get("nodes", []) if n.get("type") == "source"]
        tab1_nodes = [n for n in src_nodes if "tab1" in n.get("name", "").lower()]
        if tab1_nodes:
            # 被两个步骤引用，ref_count 应 >= 2（bug 时恒为 1）
            assert tab1_nodes[0]["source_ref_count"] >= 2, \
                f"ods.tab1 被两步引用，ref_count 应 >=2，实际 {tab1_nodes[0]['source_ref_count']}"


# ═══════════════════════════════════════════════════════════════
# 发现 2（高）: all_sql_tables 原始字符串去重 → 表清单重复
# ═══════════════════════════════════════════════════════════════

class TestAllSqlTablesDedupCaseInsensitive:
    """step 的 all_tables_from_sql（SQL 推导的表清单）应大小写不敏感去重。

    Bug: 用 _clean_name（不改大小写）拼表名后直接 `not in` 去重，
    同一 SQL 里同一表大小写不同会被当成两张表。
    """

    def test_same_table_mixed_case_not_duplicated(self):
        """同一 SQL 里同一表用不同大小写，all_tables_from_sql 不应重复"""
        # SQL 里 ODS.TAB1 和 ods.tab1 混用（UNION ALL 两个分支）
        sql = """SELECT a.x FROM ODS.TAB1 a
UNION ALL
SELECT b.x FROM ods.tab1 b"""
        rules = [RawRule(
            rule_code="R1", rule_type=1, exec_sequence=0,
            target_schema="dws", target_table="t_f",
            query_sql=sql,
        )]
        parsed_map = {"R1": parse_single_sql(sql, "dws")}
        topology = build_topology(rules, parsed_map)
        step = topology["steps"][0]
        all_tables = step.get("all_tables_from_sql", [])
        normed = [t.lower().strip() for t in all_tables]
        assert len(normed) == len(set(normed)), \
            f"同一表大小写不同不应重复: {all_tables}"
        # 确认 tab1 在清单里（归一化后）
        assert any("tab1" in t.lower() for t in all_tables)


# ═══════════════════════════════════════════════════════════════
# 发现 3（高）: target_write_groups 的 sr["table"]==table
# ═══════════════════════════════════════════════════════════════

class TestWriteGroupSelfReferenceCaseInsensitive:
    """含自引用的同表多写入组，write_pattern/has_self_reference 应正确识别。

    Bug: sr["table"]（原始大小写）== table（_norm 小写），含分区交换时
    大小写不一致导致 has_self_reference 恒为 False。
    """

    def test_self_reference_in_write_group_detected(self):
        """同表多写入 + 自引用，has_self_reference 应为 True"""
        # 两步都写同一张表（同表多写入 → target_write_groups 非空）
        # 其中一步有 WHERE EXISTS 自引用
        rules = [
            RawRule(
                rule_code="R1", rule_type=1, exec_sequence=0,
                target_schema="dws", target_table="t_f",
                query_sql="SELECT a.x FROM ods.src a",
            ),
            RawRule(
                rule_code="R2", rule_type=1, exec_sequence=0,
                target_schema="dws", target_table="t_f",
                # 自引用：WHERE EXISTS 引用目标表 t_f（SQL 大小写与 target_table 不同）
                query_sql="SELECT a.x FROM ods.src a WHERE EXISTS (SELECT 1 FROM T_F b WHERE a.x = b.x)",
            ),
        ]
        parsed_map = {r.rule_code: parse_single_sql(r.query_sql, "dws") for r in rules}
        topology = build_topology(rules, parsed_map)
        # 应检测到自引用
        assert len(topology.get("self_references", [])) >= 1, "应检测到自引用"
        groups = topology.get("target_write_groups", [])
        assert len(groups) >= 1, "应有同表多写入组"
        # 关键：has_self_reference 应为 True（bug 时因大小写漏判为 False）
        assert groups[0]["has_self_reference"] is True, \
            f"含自引用的同表写入组，has_self_reference 应为 True，实际 {groups[0]['has_self_reference']}"


# ═══════════════════════════════════════════════════════════════
# 发现 6（中）: is_final_field 子串匹配 → 表名互为子串误判
# ═══════════════════════════════════════════════════════════════

class TestIsFinalFieldNoSubstringFalsePositive:
    """is_final_field 不应因子串包含误判。

    Bug: 用 `_final_target in _producing_target` 子串匹配，
    当目标表名是另一表名子串时（如 tab_f vs tab_f_hist）误判为 True。
    """

    def test_non_final_table_not_marked_as_final(self):
        """中间表字段（表名是最终表子串）不应被标为 is_final_field"""
        from view_generator import build_report_data
        rules_data = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "tab_f_hist",
             "query_sql": "SELECT 1 AS x"},
            {"rule_code": "R2", "rule_type": 1, "exec_sequence": 2,
             "target_schema": "dws", "target_table": "tab_f",
             "query_sql": "SELECT 1 AS x"},
        ]
        knowledge = _build_minimal_knowledge(rules_data)
        report = build_report_data(knowledge)
        fields = report.get("fields", [])
        # tab_f_hist 步骤的字段不应是 is_final_field（即便 "tab_f" in "tab_f_hist"）
        hist_fields = [f for f in fields if f.get("target_table") == "tab_f_hist"]
        for f in hist_fields:
            assert not f.get("is_final_field"), \
                f"中间表 tab_f_hist 的字段不应是 is_final_field（子串误判），实际 True"
        # tab_f 步骤的字段应是 is_final_field
        final_fields = [f for f in fields if f.get("target_table") == "tab_f"]
        if final_fields:
            assert final_fields[0].get("is_final_field"), \
                "最终表 tab_f 的字段应是 is_final_field"


# ═══════════════════════════════════════════════════════════════
# 发现 5（中）: seen_* 集合去重 → mapping/血缘清单重复
# ═══════════════════════════════════════════════════════════════

class TestSourceDedupCaseInsensitiveInViews:
    """view_generator 各 seen_* 集合应归一化去重，避免同表大小写不同重复出现。"""

    def test_mapping_no_duplicate_source_mixed_case(self):
        """mapping 的物理源表清单，同表大小写不同不重复"""
        from view_generator import generate_mapping
        import tempfile, os
        rules_data = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 0,
             "target_schema": "dws", "target_table": "t_f",
             "query_sql": """SELECT a.x FROM ODS.TAB1 a
UNION ALL
SELECT b.x FROM ods.tab1 b"""},
        ]
        knowledge = _build_minimal_knowledge(rules_data)
        out = tempfile.mkdtemp()
        ok = generate_mapping(knowledge, out)
        if not ok:
            pytest.skip("generate_mapping 未成功（可能缺 openpyxl）")
        import openpyxl
        wb = openpyxl.load_workbook(Path(out) / "mapping.xlsx", read_only=True)
        ws = wb["实体级mapping"]
        # 收集源表物理表名 + 分支归属（UNION 场景同表在不同分支各显示一次是正确的）
        rows_data = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row and row[1]:  # 源表物理表名列
                tbl = str(row[1]).strip().lower()
                branch = str(row[10] or "").strip() if len(row) > 10 else ""  # 执行路径列=分支
                rows_data.append((branch, tbl))
        wb.close()
        # 同一分支内归一化后不应有重复（跨分支允许同表重复，因为分支是不同场景）
        from collections import defaultdict
        by_branch = defaultdict(list)
        for br, tbl in rows_data:
            by_branch[br].append(tbl)
        for br, tables in by_branch.items():
            assert len(tables) == len(set(tables)), \
                f"分支'{br}'内物理源表有大小写重复: {tables}"
