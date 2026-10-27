"""调度设计还原测试（schedule_recovery，ts 草稿 §11.5 波 A）。

覆盖：
1. cron → schedule_type 确定性推断（daily/hourly/weekly/monthly/realtime/判不出）
2. tskdep 依赖识别（fixture mock repo：I 任务 tskdep → F 任务）+ 未匹配不编造
3. taskParams → lts_params（${P_xxx} 提取）；GroupVariables → exec_params
4. recover_schedule 总装（tasks.f/view、upstream、evidence、显式空）
5. LTS 扩读字段两源同构（yml 文本 / dict / excel 均含 exec_path 等新键）

运行:
    pytest tests/test_schedule_recovery.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from schedule_recovery import (DEFAULT_DEP_TYPE, build_exec_params, build_lts_params,
                               extract_upstream, infer_schedule_type, recover_schedule)


# ── 1. cron 频率推断 ──────────────────────────────────────────

class TestInferScheduleType:
    @pytest.mark.parametrize("cron,expected", [
        ("0 30 2 * * ?", "daily"),          # Quartz 6 段，时位单值
        ("0 0 3 * * ?", "daily"),
        ("30 2 * * *", "daily"),            # 5 段
        ("0 0 */2 * * ?", "hourly"),        # 时位步进
        ("0 0 0,12 * * ?", "hourly"),       # 时位多值
        ("0 */10 * * * ?", "hourly"),       # 分位步进
        ("0 * * * * ?", "realtime"),        # 分时全通配
        ("0 0 2 ? * MON", "weekly"),        # 周位指定
        ("0 0 2 1 * ?", "monthly"),         # 日位指定
        ("", ""),
        ("garbage", ""),
        ("1 2 3", ""),
    ])
    def test_cases(self, cron, expected):
        assert infer_schedule_type(cron) == expected


# ── 2. tskdep 依赖识别 ────────────────────────────────────────

class TestUpstream:
    @pytest.fixture(scope="class")
    def sched(self, tmp_path_factory):
        from _build_repo import build_mock_repo
        from inputs import _discover_lts_from_repo
        repo = build_mock_repo(tmp_path_factory.mktemp("repo") / "repo")
        return _discover_lts_from_repo(repo["group_dir"], "GR_TRADE_ORDER")

    def test_i_depends_on_f_via_tskdep(self, sched):
        """fixture：I 任务的 tskdep job Pjob_trade_f → 上游 = F 任务（按主 job 名匹配）。"""
        rec = recover_schedule(sched)
        view = rec["tasks"]["view"]
        assert view["task_name"] == "TASK_TRADE_ORDER_I"
        assert len(view["upstream"]) == 1
        up = view["upstream"][0]
        assert up["task"] == "TASK_TRADE_ORDER_F"
        assert up["resolved"] is True
        assert up["dep_type"] == DEFAULT_DEP_TYPE   # 制品无依赖语义类型——不推断
        assert up["project"] == "BCNB_DAILY" and up["group"] == "GROUP_TRADE"

    def test_f_unresolved_tskdep_not_fabricated(self, sched):
        """F 任务的 tskdep Pjob_dep_f 在制品内无对应任务 → resolved=False、task 留空。"""
        rec = recover_schedule(sched)
        ups = rec["tasks"]["f"]["upstream"]
        assert len(ups) == 1
        assert ups[0]["resolved"] is False and ups[0]["task"] == ""
        assert ups[0]["table"] == "dep_f" and ups[0]["job"] == "Pjob_dep_f"

    def test_main_job_excludes_tskdep(self, sched):
        rec = recover_schedule(sched)
        assert rec["tasks"]["f"]["job_name"] == "Pjob_trade_f"
        assert rec["tasks"]["view"]["job_name"] == "Pjob_trade_i"

    def test_extract_upstream_direct(self):
        task = {"jobs": [{"name": "Pjob_a", "type": "url"},
                         {"name": "Pjob_up_x", "type": "TSKDEP"}]}  # 大小写容错
        idx = {"up_x": {"task_name": "task_up_x", "project": "P", "task_group": "G"}}
        ups = extract_upstream(task, idx)
        assert ups == [{"task": "task_up_x", "table": "up_x", "dep_type": DEFAULT_DEP_TYPE,
                        "project": "P", "group": "G", "job": "Pjob_up_x", "resolved": True}]


# ── 3. 参数还原 ───────────────────────────────────────────────

class TestParams:
    def test_lts_params_extract_etl_placeholder(self):
        task = {"params": [{"name": "V_CYCLE_ID", "value": "${P_CYCLE_ID}"},
                           {"name": "V_GROUP_CODE", "value": "GR_X"},
                           {"name": "", "value": "ignored"}]}
        out = build_lts_params(task)
        assert out == [{"lts_var": "V_CYCLE_ID", "etl_param": "P_CYCLE_ID", "desc": ""},
                       {"lts_var": "V_GROUP_CODE", "etl_param": "", "desc": ""}]

    def test_exec_params_from_group_variables(self):
        out = build_exec_params({"P_CYCLE_ID": "20260101", "P_FLAG": ""})
        assert out["P_CYCLE_ID"] == {"value_type": "", "desc": "", "default_value": "20260101"}
        assert out["P_FLAG"]["default_value"] == ""
        assert build_exec_params(None) == {}


# ── 4. 总装 + 显式空 ──────────────────────────────────────────

class TestRecoverSchedule:
    def test_none_yields_explicit_empty(self):
        rec = recover_schedule(None)
        assert rec == {"schedule_type": "", "cron": "", "upstream_added": [],
                       "tasks": {}, "lts_params": [], "exec_params": {}, "evidence": []}

    def test_full_assembly_from_fixture(self, tmp_path):
        from _build_repo import build_mock_repo
        from inputs import _discover_lts_from_repo
        repo = build_mock_repo(tmp_path / "repo")
        sched = _discover_lts_from_repo(repo["group_dir"], "GR_TRADE_ORDER")
        rec = recover_schedule(sched, {"P_CYCLE_ID": "20260101"})
        assert rec["cron"] == "0 30 2 * * ?" and rec["schedule_type"] == "daily"
        assert set(rec["tasks"]) == {"f", "view"}          # fixture 无 DQ 任务
        assert rec["tasks"]["f"]["depends_prev_cycle"] == "是"   # 自依赖开关随行，不冒充 dep_type
        assert rec["exec_params"]["P_CYCLE_ID"]["default_value"] == "20260101"
        # evidence 证据随行：两任务原文 + tskdep 原文
        names = {e["task_name"] for e in rec["evidence"]}
        assert {"TASK_TRADE_ORDER_F", "TASK_TRADE_ORDER_I"} <= names
        i_ev = next(e for e in rec["evidence"] if e["task_name"] == "TASK_TRADE_ORDER_I")
        assert i_ev["tskdep_jobs"][0]["job"] == "Pjob_trade_f"


# ── 5. 扩读字段两源同构 ───────────────────────────────────────

class TestExtendedFieldsIsomorphic:
    EXT_JOB_KEYS = {"exec_path", "params", "call_method", "desc", "var_settings", "ext_attrs"}
    EXT_TASK_KEYS = {"end_time", "freq_config", "ext_attrs"}

    def test_yml_text_source(self):
        from inputs import _parse_lts_from_text
        txt = ("'*任务名称': T_F\n调度周期: 0 0 1 * * ?\n调度频率配置: daily\n"
               "额外信息: |\n  Jobs: |\n    - '*job名称': Pjob_t\n      '*job类型': url\n"
               "      执行路径信息: ${V_URL}\n      job参数: {\"a\":1}\n")
        t = _parse_lts_from_text(txt)
        assert self.EXT_TASK_KEYS <= set(t) and t["freq_config"] == "daily"
        assert self.EXT_JOB_KEYS <= set(t["jobs"][0])
        assert t["jobs"][0]["exec_path"] == "${V_URL}"

    def test_yml_dict_source(self):
        from inputs import _parse_lts_from_dict
        data = {"*任务名称": "T_F", "调度周期": "0 0 1 * * ?", "结束时间": "2099",
                "Jobs": [{"*job名称": "Pjob_t", "*job类型": "url", "执行路径信息": "/x", "job参数": "p"}]}
        t = _parse_lts_from_dict(data)
        assert t["end_time"] == "2099"
        assert t["jobs"][0]["exec_path"] == "/x" and t["jobs"][0]["params"] == "p"
        assert self.EXT_JOB_KEYS <= set(t["jobs"][0])

    def test_excel_source(self, tmp_path):
        import openpyxl
        from inputs import read_lts_excel
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "tasks"
        ws.append(["项目名称", "任务组名称", "任务名称", "任务类型", "调度周期", "调度频率配置", "结束时间"])
        ws.append(["P", "G", "T_F", "周期任务", "0 0 1 * * ?", "daily", "2099"])
        wj = wb.create_sheet("jobs")
        wj.append(["任务名称", "job名称", "job类型", "job的父节点名称", "执行路径信息", "job参数"])
        wj.append(["T_F", "Pjob_t", "url", "start", "${V_URL}", "{}"])
        x = tmp_path / "l.xlsx"
        wb.save(str(x))
        sched = read_lts_excel(str(x))
        t = sched["f_tasks"][0]
        assert t["freq_config"] == "daily" and t["end_time"] == "2099"
        assert t["jobs"][0]["exec_path"] == "${V_URL}"
        assert self.EXT_JOB_KEYS <= set(t["jobs"][0])
