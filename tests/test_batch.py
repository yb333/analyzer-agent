"""批量分析测试。

验证多规则组批量解析 + 交付件生成 + 分批控制。

运行:
    pytest tests/test_batch.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_xlsx import build_xlsx


def _make_multi_group_xlsx(path, num_groups=3):
    """构造含多个规则组的 Excel。"""
    rules = []
    for g in range(num_groups):
        grp = f"GR{g+1:03d}"
        en = f"DWB_TEST_{g+1}_F"
        rules.append({"rule_code": f"R{g*2+1:04d}", "rule_type": 1, "exec_sequence": 1,
                      "target_schema": "dws", "target_table": f"tmp_{g}", "delete_mode": "1",
                      "query_sql": f"SELECT a.id, a.amount FROM ods.src_{g} a WHERE a.del='N'",
                      "rule_name": "源头", "rule_group_code": grp, "rule_group_en": en})
        rules.append({"rule_code": f"R{g*2+2:04d}", "rule_type": 1, "exec_sequence": 2,
                      "target_schema": "dws", "target_table": f"dwb_test_{g}_f", "delete_mode": "1",
                      "query_sql": f"SELECT t.id, SUM(t.amount) AS total FROM dws.tmp_{g} t GROUP BY t.id",
                      "rule_name": "汇总", "rule_group_code": grp, "rule_group_en": en})
    build_xlsx(str(path), rules=rules)
    return str(path)


@pytest.fixture
def multi_group_xlsx(tmp_path):
    return _make_multi_group_xlsx(tmp_path / "multi.xlsx")


class TestBatchAnalysis:
    """批量分析基本功能。"""

    def test_batch_generates_all_groups(self, multi_group_xlsx, tmp_path):
        """批量生成所有规则组的交付件"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        assert len(results) == 3, f"应处理 3 个规则组，实际 {len(results)}"
        for r in results:
            assert r.success, f"{r.rule_group_en} 应成功，错误: {r.error}"

    def test_batch_creates_output_dirs(self, multi_group_xlsx, tmp_path):
        """每个规则组应有独立输出目录"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        for r in results:
            out_path = Path(r.output_dir)
            assert out_path.exists(), f"输出目录应存在: {out_path}"

    def test_batch_dirs_named_per_group_not_global(self, multi_group_xlsx, tmp_path):
        """防回归：不同规则组必须用各自英文名建目录，不能用全局英文名撞名覆盖。

        历史 bug：RawRule 没存每行 rule_group_en，batch 用了 read_excel 返回的
        全局 rule_group_en（取第一个非空），导致所有 code 不同的组撞同一个目录名
        → 全写进同一目录互相覆盖。此用例锁定该回归。
        """
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        # 3 个组的 output_dir 必须两两不同（目录名各自独立）
        dirs = {Path(r.output_dir).name for r in results}
        assert len(dirs) == 3, f"3 个组应有 3 个独立目录，实际 {dirs}"
        # 目录名应是各自英文名，且都落在 output 基础目录下
        for r in results:
            name = Path(r.output_dir).name
            assert name == r.rule_group_en, f"目录名 {name} 应等于规则组英文名 {r.rule_group_en}"
            assert str(Path(r.output_dir).parent) == out, f"应在基础目录下: {r.output_dir}"

    def test_batch_generates_deliverables(self, multi_group_xlsx, tmp_path):
        """每个规则组应生成三个交付件"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)

        for r in results:
            if not r.success:
                continue
            out_path = Path(r.output_dir)
            assert (out_path / "mapping.xlsx").exists(), f"mapping.xlsx 应存在"
            assert (out_path / "asset_report.html").exists(), f"asset_report.html 应存在"
            assert (out_path / "tech_design.md").exists(), f"tech_design.md 应存在"
            assert (out_path / "knowledge_draft.json").exists(), f"knowledge_draft.json 应存在"

    def test_batch_size_split(self, multi_group_xlsx, tmp_path):
        """分批处理：batch_size=2 时 3 个组应分 2 批"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=2, no_ai=True)
        assert len(results) == 3, f"应处理 3 个规则组"

    def test_batch_same_name_different_code_not_overwritten(self, tmp_path):
        """防回归：英文名相同、编码不同的规则组（实时/离线区）不得互相覆盖。

        历史 bug：输出目录名只用 rule_group_en，当两个 code 不同但英文名相同的组
        （典型：实时区 RT + 离线区 OFF，表英文名相同）同时存在时，两者写进同一目录
        互相覆盖（generate_* 均覆盖写），后跑的组把先跑的组完整覆盖，造成该英文名
        下只有一组交付件、另一组丢失。此用例锁定：同名组目录追加 code 去重，
        两组各自独立、交付件都不丢。
        """
        import openpyxl
        # 两组：code 不同（RT001 实时 / OFF001 离线），英文名相同
        rules = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_same_f_rt", "delete_mode": "1",
             "query_sql": "SELECT a.id, 'RT' AS zone FROM ods.src_rt a",
             "rule_name": "实时", "rule_group_code": "RT001", "rule_group_en": "DWB_SAME_F"},
            {"rule_code": "R2", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "dwb_same_f_off", "delete_mode": "1",
             "query_sql": "SELECT b.id, 'OFF' AS zone FROM ods.src_off b",
             "rule_name": "离线", "rule_group_code": "OFF001", "rule_group_en": "DWB_SAME_F"},
        ]
        xlsx = str(tmp_path / "samename.xlsx")
        build_xlsx(xlsx, rules=rules)

        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(xlsx, out, batch_size=50, no_ai=True)

        # 1) 两组都被处理
        assert len(results) == 2, f"应处理 2 个规则组，实际 {len(results)}"
        for r in results:
            assert r.success, f"{r.rule_group_code} 应成功: {r.error}"

        # 2) 两组输出目录必须不同（不互相覆盖）
        dirs = {Path(r.output_dir).name for r in results}
        assert len(dirs) == 2, f"同名不同码组应有 2 个独立目录，实际 {dirs}"
        # 目录名都含 code 后缀（去重生效）
        for r in results:
            d = Path(r.output_dir).name
            assert r.rule_group_code in d, f"目录 {d} 应含 code {r.rule_group_code}"

        # 3) 两组交付件都存在，内容各自独立（核心：不互相覆盖）
        for r in results:
            op = Path(r.output_dir)
            assert (op / "mapping.xlsx").exists(), f"{r.rule_group_code}: mapping.xlsx 丢失"
            assert (op / "asset_report.html").exists(), f"{r.rule_group_code}: asset_report 丢失"
            assert (op / "tech_design.md").exists(), f"{r.rule_group_code}: tech_design 丢失"
            # target_table 不同 → tech_design.md 内容应不同（证明各自独立、非覆盖）
        td_rt = next(Path(r.output_dir) / "tech_design.md" for r in results if r.rule_group_code == "RT001").read_text(encoding="utf-8")
        td_off = next(Path(r.output_dir) / "tech_design.md" for r in results if r.rule_group_code == "OFF001").read_text(encoding="utf-8")
        assert td_rt != td_off, "实时/离线两组 tech_design 内容相同 → 说明被互相覆盖了"

    def test_batch_single_group_crash_does_not_kill_batch(self, tmp_path, monkeypatch):
        """防回归：一批里某组处理崩溃，不得拖垮同批其它组。

        历史 bug：_run_batch_inprocess 用列表推导 [_process_group(g) for g in groups]，
        一旦某组抛出 _process_group 内 try 未覆盖的异常（openpyxl 写 Excel、json
        序列化、OOM 等），整个列表推导中断，该组之后的组都不会被处理 → 表现为
        「一批里有的组正常、有的组直接没有文件夹」。此用例锁定：中间组崩溃时，
        前后组都正常生成交付件，失败组也有 output_dir 可追踪。

        注：主流程走子进程隔离，monkeypatch 不跨进程；这里直接测 _run_batch_inprocess
        （子进程入口 _run_child_batch 与降级路径都调用它），覆盖两条执行路径。
        """
        # 3 个组：A 正常 / B 将被注入崩溃 / C 正常（C 排在 B 之后，验证不被拖垮）
        rules = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f_a", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.src_a a",
             "rule_name": "A", "rule_group_code": "GRP_A", "rule_group_en": "DWB_A_F"},
            {"rule_code": "R2", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f_b", "delete_mode": "1",
             "query_sql": "SELECT b.id FROM ods.src_b b",
             "rule_name": "B", "rule_group_code": "GRP_B", "rule_group_en": "DWB_B_F"},
            {"rule_code": "R3", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f_c", "delete_mode": "1",
             "query_sql": "SELECT c.id FROM ods.src_c c",
             "rule_name": "C", "rule_group_code": "GRP_C", "rule_group_en": "DWB_C_F"},
        ]
        xlsx = str(tmp_path / "crash.xlsx")
        build_xlsx(xlsx, rules=rules)

        # 注入：处理 GRP_B 时抛异常（模拟 _process_group 内 try 穿透的极端异常，
        # 如 openpyxl IllegalCharacterError、OOM 被杀等）
        import batch as batch_mod
        real_process = batch_mod._process_group

        def crashing_process(group, *args, **kwargs):
            if group["rule_group_code"] == "GRP_B":
                raise RuntimeError("模拟崩溃：openpyxl 写 Excel 失败")
            return real_process(group, *args, **kwargs)

        monkeypatch.setattr(batch_mod, "_process_group", crashing_process)

        out = str(tmp_path / "output")
        # 直接调 _run_batch_inprocess（子进程与降级路径共用），不经过子进程封装
        results = batch_mod._run_batch_inprocess(xlsx, out, no_ai=True, ddl_dir="",
                                                  batch_start=0, batch_end=3)

        # 3 组都被处理（崩溃组也返回了失败结果，没让批处理中断）
        assert len(results) == 3, f"应处理 3 组，实际 {len(results)}"
        by_code = {r.rule_group_code: r for r in results}
        # A、C 正常（C 在崩溃组之后，验证不被拖垮）
        assert by_code["GRP_A"].success, f"GRP_A 应成功: {by_code['GRP_A'].error}"
        assert by_code["GRP_C"].success, f"GRP_C 应成功（崩溃组之后的组不得被拖垮）: {by_code['GRP_C'].error}"
        # B 失败但可追踪
        assert not by_code["GRP_B"].success, "GRP_B 应标记失败"
        assert by_code["GRP_B"].error, "失败组应有错误信息"
        assert by_code["GRP_B"].output_dir, "失败组应有 output_dir 可追踪定位"
        # A、C 交付件确实生成了
        for code in ("GRP_A", "GRP_C"):
            op = Path(by_code[code].output_dir)
            assert (op / "mapping.xlsx").exists(), f"{code}: mapping.xlsx 应存在"

    def test_batch_stdout_never_has_per_group_detail(self, multi_group_xlsx, tmp_path, capsys):
        """防回归：逐组详细内容绝不能进 stdout。

        铁律：stdout 只允许出现与规则组数无关的常数级简要信息（批次进度、失败计数）。
        逐组 [OK]/组名等内容与规则组数成正比，大批量时累积超出上游捕获管道上限
        （如 AI 工具捕获 stdout），整个进程树会被 SIGKILL——典型表现为「前两批正常、
        第三批起步即被杀」。早期曾用 --verbose 开关把逐组内容打到 stdout，正是这起
        故障的根因（已移除该参数）。
        """
        from batch import run_batch
        out = str(tmp_path / "output")
        run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)
        captured = capsys.readouterr()

        # 核心铁律：逐组详细绝不进 stdout
        assert "[OK]" not in captured.out, "逐组 [OK] 不应进 stdout（应去日志）"
        assert "DWB_TEST_" not in captured.out, "逐组规则组名不应进 stdout（应去日志）"

        # 日志始终落盘
        log_dir = Path(out) / "batch_logs"
        assert log_dir.exists(), "应生成日志目录"
        logs = list(log_dir.glob("batch_*.log"))
        assert logs, "应生成批次日志文件"
        log_text = "\n".join(lf.read_text(encoding="utf-8") for lf in logs)
        assert "[OK]" in log_text, "逐组 [OK] 应写入日志文件"

    def test_batch_no_ai_skips_summary(self, multi_group_xlsx, tmp_path):
        """--no-ai 时不生成 knowledge_summary.md"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=True)
        for r in results:
            if r.success:
                assert not r.has_ai, "no_ai 时不应标记 has_ai"

    def test_batch_with_ai_generates_summary(self, multi_group_xlsx, tmp_path):
        """启用 AI 时生成 knowledge_summary.md"""
        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(multi_group_xlsx, out, batch_size=50, no_ai=False)
        for r in results:
            if r.success:
                assert r.has_ai, "启用 AI 时应标记 has_ai"
                assert (Path(r.output_dir) / "knowledge_summary.md").exists(), \
                    "knowledge_summary.md 应存在"

    def test_batch_error_handling(self, tmp_path):
        """有错误规则组时，其他组应正常处理"""
        import openpyxl
        # 构造一个有问题的 Excel（空 SQL）
        rules = [
            {"rule_code": "R1", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f1", "delete_mode": "1",
             "query_sql": "SELECT a.id FROM ods.t a", "rule_name": "正常",
             "rule_group_code": "GR001", "rule_group_en": "OK_F"},
            {"rule_code": "R2", "rule_type": 1, "exec_sequence": 1,
             "target_schema": "dws", "target_table": "f2", "delete_mode": "1",
             "query_sql": "", "rule_name": "空SQL",
             "rule_group_code": "GR002", "rule_group_en": "EMPTY_F"},
        ]
        xlsx = str(tmp_path / "mixed.xlsx")
        build_xlsx(xlsx, rules=rules)

        from batch import run_batch
        out = str(tmp_path / "output")
        results = run_batch(xlsx, out, batch_size=50, no_ai=True)
        # 至少一个应成功
        success = [r for r in results if r.success]
        assert len(success) >= 1, f"至少一个应成功，实际 {results}"
