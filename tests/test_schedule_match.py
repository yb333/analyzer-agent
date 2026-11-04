"""schedule_match 依赖 job 匹配测试（docs/17 §六，2026-11-02 三轮定稿）。

规则一句话：job 名（归一化后）包含表名 → 命中；唯一命中就填；
多个命中（歧义）或零命中 → 拿出来列举。

运行:
    pytest tests/test_schedule_match.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dws-pipeline-analyzer" / "references"))

from schedule_match import (_hit, _norm, dependency_candidates,
                            match_schedules)

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


class TestHit:
    """包含即命中（用户口径）：pjob_x_表名 / 表名_后缀 / 等值 全部命中。"""

    def test_equality(self):
        assert _hit("Pjob_ods_acct_balance_di", "ods_acct_balance_di")

    def test_prefix_segment(self):
        assert _hit("Pjob_x_ods_acct_balance_di", "ods_acct_balance_di")

    def test_suffix_segment(self):
        assert _hit("Pjob_ods_acct_di", "ods_acct")
        assert _hit("Pjob_ods_acct_balance_di", "ods_acct_balance")

    def test_no_match(self):
        assert not _hit("Pjob_unrelated", "ods_acct")
        assert not _hit("Pjob_ods_acct_balance_dx", "ods_acct_balance_di")


class TestDependencyCandidates:
    """候选集纪律：只有 tskdep 指向的上游任务，资产自身任务绝不进候选。"""

    UP1 = _task("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di")
    UP2 = _task("task_ods_cust_info_di", "Pjob_ods_cust_info_di")
    F = _f_task("DWB_CUST_ASSET_F_F",
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

    def test_unique_hit_fills(self):
        """唯一命中自动填：等值出精确标注，包含形变出含表名标注。"""
        matched, unmatched, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_ods_acct_balance_di",
                                  "Pjob_x_ods_cust_info_di",
                                  "Pjob_ods_order_di"]),
                  _task("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di"),
                  _task("task_ods_cust", "Pjob_x_ods_cust_info_di"),
                  _task("task_ods_order_di", "Pjob_ods_order_di")),
            SOURCES)
        assert status == "ok"
        r1 = matched["ods.ods_acct_balance_di"]
        assert r1["task_name"] == "task_ods_acct_balance_di"
        assert r1["pjob"] == "Pjob_ods_acct_balance_di"
        assert "精确匹配" in r1["remark"]
        r2 = matched["ods.ods_cust_info_di"]
        assert r2["pjob"] == "Pjob_x_ods_cust_info_di"      # 工程前缀段命中
        assert "job名含表名" in r2["remark"]
        assert len(unmatched) == 1                          # ods_order_di 列举
        assert "未唯一匹配" in unmatched[0]["remark"]

    def test_task_name_key_also_hits(self):
        """匹配键含上游任务名：job 名不含但任务名含表名也命中。"""
        matched, _, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_acct_bal"]),
                  _task("task_ods_acct_balance_di", "Pjob_acct_bal")),
            SOURCES[:1])
        row = matched.get("ods.ods_acct_balance_di")
        assert row and row["task_name"] == "task_ods_acct_balance_di"

    def test_multiple_hits_listed_not_filled(self):
        """多个候选都包含表名（歧义）→ 不自动填，全部列举（用户口径）。"""
        matched, unmatched, status = match_schedules(
            _info(_f_task("A_F", ["Pjob_x_ods_acct", "Pjob_ods_acct_y"]),
                  _task("t1", "Pjob_x_ods_acct"),
                  _task("t2", "Pjob_ods_acct_y")),
            [{"schema": "ods", "table": "ods_acct"}])
        assert matched == {}
        assert {u["pjob"] for u in unmatched} == {"Pjob_x_ods_acct",
                                                  "Pjob_ods_acct_y"}

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
