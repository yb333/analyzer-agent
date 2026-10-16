"""交付门测试（消费方 docs/specs/opt/12 v3.1：自检三态 + 严重度分级 + 一致性交叉）。

覆盖：
1. crosscheck 四类交叉（DQ 目标表 / DDL 目标缺失 / DDL 内部重复 / LTS 组码）
2. 严重度归一化（_SEVERITY_BY_TYPE 唯一权威）与 self_check 三态派生
3. facts e2e：结构级矛盾 → exit 0 + 产物照常落盘 + verdict=review（待审不是失败）

运行:
    pytest tests/test_crosscheck.py -v
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

from crosscheck import run_crosscheck

ET_XLSX = DOCS / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_D.xlsx"


def _draft(final_table="dwb_cust_asset_d", rgc="GR_CUST_ASSET"):
    return {"meta": {"target_table": final_table, "rule_group_code": rgc}}


# ── 1. crosscheck 交叉 ────────────────────────────────────────

class TestCrosscheck:
    def test_dq_target_mismatch(self):
        rules = [{"rule_number": "R1", "target_table": "other_table"},
                 {"rule_number": "R2", "target_table": "DWB_CUST_ASSET_D"}]
        out = run_crosscheck(_draft(), {}, {}, dq_rules=rules)
        mism = [w for w in out if w["type"] == "dq_target_mismatch"]
        assert len(mism) == 1  # 大小写不敏感：R2 匹配不算
        assert mism[0]["severity"] == "structural"
        assert "R1" in mism[0]["title"] and "other_table" in mism[0]["title"]

    def test_dq_clean_no_warning(self):
        rules = [{"rule_number": "R1", "target_table": "DWB_CUST_ASSET_D"}]
        out = run_crosscheck(_draft(), {}, {}, dq_rules=rules)
        assert not [w for w in out if w["type"] == "dq_target_mismatch"]

    def test_ddl_target_missing(self, tmp_path):
        (tmp_path / "other.sql").write_text(
            "CREATE TABLE unrelated_table (a INT);", encoding="utf-8")
        out = run_crosscheck(_draft(), {"table_catalog": {"dws.unrelated_table": {}}},
                             {"ddl": [str(tmp_path / "other.sql")]})
        miss = [w for w in out if w["type"] == "ddl_target_missing"]
        assert len(miss) == 1 and miss[0]["severity"] == "structural"

    def test_ddl_target_present_no_warning(self, tmp_path):
        (tmp_path / "t.sql").write_text(
            "CREATE TABLE dwb_cust_asset_d (cust_id INT);", encoding="utf-8")
        out = run_crosscheck(_draft(),
                             {"table_catalog": {"dws.dwb_cust_asset_d": {}},
                              "final_target_table": "dwb_cust_asset_d",
                              "final_target_schema": "dws"},
                             {"ddl": [str(tmp_path / "t.sql")]})
        assert not [w for w in out if w["type"] == "ddl_target_missing"]

    def test_ddl_duplicate_table(self, tmp_path):
        for name in ("a.sql", "b.sql"):
            (tmp_path / name).write_text(
                "CREATE TABLE dwb_x (a INT);", encoding="utf-8")
        out = run_crosscheck(_draft(), {}, {"ddl": [str(tmp_path / "a.sql"),
                                                    str(tmp_path / "b.sql")]})
        dup = [w for w in out if w["type"] == "ddl_duplicate_table"]
        assert len(dup) == 1 and "a.sql" in dup[0]["title"] and "b.sql" in dup[0]["title"]

    def test_lts_group_mismatch(self):
        sched = {"all_tasks": [
            {"task_name": "T_RIGHT_F", "group_code": "GR_CUST_ASSET"},
            {"task_name": "T_WRONG_F", "group_code": "GR_OTHER"},
        ]}
        out = run_crosscheck(_draft(), {}, {}, schedule_info=sched)
        mism = [w for w in out if w["type"] == "lts_group_mismatch"]
        assert len(mism) == 1 and "T_WRONG_F" in mism[0]["title"]
        assert mism[0]["severity"] == "structural"

    def test_lts_empty_group_code_tolerated(self):
        """任务无 V_GROUP_CODE（I 任务常态）不报警——空≠不符。"""
        sched = {"all_tasks": [{"task_name": "T_I", "group_code": ""}]}
        out = run_crosscheck(_draft(), {}, {}, schedule_info=sched)
        assert not [w for w in out if w["type"] == "lts_group_mismatch"]

    def test_no_inputs_no_warnings(self):
        assert run_crosscheck(_draft(), {}, {}) == []


# ── 2. 严重度归一化 + self_check 派生 ─────────────────────────

class TestSeverityAndVerdict:
    def test_severity_normalized_by_type(self):
        from baseline_export import _project_warnings
        draft = {"field_mappings": {"warnings": [{"type": "field_name_mismatch", "title": "x"}]},
                 "quality": {"issues": []}}
        out = _project_warnings(draft, [{"type": "write_plan_gap", "title": "y"}],
                                [{"type": "dq_target_mismatch", "title": "z"}])
        by_type = {w["type"]: w["severity"] for w in out}
        assert by_type["field_name_mismatch"] == "noise"
        assert by_type["write_plan_gap"] == "structural"
        assert by_type["dq_target_mismatch"] == "structural"
        # 分拣披露两类定级
        extra = _project_warnings({}, [], [
            {"type": "unrecognized_artifact", "title": "m"},
            {"type": "dq_not_detected", "title": "d"},
        ])
        assert {w["type"]: w["severity"] for w in extra} == {
            "unrecognized_artifact": "noise", "dq_not_detected": "structural"}

    def test_self_check_three_states(self):
        from baseline_export import _derive_self_check
        assert _derive_self_check([]) == {"verdict": "pass",
                                          "structural_count": 0, "noise_count": 0}
        assert _derive_self_check([{"severity": "noise"}])["verdict"] == "pass_with_noise"
        r = _derive_self_check([{"severity": "noise"}, {"severity": "structural"}])
        assert r["verdict"] == "review" and r == {"verdict": "review",
                                                  "structural_count": 1, "noise_count": 1}

    def test_project_carries_self_check(self):
        from baseline_export import project_baseline_v1
        draft = {"meta": {"target_table": "t"}, "topology": {"steps": []},
                 "data_flow": {"tables": [{"schema": "d", "name": "t"}]},
                 "field_mappings": {"fields": [], "warnings": []},
                 "quality": {"issues": []}}
        out = project_baseline_v1(draft, {"provenance": [{"type": "other", "path": "x"}],
                                          "extra_warnings": [
                                              {"type": "unrecognized_artifact", "title": "m"}]})
        assert out["self_check"]["verdict"] == "pass_with_noise"
        assert out["self_check"]["noise_count"] == 1
        assert out["warnings"][0]["severity"] == "noise"


# ── 3. facts e2e：待审不是失败 ─────────────────────────────────

def _run_facts(argv):
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


class TestFactsGateE2E:
    def test_structural_mismatch_yields_review_not_failure(self, tmp_path):
        """混入他资产 DQ → 结构级矛盾：exit 0 + 产物照常落盘 + verdict=review + 结论行。"""
        import openpyxl
        bag = tmp_path / "bag"
        bag.mkdir()
        shutil.copy(ET_XLSX, bag / "et.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "规则基础信息"
        ws.append(["规则Number", "规则中文名", "规则中文描述", "检查类型",
                   "目标表schema", "目标表", "SQL", "告警级别", "规则负责人"])
        ws.append(["R999", "他资产规则", "desc", "空值检查",
                   "dws", "other_asset_table", "SELECT 1", "严重", "u"])
        wb.save(str(bag / "dq.xlsx"))
        out = tmp_path / "o" / "asset_facts.json"
        stdout, _, code = _run_facts(["--dir", str(bag), "--output", str(out)])
        assert code == 0            # 待审不是失败
        assert out.exists()         # 产物总是落盘（§七.2）
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["version"] == "1.4"
        assert data["self_check"]["verdict"] == "review"
        assert data["self_check"]["structural_count"] >= 1
        mism = [w for w in data["warnings"] if w["type"] == "dq_target_mismatch"]
        assert len(mism) == 1 and mism[0]["severity"] == "structural"
        lines = stdout.splitlines()
        assert any(l.startswith("asset_facts: ") for l in lines)
        assert any(l.startswith("self_check: review") for l in lines)

    def test_cleanish_dir_yields_pass_with_noise(self, tmp_path):
        """仅术加（未检出 DQ/LTS 属结构级披露）——review；混入噪音文件不影响判定机制。"""
        bag = tmp_path / "bag"
        bag.mkdir()
        shutil.copy(ET_XLSX, bag / "et.xlsx")
        out = tmp_path / "o.json"
        _, _, code = _run_facts(["--dir", str(bag), "--output", str(out)])
        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        # 未检出 DQ/LTS = structural 披露 → review（人放行"全量资产无 DQ/LTS"即过）
        assert data["self_check"]["verdict"] == "review"
        types = {w["type"] for w in data["warnings"]}
        assert {"dq_not_detected", "lts_not_detected"} <= types
