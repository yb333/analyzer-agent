"""schedule_match 依赖 job 匹配测试（docs/17 §六实测修订⑧）。

候选集 = 资产 F 任务的 tskdep 依赖 job（不是全量任务）；
精确层（tskdep 表名）+ 模糊层三档 + 歧义保护 + 未匹配列举。

运行:
    pytest tests/test_schedule_match.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dws-pipeline-analyzer" / "references"))

from schedule_match import (_norm, _score, dependency_candidates,
                            match_schedules, SIM_THRESHOLD, AMBIGUITY_MARGIN)

SOURCES = [
    {"schema": "ods", "table": "ods_acct_balance_di"},
    {"schema": "ods", "table": "ods_cust_info_di"},
]


def _task(name, main_pjob=None, project="PRJ", group="GRP",
          cron="0 30 2 * * ?"):
    """上游任务（主 job 一个）。"""
    jobs = [{"name": main_pjob, "type": "url"}] if main_pjob else []
    return {"task_name": name, "task_type": "周期任务", "schedule_cron": cron,
            "project": project, "task_group": group, "jobs": jobs}


def _f_task(name, deps, main="Pjob_DWB_ASSET_MAIN"):
    """资产 F 任务：主 job + tskdep 依赖 job（job 名 = 上游任务主 job 名）。"""
    jobs = [{"name": main, "type": "url"}]
    jobs += [{"name": d, "type": "tskdep"} for d in deps]
    return {"task_name": name, "task_type": "周期任务",
            "schedule_cron": "0 0 3 * * ?", "project": "PRJ_ASSET",
            "task_group": "GRP_ASSET", "jobs": jobs}


def _info(f, *others):
    return {"f_tasks": [f], "all_tasks": [f, *others]}


class TestNorm:
    def test_prefix_schema_separator(self):
        assert _norm("Pjob_ods.ods_acct_balance_di") == "odsacctbalancedi"
        assert _norm("task__ODS-ACCT-BALANCE-DI") == "odsacctbalancedi"


class TestTiers:
    def test_tier1_exact(self):
        tier, score, label = _score("Pjob_ods_acct_balance_di", "ods_acct_balance_di")
        assert tier == 1 and score == 1.0 and "精确" in label

    def test_tier2_contains(self):
        tier, score, label = _score("Pjob_acct_bal", "ods_acct_balance_di")
        assert tier == 2 and "包含" in label

    def test_tier3_similarity(self):
        tier, score, label = _score("Pjob_cust_asset_dly", "dwb_cust_asset_di")
        assert tier == 3 and score >= SIM_THRESHOLD and "相似度" in label


class TestDependencyCandidates:
    """候选集纪律：只有 tskdep 指向的上游任务，资产自身任务绝不进候选。"""

    UP1 = _task("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di")
    UP2 = _task("task_ods_cust_info_di", "Pjob_ods_cust_info_di")
    F = _f_task("DWB_CUST_ASSET_D_F",
                ["Pjob_ods_acct_balance_di", "Pjob_ods_cust_info_di"])

    def test_candidates_from_tskdep_only(self):
        cands, status = dependency_candidates(_info(self.F, self.UP1, self.UP2))
        assert status == "ok" and len(cands) == 2
        assert {c["job"] for c in cands} == {"Pjob_ods_acct_balance_di",
                                             "Pjob_ods_cust_info_di"}
        assert all(c["up_task"] in (self.UP1, self.UP2) for c in cands)

    def test_asset_task_never_a_candidate(self):
        """资产自身任务/主 job 不参与匹配（实测⑧核心：曾把资产 job 填进源表行）。"""
        f = _f_task("ods_acct_balance_di_F", ["Pjob_ods_cust_info_di"])
        matched, unmatched, status = match_schedules(
            _info(f, self.UP2), SOURCES)
        assert status == "ok"
        assert "ods.ods_acct_balance_di" not in matched  # 资产任务没顶上来
        assert "ods.ods_cust_info_di" in matched

    def test_no_f_task(self):
        cands, status = dependency_candidates(
            {"f_tasks": [], "all_tasks": [_task("t", "Pjob_t")]})
        assert status == "no_f_task" and cands == []

    def test_no_tskdep(self):
        f = {"task_name": "X_F", "task_type": "", "schedule_cron": "",
             "project": "", "task_group": "",
             "jobs": [{"name": "Pjob_x", "type": "url"}]}
        cands, status = dependency_candidates({"f_tasks": [f], "all_tasks": [f]})
        assert status == "no_tskdep" and cands == []


class TestMatchSchedules:

    def test_exact_layer_by_tskdep_table(self):
        """精确层：tskdep job 名的表名 == 源表裸名，直接命中。"""
        matched, unmatched, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_ods_acct_balance_di",
                                  "Pjob_ods_cust_info_di",
                                  "Pjob_ods_order_di"]),
                  _task("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di"),
                  _task("task_ods_cust_info_di", "Pjob_ods_cust_info_di"),
                  _task("task_ods_order_di", "Pjob_ods_order_di")),
            SOURCES)
        assert status == "ok"
        row = matched["ods.ods_acct_balance_di"]
        assert row["task_name"] == "task_ods_acct_balance_di"
        assert row["pjob"] == "Pjob_ods_acct_balance_di"
        assert row["project"] == "PRJ" and row["group"] == "GRP"
        assert "精确匹配" in row["remark"]
        assert len(unmatched) == 1                     # ods_order_di 列举
        assert "未匹配到本资产源表" in unmatched[0]["remark"]

    def test_unresolved_upstream_listed(self):
        """tskdep 指向的任务不在制品内 → 未解析列举（不编造任务名）。"""
        matched, unmatched, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_ods_acct_balance_di"]),
                  _task("task_ods_cust_info_di", "Pjob_ods_cust_info_di")),
            SOURCES)
        assert status == "ok" and matched == {}
        assert len(unmatched) == 1
        assert unmatched[0]["unresolved"] is True
        assert "未找到对应上游任务" in unmatched[0]["remark"]

    def test_fuzzy_layer_fills_and_labels(self):
        """模糊层：tskdep 表名不精确命中时，job 名/任务名三档兜底。"""
        matched, unmatched, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_acct_bal"]),
                  _task("task_acct_bal", "Pjob_acct_bal")),
            SOURCES[:1])
        row = matched.get("ods.ods_acct_balance_di")
        assert row and "模糊匹配(包含)" in row["remark"]
        assert row["task_name"] == "task_acct_bal"

    def test_ambiguity_guard(self):
        """两个依赖得分近并列（差 < AMBIGUITY_MARGIN）→ 都不自动填。"""
        matched, unmatched, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_ods_acct_balance_dx",
                                  "Pjob_ods_acct_balance_dy"]),
                  _task("t1", "Pjob_ods_acct_balance_dx"),
                  _task("t2", "Pjob_ods_acct_balance_dy")),
            SOURCES[:1])
        assert matched == {}
        assert {u["pjob"] for u in unmatched} == {
            "Pjob_ods_acct_balance_dx", "Pjob_ods_acct_balance_dy"}
