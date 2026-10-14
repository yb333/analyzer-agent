"""manifest 编排合入测试（v2 波 3）。

覆盖：
1. load_manifest 结构校验（必填/三态/路径，全 fail loud）
2. stage_ddl_files（文件+目录混合/映射/同名冲突/路径不存在）
3. _project_dq_rules 三态（declared_absent → []，未提供 → null）
4. run_manifest e2e（真实制品 + LTS + declared_absent + DDL 清单 → 精确路径产物）
5. 编排失败语义（provided 拒绝 / 资产不一致 fail loud / 互斥校验）

运行:
    pytest tests/test_manifest.py -v
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
DOCS = PROJECT_ROOT / "docs"
sys.path.insert(0, str(ANALYZER_REF))

from inputs import ManifestError, load_manifest, stage_ddl_files

ET_XLSX = DOCS / "术加制品参考案例" / "execution_tasks_DWB_CUST_ASSET_D.xlsx"
LTS_XLSX = DOCS / "schedule-tasks_副本.xlsx"


def _write_manifest(tmp_path, **overrides):
    mf = {
        "asset": {"schema": "", "table": "dwb_cust_asset_d"},
        "artifacts": {
            "execution_tasks": str(ET_XLSX),
            "dq": {"status": "declared_absent"},
        },
        "output": str(tmp_path / "out" / "baseline_v1.json"),
    }
    mf.update(overrides)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(mf, ensure_ascii=False), encoding="utf-8")
    return p


# ── 1. load_manifest 结构校验 ──────────────────────────────────

class TestLoadManifest:
    def test_valid_minimal(self, tmp_path):
        p = _write_manifest(tmp_path)
        mf = load_manifest(p)
        assert mf["asset"]["table"] == "dwb_cust_asset_d"
        assert mf["artifacts"]["dq"]["status"] == "declared_absent"

    def test_missing_table(self, tmp_path):
        p = _write_manifest(tmp_path, asset={"schema": ""})
        with pytest.raises(ManifestError, match="asset.table"):
            load_manifest(p)

    def test_missing_output(self, tmp_path):
        p = _write_manifest(tmp_path, output="")
        with pytest.raises(ManifestError, match="output"):
            load_manifest(p)

    def test_missing_execution_tasks(self, tmp_path):
        mf = {"asset": {"table": "t"}, "artifacts": {"dq": {"status": "declared_absent"}},
              "output": "x.json"}
        p = tmp_path / "m.json"
        p.write_text(json.dumps(mf), encoding="utf-8")
        with pytest.raises(ManifestError, match="execution_tasks 必给"):
            load_manifest(p)

    def test_dq_silence_is_rejected(self, tmp_path):
        """DQ 沉默（缺 dq key）= 违规——三态必须显式，区分'没有'和'忘了'。"""
        mf = {"asset": {"table": "t"},
              "artifacts": {"execution_tasks": str(ET_XLSX)},
              "output": "x.json"}
        p = tmp_path / "m.json"
        p.write_text(json.dumps(mf), encoding="utf-8")
        with pytest.raises(ManifestError, match="三态"):
            load_manifest(p)

    def test_dq_provided_requires_existing_file(self, tmp_path):
        mf = {"asset": {"table": "t"},
              "artifacts": {"execution_tasks": str(ET_XLSX),
                            "dq": {"status": "provided", "path": "nope.xlsx"}},
              "output": "x.json"}
        p = tmp_path / "m.json"
        p.write_text(json.dumps(mf), encoding="utf-8")
        with pytest.raises(ManifestError, match="provided.*不存在"):
            load_manifest(p)

    def test_missing_file_fails(self, tmp_path):
        p = _write_manifest(tmp_path)
        mf = json.loads(p.read_text(encoding="utf-8"))
        mf["artifacts"]["execution_tasks"] = "/not/exist.xlsx"
        p.write_text(json.dumps(mf), encoding="utf-8")
        with pytest.raises(ManifestError, match="不存在"):
            load_manifest(p)


# ── 2. stage_ddl_files ─────────────────────────────────────────

class TestStageDdlFiles:
    def test_file_and_dir_mixed_with_mapping(self, tmp_path):
        (tmp_path / "a.sql").write_text("CREATE TABLE a (x INT);", encoding="utf-8")
        d = tmp_path / "ddldir"
        d.mkdir()
        (d / "b.sql").write_text("CREATE TABLE b (y INT);", encoding="utf-8")
        stage = tmp_path / "stage"
        mapping = stage_ddl_files([str(tmp_path / "a.sql"), str(d)], stage)
        staged = sorted(p.name for p in stage.iterdir())
        assert staged == ["a.sql", "b.sql"]
        # 映射：stage 绝对路径 → 原路径
        for k, v in mapping.items():
            assert Path(k).is_absolute() and Path(v).exists()

    def test_name_conflict_prefixed(self, tmp_path):
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        d1.mkdir(); d2.mkdir()
        (d1 / "t.sql").write_text("CREATE TABLE t (x INT);", encoding="utf-8")
        (d2 / "t.sql").write_text("CREATE TABLE t2 (x INT);", encoding="utf-8")
        mapping = stage_ddl_files([str(d1), str(d2)], tmp_path / "stage")
        staged = sorted(p.name for p in (tmp_path / "stage").iterdir())
        # 首个保持原名，冲突者加序号前缀——两个文件都在、映射不丢
        assert staged == ["1_t.sql", "t.sql"]
        assert len(mapping) == 2

    def test_missing_path_raises(self, tmp_path):
        with pytest.raises(ManifestError, match="不存在"):
            stage_ddl_files(["/not/exist.sql"], tmp_path / "stage")


# ── 3. dq 三态投影口径 ─────────────────────────────────────────

class TestDqTriStateProjection:
    def test_declared_absent_yields_empty_list(self):
        from baseline_export import _project_dq_rules
        out = _project_dq_rules({"dq_declared_absent": True})
        assert out == []

    def test_not_provided_yields_null(self):
        from baseline_export import _project_dq_rules
        assert _project_dq_rules({}) is None

    def test_rules_take_precedence(self):
        from baseline_export import _project_dq_rules
        meta = {"dq_rules": [{"rule_number": "1"}], "dq_declared_absent": True}
        out = _project_dq_rules(meta)
        assert len(out) == 1  # 有规则时声明无是矛盾输入，事实优先


# ── 4. e2e：run_manifest（一次调用一次交卷）─────────────────────

def _run_manifest(argv):
    """patch argv 调 analyzer.main()（--manifest 分流进 run_manifest）。"""
    from analyzer import main
    old_argv = sys.argv
    sys.argv = ["analyzer.py"] + argv
    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            main()
        return buf.getvalue()
    finally:
        sys.argv = old_argv


class TestRunManifestE2E:
    @pytest.fixture(scope="class")
    def product(self, tmp_path_factory):
        base = tmp_path_factory.mktemp("mf_e2e")
        ddl_dir = base / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "dwb_cust_asset_d.sql").write_text(
            "CREATE TABLE dwb_cust_asset_d (\n"
            "  cust_id INT NOT NULL,\n"
            "  asset_amt DECIMAL(18,2)\n"
            ")\n"
            "DISTRIBUTE BY HASH (cust_id)\n"
            "PARTITION BY RANGE (asset_amt) (PARTITION p1 VALUES LESS THAN (1000));",
            encoding="utf-8")
        mf = {
            "asset": {"schema": "", "table": "dwb_cust_asset_d"},
            "artifacts": {
                "execution_tasks": str(ET_XLSX),
                "lts": str(LTS_XLSX),
                "dq": {"status": "declared_absent"},
                "ddl": [str(ddl_dir / "dwb_cust_asset_d.sql")],
            },
            "output": str(base / "deep" / "nested" / "baseline_v1.json"),
        }
        mf_path = base / "manifest.json"
        mf_path.write_text(json.dumps(mf, ensure_ascii=False), encoding="utf-8")
        stdout = _run_manifest(["--manifest", str(mf_path)])
        product_path = Path(mf["output"])
        assert product_path.exists()
        return json.loads(product_path.read_text(encoding="utf-8")), stdout, product_path

    def test_product_shape(self, product):
        d, stdout, _ = product
        assert d["version"] == "1.2"
        assert d["dq_rules"] == []  # declared_absent → 空数组（不是 null）
        assert d["schedule"] is not None  # --lts 通道
        # stdout 定位行（消费方按前缀解析）
        assert any(line.startswith("baseline_v1: ") for line in stdout.splitlines())

    def test_only_contract_file_in_dir(self, product):
        """编排模式只交契约文件（draft/summary 不落产物旁——实现自由不外泄）。"""
        _, _, product_path = product
        siblings = [p.name for p in product_path.parent.iterdir()]
        assert siblings == ["baseline_v1.json"]

    def test_provenance_carries_manifest_paths_and_sheets(self, product):
        d, _, _ = product
        et = [p for p in d["provenance"] if p["type"] == "execution_tasks"][0]
        assert et["sheets"] == ["RULE", "TargetFields", "GroupVariables"]
        # DDL 记 manifest 原路径（非 stage 临时路径）
        ddl = [p for p in d["provenance"] if p["type"] == "ddl"][0]
        assert ddl["path"].endswith("dwb_cust_asset_d.sql")
        assert "ddl_stage_" not in ddl["path"]
        lts = [p for p in d["provenance"] if p["type"] == "lts"][0]
        assert lts["sheets"] == ["tasks", "jobs", "taskParams"]

    def test_table_props_from_ddl_list(self, product):
        d, _, _ = product
        t = [x for x in d["tables"] if x["name"].lower() == "dwb_cust_asset_d"][0]
        assert t["table_props"]["distribute_by"] == "HASH (cust_id)"
        assert t["table_props"]["distribute_columns"] == ["cust_id"]
        assert [f["name"] for f in t["fields"] if f["is_distribute_key"]] == ["cust_id"]


# ── 5. 编排失败语义 ────────────────────────────────────────────

class TestManifestFailLoud:
    def test_provided_dq_rejected_until_loader_ready(self, tmp_path, capsys):
        mf = {
            "asset": {"table": "t"},
            "artifacts": {"execution_tasks": str(ET_XLSX),
                          "dq": {"status": "provided", "path": str(LTS_XLSX)}},
            "output": str(tmp_path / "o.json"),
        }
        p = tmp_path / "m.json"
        p.write_text(json.dumps(mf), encoding="utf-8")
        with pytest.raises(SystemExit):
            _run_manifest(["--manifest", str(p)])
        err = capsys.readouterr().err
        assert "provided 暂不支持" in err
        assert not Path(mf["output"]).exists()

    def test_asset_mismatch_fails(self, tmp_path, capsys):
        p = _write_manifest(tmp_path, asset={"schema": "", "table": "other_table"})
        with pytest.raises(SystemExit):
            _run_manifest(["--manifest", str(p)])
        assert "资产不一致" in capsys.readouterr().err

    def test_manifest_and_input_mutually_exclusive(self, tmp_path, capsys):
        p = _write_manifest(tmp_path)
        with pytest.raises(SystemExit):
            _run_manifest(["--manifest", str(p), "--input", "x.xlsx", "--output", "o"])
