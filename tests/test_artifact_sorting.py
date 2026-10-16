"""制品目录自动分拣测试（docs/15 v3 语义 + 消费方 docs/specs/opt/12 v3 修正 1/2/3）。

行为要点：
- 识别职责全在逆向侧：excel 按 sheet 指纹、.sql/.ddl 后缀即型
- 〔修正1〕无法识别的文件（mapping/说明文档等）**不阻断**——进 unrecognized →
  产物 warnings（facts 层），只有术加缺失才整体 fail
- 〔修正2〕.sql 自动归 DDL，不算无法识别
- 〔修正3〕DQ 目录通道两态：未检出 → dq=None（facts 层落 dq_rules=[] + warning）
- 多义（同型多个）fail loud 不静默取一
- facts 入口 e2e：目录直入 → asset_facts.json（version 1.3 + asset_facts: 定位行）

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

from inputs import ManifestError, assemble_artifact_dir, classify_artifact_excel

ET_XLSX = DOCS / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_D.xlsx"
LTS_XLSX = DOCS / "schedule-tasks_副本.xlsx"
DQ_XLSX = DOCS / "dq模板.xlsx"


def _make_excel(path, sheets: dict):
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


def _et(d): return _make_excel(d / "et.xlsx", {"RULE": ["规则编码"], "TargetFields": ["规则编码"], "GroupVariables": []})
def _lts(d): return _make_excel(d / "lts.xlsx", {"tasks": ["任务名称"], "jobs": [], "taskParams": []})
def _dq(d): return _make_excel(d / "dq.xlsx", {"规则基础信息": ["规则Number"], "参数配置": []})


# ── classify_artifact_excel：指纹判型 ─────────────────────────

class TestClassify:
    def test_real_templates(self):
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


# ── assemble_artifact_dir：分拣纪律 ───────────────────────────

class TestAssembleDir:
    def test_clean_mixed_dir(self, tmp_path):
        d = tmp_path / "bag"; d.mkdir()
        _et(d); _lts(d); _dq(d)
        (d / "t.sql").write_text("CREATE TABLE t (a INT);", encoding="utf-8")
        asm = assemble_artifact_dir(str(d))
        assert asm["execution_tasks"].endswith("et.xlsx")
        assert asm["lts"].endswith("lts.xlsx")
        assert asm["dq"].endswith("dq.xlsx")
        assert [Path(x).name for x in asm["ddl"]] == ["t.sql"]  # 修正2
        assert asm["unrecognized"] == []

    def test_fix1_unrecognized_not_blocking(self, tmp_path):
        """修正1：mapping（缺指纹 excel）/ 说明文档混目录不阻断——进 unrecognized。"""
        d = tmp_path / "bag"; d.mkdir()
        _et(d)
        _make_excel(d / "mapping.xlsx", {"字段映射": ["源字段", "目标字段"]})
        (d / "需求说明.docx").write_bytes(b"doc")
        (d / "readme.txt").write_text("说明", encoding="utf-8")
        asm = assemble_artifact_dir(str(d))
        assert asm["execution_tasks"].endswith("et.xlsx")
        names = [n for n, _ in asm["unrecognized"]]
        assert set(names) == {"mapping.xlsx", "需求说明.docx", "readme.txt"}

    def test_et_missing_is_the_only_hard_fail(self, tmp_path):
        """术加判不出 = 唯一整体 fail（错误含无法识别清单辅助排查）。"""
        d = tmp_path / "bag"; d.mkdir()
        _lts(d)
        with pytest.raises(ManifestError, match="术加"):
            assemble_artifact_dir(str(d))

    def test_ambiguous_same_type_fails(self, tmp_path):
        d = tmp_path / "bag"; d.mkdir()
        _et(d)
        _make_excel(d / "et2.xlsx", {"RULE": ["规则编码"], "TargetFields": []})
        with pytest.raises(ManifestError, match="execution_tasks 多义"):
            assemble_artifact_dir(str(d))

    def test_dq_not_detected_returns_none(self, tmp_path):
        """修正3：未检出 DQ → dq=None（两态落定在 facts 层，不要求显式声明）。"""
        d = tmp_path / "bag"; d.mkdir()
        _et(d)
        asm = assemble_artifact_dir(str(d))
        assert asm["dq"] is None
        assert asm["lts"] is None  # LTS 同为可选

    def test_skip_lock_hidden_subdir(self, tmp_path):
        d = tmp_path / "bag"; d.mkdir()
        _et(d)
        (d / "~$et.xlsx").write_bytes(b"")
        (d / ".DS_Store").write_bytes(b"")
        (d / "sub").mkdir()
        asm = assemble_artifact_dir(str(d))
        assert asm["unrecognized"] == []

    def test_missing_dir_fails(self):
        with pytest.raises(ManifestError, match="目录不存在"):
            assemble_artifact_dir("/not/exist/dir")

    def test_ddl_and_sql_suffix(self, tmp_path):
        d = tmp_path / "bag"; d.mkdir()
        _et(d)
        (d / "a.sql").write_text("x", encoding="utf-8")
        (d / "b.ddl").write_text("x", encoding="utf-8")
        (d / "c.SQL").write_text("x", encoding="utf-8")  # 大写后缀
        asm = assemble_artifact_dir(str(d))
        assert len(asm["ddl"]) == 3
        # .txt 不进 DDL（可能是说明文档——歧义后缀不猜）
        (d / "note.txt").write_text("x", encoding="utf-8")
        asm = assemble_artifact_dir(str(d))
        assert len(asm["ddl"]) == 3
        assert any(n == "note.txt" for n, _ in asm["unrecognized"])


# ── facts 入口 e2e ────────────────────────────────────────────

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


class TestFactsE2E:
    def _bag(self, tmp_path):
        d = tmp_path / "bag"; d.mkdir()
        shutil.copy(ET_XLSX, d / "et.xlsx")
        shutil.copy(LTS_XLSX, d / "lts.xlsx")
        return d

    def test_success_full_channel(self, tmp_path):
        """真实语料 + .sql DDL：一次调用交卷，warnings 含未检出 DQ 披露。"""
        d = self._bag(tmp_path)
        (d / "t.sql").write_text(
            "CREATE TABLE dwb_cust_asset_d (cust_id INT)\nDISTRIBUTE BY HASH (cust_id);",
            encoding="utf-8")
        out = tmp_path / "deep" / "asset_facts.json"
        stdout, stderr, code = _run_facts(["--dir", str(d), "--output", str(out)])
        assert code == 0
        assert any(line.startswith("asset_facts: ") for line in stdout.splitlines())
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["version"] == "1.4"
        assert data["dq_rules"] == []  # 修正3：未检出 = 空数组（非 null）
        assert data["schedule"] is not None
        types = {w["type"] for w in data["warnings"]}
        assert "dq_not_detected" in types  # 两态披露进 warnings
        t = [x for x in data["tables"] if x["name"].lower() == "dwb_cust_asset_d"][0]
        assert t["table_props"]["distribute_by"] == "HASH (cust_id)"

    def test_unrecognized_files_in_warnings(self, tmp_path):
        """修正1 e2e：mapping 等干扰文件不阻断，进产物 warnings。"""
        d = self._bag(tmp_path)
        _make_excel(d / "mapping.xlsx", {"字段映射": ["源"]})
        out = tmp_path / "o.json"
        _, _, code = _run_facts(["--dir", str(d), "--output", str(out)])
        assert code == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        unrecog = [w for w in data["warnings"] if w["type"] == "unrecognized_artifact"]
        assert any("mapping.xlsx" in w["title"] for w in unrecog)

    def test_et_missing_fails_no_product(self, tmp_path):
        d = tmp_path / "bag"; d.mkdir()
        _lts(d)
        out = tmp_path / "o.json"
        _, stderr, code = _run_facts(["--dir", str(d), "--output", str(out)])
        assert code == 1
        assert "术加" in stderr
        assert not out.exists()

    def test_bad_args_exit_2(self, tmp_path):
        _, _, code = _run_facts(["--dir"])  # 缺 --output
        assert code == 2
