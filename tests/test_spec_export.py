"""SPEC 通道端到端测试 —— spec 入口 → 需求SPEC.xlsx（docs/17 §〇~§五波A 口径）。

黄金案例 = docs/术加制品参考案例（DWB_CUST_ASSET_F：双分区多场景 + 中间表链）；
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

GOLDEN_ET = PROJECT_ROOT / "docs" / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_F.xlsx"
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
# 黄金案例（DWB_CUST_ASSET_F，场景 P_CUR）
# ═══════════════════════════════════════════════════════════════

class TestGoldenCase:
    """端到端对拍 docs/需求SPEC-案例mock-DWB_CUST_ASSET_F.xlsx（波A 基础列）。"""

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
        assert [r[0] for r in rows] == [1, 2]         # 记录ID 顺序生成
        assert all("tmp_" not in str(r) for r in rows)   # 中间表零出现
        assert all("LTS制品未提供" in (r[12] or "") for r in rows)  # 两态备注

    def test_sheet3_materialization_invariant(self, golden_run):
        """物化无关模型（docs/14 P1/C2/C3 拍板）：tmp 链压平为嵌套锚，每表一行，
        登记值零 tmp 名/规则编号；GROUP BY/HAVING 在定义片段内逐字。"""
        out, _ = golden_run
        rows = _rows(out, "3表关联")
        assert len(rows) == 2
        main, inner = rows
        # 主表行：驱动表（tmp 链最深层 FROM 首表）
        assert (main[2], main[3], main[5]) == ("ods_acct_balance_di", "a", "主表")
        g = main[6]
        assert "限定：a.del_flag = 'N' AND a.acct_date = '20260818'" in g
        # 嵌套锚定义挂载：深层（t=R0001）在前，外层（s=R0002）随后，逐字原文
        assert "【逻辑从表 t · 定义】SELECT" in g
        assert "JOIN ods.ods_cust_info_di c ON a.cust_id = c.cust_id" in g
        assert "【逻辑从表 s · 定义】SELECT" in g
        assert "GROUP BY t.cust_id, t.asset_type HAVING SUM(t.balance) > 0" in g
        # 场景写入过滤（锚 s 引用合法形态）
        assert "写入前限制：s.data_flag = 'D'" in g
        # 生产步内物理表：每表一行，【锚 内】ON 原文 + 指回驱动表行
        assert (inner[1], inner[2], inner[3], inner[4], inner[5]) == (
            "ods_acct_balance_di", "a", "ods_cust_info_di", "c", "INNER JOIN")
        assert "【t 内】a.cust_id = c.cust_id" in inner[6]
        assert "参与逻辑从表 t，定义见 a 行" in inner[6]
        # 物化零痕迹（登记值/关联方式列）：tmp 名与规则编号零出现
        for r in rows:
            assert "tmp_" not in str(r[:6]) and "R000" not in str(r[:6])

    def test_sheet4_field_typing_and_constants(self, golden_run):
        """需求字段：资产字段不重复（单行合并）；四枚举类型；记录ID 顺序生成。"""
        out, _ = golden_run
        rows = _rows(out, "4需求字段")
        assert len(rows) == 4
        assert [r[10] for r in rows] == ["cust_id", "asset_type",
                                         "total_balance", "data_scope"]  # 资产字段零重复
        assert [r[0] for r in rows] == [1, 2, 3, 4]     # 记录ID 顺序生成
        by_target = {r[10]: r for r in rows}
        # 跨步加工：终态步骤 direct，但穿透 transform=aggregate → 数据加工，
        # 逻辑列 = 拆解式（无规则编码、无表级条件、AS 尾巴剥离）
        tb = by_target["total_balance"]
        assert tb[5] == "数据加工"
        # 展开态（C1）：物理根别名前缀 + 说明段（分组/保留/锚），无前缀词
        assert tb[6] == ("SUM(a.balance)（按 cust_id, asset_type 分组，"
                         "保留 SUM(balance) > 0；经逻辑从表 s）")
        assert (tb[2], tb[4]) == ("ods_acct_balance_di", "balance")
        assert tb[1] == "balance"                        # 业务需求字段 = copy 源字段（Q5）
        # 常量字段：value → 赋值；说明段=锚+场景（不带分组口径）
        ds = by_target["data_scope"]
        assert ds[5] == "赋值" and ds[6] == "'CUR'（经逻辑从表 s；场景 P_CUR 常量）"
        assert ds[2] is None and "无物理源" in (ds[12] or "")
        # 直取穿链：备注指锚
        assert "经逻辑从表 s" in (by_target["cust_id"][12] or "")
        # 直取
        assert by_target["asset_type"][5] == "直接复制"
        assert by_target["asset_type"][6] in (None, "")
        # 终态列
        assert all(r[8] == "dws.dwb_cust_asset_f" for r in rows)

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
        assert all(r[5] == "直接复制" for r in frows)
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

def _write_lts_excel(path, upstreams, f_name, deps, group_code="GR_CUST_ASSET"):
    """构造 LTS 制品（任务图：上游任务 + 资产 F 任务 + tskdep 依赖，实测⑧口径）。

    upstreams: [(task_name, pjob, project, group, cron)] 上游（被依赖）任务
    f_name: 资产 F 任务名（需 *_F 结尾才会被分类为 f_tasks）
    deps: 资产 F 任务的 tskdep job 名列表（= 上游任务主 job 名）
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "tasks"
    ws.append(["项目名称", "任务组名称", "任务名称", "任务类型", "开始时间",
               "结束时间", "调度周期", "依赖上一周期", "责任人", "调度频率配置"])
    f_row = ["PRJ_CUST_DAILY", "GRP_ASSET", f_name, "周期任务", "", "",
             "0 0 3 * * ?", "", "zhang_san", ""]
    ws.append(f_row)
    for name, pjob, project, group, cron in upstreams:
        ws.append([project, group, name, "周期任务", "", "", cron, "", "zhang_san", ""])
    wsj = wb.create_sheet("jobs")
    wsj.append(["项目名称", "任务组名称", "任务名称", "job名称", "job类型"])
    # 资产 F 任务：主 job（父为 getdata 类根）+ tskdep 依赖 job
    wsj.append(["PRJ_CUST_DAILY", "GRP_ASSET", f_name,
                f"Pjob_{f_name[:-2]}", "url"])
    for d in deps:
        wsj.append(["PRJ_CUST_DAILY", "GRP_ASSET", f_name, d, "tskdep"])
    for name, pjob, project, group, cron in upstreams:
        if pjob:
            wsj.append([project, group, name, pjob, "url"])
    wsp = wb.create_sheet("taskParams")
    wsp.append(["项目名称", "任务组名称", "任务名称", "参数名称", "参数值"])
    wsp.append(["PRJ_CUST_DAILY", "GRP_ASSET", f_name, "V_GROUP_CODE", group_code])
    wb.save(str(path))


class TestLtsScheduleMatching:

    def test_matched_unmatched_and_remarks(self, tmp_path):
        """三档之精确档：命中 2 源表填调度 7 列；未匹配任务尾部列举。"""
        bag = _bag(tmp_path, GOLDEN_ET)
        _write_lts_excel(
            bag / "lts.xlsx",
            upstreams=[
                ("task_ods_acct_balance_di", "Pjob_ods_acct_balance_di",
                 "PRJ_CUST_DAILY", "GRP_ASSET", "0 30 2 * * ?"),
                ("task_ods_cust_info_di", "Pjob_ods_cust_info_di",
                 "PRJ_CUST_DAILY", "GRP_ASSET", "0 40 2 * * ?"),
                ("task_ods_order_di", "Pjob_ods_order_di",
                 "PRJ_CUST_DAILY", "GRP_ASSET", "0 50 2 * * ?"),
            ],
            f_name="DWB_CUST_ASSET_F_F",
            deps=["Pjob_ods_acct_balance_di", "Pjob_ods_cust_info_di",
                  "Pjob_ods_order_di"],
        )
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
        assert "精确匹配" in (r0[12] or "")            # 按依赖job（tskdep）表名精确匹配
        assert all(r[5] != "DWB_CUST_ASSET_F_F" for r in rows)      # 资产自身任务不进匹配
        assert all(r[9] != "Pjob_DWB_CUST_ASSET_F" for r in rows)    # 资产自身主 job 不进匹配
        assert all("LTS制品未提供" not in (r[12] or "") for r in src)  # 两态不混淆
        um = [r for r in rows if not r[2]]
        assert len(um) == 1 and um[0][5] == "task_ods_order_di"
        assert "未唯一匹配到本资产源表" in (um[0][12] or "")

    def test_lts_provided_but_zero_match_disclosure(self, tmp_path):
        """两态之「给了没匹配上」：全未命中 → 调度列空 + 披露（区别于没给）。"""
        bag = _bag(tmp_path, CASE08_ET)
        _write_lts_excel(
            bag / "lts.xlsx",
            upstreams=[("task_unrelated", "Pjob_somewhere_else",
                        "P", "G", "0 0 3 * * ?")],
            f_name="DWB_INCREMENTAL_F_F",
            deps=["Pjob_somewhere_else"],
        )
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
        assert by_target["user_name"][5] == "直接复制"
        cnt = by_target["cnt"]
        assert cnt[2] == "events"                          # CTE 主物理源回填
        assert "表3预处理" in (cnt[12] or "")
        assert by_target["total"][5] == "数据加工"
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

        # 剪掉定义片段（HAVING 所在）→ R0002 HAVING 漏放被检出
        g_tampered2 = g.split("；【逻辑从表 s · 定义】")[0]
        warns2 = predicate_conservation(
            ctx, [[None] * 6 + [g_tampered2]], model["sheet4"])
        assert any(w["type"] == "predicate_missing_having" for w in warns2)

    def test_reference_closure_and_trace_checks(self, tmp_path):
        """引用闭包 + 物化零痕迹（docs/14 §六性质1/7）：干净模型零告警；
        伪造断链引用 / tmp 痕迹 → structural 检出。"""
        import contextlib
        import io as _io
        from pipeline_core import run_from_dir
        from spec_export import (_materialization_trace_check,
                                 _reference_closure, build_spec_model,
                                 find_scenario, select_scenario)
        from viewmodel import build_context

        bag = _bag(tmp_path, GOLDEN_ET)
        with contextlib.redirect_stdout(_io.StringIO()):
            core = run_from_dir(str(bag), include_dq=False, channel="spec",
                                entry_title="")
        k = select_scenario(core["knowledge"],
                            find_scenario(core["knowledge"], "P_CUR"))
        model = build_spec_model(k, core["engine_ctx"])
        ctx = build_context(k)
        sheet2, sheet3, sheet4 = model["sheet2"], model["sheet3"], model["sheet4"]

        # 干净模型：零违规（黄金在 W1 已物化无关化）
        assert _reference_closure(ctx, sheet2, sheet3, sheet4) == []
        assert _materialization_trace_check(ctx, sheet2, sheet3, sheet4) == []

        # 断链引用：表达式引用未登记别名 → 闭包检出（说明段豁免）
        bad4 = [None] * 6 + ["SUM(z.unknown)（经逻辑从表 s）"] + [None] * 6
        warns = _reference_closure(ctx, sheet2, sheet3, [bad4])
        assert any(w["type"] == "reference_closure" and "z.unknown" in w["title"]
                   for w in warns)
        ok4 = [None] * 6 + ["SUM(a.balance)（经逻辑从表 s）"] + [None] * 6
        assert _reference_closure(ctx, sheet2, sheet3, [ok4]) == []

        # 物化痕迹：登记值出现 tmp 名/规则编号 → 检出；G 列片段豁免
        bad2 = [None] * 4 + ["tmp_cust_asset_sum 链"] + [None] * 8
        warns2 = _materialization_trace_check(ctx, [bad2], sheet3, sheet4)
        assert any(w["type"] == "materialization_trace"
                   and "tmp_cust_asset_sum" in w["title"] for w in warns2)
        frag3 = [None] * 6 + ["【逻辑从表 s · 定义】SELECT … FROM dws.tmp_cust_asset_sum t"]
        assert _materialization_trace_check(ctx, sheet2, [frag3], sheet4) == []


# ═══════════════════════════════════════════════════════════════
# 多源合并（case_18_many_case_when：CASE WHEN 双来源字段，实测修订③）
# ═══════════════════════════════════════════════════════════════

class TestMultiSourceMerge:

    def test_case_when_two_sources_single_row(self, tmp_path):
        """CASE WHEN 引用同表两字段（amount+month）：资产字段一行，源字段列内合并。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_18_many_case_when" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        rows = _rows(out, "4需求字段")
        targets = [r[10] for r in rows]
        assert len(targets) == len(set(targets)), f"资产字段重复: {targets}"
        by_target = {r[10]: r for r in rows}
        amt = by_target["amt_01"]
        assert (amt[2], amt[4]) == ("monthly_sales", "t.amount;t.month")  # 英文分号+别名.字段
        assert amt[5] == "数据加工"
        assert "SUM(CASE WHEN" in (amt[6] or "")               # 逻辑为整表达式
        assert [r[0] for r in rows] == list(range(1, len(rows) + 1))


# ═══════════════════════════════════════════════════════════════
# 包裹形态（case_28：FROM (整个关联逻辑) s WHERE，实测修订⑦）
# ═══════════════════════════════════════════════════════════════

class TestWrappedFromSubquery:

    def test_wrapped_decomposed_rows(self, tmp_path):
        """FROM 包裹（W4 分解模型）：直接 JOIN 行 + 锚行（含内部表行）+ 包裹
        残余行（虚拟逻辑表，方式空）——引擎此形态零结构化，全部由片段分解。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_28_wrapped_from_subquery" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        srows = _rows(out, "2数据源清单")
        assert {r[2] for r in srows} == {"dwd_cust_main", "ods_order_pay",
                                         "ods_pay_channel", "dim_cust_grade"}
        jrows = _rows(out, "3表关联")
        assert len(jrows) == 4
        by_right = {r[3]: r for r in jrows}
        # 锚行：右表=子查询驱动表，右别名=锚 b，定义片段逐字
        b_row = by_right["ods_order_pay"]
        assert (b_row[1], b_row[2], b_row[4], b_row[5]) == (
            "dwd_cust_main", "a", "b", "LEFT JOIN")
        assert "a.cust_id = b.cust_id" in b_row[6]
        assert "【逻辑从表 b · 定义】(" in b_row[6]
        assert "GROUP BY e.cust_id" in b_row[6]
        # 内部表行（每表一行，指回最外层锚）
        g_row = by_right["ods_pay_channel"]
        assert (g_row[1], g_row[2], g_row[4], g_row[5]) == (
            "ods_order_pay", "e", "g", "INNER JOIN")
        assert "【b 内】e.channel_id = g.channel_id" in g_row[6]
        # 直接 JOIN 行
        f_row = by_right["dim_cust_grade"]
        assert (f_row[1], f_row[5]) == ("dwd_cust_main", "INNER JOIN")
        assert "a.grade_id = f.grade_id" in f_row[6]
        # 包裹残余行（D-b：虚拟逻辑表，方式空，装外层限定）
        s_row = jrows[-1]
        assert (s_row[1], s_row[3], s_row[4]) == (
            "dwd_cust_main", "虚拟逻辑表 s", "s")
        assert s_row[5] in ("", None)
        assert "限定：NOT s.cust_id IS NULL" in s_row[6]
        assert "【s 内】关联见上" in s_row[6]
        # Sheet4：⑯ 多跳代入升级——经 b 加工字段穿透为物理呈现
        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        ta = by["total_amt"]
        assert ta[5] == "数据加工"
        assert ta[6] == "SUM(e.pay_amt)（按 cust_id 分组收敛；经逻辑从表 b）"
        assert (ta[2], ta[3], ta[4]) == ("ods_order_pay", "e", "pay_amt")
        assert by["cust_id"][5] == "直接复制"
        assert by["cust_id"][2] == "dwd_cust_main"
        assert by["cust_id"][3] == "a"          # 别名注册表回填（实测问题2修复）


# ═══════════════════════════════════════════════════════════════
# 嵌套包裹（case_29：FROM (t1 ⋈ ((t2⋈t3) tt GROUP BY) b ⋈ t4) s WHERE rn=1）
# ═══════════════════════════════════════════════════════════════

class TestWrappedNestedSubqueries:

    def test_user_case_four_rows(self, tmp_path):
        """用户实测案例（2026-11-03）：嵌套子查询包裹 → 四行（t1⋈t2-b /
        t2⋈t3【b 内】/ t1⋈t4 / t1⋈虚拟s 开窗收敛），D-a 左表=驱动表。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_29_wrapped_nested_subqueries" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        srows = _rows(out, "2数据源清单")
        assert {r[2] for r in srows} == {"dwd_cust_main", "ods_cust_pay",
                                         "ods_channel", "dim_cust_grade"}
        jrows = _rows(out, "3表关联")
        assert len(jrows) == 4
        r1, r2, r3, r4 = jrows
        # 行1：t1 ⋈ t2（b 的驱动表），右别名=锚 b，定义片段含嵌套 tt（整块逐字）
        assert (r1[1], r1[2], r1[3], r1[4], r1[5]) == (
            "dwd_cust_main", "m", "ods_cust_pay", "b", "LEFT JOIN")
        assert "m.cust_id = b.cust_id" in r1[6]
        assert "【逻辑从表 b · 定义】(" in r1[6]
        assert "GROUP BY tt.cust_id" in r1[6]           # tt 层在 b 片段内逐字
        # 行2：t2 ⋈ t3（D-a：左表=驱动表），指回最外层锚 b
        assert (r2[1], r2[2], r2[3], r2[4], r2[5]) == (
            "ods_cust_pay", "p", "ods_channel", "ch", "LEFT JOIN")
        assert "【b 内】p.channel_id = ch.channel_id" in r2[6]
        assert "参与逻辑从表 b，定义见 ods_cust_pay 行" in r2[6]
        # 行3：直接 JOIN
        assert (r3[3], r3[5]) == ("dim_cust_grade", "LEFT JOIN")
        assert "m.grade_id = g.grade_id" in r3[6]
        # 行4：包裹残余行（D-b：虚拟逻辑表，方式空，开窗收敛）
        assert (r4[1], r4[3], r4[4]) == ("dwd_cust_main", "虚拟逻辑表 s", "s")
        assert r4[5] in ("", None)
        assert "限定：s.rn = 1（开窗收敛，窗口定义见原文）" in r4[6]
        assert "【s 内】关联见上" in r4[6]
        # Sheet4：别名经 Sheet3 注册表回填（实测问题2）
        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        assert {r[10] for r in frows} == {"order_id", "cust_name",
                                          "pay_channel", "grade_name"}
        assert by["order_id"][3] == "m"
        assert by["grade_name"][3] == "g"
        assert by["order_id"][5] == "直接复制"
        # ⑯ 代入升级：穿 b 直取字段 → 物理呈现 + 直取说明段
        pc = by["pay_channel"]
        assert pc[5] == "直接复制"
        assert pc[6] == "ch.channel_name（经逻辑从表 b 直取）"
        assert (pc[2], pc[3], pc[4]) == ("ods_channel", "ch", "channel_name")


# ═══════════════════════════════════════════════════════════════
# 跨聚合多跳值链（case_30：tt 层 CASE → b 层 SUM CASE 收敛，⑯ 代入）
# ═══════════════════════════════════════════════════════════════

class TestCrossAggValueChain:

    def test_field1_expanded_semantic_expr(self, tmp_path):
        """field1 两跳值链（tt CASE → b SUM CASE 收敛）→ 跨聚合语义表达式：
        各跳原样代入、聚合函数内嵌、粒度与锚进说明段、物理根引用。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_30_cross_agg_chain" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        f1 = by["field1"]
        assert f1[5] == "数据加工"
        assert f1[6] == ("CASE WHEN SUM(CASE WHEN ch.channel_type = '01' "
                         "THEN 'ZC' END) > 0 THEN 'Y' ELSE 'N' END"
                         "（按 cust_id 分组收敛；经逻辑从表 b）")
        assert (f1[2], f1[3], f1[4]) == ("ods_channel", "ch", "channel_type")


# ═══════════════════════════════════════════════════════════════
# 用户真实结构语料（case_31：tt CASE → b SUM CASE 收敛 → s 带出 + rn 开窗）
# ═══════════════════════════════════════════════════════════════

class TestRealNestedChain:

    def test_full_chain_and_inner_where(self, tmp_path):
        """用户实测结构（2026-11-03）：is_flag 三层链全展开到物理根；
        s 层内部 WHERE（b.is_flag IS NOT NULL）落限制段不丢失。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_31_real_nested_chain" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        f1 = by["is_flag"]
        assert f1[5] == "数据加工"
        assert f1[6] == ("CASE WHEN SUM(CASE WHEN t3.tag_id = '1' "
                         "THEN '1' ELSE '0' END) > 0 THEN 'Y' ELSE 'N' END"
                         "（按 account_id 分组收敛；经逻辑从表 b）")
        assert (f1[2], f1[3], f1[4]) == ("ods_acct_tag", "t3", "tag_id")
        assert by["id"][5] == "直接复制"
        assert (by["id"][2], by["id"][4]) == ("ods_acct_main", "account_id")

        jrows = _rows(out, "3表关联")
        assert len(jrows) == 4
        s_row = jrows[-1]
        assert "限制：NOT b.is_flag IS NULL" in s_row[6]  # 引擎渲染归一（结构保真）      # 内层 WHERE 落位
        assert "限定：s.rn = 1（开窗收敛，窗口定义见原文）" in s_row[6]
        b_row = next(r for r in jrows if r[4] == "b")
        assert "SUM(tt.is_flag) > 0" in b_row[6]
        t3_row = next(r for r in jrows if r[4] == "t3")
        assert "【b 内】t2.id = t3.id AND t3.tag_name = 'Y'" in t3_row[6]


# ═══════════════════════════════════════════════════════════════
# sysdate 字段（case_31b：s 层 sysdate() AS dw_last_update_date，⑲）
# ═══════════════════════════════════════════════════════════════

class TestSysdateField:

    def test_sysdate_assign_and_others_intact(self, tmp_path):
        """sysdate() 字段：纯函数无物理引用 → 赋值 + 代入出表达式；
        兜底格式穿透项（无 step_id）不阻断代入；其他字段不受影响。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_31b_sysdate_field" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])
        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        dw = by["dw_last_update_date"]
        assert dw[5] == "赋值"
        # sqlglot oracle 方言归一：sysdate() → CURRENT_TIMESTAMP（结构保真）
        assert dw[6] == "CURRENT_TIMESTAMP（经逻辑从表 s）"
        # 其他字段不受 s 层新增字段影响
        assert by["is_flag"][5] == "数据加工"
        assert "SUM(CASE WHEN t3.tag_id" in by["is_flag"][6]
        assert by["id"][5] == "直接复制"


# ═══════════════════════════════════════════════════════════════
# 用户真实 SQL（case_32：含 decode/nvl/sysdate/${p_cycle_id}/嵌套子查询）
# ═══════════════════════════════════════════════════════════════

class TestEmpRealSql:

    def test_full_field_set(self, tmp_path):
        """用户实测 SQL（docs/emp/案例.sql）全 8 字段端到端。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_32_emp_real_sql" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        assert {r[10] for r in frows} == {
            "id", "account_number", "is_datacom_flag", "cust_account_key",
            "create_by", "del_flag", "crt_cycle_id", "dw_last_update_date"}

        # 核心字段：三层链全展开（tt CASE → b SUM CASE → s 带出）
        f1 = by["is_datacom_flag"]
        assert f1[5] == "数据加工"
        assert f1[6] == ("CASE WHEN SUM(CASE WHEN t3.tag_id = '1122334455' "
                         "THEN '1' ELSE '0' END) > 0 THEN 'Y' ELSE 'N' END"
                         "（按 account_id 分组收敛；经逻辑从表 b）")
        assert (f1[2], f1[3], f1[4]) == ("eb_account_common_tag_t", "t3", "tag_id")

        # decode/nvl 多表多源
        ck = by["cust_account_key"]
        assert ck[5] == "数据加工"
        assert "DECODE(t1.account_number" in ck[6]
        assert "NVL(cust_d.cust_account_key" in ck[6]
        assert set(ck[2].split("、")) == {"eb_account_t", "customer_d"}

        # 同表双字段 CASE
        df = by["del_flag"]
        assert df[5] == "数据加工"
        assert "t1.del_flag" in df[6] and "t1.delete_flag" in df[6]

        # 平台变量 + 系统函数
        assert by["crt_cycle_id"][5] == "赋值"
        assert "'${p_cycle_id}'" in by["crt_cycle_id"][6]
        assert by["dw_last_update_date"][5] == "赋值"
        assert "CURRENT_TIMESTAMP" in by["dw_last_update_date"][6]

        # 直取
        for fn in ("id", "account_number", "create_by"):
            assert by[fn][5] == "直接复制"
            assert by[fn][2] == "eb_account_t"

        # 表关联四行
        jrows = _rows(out, "3表关联")
        assert len(jrows) == 4
        assert {r[3] for r in jrows} == {
            "eb_account_tag_t", "eb_account_common_tag_t",
            "customer_d", "虚拟逻辑表 s"}


class TestJoinKindNormalization:

    def test_join_kind_enum(self):
        """关联方式归一（实测修订②）：只出标准枚举。"""
        from spec_extract import _join_kind
        assert _join_kind("JOIN") == "INNER JOIN"
        assert _join_kind("INNER JOIN") == "INNER JOIN"
        assert _join_kind("LEFT OUTER JOIN") == "LEFT JOIN"
        assert _join_kind("left join") == "LEFT JOIN"
        assert _join_kind("") == ""


# ═══════════════════════════════════════════════════════════════
# 嵌套子查询（case_27：a JOIN ((e⋈g WHERE) GROUP BY) b JOIN f，2026-11-02 方案）
# ═══════════════════════════════════════════════════════════════

class TestNestedSubquery:

    def test_subquery_join_row_shape(self, tmp_path):
        """main-subquery 行：右表=子查询主物理源、右别名=外层别名、ON 原文、
        预处理贴子查询原文（e⋈g/WHERE/GROUP BY 全收贴码，不单独成行）。"""
        et = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / \
            "case_27_nested_subquery_join" / "execution_tasks.xlsx"
        bag = _bag(tmp_path, et)
        out = tmp_path / "spec.xlsx"
        _run_spec(["--dir", str(bag), "--output", str(out)])

        # Sheet2：四张物理源表（a/e/g/f），子查询伪名零出现
        srows = _rows(out, "2数据源清单")
        assert [r[2] for r in srows] == ["dwd_cust_main", "ods_order_pay",
                                         "ods_pay_channel", "dim_cust_grade"]

        # Sheet3：三行（每表一行），关联方式全在标准枚举内
        jrows = _rows(out, "3表关联")
        assert len(jrows) == 3
        assert all(r[5] in ("LEFT JOIN", "RIGHT JOIN", "INNER JOIN",
                            "FULL JOIN", "CROSS JOIN", "") for r in jrows)
        sq = next(r for r in jrows if r[3] == "ods_order_pay")
        assert (sq[1], sq[2], sq[4], sq[5]) == ("dwd_cust_main", "a", "b", "LEFT JOIN")
        g = sq[6]
        assert "a.cust_id = b.cust_id" in g                       # ON 原文（外层别名）
        assert "限制：a.stat_date = '20261031'" in g              # 外层 WHERE 落限制段
        assert "【逻辑从表 b · 定义】(" in g   # 波2b 统一挂载标签
        for frag in ("JOIN ods.ods_pay_channel g ON e.channel_id = g.channel_id",
                     "WHERE e.del_flag = 'N'", "GROUP BY e.cust_id"):
            assert frag in g, f"子查询贴码缺: {frag}"
        f_row = next(r for r in jrows if r[3] == "dim_cust_grade")
        assert f_row[5] == "INNER JOIN" and "a.grade_id = f.grade_id" in f_row[6]
        # 子查询内非驱动表：每表一行，【锚 内】ON 原文 + 指回（W2/C3）
        g_row = next(r for r in jrows if r[3] == "ods_pay_channel")
        assert (g_row[1], g_row[2], g_row[4], g_row[5]) == (
            "ods_order_pay", "e", "g", "INNER JOIN")
        assert "【b 内】e.channel_id = g.channel_id" in g_row[6]
        assert "参与逻辑从表 b，定义见 ods_order_pay 行" in g_row[6]

        # Sheet4：穿透不受子查询嵌套影响
        frows = _rows(out, "4需求字段")
        by = {r[10]: r for r in frows}
        assert by["total_amt"][5] == "数据加工"
        assert "SUM(e.pay_amt)" in by["total_amt"][6]
        assert (by["total_amt"][2], by["total_amt"][4]) == ("ods_order_pay", "pay_amt")
        assert by["cust_id"][5] == "直接复制"
