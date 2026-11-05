"""ts 草稿投影测试（ts_export，v2 波 B——ViewContext JSON 渲染 + facts --format ts_draft）。

覆盖：
1. 拓扑派生（中间表 reads/produces_for/target_role）
2. 三桶分类（direct 带 AS 逐字 / value→assign / 加工→processed+refs）
3. 物理属性映射（distribute/partition/storage/to_group；无 DDL 默认）
4. load_mode 词表（词表外 kind → ""+gap；merge_into 无条件 → fail loud）
5. 调度还原落位（upstream 未匹配不进本体、evidence 随行）
6. e2e：真实制品 facts --format ts_draft（进门验 + 定位行 + self_check）

运行:
    pytest tests/test_ts_export.py -v
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
DOCS = PROJECT_ROOT / "docs"
sys.path.insert(0, str(ANALYZER_REF))

from ts_export import (LOAD_MODE_BY_KIND, TsDraftError, export_ts_draft,
                       render_ts_draft, validate_ts_draft)

ET_XLSX = DOCS / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_F.xlsx"
GOLDEN = PROJECT_ROOT / "tests" / "golden" / "snapshots"


def _draft(case: str) -> dict:
    return json.loads((GOLDEN / f"{case}.json").read_text(encoding="utf-8"))["knowledge_draft"]


def _render(case: str, engine_ctx=None, **kw):
    d = _draft(case)
    ectx = engine_ctx or {"final_target_schema": "dws",
                          "final_target_table": d["meta"]["target_table"],
                          "table_catalog": {}}
    return render_ts_draft(d, ectx, **kw)


# ── 1-2. 拓扑与三桶 ───────────────────────────────────────────

class TestTopologyAndBuckets:
    def test_multi_step_intermediate_chain(self):
        ts = _render("case_07_multi_step_serial")
        a, b = ts["rules"]["UR007A"], ts["rules"]["UR007B"]
        assert a["target_role"] == "intermediate" and b["target_role"] == "target"
        assert a["produces_for"] == ["UR007B"] and b["reads"] == ["dws.dwb_order_base_f"]
        assert a["target_table"] == "dws.dwb_order_base_f"  # 带 schema
        # 中间表进 tables（type=intermediate），源表只在 meta.source_tables
        assert ts["tables"]["dwb_order_base_f"]["type"] == "intermediate"
        src = {s["table"] for s in ts["meta"]["source_tables"]}
        assert "orders" in src and "dwb_order_base_f" not in src

    def test_direct_bucket_with_as(self):
        ts = _render("case_07_multi_step_serial")
        direct = ts["rules"]["UR007A"]["fields"]["direct"]
        assert "o.order_id AS order_id" in direct  # 逐字 alias.col AS target（同名也带）

    def test_aggregate_into_processed_with_refs(self):
        ts = _render("case_03_cte_nested")  # 含 CTE 聚合 total
        processed = ts["rules"]["UR003"]["fields"]["processed"]
        agg = next(p for p in processed if p["target"] == "total")
        assert agg["refs"] == ["agg.total"]  # CTE 内引用形态逐字
        assert agg["logic"]

    def test_value_bucket_assign(self):
        d = _draft("case_01_minimal")
        # 注入一个常量赋值字段（transform_type=value）
        d["field_mappings"]["fields"].append({
            "target_field": "flag_col", "producing_step": d["topology"]["steps"][0]["step_id"],
            "rule_code": d["topology"]["steps"][0]["rule_code"], "transform_type": "value",
            "lineage": [{"step": "step_1", "source_table": "", "source_field": "",
                         "transform": "value", "raw_sql": "'N'"}], "physical_source": [],
        })
        ts = render_ts_draft(d, {"final_target_schema": "dws",
                                 "final_target_table": d["meta"]["target_table"]})
        r0 = next(iter(ts["rules"].values()))
        assert {"target": "flag_col", "value": "'N'"} in r0["fields"]["assign"]

    def test_query_sql_verbatim(self):
        """v2.1：rules[].query_sql 逐字必填——与 source.raw_sql（解析原文附录）字节一致。"""
        d = _draft("case_03_cte_nested")
        ts = render_ts_draft(d, {"final_target_schema": "dws",
                                 "final_target_table": d["meta"]["target_table"]})
        verbatim = next(r["sql"] for r in d["source"]["raw_sql"] if r["rule_code"] == "UR003")
        assert ts["rules"]["UR003"]["query_sql"] == verbatim
        assert ts["version"] == "2.1"
        assert validate_ts_draft(ts) == []  # v2.1 schema required 含 query_sql

    def test_field_targets_union_and_count(self):
        ts = _render("case_07_multi_step_serial")
        r = ts["rules"]["UR007A"]
        n = len(r["fields"]["direct"]) + len(r["fields"]["processed"]) + len(r["fields"]["assign"])
        assert r["field_count"] == n
        assert set(r["field_targets"]) >= {"order_id"}

    def test_field_targets_follow_select_order(self):
        """field_targets 与 query_sql 的 SELECT 列序同源同序（不排序——2026-11-04 实证：
        sorted 后字母序与逐字 SQL 字段顺序矛盾，同一源两种说法）。"""
        ts = _render("case_05_window")  # 4 列跨 direct/processed 两桶，字母序≠SELECT 序
        r = ts["rules"]["UR005"]
        assert r["field_targets"] == ["user_id", "login_date", "rn", "prev_login"]


# ── 3. 物理属性 ───────────────────────────────────────────────

class TestPhysicalProps:
    def _ctx(self, props, dist=False):
        entry = {"cust_id": {"type": "INT", "comment": "", "is_distribute_key": dist}}
        if props:
            entry["__table_props__"] = props
        return {"final_target_schema": "dws", "final_target_table": "dwb_nested_f",
                "table_catalog": {"dws.dwb_nested_f": entry}}

    def test_full_props(self):
        ts = _render("case_03_cte_nested", engine_ctx=self._ctx({
            "distribute_by": "HASH (cust_id)", "distribute_columns": ["cust_id"],
            "partition_by": "PARTITION BY RANGE (dt)",
            "with_options": "orientation=column, compression=low", "to_group": "LC_DW1"}, dist=True))
        t = ts["tables"]["dwb_nested_f"]
        assert t["distribution_key"] == ["cust_id"] and t["distribute_type"] == "HASH"
        assert t["partition"] == "PARTITION BY RANGE (dt)"
        assert t["storage"] == "column" and t["logical_group"] == "LC_DW1"
        # tables.fields 三键纯元数据（两视图分界）
        assert set(t["fields"][0]) == {"target_field", "field_type", "field_comment"}

    def test_no_ddl_defaults(self):
        ts = _render("case_03_cte_nested")
        t = ts["tables"]["dwb_nested_f"]
        assert t["distribute_type"] == "ROUNDROBIN"  # 无子句无分布列
        assert t["storage"] == "" and t["partition"] == "" and t["logical_group"] == ""


# ── 4. load_mode 词表与语义不变量 ──────────────────────────────

class TestLoadModeAndValidation:
    def test_vocab_out_of_map_yields_empty(self):
        """词表外 kind（dm=9 交换分区 → exchange_partition）→ load_mode 空串，不硬映射；
        原文 kind 随 _reverse.write_plan 证据随行。"""
        assert "exchange_partition" not in LOAD_MODE_BY_KIND
        d = _draft("case_03_cte_nested")
        d["topology"]["steps"][0]["delete_mode"] = "9"
        d["topology"]["steps"][0]["exchange_temp_table"] = "dws.tmp_x"
        ts = render_ts_draft(d, {"final_target_schema": "dws",
                                 "final_target_table": d["meta"]["target_table"]})
        assert ts["rules"]["UR003"]["load_mode"] == ""
        assert ts["_reverse"]["write_plan"][0]["kind"] == "exchange_partition"

    def test_merge_without_condition_fails_loud(self, tmp_path):
        d = _draft("case_03_cte_nested")
        s = d["topology"]["steps"][0]
        s["delete_mode"] = "6"          # MERGE INTO
        s["delete_condition"] = ""      # 无 ON 条件
        out = tmp_path / "ts.json"
        with pytest.raises(TsDraftError, match="write_condition"):
            export_ts_draft(d, {"final_target_schema": "dws",
                                "final_target_table": d["meta"]["target_table"]}, out)
        assert not out.exists()         # 校验不过不落盘

    def test_product_passes_vendor_schema(self):
        ts = _render("case_07_multi_step_serial", provenance=[{"type": "other", "path": "x"}])
        assert validate_ts_draft(ts) == []


# ── 5. 调度还原落位 ───────────────────────────────────────────

class TestSchedulePlacement:
    def test_unresolved_upstream_not_in_body(self):
        sched = {"schedule_type": "daily", "cron": "0 30 2 * * ?",
                 "tasks": {"f": {"task_name": "T_F", "job_name": "Pjob_t", "cron": "0 30 2 * * ?",
                                 "upstream": [{"task": "", "table": "dep_f", "dep_type": "宽依赖",
                                               "project": "", "group": "", "job": "Pjob_dep_f",
                                               "resolved": False}],
                                 "project_name": "P", "task_group": "G", "depends_prev_cycle": "是"}},
                 "lts_params": [{"lts_var": "V_CYCLE_ID", "etl_param": "P_CYCLE_ID", "desc": ""}],
                 "exec_params": {"P_CYCLE_ID": {"value_type": "", "desc": "", "default_value": "1"}},
                 "evidence": [{"task_name": "T_F", "cron": "0 30 2 * * ?"}]}
        ts = _render("case_07_multi_step_serial", schedule_rec=sched)
        sch = ts["meta"]["schedule"]
        assert sch["schedule_type"] == "daily" and sch["cron"] == "0 30 2 * * ?"
        assert sch["tasks"]["f"]["upstream"] == []          # 未匹配不进本体
        assert sch["upstream_added"] == []
        assert sch["lts_params"][0]["etl_param"] == "P_CYCLE_ID"
        assert sch["exec_params"]["P_CYCLE_ID"]["default_value"] == {"type": "static", "expr": "1"}
        assert ts["_reverse"]["schedule_evidence"][0]["task_name"] == "T_F"  # 证据随行
        # depends_prev_cycle（自依赖开关）不进本体 tasks（evidence 已有原文）
        assert "depends_prev_cycle" not in sch["tasks"]["f"]
        # 未匹配上游 → noise warning
        assert any(w["type"] == "lts_upstream_unresolved" for w in ts["_reverse"]["warnings"])


# ── 6. e2e：facts --format ts_draft（真实制品）────────────────

class TestFactsFormatE2E:
    def _run(self, argv):
        import io, contextlib
        from facts import main
        old_argv = sys.argv
        sys.argv = ["facts.py"] + argv
        buf, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                main()
            return buf.getvalue(), err.getvalue(), 0
        except SystemExit as e:
            return buf.getvalue(), err.getvalue(), e.code
        finally:
            sys.argv = old_argv

    def test_real_artifacts_ts_draft(self, tmp_path):
        bag = tmp_path / "bag"
        bag.mkdir()
        shutil.copy(ET_XLSX, bag / "et.xlsx")
        (bag / "ddl.sql").write_text(
            "CREATE TABLE dws.dwb_cust_asset_f (cust_id INT NOT NULL)\n"
            "DISTRIBUTE BY HASH (cust_id)\nTO GROUP LC_DW1;\n"
            "CREATE OR REPLACE VIEW dws.dwb_cust_asset_i AS SELECT * FROM dws.dwb_cust_asset_f;",
            encoding="utf-8")
        out = tmp_path / "o" / "ts_draft.json"
        stdout, _, code = self._run(["--dir", str(bag), "--output", str(out)])
        assert code == 0
        assert any(l.startswith("ts_draft: ") for l in stdout.splitlines())
        assert any(l.startswith("self_check: ") for l in stdout.splitlines())
        d = json.loads(out.read_text(encoding="utf-8"))
        assert d["version"] == "2.1" and d["spec_type"] == "ts"
        assert all(r["query_sql"] for r in d["rules"].values())   # 四规则逐字 SQL 齐
        assert d["meta"]["target"]["i_view"]["table"] == "dwb_cust_asset_i"   # CREATE VIEW 识别
        t = d["tables"]["dwb_cust_asset_f"]
        assert t["distribution_key"] == ["cust_id"] and t["logical_group"] == "LC_DW1"
        assert len(d["rules"]) == 4                                          # 真实资产四规则
        assert validate_ts_draft(d) == []                                    # 进门验复验
        assert any(l.startswith("self_check: ") for l in stdout.splitlines())

# ── 7. 保真契约：TargetFields 原貌 + 别名大小写（2026-11-05 用户实报）────

class TestFidelity:
    def test_target_fields_by_rule_verbatim(self):
        """_reverse.target_fields_by_rule = TargetFields 制品原序原量（与 field_targets
        的 SELECT 序是两个独立事实——术加制品里二者顺序/数量本就可能不一致）。
        case_13 夹具：TargetFields=3 条乱序（ProductID/PRODUCT_NAME/extra_field），
        SELECT=2 列（product_id/product_name）。"""
        ts = _render("case_13_field_mismatch")
        tf = ts["_reverse"]["target_fields_by_rule"]["UR013"]
        assert [t["target_field"] for t in tf] == ["ProductID", "PRODUCT_NAME", "extra_field"]
        assert ts["rules"]["UR013"]["field_targets"] == \
            ["product_id", "product_name", "ProductID", "extra_field"]  # SELECT 序+尾部披露

    def test_alias_case_verbatim_e2e(self, tmp_path):
        """e2e：大写别名/表名（A/C、_DI）→ ts 草稿 source_tables/joins 与逐字
        query_sql 同形态（解析层存储保原文，不再 lower）。"""
        import openpyxl
        bag = tmp_path / "bag"
        bag.mkdir()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "RULE"
        ws.append(["规则编码", "规则中文名称", "执行序列", "规则类型", "删除模式",
                   "删除条件", "目标Schema", "目标表", "(生成的）查询语句1"])
        ws.append(["R0001", "测试", "1", "1", "1", "", "dws", "dwb_test_f",
                   "SELECT A.cust_id, C.cust_name FROM ods.ods_acct_balance_DI A "
                   "JOIN ods.ods_cust_info_DI C ON A.cust_id = C.cust_id"])
        ws_tf = wb.create_sheet("TargetFields")
        ws_tf.append(["规则编码", "目标字段名称", "来源字段名称"])
        ws_tf.append(["R0001", "cust_name", "C.cust_name"])
        ws_tf.append(["R0001", "cust_id", "A.cust_id"])
        wb.save(bag / "et.xlsx")
        out = tmp_path / "ts.json"
        stdout, _, code = TestFactsFormatE2E()._run(["--dir", str(bag), "--output", str(out)])
        assert code == 0, stdout
        d = json.loads(out.read_text(encoding="utf-8"))
        r = d["rules"]["R0001"]
        assert r["source_tables"] == [
            {"schema": "ods", "table": "ods_acct_balance_DI", "alias": "A"},
            {"schema": "ods", "table": "ods_cust_info_DI", "alias": "C"},
        ]
        assert r["joins"][0]["alias"] == "C"
        assert "A.cust_id = C.cust_id" in r["joins"][0]["condition"]  # 同条 join 一个说法
        # TargetFields 原貌（乱序 2 条）独立随行
        assert [t["target_field"] for t in d["_reverse"]["target_fields_by_rule"]["R0001"]] \
            == ["cust_name", "cust_id"]
