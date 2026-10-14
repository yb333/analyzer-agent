"""制品类型自动分拣测试（docs/15）。

验收标准逐条对应：
1. 三类参考语料 + 干扰文件（非 excel / 缺 sheet 的 excel）混一个目录 → 分型正确、
   干扰文件报"无法识别"
2. 同目录两个同型 excel（多义）→ fail loud，不静默取其一
3. 显式指认 + 目录混给 → 指认优先、目录分拣其余
4. declared_absent + 目录里恰有 DQ 形态文件 → fail loud（声明与发现矛盾）
5. 现有 manifest 测试全绿（显式通道零回归，见 test_manifest.py）

运行:
    pytest tests/test_artifact_sorting.py -v
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

from inputs import ManifestError, classify_artifact_excel, resolve_artifacts

ET_XLSX = DOCS / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_D.xlsx"
LTS_XLSX = DOCS / "schedule-tasks_副本.xlsx"
DQ_XLSX = DOCS / "dq模板.xlsx"


def _make_excel(path, sheets: dict):
    """构造指定 sheet 结构的 excel（sheets: {sheet名: [表头]}）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    first = True
    for name, headers in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        ws.append(headers)
        first = False
    wb.save(str(path))
    return path


def _et_excel(tmp):
    return _make_excel(tmp / "et.xlsx", {"RULE": ["规则编码"], "TargetFields": ["规则编码"], "GroupVariables": []})


def _lts_excel(tmp):
    return _make_excel(tmp / "lts.xlsx", {"tasks": ["任务名称"], "jobs": [], "taskParams": []})


def _dq_excel(tmp):
    return _make_excel(tmp / "dq.xlsx", {"规则基本信息": ["规则Number"], "参数配置": []})


# ── classify_artifact_excel：指纹判型 ─────────────────────────

class TestClassify:
    def test_real_templates(self):
        """仓内三类真实模板/语料判型正确。"""
        assert classify_artifact_excel(ET_XLSX)[0] == "execution_tasks"
        assert classify_artifact_excel(LTS_XLSX)[0] == "lts"
        assert classify_artifact_excel(DQ_XLSX)[0] == "dq"

    def test_sheetless_excel_unrecognized(self, tmp_path):
        x = _make_excel(tmp_path / "misc.xlsx", {"Sheet1": ["随便"]})
        kind, reason = classify_artifact_excel(x)
        assert kind is None and "缺已知指纹" in reason

    def test_non_excel_unrecognized(self, tmp_path):
        f = tmp_path / "readme.txt"
        f.write_text("hi", encoding="utf-8")
        kind, reason = classify_artifact_excel(f)
        assert kind is None and "非 excel" in reason

    def test_partial_fingerprint_not_lts(self, tmp_path):
        """只含 jobs（缺 tasks 主 sheet）不判 LTS——指纹以 loader 必选 sheet 为准。"""
        x = _make_excel(tmp_path / "half.xlsx", {"jobs": []})
        assert classify_artifact_excel(x)[0] is None


# ── resolve_artifacts：验收 1-4 ───────────────────────────────

class TestResolveExcelDir:
    def test_acceptance_1_mixed_dir(self, tmp_path):
        """验收1：三类 + 干扰文件混目录 → 分拣正确，干扰报无法识别（汇总报齐）。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        _lts_excel(d)
        _dq_excel(d)
        (d / "readme.txt").write_text("干扰", encoding="utf-8")
        _make_excel(d / "unknown.xlsx", {"随便": []})
        with pytest.raises(ManifestError) as ei:
            resolve_artifacts({"excel_dir": str(d), "dq": {"status": "declared_absent"}})
        msg = str(ei.value)
        assert "无法识别的文件 readme.txt" in msg
        assert "无法识别的文件 unknown.xlsx" in msg
        # 错误一次报齐（两条都在）

    def test_acceptance_1_clean_mixed_dir(self, tmp_path):
        """验收1（干净目录）：三类分拣正确，DQ 自动落 provided。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        _lts_excel(d)
        _dq_excel(d)
        ra = resolve_artifacts({"excel_dir": str(d)})
        assert ra["execution_tasks"].endswith("et.xlsx")
        assert ra["lts"].endswith("lts.xlsx")
        assert ra["dq_status"] == "provided"
        assert ra["dq_path"].endswith("dq.xlsx")

    def test_skip_lock_and_hidden_files(self, tmp_path):
        """Excel 锁文件（~$）/ 隐藏文件 / 子目录不干扰分拣。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        (d / "~$et.xlsx").write_bytes(b"lock")
        (d / ".DS_Store").write_bytes(b"")
        (d / "sub").mkdir()
        ra = resolve_artifacts({"excel_dir": str(d), "dq": {"status": "declared_absent"}})
        assert ra["execution_tasks"].endswith("et.xlsx")

    def test_acceptance_2_same_type_ambiguous(self, tmp_path):
        """验收2：同目录两个同型 → fail loud 报多义，不静默取其一。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        _make_excel(d / "et2.xlsx", {"RULE": ["规则编码"], "TargetFields": []})
        with pytest.raises(ManifestError, match="execution_tasks 多义"):
            resolve_artifacts({"excel_dir": str(d), "dq": {"status": "declared_absent"}})

    def test_acceptance_3_explicit_wins(self, tmp_path):
        """验收3：显式指认 + 目录混给 → 指认优先（notes 提示），目录分拣其余。"""
        d = tmp_path / "bag"
        d.mkdir()
        explicit_et = _et_excel(tmp_path)  # 放目录外，显式指认它
        _make_excel(d / "et_other.xlsx", {"RULE": ["规则编码"], "TargetFields": []})
        _lts_excel(d)
        _dq_excel(d)
        ra = resolve_artifacts({"execution_tasks": str(explicit_et), "excel_dir": str(d)})
        assert ra["execution_tasks"] == str(explicit_et)
        assert ra["lts"].endswith("lts.xlsx")
        assert ra["dq_status"] == "provided"
        assert any("显式指认优先" in n for n in ra["notes"])

    def test_acceptance_4_declared_absent_conflict(self, tmp_path):
        """验收4：declared_absent + 目录里有 DQ 形态文件 → fail loud。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        _dq_excel(d)
        with pytest.raises(ManifestError, match="矛盾"):
            resolve_artifacts({"excel_dir": str(d), "dq": {"status": "declared_absent"}})

    def test_dq_silence_still_violation(self, tmp_path):
        """分拣未发现 DQ ≠ 无 DQ：无声明仍违规（三态语义不变）。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        with pytest.raises(ManifestError, match="三态必须落定"):
            resolve_artifacts({"excel_dir": str(d)})

    def test_lts_optional_zero_ok(self, tmp_path):
        """全量资产可无 LTS：目录只有术加 + DQ 声明 → lts=None 合法。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        ra = resolve_artifacts({"excel_dir": str(d), "dq": {"status": "declared_absent"}})
        assert ra["lts"] is None
        assert ra["dq_status"] == "declared_absent"


class TestResolveTypedDirAndList:
    def test_typed_dir_sorts_only_that_type(self, tmp_path):
        """单键给目录：只收该型，混入他型文件报无法归入。"""
        d = tmp_path / "only_lts"
        d.mkdir()
        _lts_excel(d)
        _et_excel(d)  # 混入的术加 → 报错
        with pytest.raises(ManifestError, match="无法归入该型"):
            resolve_artifacts({"execution_tasks": str(ET_XLSX),
                               "lts": str(d), "dq": {"status": "declared_absent"}})

    def test_list_of_files(self, tmp_path):
        """文件组：数组内每个文件判型归位。"""
        et = _et_excel(tmp_path)
        lts = _lts_excel(tmp_path)
        ra = resolve_artifacts({"execution_tasks": [str(et)],
                                "lts": [str(lts)],
                                "dq": {"status": "declared_absent"}})
        assert ra["execution_tasks"] == str(et)
        assert ra["lts"] == str(lts)

    def test_dq_path_string_means_provided(self, tmp_path):
        """dq 直接给路径字符串 = provided + 自动定位。"""
        dq = _dq_excel(tmp_path)
        ra = resolve_artifacts({"execution_tasks": str(ET_XLSX),
                                "dq": str(dq)})
        assert ra["dq_status"] == "provided"
        assert ra["dq_path"] == str(dq)

    def test_missing_path_reports(self):
        with pytest.raises(ManifestError, match="路径不存在"):
            resolve_artifacts({"execution_tasks": "/not/exist.xlsx",
                               "dq": {"status": "declared_absent"}})

    def test_provided_dict_path_priority(self, tmp_path):
        """dict.provided.path 显式优先于 excel_dir 分拣结果。"""
        d = tmp_path / "bag"
        d.mkdir()
        _et_excel(d)
        picked = _dq_excel(d)
        other = _make_excel(tmp_path / "dq_other.xlsx", {"规则基本信息": ["规则Number"]})
        ra = resolve_artifacts({"excel_dir": str(d),
                                "dq": {"status": "provided", "path": str(picked)}})
        assert ra["dq_path"] == str(picked)
        assert str(other) not in str(ra["dq_path"])


# ── e2e：excel_dir 全自动（真实模板/语料）─────────────────────

class TestSortingE2E:
    def test_excel_dir_end_to_end(self, tmp_path):
        """真实语料混目录 + declared_absent → 一次调用产出契约。"""
        from analyzer import main
        import io, contextlib
        d = tmp_path / "bag"
        d.mkdir()
        shutil.copy(ET_XLSX, d / "et.xlsx")
        shutil.copy(LTS_XLSX, d / "lts.xlsx")
        mf = {
            "asset": {"schema": "", "table": "dwb_cust_asset_d"},
            "artifacts": {"excel_dir": str(d),
                          "dq": {"status": "declared_absent"}},
            "output": str(tmp_path / "o" / "baseline_v1.json"),
        }
        mf_path = tmp_path / "m.json"
        mf_path.write_text(json.dumps(mf, ensure_ascii=False), encoding="utf-8")
        old_argv = sys.argv
        sys.argv = ["analyzer.py", "--manifest", str(mf_path)]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                main()
        finally:
            sys.argv = old_argv
        d_out = json.loads(Path(mf["output"]).read_text(encoding="utf-8"))
        assert d_out["version"] == "1.2"
        assert d_out["dq_rules"] == []
        assert d_out["schedule"] is not None  # excel_dir 分拣出的 LTS 生效
        # stdout 定位行
        assert any(line.startswith("baseline_v1: ") for line in buf.getvalue().splitlines())
