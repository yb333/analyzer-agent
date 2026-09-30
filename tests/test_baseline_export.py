"""baseline_v1 契约导出测试（--export baseline_v1）。

覆盖：
- write_plan 全模式矩阵（dm 1-9 / 视图 / 未知 dm / 条件缺失告警 / MERGE ON 抽取）
- 投影单测（显式空、dq 裁剪排序、joins 子查询过滤、tables 字段并集、lineage 裁剪、warnings 合流）
- e2e yml（demo 两步链）：schema+语义自校验、provenance、schedule、逐字保真
- e2e xlsx：schedule/dq_rules 显式 null（验收 #3）
- e2e dm=6：merge_on 逐字 + write_plan；取不到 ON 条件时 fail loud 不落盘（验收 #4）
- v1.0 形态向后兼容（无 write_plan 的产物过 v1.1 schema）

运行:
    pytest tests/test_baseline_export.py -v
"""

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES / "analyzer"))
sys.path.insert(0, str(FIXTURES / "baseline"))

from baseline_export import (  # noqa: E402
    CONTRACT_VERSION,
    BaselineExportError,
    build_write_plan,
    export_baseline_v1,
    project_baseline_v1,
    validate_baseline_v1,
)
from baseline_export import DEFAULT_SCHEMA_PATH  # noqa: E402


def _load_schema():
    return json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _run_analyzer_main(argv):
    """patch argv 调 analyzer.main()，捕获 stdout（不落盘场景捕获 SystemExit）。"""
    from analyzer import main
    old_argv = sys.argv
    sys.argv = ["analyzer.py"] + argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            main()
        return buf.getvalue()
    finally:
        sys.argv = old_argv


# ═════════════════════════════════════════════════════════════════
# write_plan 单测
# ═════════════════════════════════════════════════════════════════

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


class TestProjection:
    def test_explicit_null_and_trim(self):
        out = project_baseline_v1(_mini_draft(), {
            "rule_group_en": "DWB_X", "platform_generation": "1.0",
            "source_type": "repo_yml", "provenance": [],
            "final_target_schema": "dws", "final_target_table": "dwb_x",
            "table_catalog": {},
        })
        assert out["version"] == CONTRACT_VERSION
        assert out["schedule"] is None          # 显式空：无调度
        assert out["dq_rules"] is None          # 显式空：无 DQ
        assert out["asset"]["source_type"] == "repo_yml"   # ctx 覆盖硬编码 meta
        assert out["asset"]["platform_generation"] == "1.0"
        rule = out["rules"][0]
        assert rule["merge_on"] == "t.id = s.id"
        assert rule["write_plan"]["kind"] == "merge_upsert"
        assert rule["query_sql"] == "SELECT s.id FROM ods.s"

    def test_joins_filter_subquery_and_cte(self):
        out = project_baseline_v1(_mini_draft(), {"provenance": []})
        joins = out["rules"][0]["joins"]
        assert [j["source_table"] for j in joins] == ["ods.s"]  # 假名/CTE 不进契约

    def test_lineage_trim_physical_sources(self):
        out = project_baseline_v1(_mini_draft(), {"provenance": []})
        assert out["lineage"][0]["physical_sources"] == [{"table": "ods.s", "field": "id"}]

    def test_warnings_merge_three_sources(self):
        out = project_baseline_v1(_mini_draft(), {"provenance": []})
        types = [w["type"] for w in out["warnings"]]
        assert "field_name_mismatch" in types
        assert "data_quality" in types

    def test_tables_fields_union_catalog_and_lineage(self):
        draft = _mini_draft()
        out = project_baseline_v1(draft, {
            "provenance": [],
            "final_target_schema": "dws", "final_target_table": "dwb_x",
            "table_catalog": {"dws.dwb_x": {"order_id": {"type": "VARCHAR(64)", "comment": "订单ID"},
                                             "__table_comment__": "表注释"}},
        })
        dwb = next(t for t in out["tables"] if t["name"] == "dwb_x")
        # DDL 类型事实 + 血缘字段名并集（本例同名字段，只剩 DDL 版本）
        assert dwb["fields"] == [{"name": "order_id", "type": "VARCHAR(64)", "comment": "订单ID"}]

    def test_export_fail_loud_no_file(self, tmp_path):
        """校验不过（dm=6 无 merge_on）→ 抛错且不落盘。"""
        draft = _mini_draft()
        draft["topology"]["steps"][0]["delete_condition"] = ""
        with pytest.raises(BaselineExportError):
            export_baseline_v1(draft, {"provenance": [{"type": "other", "path": "x"}]},
                               tmp_path)
        assert not (tmp_path / "baseline_v1.json").exists()

    def test_v10_shape_backward_compatible(self):
        """v1.0 形态（无 write_plan）通过 v1.1 权威校验——纯增量承诺。"""
        v10_product = {
            "version": "1.0",
            "asset": {"schema": "dws", "table": "t", "rule_group_code": "", "rule_group_en": "",
                      "dialect": "dws", "source_type": "repo_yml", "analysis_time": "x",
                      "platform_generation": "1.0"},
            "provenance": [{"type": "rule_yml", "path": "a.yml"}],
            "rules": [{"rule_code": "R1", "exec_sequence": 1, "scenario_id": "",
                       "target_schema": "dws", "target_table": "t", "delete_mode": "1",
                       "delete_condition": "", "query_sql": "SELECT 1", "source_tables": []}],
            "tables": [{"schema": "dws", "name": "t", "fields": []}],
            "lineage": [],
            "schedule": None, "dq_rules": None,
            "load_strategy": {}, "patterns": [], "warnings": [],
        }
        assert validate_baseline_v1(v10_product, _load_schema()) == []


# ═════════════════════════════════════════════════════════════════
# e2e：yml 两步链 demo（验收 #1/#2）
# ═════════════════════════════════════════════════════════════════

class TestE2ERepoYml:
    @pytest.fixture(scope="class")
    def exported(self, tmp_path_factory):
        from _build_repo import build_mock_repo
        repo = build_mock_repo(tmp_path_factory.mktemp("repo") / "repo")
        out_base = tmp_path_factory.mktemp("out")
        _run_analyzer_main(["--input", str(repo["group_dir"]),
                            "--output", str(out_base), "--export", "baseline_v1"])
        out_dir = out_base / "DWB_TRADE_ORDER_D"
        return json.loads((out_dir / "baseline_v1.json").read_text(encoding="utf-8"))

    def test_passes_authoritative_schema(self, exported):
        assert validate_baseline_v1(exported, _load_schema()) == []
        assert exported["version"] == "1.1"

    def test_provenance_types(self, exported):
        types = {p["type"] for p in exported["provenance"]}
        assert {"rule_yml", "ddl", "lts", "dq"} <= types
        # LTS 去重（f/i/all_tasks 双计不重复出现）
        lts = [p["path"] for p in exported["provenance"] if p["type"] == "lts"]
        assert len(lts) == len(set(lts))

    def test_schedule_present_trimmed(self, exported):
        assert exported["schedule"] is not None
        assert "all_tasks" not in exported["schedule"]
        assert "lts_file" not in json.dumps(exported["schedule"])
        assert exported["schedule"]["f_tasks"][0]["task_name"] == "TASK_TRADE_ORDER_F"

    def test_query_sql_byte_identical_to_yml(self, exported, tmp_path_factory):
        """验收 #2：query_sql 与源 yml 原文逐字节一致。"""
        from _build_repo import build_mock_repo
        repo = build_mock_repo(tmp_path_factory.mktemp("repo2") / "repo")
        import yaml as _yaml
        r1 = _yaml.safe_load((Path(repo["group_dir"]) / "R0001.yml").read_text(encoding="utf-8"))
        rule = next(r for r in exported["rules"] if r["rule_code"] == "R0001")
        assert rule["query_sql"] == r1["(生成的)查询语句"]

    def test_write_plan_kinds(self, exported):
        by_code = {r["rule_code"]: r for r in exported["rules"]}
        assert by_code["R0001"]["write_plan"]["kind"] == "full_truncate"
        assert by_code["R0002"]["write_plan"]["kind"] == "partition_truncate"

    def test_dq_sorted_and_trimmed(self, exported):
        assert [q["rule_number"] for q in exported["dq_rules"]] == ["DQ_000001", "DQ_000002"]
        assert set(exported["dq_rules"][0]) == {
            "rule_number", "rule_name", "rule_desc", "check_type",
            "alert_level", "sql", "target_table"}

    def test_tables_ddl_plus_lineage_union(self, exported):
        tmp = next(t for t in exported["tables"] if t["name"] == "tmp_trade_order")
        names = [f["name"] for f in tmp["fields"]]
        # DDL 只有 order_id，血缘补充 cust_id/amount（不丢信息，类型留空）
        assert names == ["order_id", "cust_id", "amount"]
        typed = next(f for f in tmp["fields"] if f["name"] == "order_id")
        assert typed["type"] == "VARCHAR(64)"
        untyped = next(f for f in tmp["fields"] if f["name"] == "cust_id")
        assert untyped["type"] == ""


# ═════════════════════════════════════════════════════════════════
# e2e：xlsx 模式（验收 #3：schedule 显式 null）
# ═════════════════════════════════════════════════════════════════

class TestE2EXlsx:
    def test_schedule_and_dq_explicit_null(self, tmp_path):
        from _build_xlsx import build_xlsx
        xlsx = build_xlsx(str(tmp_path / "pkg.xlsx"), rules=[
            {"rule_code": "R0001", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_x", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.t a",
             "rule_group_code": "GR_X", "rule_group_en": "DWB_X"},
        ])
        out_base = tmp_path / "out"
        _run_analyzer_main(["--input", xlsx, "--output", str(out_base),
                            "--export", "baseline_v1"])
        data = json.loads((out_base / "DWB_X" / "baseline_v1.json").read_text(encoding="utf-8"))
        assert "schedule" in data and data["schedule"] is None
        assert "dq_rules" in data and data["dq_rules"] is None
        assert validate_baseline_v1(data, _load_schema()) == []
        assert data["asset"]["source_type"] == "execution_tasks.xlsx"
        prov = [p for p in data["provenance"] if p["type"] == "execution_tasks"]
        assert prov and prov[0]["path"].endswith("pkg.xlsx")


# ═════════════════════════════════════════════════════════════════
# e2e：dm=6 增量合并（验收 #4；科普案例语料）
# ═════════════════════════════════════════════════════════════════

class TestE2EMerge:
    @pytest.fixture(scope="class")
    def merge_repo(self, tmp_path_factory):
        from _build_merge_repo import build_merge_repo
        return build_merge_repo(tmp_path_factory.mktemp("merge") / "repo")

    def test_merge_on_and_write_plan(self, merge_repo, tmp_path):
        from _build_merge_repo import MERGE_ON_CONDITION
        out_base = tmp_path / "out"
        _run_analyzer_main(["--input", str(merge_repo["group_dir"]),
                            "--output", str(out_base), "--export", "baseline_v1"])
        data = json.loads((out_base / "DWB_TRADE_MERGE_D" / "baseline_v1.json")
                          .read_text(encoding="utf-8"))
        assert validate_baseline_v1(data, _load_schema()) == []
        r2 = next(r for r in data["rules"] if r["rule_code"] == "R0002")
        assert r2["merge_on"] == MERGE_ON_CONDITION          # 逐字
        assert r2["delete_condition"] == MERGE_ON_CONDITION  # 原文锚点不动
        assert r2["write_plan"] == {
            "kind": "merge_upsert", "condition_role": "on_predicate",
            "condition_expr": MERGE_ON_CONDITION,
            "condition_columns": ["m.order_id", "t.order_id"],
            "condition_source": "delete_condition",
        }
        # R0001 双源 join 进契约
        r1 = next(r for r in data["rules"] if r["rule_code"] == "R0001")
        assert {j["source_table"] for j in r1["joins"]} == {
            "ods.ods_trade_order_di", "ods.ods_trade_pay_di"}

    def test_dm6_without_condition_fails_loud(self, merge_repo, tmp_path):
        """dm=6 且 SQL 非 MERGE（取不到 ON）：exit 1、stderr 可定位、不落盘。"""
        import yaml as _yaml
        group = Path(merge_repo["group_dir"])
        # 复制一份仓，去掉 R0002 的删除条件
        broken = tmp_path / "broken_repo"
        broken_group = broken / "g"
        broken_group.mkdir(parents=True)
        for yf in sorted(group.glob("*.yml")):
            data = _yaml.safe_load(yf.read_text(encoding="utf-8"))
            if yf.name == "R0002.yml":
                data["删除条件"] = ""
            (broken_group / yf.name).write_text(
                _yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        out_base = tmp_path / "out2"
        err = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["analyzer.py", "--input", str(broken_group),
                    "--output", str(out_base), "--export", "baseline_v1"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                with pytest.raises(SystemExit) as ei:
                    from analyzer import main
                    main()
        finally:
            sys.argv = old_argv
        assert ei.value.code == 1
        assert "R0002" in err.getvalue() and "merge_on" in err.getvalue()
        assert not (out_base / "DWB_TRADE_MERGE_D" / "baseline_v1.json").exists()
