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

ET_XLSX = DOCS / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_D.xlsx"
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

    def test_field_targets_union_and_count(self):
        ts = _render("case_07_multi_step_serial")
        r = ts["rules"]["UR007A"]
        n = len(r["fields"]["direct"]) + len(r["fields"]["processed"]) + len(r["fields"]["assign"])
        assert r["field_count"] == n
        assert set(r["field_targets"]) >= {"order_id"}


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
            "CREATE TABLE dws.dwb_cust_asset_d (cust_id INT NOT NULL)\n"
            "DISTRIBUTE BY HASH (cust_id)\nTO GROUP LC_DW1;\n"
            "CREATE OR REPLACE VIEW dws.dwb_cust_asset_i AS SELECT * FROM dws.dwb_cust_asset_d;",
            encoding="utf-8")
        out = tmp_path / "o" / "ts_draft.json"
        stdout, _, code = self._run(["--dir", str(bag), "--output", str(out),
                                     "--format", "ts_draft"])
        assert code == 0
        assert any(l.startswith("ts_draft: ") for l in stdout.splitlines())
        assert any(l.startswith("self_check: ") for l in stdout.splitlines())
        d = json.loads(out.read_text(encoding="utf-8"))
        assert d["version"] == "2.0" and d["spec_type"] == "ts"
        assert d["meta"]["target"]["i_view"]["table"] == "dwb_cust_asset_i"   # CREATE VIEW 识别
        t = d["tables"]["dwb_cust_asset_d"]
        assert t["distribution_key"] == ["cust_id"] and t["logical_group"] == "LC_DW1"
        assert len(d["rules"]) == 4                                          # 真实资产四规则
        assert validate_ts_draft(d) == []                                    # 进门验复验
        # 同目录双格式：asset_facts 产物同样可产出（双支持期）
        out2 = tmp_path / "o" / "asset_facts.json"
        _, _, code2 = self._run(["--dir", str(bag), "--output", str(out2)])
        assert code2 == 0 and out2.exists()
