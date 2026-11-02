"""SPEC 通道端到端测试 —— spec 入口 → 需求SPEC.xlsx（docs/17 §〇~§五波A 口径）。

黄金案例 = docs/术加制品参考案例（DWB_CUST_ASSET_D：双分区多场景 + 中间表链）；
单场景案例 = tests/fixtures/analyzer/case_08_self_reference（自引用防重，dm=0）。

运行:
    pytest tests/test_spec_export.py -v
"""

import io
import contextlib
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

GOLDEN_ET = PROJECT_ROOT / "docs" / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_D.xlsx"
CASE08_ET = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / "case_08_self_reference" / "execution_tasks.xlsx"
CASE17_ET = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / "case_17_many_ctes" / "execution_tasks.xlsx"


def _run_spec(argv):
    """进程内调 spec.main()（stdout 重定向，stderr 留给 capsys 断言）。"""
    from spec import main
    old_argv = sys.argv
    sys.argv = ["spec.py"] + argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            main()
        return buf.getvalue()
    finally:
        sys.argv = old_argv


def _bag(tmp_path, src, name="et.xlsx"):
    bag = tmp_path / "bag"
    bag.mkdir(exist_ok=True)
    shutil.copy(src, bag / name)
    return bag


def _rows(xlsx_path, sheet):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    try:
        ws = wb[sheet]
        return [list(r) for r in ws.iter_rows(min_row=2, values_only=True)]
    finally:
        wb.close()


# ═══════════════════════════════════════════════════════════════
# 场景门（docs/17 §〇 方案 A）
# ═══════════════════════════════════════════════════════════════

class TestScenarioGate:

    def test_multi_scenario_requires_flag(self, tmp_path, capsys):
        """多场景资产不指定 --scenario → 不产文件 + exit 1 + 人话提示。"""
        bag = _bag(tmp_path, GOLDEN_ET)
        out = tmp_path / "o" / "spec.xlsx"
        with pytest.raises(SystemExit) as ei:
            _run_spec(["--dir", str(bag), "--output", str(out)])
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "多场景" in err and "--scenario" in err
        assert "P_CUR" in err and "P_HIS" in err
        assert not out.exists()

    def test_scenarios_locator_line(self, tmp_path, capsys):
        """场景门失败输出机器可解析行 `scenarios: …`（/export-spec 封装弹选择用）。"""
        bag = _bag(tmp_path, GOLDEN_ET)
        with pytest.raises(SystemExit):
            _run_spec(["--dir", str(bag), "--output", str(tmp_path / "o.xlsx")])
        err = capsys.readouterr().err
        assert "scenarios: P_CUR, P_HIS" in err

    def test_unknown_scenario_rejected(self, tmp_path, capsys):
        bag = _bag(tmp_path, GOLDEN_ET)
        out = tmp_path / "o.xlsx"
        with pytest.raises(SystemExit) as ei:
            _run_spec(["--dir", str(bag), "--output", str(out), "--scenario", "P_XXX"])
        assert ei.value.code == 1
        assert "不存在" in capsys.readouterr().err
        assert not out.exists()


# ═══════════════════════════════════════════════════════════════
# 面向人的入口增强（/export-spec 封装的底座）
# ═══════════════════════════════════════════════════════════════

class TestHumanFriendlyEntry:

    def test_output_optional_default_path(self, tmp_path, monkeypatch):
        """--output 可省略：默认 ./output/需求SPEC-{规则组英文名}.xlsx。"""
        monkeypatch.chdir(tmp_path)
        bag = _bag(tmp_path, CASE08_ET)
        stdout = _run_spec(["--dir", str(bag)])
        produced = list((tmp_path / "output").glob("需求SPEC-*.xlsx"))
        assert len(produced) == 1
        assert f"spec: {produced[0].resolve()}" in stdout
        assert produced[0].name == "需求SPEC-GR008.xlsx"  # 规则组英文名命名


# ═══════════════════════════════════════════════════════════════
# 黄金案例（DWB_CUST_ASSET_D，场景 P_CUR）
# ═══════════════════════════════════════════════════════════════

class TestGoldenCase:
    """端到端对拍 docs/需求SPEC-案例mock-DWB_CUST_ASSET_D.xlsx（波A 基础列）。"""

    @pytest.fixture(scope="class")
    def golden_run(self, tmp_path_factory):
        bag = _bag(tmp_path_factory.mktemp("golden"), GOLDEN_ET)
        out = tmp_path_factory.mktemp("golden_out") / "spec.xlsx"
        stdout = _run_spec(["--dir", str(bag), "--output", str(out),
                            "--scenario", "P_CUR"])
        return out, stdout

    def test_locator_and_self_check(self, golden_run):
        out, stdout = golden_run
        assert out.exists()
        assert f"spec: {out.resolve()}" in stdout
        assert "self_check: pass_with_noise" in stdout   # 缺 LTS + 缺 DDL → noise
        assert "LTS制品未提供" in stdout                 # 两态披露之「没给」

    def test_five_sheets(self, golden_run):
        import openpyxl
        out, _ = golden_run
        wb = openpyxl.load_workbook(out, read_only=True)
        assert wb.sheetnames == ["1业务背景", "2数据源清单", "3表关联",
                                 "4需求字段", "5业务样例数据"]
        wb.close()

    def test_sheet2_physical_sources_only(self, golden_run):
        """数据源清单 = 最源头物理表；中间表 tmp_* 绝不出现；记录ID 不填。"""
        out, _ = golden_run
        rows = _rows(out, "2数据源清单")
        tables = [r[2] for r in rows]
        assert tables == ["ods_acct_balance_di", "ods_cust_info_di"]
        assert all(r[1] == "ODS" for r in rows)          # 源系统 = schema 大写
        assert all(r[0] is None for r in rows)           # 记录ID（N 不填）
        assert all("tmp_" not in str(r) for r in rows)   # 中间表零出现
        assert all("LTS制品未提供" in (r[12] or "") for r in rows)  # 两态备注

    def test_sheet3_segment_syntax(self, golden_run):
        """表关联 G 列段式（docs/17 §三）：ON/限制/写入前限制/主表链预处理四段。"""
        out, _ = golden_run
        rows = _rows(out, "3表关联")
        assert len(rows) == 1
        r = rows[0]
        assert (r[1], r[2], r[3], r[4], r[5]) == (
            "ods_acct_balance_di", "a", "ods_cust_info_di", "c", "INNER JOIN")
        g = r[6]
        assert "a.cust_id = c.cust_id" in g
        assert "限制：a.del_flag = 'N' AND a.acct_date = '20260818'" in g
        # 写入前限制：无 JOIN 步骤（场景写入 R0003）的 WHERE 不得丢（谓词守恒）
        assert "写入前限制：s.data_flag = 'D'" in g
        # 主表链预处理：中间表聚合步的分组+保留（引擎 group_by/having_clause）
        assert "主表链预处理：按 t.cust_id, t.asset_type 分组" in g
        assert "保留 SUM(t.balance) > 0" in g

    def test_sheet4_field_typing_and_constants(self, golden_run):
        """需求字段：终态 4 字段；跨步加工按穿透 transform 判型；常量无源。"""
        out, _ = golden_run
        rows = _rows(out, "4需求字段")
        assert len(rows) == 4
        by_target = {r[10]: r for r in rows}
        assert set(by_target) == {"cust_id", "asset_type",
                                  "total_balance", "data_scope"}
        # 跨步加工：终态步骤 direct，但穿透 transform=aggregate → 加工字段，
        # 逻辑列 = 拆解式（无规则编码、无表级条件、AS 尾巴剥离）
        tb = by_target["total_balance"]
        assert tb[5] == "加工字段"
        assert tb[6] == "表达式：SUM(balance)"
        assert (tb[2], tb[4]) == ("ods_acct_balance_di", "balance")
        assert tb[1] == "balance"                        # 业务需求字段 = copy 源字段（Q5）
        # 常量字段：无物理源，源表列留空（mock data_scope 长相）
        ds = by_target["data_scope"]
        assert ds[5] == "加工字段" and ds[6] == "常量：'CUR'"
        assert ds[2] is None and "无物理源" in (ds[12] or "")
        # 直取
        assert by_target["asset_type"][5] == "直取字段"
        assert by_target["asset_type"][6] in (None, "")
        # 终态列
        assert all(r[8] == "dws.dwb_cust_asset_d" for r in rows)

    def test_business_background_and_sample_empty(self, golden_run):
        """业务背景（人工位）与样例数据留空。"""
        out, _ = golden_run
        assert _rows(out, "1业务背景") == []
        assert _rows(out, "5业务样例数据") == []


# ═══════════════════════════════════════════════════════════════
# 单场景案例（case_08 自引用：单表无 JOIN，dm=0）
# ═══════════════════════════════════════════════════════════════

class TestSingleScenarioCase:

    def test_no_flag_needed_and_target_not_a_source(self, tmp_path):
        """单场景资产无需 --scenario；目标表自身（NOT EXISTS 自引用）不进数据源。"""
        bag = _bag(tmp_path, CASE08_ET)
        out = tmp_path / "spec.xlsx"
        stdout = _run_spec(["--dir", str(bag), "--output", str(out)])
        assert out.exists()
        assert "self_check: pass_with_noise" in stdout

        rows = _rows(out, "2数据源清单")
        assert [r[2] for r in rows] == ["staging_orders"]    # 只有物理源表
        assert all("dwb_incremental_f" not in str(r) for r in rows)  # 目标表零出现

        frows = _rows(out, "4需求字段")
        assert {r[10] for r in frows} == {"order_id", "order_date", "amount"}
        assert all(r[5] == "直取字段" for r in frows)
        assert all(r[8] == "dws.dwb_incremental_f" for r in frows)

    def test_e1_single_table_row_with_verbatim_not_exists(self, tmp_path):
        """E1（docs/17 §三）：无关联资产也占一行（右表=—）；防重落「限制」，
        且子查询谓词从 raw_sql 回贴原文（引擎 where_clause 会丢子查询 WHERE）。"""
        bag = _bag(tmp_path, CASE08_ET)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])
        rows = _rows(out, "3表关联")
        assert len(rows) == 1
        r = rows[0]
        assert r[1] == "staging_orders" and r[2] == "s"
        assert r[3] == "—（无关联）"
        g = r[6]
        assert g.startswith("限制：NOT EXISTS")
        assert "WHERE t.order_id = s.order_id" in g   # 原文保真（贴码原则）


# ═══════════════════════════════════════════════════════════════
# LTS 调度匹配端到端（docs/17 §二/§六）
# ═══════════════════════════════════════════════════════════════

def _write_lts_excel(path, tasks):
    """构造 LTS 制品（tasks/jobs/taskParams 三 sheet，列头对齐平台导出）。

    tasks: [(task_name, pjob, project, group, cron)]，pjob 为空则不写 jobs 行。
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "tasks"
    ws.append(["项目名称", "任务组名称", "任务名称", "任务类型", "开始时间",
               "结束时间", "调度周期", "依赖上一周期", "责任人", "调度频率配置"])
    for name, pjob, project, group, cron in tasks:
        ws.append([project, group, name, "周期任务", "", "", cron, "", "zhang_san", ""])
    wsj = wb.create_sheet("jobs")
    wsj.append(["项目名称", "任务组名称", "任务名称", "job名称"])
    for name, pjob, project, group, cron in tasks:
        if pjob:
            wsj.append([project, group, name, pjob])
    wsp = wb.create_sheet("taskParams")
    wsp.append(["项目名称", "任务组名称", "任务名称", "参数名称", "参数值"])
    wsp.append(["p", "g", tasks[0][0], "V_GROUP_CODE", ""])
    wb.save(str(path))


class TestLtsScheduleMatching:

    def test_matched_unmatched_and_remarks(self, tmp_path):
        """三档之精确档：命中 2 源表填调度 7 列；未匹配任务尾部列举。"""
        bag = _bag(tmp_path, GOLDEN_ET)
        _write_lts_excel(bag / "lts.xlsx", [
            ("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di",
             "PRJ_CUST_DAILY", "GRP_ASSET", "0 30 2 * * ?"),
            ("task_ods_cust_info_di", "Pjob_ods_cust_info_di",
             "PRJ_CUST_DAILY", "GRP_ASSET", "0 40 2 * * ?"),
            ("task_ods_order_di", "Pjob_ods_order_di",
             "PRJ_CUST_DAILY", "GRP_ASSET", "0 50 2 * * ?"),
        ])
        out = tmp_path / "spec.xlsx"
        stdout = _run_spec(["--dir", str(bag), "--output", str(out),
                            "--scenario", "P_CUR"])
        assert "调度匹配" in stdout and "命中 2" in stdout

        rows = _rows(out, "2数据源清单")
        assert len(rows) == 3
        src = [r for r in rows if r[2]]
        assert len(src) == 2
        r0 = src[0]
        assert r0[5] == "task_ods_acct_balance_di"
        assert r0[9] == "Pjob_ods_acct_balance_di"
        assert r0[11] == "0 30 2 * * ?" and r0[10] == "周期任务"
        assert r0[7] == "PRJ_CUST_DAILY" and r0[8] == "GRP_ASSET"
        assert not r0[6]                                  # 调度应用留空（缺口1）
        assert "精确匹配" in (r0[12] or "")
        assert all("LTS制品未提供" not in (r[12] or "") for r in src)  # 两态不混淆
        um = [r for r in rows if not r[2]]
        assert len(um) == 1 and um[0][5] == "task_ods_order_di"
        assert "未匹配到本资产源表" in (um[0][12] or "")

    def test_lts_provided_but_zero_match_disclosure(self, tmp_path):
        """两态之「给了没匹配上」：全未命中 → 调度列空 + 披露（区别于没给）。"""
        bag = _bag(tmp_path, CASE08_ET)
        _write_lts_excel(bag / "lts.xlsx", [
            ("task_unrelated", "Pjob_somewhere_else", "P", "G", "0 0 3 * * ?"),
        ])
        out = tmp_path / "spec.xlsx"
        stdout = _run_spec(["--dir", str(bag), "--output", str(out)])
        assert "命中 0" in stdout
        assert "给了没匹配上" in stdout                    # 两态披露之「给了没匹配上」
        rows = _rows(out, "2数据源清单")
        src = [r for r in rows if r[2]]
        assert len(src) == 1 and not src[0][5]            # 调度列空
        assert not any("LTS制品未提供" in (r[12] or "") for r in src)  # 不是「没给」
        um = [r for r in rows if not r[2]]
        assert len(um) == 1                                # 任务列举


# ═══════════════════════════════════════════════════════════════
# CTE 重度案例（case_17：4 CTE，主查询 JOIN CTE，设计场景②）
# ═══════════════════════════════════════════════════════════════

class TestCteCase:
    """CTE 坍缩长相（docs/17 §三 + D2 拍板）：右表=物理真名、ON 用预处理后
    别名照抄、预处理只贴相关 CTE 依赖链；CTE 名绝不进数据源清单。"""

    def test_cte_collapse_and_preprocess_paste(self, tmp_path):
        bag = _bag(tmp_path, CASE17_ET)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        # 数据源清单：4 张物理表，CTE 名零出现
        srows = _rows(out, "2数据源清单")
        assert {r[2] for r in srows} == {"dim_user", "orders", "events", "logins"}
        assert all("cte" not in (r[2] or "").lower() for r in srows)

        # 表关联：主表 dim_user × 3 右表；ON 引用 CTE 别名（真实代码样子）
        jrows = _rows(out, "3表关联")
        assert len(jrows) == 3
        by_right = {r[3]: r for r in jrows}
        o = by_right["orders"]
        assert o[1] == "dim_user" and o[2] == "u"
        assert o[4] in (None, "")                          # 内部别名（无则空，D2）
        assert "u.user_id = c2.user_id" in o[6]
        # 贴码只贴相关依赖链（cte1→cte2），不贴全量 WITH
        assert "[CTE cte1]" in o[6] and "[CTE cte2]" in o[6]
        assert "[CTE cte3]" not in o[6]
        e = by_right["events"]
        assert "u.user_id = c3.user_id" in e[6] and "[CTE cte3]" in e[6]

    def test_cte_fields_physical_source_backfill(self, tmp_path):
        """CTE 内加工字段：源表列用 CTE 主物理源回填 + 备注指向表3 贴码。"""
        bag = _bag(tmp_path, CASE17_ET)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])
        frows = _rows(out, "4需求字段")
        by_target = {r[10]: r for r in frows}
        assert by_target["user_name"][2] == "dim_user"     # 直取不受影响
        assert by_target["user_name"][5] == "直取字段"
        cnt = by_target["cnt"]
        assert cnt[2] == "events"                          # CTE 主物理源回填
        assert "表3预处理" in (cnt[12] or "")
        assert by_target["total"][5] == "加工字段"
        assert "SUM(amount)" in (by_target["total"][6] or "")


# ═══════════════════════════════════════════════════════════════
# 谓词守恒校验（docs/17 §七）
# ═══════════════════════════════════════════════════════════════

class TestPredicateConservation:

    def test_clean_render_zero_warnings_and_tamper_detected(self, tmp_path):
        """完整渲染 → 零告警；剪掉一段（模拟渲染漏放）→ 检出 structural。"""
        from pipeline_core import run_from_dir
        from spec_export import (build_spec_model, find_scenario,
                                 select_scenario)
        from spec_extract import predicate_conservation
        from viewmodel import build_context

        bag = _bag(tmp_path, GOLDEN_ET)
        with contextlib.redirect_stdout(io.StringIO()):
            core = run_from_dir(str(bag), include_dq=False, channel="spec",
                                entry_title="")
        k = select_scenario(core["knowledge"],
                            find_scenario(core["knowledge"], "P_CUR"))
        model = build_spec_model(k, core["engine_ctx"])
        ctx = build_context(k)

        # 完整渲染：黄金案例三个谓词（R0001 WHERE / R0002 HAVING / R0003 WHERE）全落位
        assert predicate_conservation(ctx, model["sheet3"], model["sheet4"]) == []
        # model 披露里也不应有 structural（守恒告警直接升 review）
        assert not any(d["severity"] == "structural" for d in model["disclosures"])

        # 剪掉写入前限制段 → R0003 WHERE 漏放被检出
        g = model["sheet3"][0][6]
        g_tampered = g.split("；写入前限制")[0]
        tampered_row = [None] * 6 + [g_tampered]
        warns = predicate_conservation(ctx, [tampered_row], model["sheet4"])
        assert any(w["type"] == "predicate_missing_where" for w in warns)

        # 剪掉主表链预处理段 → R0002 HAVING 漏放被检出
        g_tampered2 = g.split("；主表链预处理")[0]
        warns2 = predicate_conservation(
            ctx, [[None] * 6 + [g_tampered2]], model["sheet4"])
        assert any(w["type"] == "predicate_missing_having" for w in warns2)
