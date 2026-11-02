"""schedule_match 三档模糊匹配测试（docs/17 §六）。

档位/歧义保护/未匹配列举的单元行为，端到端见 test_spec_export.py。

运行:
    pytest tests/test_schedule_match.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dws-pipeline-analyzer" / "references"))

from schedule_match import (_norm, _score, match_schedules, SIM_THRESHOLD,
                            AMBIGUITY_MARGIN)


def _task(name, pjob=None, project="PRJ", group="GRP", cron="0 30 2 * * ?"):
    jobs = [{"name": pjob}] if pjob else []
    return {"task_name": name, "task_type": "周期任务", "schedule_cron": cron,
            "project": project, "task_group": group, "jobs": jobs}


SOURCES = [
    {"schema": "ods", "table": "ods_acct_balance_di"},
    {"schema": "ods", "table": "dwb_cust_asset_di"},
]


class TestNorm:
    def test_prefix_schema_separator(self):
        """归一化：去 Pjob_/task_ 前缀、去 schema、去分隔符。"""
        assert _norm("Pjob_ods.ods_acct_balance_di") == "odsacctbalancedi"
        assert _norm("task__ODS-ACCT-BALANCE-DI") == "odsacctbalancedi"
        assert _norm("ods_acct_balance_di") == "odsacctbalancedi"


class TestTiers:
    def test_tier1_exact(self):
        tier, score, label = _score("Pjob_ods_acct_balance_di", "ods_acct_balance_di")
        assert tier == 1 and score == 1.0 and "精确" in label

    def test_tier2_contains(self):
        tier, score, label = _score("Pjob_acct_bal", "ods_acct_balance_di")
        assert tier == 2 and "包含" in label

    def test_tier3_similarity(self):
        # 归一化后非包含关系但高相似（dly vs di 结尾）
        tier, score, label = _score("Pjob_cust_asset_dly", "dwb_cust_asset_di")
        assert tier == 3 and score >= SIM_THRESHOLD and "相似度" in label

    def test_no_match(self):
        tier, score, _ = _score("Pjob_order_flow", "ods_acct_balance_di")
        assert tier == 0


class TestMatchSchedules:

    def _info(self, *tasks):
        return {"all_tasks": list(tasks)}

    def test_matched_and_unmatched(self):
        """精确命中两张源表；第三任务未匹配 → 尾部列举（源表列留空由渲染层负责）。"""
        matched, unmatched = match_schedules(
            self._info(
                _task("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di"),
                _task("task_dwb_cust_asset_di", "Pjob_dwb_cust_asset_di"),
                _task("task_ods_order_di", "Pjob_ods_order_di"),
            ), SOURCES)
        assert set(matched) == {"ods.ods_acct_balance_di", "ods_acct_balance_di",
                                "dwb_cust_asset_di", "ods.dwb_cust_asset_di"}
        row = matched["ods.ods_acct_balance_di"]
        assert row["task_name"] == "task_ods_acct_balance_di"
        assert row["pjob"] == "Pjob_ods_acct_balance_di"
        assert row["project"] == "PRJ" and row["group"] == "GRP"
        assert "精确" in row["remark"]
        assert len(unmatched) == 1
        assert unmatched[0]["task_name"] == "task_ods_order_di"
        assert "未匹配到本资产源表" in unmatched[0]["remark"]

    def test_ambiguity_guard(self):
        """两个任务得分近并列（差 < AMBIGUITY_MARGIN）→ 都不自动填，防错配。"""
        matched, unmatched = match_schedules(
            self._info(
                _task("t1", "Pjob_ods_acct_balance_dx"),
                _task("t2", "Pjob_ods_acct_balance_dy"),
            ), SOURCES[:1])
        assert matched == {}
        assert {u["task_name"] for u in unmatched} == {"t1", "t2"}

    def test_fuzzy_contains_auto_fill(self):
        """真实制品 Pjob 名常缺表名段：包含式模糊自动填 + 标注人工核对。"""
        matched, unmatched = match_schedules(
            self._info(_task("t_acct", "Pjob_acct_bal")), SOURCES[:1])
        row = matched.get("ods.ods_acct_balance_di")
        assert row and "模糊匹配(包含)" in row["remark"] and "请人工核对" in row["remark"]

    def test_pjob_prefers_pjob_naming_over_first_job(self):
        """实测修订①：任务挂多 job（start 链）时，Pjob名称 优先 Pjob_ 前缀的
        主 job，不取链上首个（曾表现为父节点名）。"""
        t = {"task_name": "task_x", "task_type": "周期任务", "schedule_cron": "",
             "project": "P", "task_group": "G",
             "jobs": [{"name": "Job_pre"}, {"name": "Pjob_ods_x"}, {"name": "Job_post"}]}
        matched, _ = match_schedules({"all_tasks": [t]},
                                     [{"schema": "ods", "table": "ods_x"}])
        row = matched.get("ods.ods_x")
        assert row and row["pjob"] == "Pjob_ods_x"

    def test_no_jobs_falls_back_to_task_name(self):
        """任务无 jobs 行 → 用任务名做匹配键兜底。"""
        matched, _ = match_schedules(
            self._info(_task("task_ods_acct_balance_di", None)), SOURCES[:1])
        row = matched.get("ods.ods_acct_balance_di")
        assert row and row["pjob"] == ""
