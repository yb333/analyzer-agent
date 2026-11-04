"""write_plan 构建测试（原 test_baseline_export.TestBuildWritePlan——组件随 v1 投影
退役迁入 ts_export，测试跟随；口径不变）。

运行:
    pytest tests/test_write_plan.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from ts_export import WRITE_KIND_BY_MODE, build_write_plan


def _mini_draft():
    """最小 draft（write_plan 测试只需 topology/data_flow 结构）。"""
    return {}


class TestBuildWritePlan:
    """delete_mode → write_plan 全模式矩阵（platform_generation=1.0 权威映射）。"""

    @pytest.mark.parametrize("dm,kind,role", [
        ("1", "full_truncate", "none"),
        ("2", "append", "none"),
        ("3", "subpartition_truncate", "subpartition_expr"),
        ("4", "delete_by_condition", "where_predicate"),
        ("5", "partition_truncate", "partition_expr"),
        ("6", "merge_upsert", "on_predicate"),
        ("7", "rpt_item", "unclear"),
        ("8", "update_by_condition", "where_predicate"),
        ("9", "exchange_partition", "exchange_table"),
    ])
    def test_mode_matrix(self, dm, kind, role):
        wp, merge_on, warns = build_write_plan({"rule_code": "R1", "delete_mode": dm})
        assert wp["kind"] == kind
        assert wp["condition_role"] == role
        assert wp["condition_expr"] is None
        assert wp["condition_source"] == "none"

    def test_dm6_merge_on_from_condition_verbatim(self):
        """dm=6：删除条件列原文逐字进 merge_on / condition_expr。"""
        cond = "m.order_id = t.order_id"
        wp, merge_on, warns = build_write_plan(
            {"rule_code": "R2", "delete_mode": "6", "delete_condition": cond})
        assert merge_on == cond
        assert wp["condition_expr"] == cond
        assert wp["condition_source"] == "delete_condition"
        assert wp["condition_columns"] == ["m.order_id", "t.order_id"]

    def test_dm6_merge_on_from_sql(self):
        """dm=6 无删除条件但 SQL 是 MERGE：从 ON 解析（等价重排版，source=query_sql）。"""
        sql = ("MERGE INTO dws.tgt m USING dws.src t "
               "ON m.order_id = t.order_id "
               "WHEN MATCHED THEN UPDATE SET m.pay = t.pay")
        wp, merge_on, warns = build_write_plan({"rule_code": "R2", "delete_mode": "6"},
                                               query_sql=sql)
        assert merge_on is not None
        assert "order_id" in merge_on
        assert wp["condition_source"] == "query_sql"

    def test_dm6_no_condition_no_merge_sql(self):
        """dm=6 两处都取不到 ON：write_plan 不伪造（null），校验层负责 fail loud。"""
        wp, merge_on, warns = build_write_plan(
            {"rule_code": "R2", "delete_mode": "6", "delete_condition": ""},
            query_sql="SELECT 1 FROM dws.t")
        assert merge_on is None
        assert wp["condition_expr"] is None

    def test_dm6_non_merge_select_sql(self):
        """dm=6 + 普通 SELECT（平台运行期才拼 MERGE）：SQL 抽取不应报错，返回 None。"""
        wp, merge_on, warns = build_write_plan(
            {"rule_code": "R2", "delete_mode": "6"},
            query_sql="SELECT t.a FROM dws.t")
        assert merge_on is None

    def test_dm4_where_predicate(self):
        cond = "t.dt = '20260101' AND t.type = 'A'"
        wp, _, _ = build_write_plan(
            {"rule_code": "R3", "delete_mode": "4", "delete_condition": cond})
        assert wp["kind"] == "delete_by_condition"
        assert wp["condition_expr"] == cond
        assert wp["condition_columns"] == ["t.dt", "t.type"]

    def test_dm4_empty_condition_warns(self):
        """dm=4 无删除条件（影响范围不可界定）：warning，不伪造。"""
        wp, _, warns = build_write_plan({"rule_code": "R3", "delete_mode": "4"})
        assert wp["condition_expr"] is None
        assert any(w["type"] == "write_plan_gap" for w in warns)

    def test_dm5_partition_expr(self):
        wp, _, warns = build_write_plan(
            {"rule_code": "R4", "delete_mode": "5", "delete_condition": "P20260101"})
        assert wp["condition_expr"] == "P20260101"
        assert wp["condition_columns"] == []  # 非谓词形态无列引用

    def test_dm1_with_condition_is_unclear(self):
        """truncate 却带条件：如实携带 + 告警，不解释平台行为。"""
        wp, _, warns = build_write_plan(
            {"rule_code": "R5", "delete_mode": "1", "delete_condition": "x = 1"})
        assert wp["kind"] == "full_truncate"
        assert wp["condition_role"] == "unclear"
        assert wp["condition_expr"] == "x = 1"
        assert any(w["type"] == "write_plan_gap" for w in warns)

    def test_dm9_exchange(self):
        wp, _, _ = build_write_plan(
            {"rule_code": "R6", "delete_mode": "9", "exchange_temp_table": "dws.tmp_x"})
        assert wp["kind"] == "exchange_partition"
        assert wp["condition_expr"] == "dws.tmp_x"
        assert wp["condition_source"] == "exchange_config"

    def test_view_step(self):
        wp, _, _ = build_write_plan(
            {"rule_code": "V1", "delete_mode": "", "is_view_step": True})
        assert wp["kind"] == "view"

    def test_unknown_dm_warns(self):
        wp, _, warns = build_write_plan({"rule_code": "R7", "delete_mode": "42"})
        assert wp["kind"] == "unknown"
        assert any("不在平台代次映射表" in w["title"] for w in warns)


# ═════════════════════════════════════════════════════════════════
# 投影单测（合成 mini draft）
# ═════════════════════════════════════════════════════════════════

def _mini_draft():
    return {
        "meta": {
            "source_type": "execution_tasks.xlsx",
            "dialect": "dws",
            "analysis_time": "2026-08-18T00:00:00",
            "rule_group_code": "GR_X",
            "target_table": "dwb_x",
            "target_field_types": {"order_id": "VARCHAR(64)"},
            "target_field_comments": {"order_id": "订单ID"},
            "patterns": [{"label": "p1"}],
            "load_strategy": {"strategy": "full"},
        },
        "topology": {"steps": [{
            "step_id": "step_1", "rule_code": "R0001", "rule_name": "s1",
            "exec_sequence": 1, "scenario_id": "scenario_1", "is_common_step": False,
            "target_schema": "dws", "target_table": "dwb_x",
            "delete_mode": "6", "delete_condition": "t.id = s.id",
            "source_tables_from_sql": ["ods.s"], "exchange_temp_table": "",
            "is_view_step": False,
        }]},
        "data_flow": {
            "steps": [{
                "step_id": "step_1", "raw_sql": "SELECT s.id FROM ods.s",
                "joins": [
                    {"source_table": "ods.s", "alias": "s", "join_type": "FROM", "join_condition": ""},
                    {"source_table": "(subquery:xxx)", "alias": "", "join_type": "FROM_SUBQUERY", "join_condition": ""},
                ],
                "where_clause": "", "group_by": [], "ctes": [{"name": "c1"}],
            }],
            "tables": [
                {"schema": "ods", "name": "s", "role": "source", "layer": "ODS"},
                {"schema": "dws", "name": "dwb_x", "role": "target", "layer": "DWS"},
            ],
        },
        "field_mappings": {
            "fields": [{
                "target_field": "order_id", "producing_step": "step_1", "rule_code": "R0001",
                "transform_type": "direct",
                "lineage": [{"raw_sql": "s.id"}],
                "physical_source": [{"table": "ods.s", "field": "id", "step_id": "step_1",
                                     "alias": "s", "transform": "direct", "raw_sql": "s.id"}],
            }],
            "warnings": [{"type": "field_name_mismatch", "severity": "info", "title": "w1"}],
        },
        "quality": {"issues": [{"id": "ISS_001", "severity": "low", "category": "data_quality",
                                "title": "q1", "rule_code": "R0001"}]},
        "source": {"raw_sql": [{"step_id": "step_1", "rule_code": "R0001",
                                "sql": "SELECT s.id FROM ods.s"}]},
    }
