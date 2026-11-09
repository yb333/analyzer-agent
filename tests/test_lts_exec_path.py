#!/usr/bin/env python3
"""LTS 执行路径解析 + RT 实时任务识别 契约测试（2026-11-09 用户口径）。

口径（用户拍板）：
- RT 识别纯靠命名（表名_RT），tasks 页签任务类型列不可靠
- tskdep 执行路径 = `集群|appid|项目|任务组|任务`；同集群省集群段
  （首段 com.huawei. 开头即 appid）；集群信息忽略不呈现
- url 型 ${xxx} 实值从 taskParams 解析；实值不是 tskdep 坐标，不参与依赖
- database 型 select 只分类标注，不参与依赖
- 依赖解析优先级：制品内任务行权威 → 执行路径坐标兜底（跨制品可解析）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "dws-pipeline-analyzer" / "references"))

from inputs import parse_exec_path, read_lts_excel, strip_rt_suffix

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "lts" / "rt_tskdep_sample.xlsx"


# ════════════════════════════════════════════════════════════
# parse_exec_path：三种形态 + 不硬解纪律
# ════════════════════════════════════════════════════════════

class TestParseExecPath:

    def test_tskdep_same_cluster(self):
        """同集群：首段 com.huawei. 开头即 appid，4 段，无集群。"""
        r = parse_exec_path(
            "com.huawei.fin.dwl.dwsapp|FIN_DWL_PROJ|GRP_DWB|TASK_DWB_CUST_BASE_F")
        assert r["kind"] == "tskdep"
        assert r["cluster"] == ""
        assert r["appid"] == "com.huawei.fin.dwl.dwsapp"
        assert r["project"] == "FIN_DWL_PROJ"
        assert r["task_group"] == "GRP_DWB"
        assert r["task"] == "TASK_DWB_CUST_BASE_F"

    def test_tskdep_cross_cluster(self):
        """跨集群：首段是集群名，5 段；集群解析出（呈现层忽略）。"""
        r = parse_exec_path(
            "bd-cluster-02|com.huawei.fin.ods.core|ODS_PROJ|GRP_ODS|TASK_ODS_ACCT_TAG")
        assert r["kind"] == "tskdep"
        assert r["cluster"] == "bd-cluster-02"
        assert r["appid"] == "com.huawei.fin.ods.core"
        assert r["task"] == "TASK_ODS_ACCT_TAG"

    def test_url_resolved_from_taskparams(self):
        """url 型 ${xxx} → taskParams 实值；实值不是坐标，坐标字段全空。"""
        params = [{"name": "V_NOTIFY_URL", "value": "http://inner-esb.fin/notify"}]
        r = parse_exec_path("${V_NOTIFY_URL}", params)
        assert r["kind"] == "url"
        assert r["resolved"] == "http://inner-esb.fin/notify"
        assert r["task"] == "" and r["appid"] == ""

    def test_url_unresolved_param(self):
        """url 型参数在 taskParams 里没有 → resolved 留空（不编造）。"""
        r = parse_exec_path("${V_MISSING}", [])
        assert r["kind"] == "url"
        assert r["resolved"] == ""

    def test_database_select(self):
        """database 型：select 语句只分类，不解析。"""
        r = parse_exec_path("select t1.a from dwb.x t1 where t1.f = 'D'")
        assert r["kind"] == "database"
        assert r["task"] == ""

    def test_pipe_without_appid_not_forced(self):
        """| 分段但无 com.huawei. appid 段 → 不硬解（kind=other，不编造坐标）。"""
        r = parse_exec_path("a|b|c|d")
        assert r["kind"] == "other"
        assert r["task"] == "" and r["appid"] == ""

    def test_wrong_segment_count_not_forced(self):
        """appid 段在但段数不吻合（6 段）→ 不硬解。"""
        r = parse_exec_path(
            "com.huawei.x|P|G|T|EXTRA")
        assert r["kind"] == "other"

    def test_empty(self):
        assert parse_exec_path("")["kind"] == ""


# ════════════════════════════════════════════════════════════
# RT 实时任务识别（纯命名，样例 excel 锁行为）
# ════════════════════════════════════════════════════════════

class TestRtClassification:

    @pytest.fixture(scope="class")
    def si(self):
        r = read_lts_excel(str(SAMPLE), "RG2026110901")
        assert r, "样例 excel 应读到任务"
        return r

    def test_rt_asset_classified_as_f_task(self, si):
        """只有实时任务的资产：X_F_RT 剥尾缀后归 F 任务，realtime 标记。"""
        assert len(si["f_tasks"]) == 1
        ft = si["f_tasks"][0]
        assert ft["task_name"] == "DWB_ACCT_RISK_F_RT"
        assert ft.get("realtime") is True

    def test_rt_i_task_associated_and_marked(self, si):
        """I 实时任务经 F→I 基名关联补入，且 realtime 标记（不在 matched 也补标）。"""
        assert len(si["i_tasks"]) == 1
        it = si["i_tasks"][0]
        assert it["task_name"] == "DWB_ACCT_RISK_I_RT"
        assert it.get("realtime") is True

    def test_task_type_column_not_trusted(self, si):
        """任务类型列填的「批处理调度」不影响 RT 识别（纯命名口径）。"""
        assert si["f_tasks"][0]["task_type"] == "批处理调度"
        assert si["f_tasks"][0].get("realtime") is True

    def test_strip_rt_suffix(self):
        assert strip_rt_suffix("DWB_X_F_RT") == "DWB_X_F"
        assert strip_rt_suffix("dwb_x_f_rt") == "dwb_x_f"
        assert strip_rt_suffix("TASK_X-F-RT") == "TASK_X-F"
        assert strip_rt_suffix("DWB_X_F") == "DWB_X_F"   # 无尾缀原样
        assert strip_rt_suffix("") == ""


# ════════════════════════════════════════════════════════════
# 依赖解析：制品内权威 → 路径坐标兜底（跨制品可解析）
# ════════════════════════════════════════════════════════════

class TestDependencyResolution:

    @pytest.fixture(scope="class")
    def si(self):
        return read_lts_excel(str(SAMPLE), "RG2026110901")

    def test_candidates_mixed_resolution(self, si):
        """三个 tskdep：一个制品内解析、两个路径坐标解析（跨应用/跨集群）。"""
        from schedule_match import dependency_candidates
        cands, status = dependency_candidates(si)
        assert status == "ok"
        by_job = {c["job"]: c for c in cands}
        assert by_job["Pjob_dwb_cust_base_f"]["up_task"]["task_name"] == \
            "TASK_DWB_CUST_BASE_F"
        assert by_job["Pjob_dwrdim_cust_d"]["up_task"]["_via"] == "path"
        assert by_job["Pjob_dwrdim_cust_d"]["up_task"]["task_name"] == \
            "TASK_DWRDIM_CUST_D"

    def test_match_fills_app_project_group(self, si):
        """匹配行填调度应用(appid)/项目/任务组；跨制品依赖同样可填。"""
        from schedule_match import match_schedules
        srcs = [{"schema": "fin_dwl", "table": "dwb_cust_base_f"},
                {"schema": "dwrdim", "table": "dwrdim_cust_d"},
                {"schema": "ods", "table": "ods_acct_tag"}]
        matched, unmatched, status = match_schedules(si, srcs)
        assert status == "ok" and not unmatched
        row = matched["dwrdim.dwrdim_cust_d"]
        assert row["app"] == "com.huawei.dwrdim.coreapp"
        assert row["project"] == "DWRDIM_PROJ"
        assert row["group"] == "GRP_DIM"
        assert row["via"] == "path"
        # 制品内权威：项目/任务组用任务行原值，不是路径坐标
        in_row = matched["fin_dwl.dwb_cust_base_f"]
        assert in_row["project"] == "fin_dwl_etl"
        assert in_row["group"] == "G_DWB_BASE"
        assert in_row["via"] == "in_artifact"
        # 跨集群：集群段解析但不呈现（无 cluster 键）
        assert "cluster" not in matched["ods.ods_acct_tag"]

    def test_recovery_upstream_path_fallback(self, si):
        """schedule_recovery.extract_upstream：跨制品依赖 resolved=True(via=path)。"""
        from schedule_recovery import extract_upstream
        ft = si["f_tasks"][0]
        pool = {t["task_name"]: t for t in si["_all_raw_tasks"]}
        # 任务索引（与 recover_schedule 同构：任务名/job 名双键）
        from schedule_recovery import _task_table, _main_job_name
        idx = {}
        for t in pool.values():
            for k in (_task_table(t["task_name"]), _main_job_name(t)):
                if k:
                    idx.setdefault(k, t)
        ups = extract_upstream(ft, idx)
        by_job = {u["job"]: u for u in ups}
        assert by_job["Pjob_dwb_cust_base_f"]["via"] == "in_artifact"
        assert by_job["Pjob_dwb_cust_base_f"]["resolved"] is True
        path_up = by_job["Pjob_ods_acct_tag"]
        assert path_up["resolved"] is True and path_up["via"] == "path"
        assert path_up["task"] == "TASK_ODS_ACCT_TAG"
        assert path_up["app"] == "com.huawei.fin.ods.core"
        assert path_up["project"] == "ODS_PROJ"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
