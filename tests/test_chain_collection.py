"""
pytest 测试：analyze-chain collection 形态（v1.30）——资产集合目录闭环建图

覆盖：
- build_collection_graph：链/森林+菱形/环/终点识别/外部源表（闭环不外追）
- _merge_group_data：rule_code 跨组冲突重键、dataclasses.replace 整拷
  （delete_condition 不再丢——存量缺陷修复回归）
- engine DDL 多目录（parse_ddl_for_metadata str|list 合并）
- CLI 端到端：run.py analyze-chain --input 集合目录（三组链 + 各带 DDL），
  三件套 + 过程件产出；单资产目录 = 1 组链；xlsx 文件输入拒绝

运行:
    pytest tests/test_chain_collection.py -v
"""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

# ── 路径配置 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_SKILL = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
RUN_PY = PROJECT_ROOT / "dws-pipeline-analyzer" / "run.py"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"

sys.path.insert(0, str(ANALYZER_SKILL))
sys.path.insert(0, str(FIXTURES_DIR))

from _build_xlsx import build_xlsx
from analyzer import (
    _chain_load_child, _merge_group_data, build_collection_graph,
)
from engine import RawRule, parse_ddl_for_metadata


# ── Fixture 构造 ─────────────────────────────────────────

def _asset_dir(base: Path, name: str, rules: list, *, target_fields=None,
               ddl_sql: str = "") -> Path:
    """一个资产的制品目录：execution_tasks.xlsx 必需 + 可选 DDL。"""
    d = base / name
    d.mkdir(parents=True)
    build_xlsx(str(d / "execution_tasks.xlsx"), rules=rules,
               target_fields=target_fields)
    if ddl_sql:
        (d / f"{name}.sql").write_text(ddl_sql, encoding="utf-8")
    return d


def _mid_a_rules(code="R0001"):
    return [{
        "rule_code": code, "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_trade_mid_f",
        "delete_mode": "1",
        "query_sql": "SELECT a.order_id, a.user_id FROM ods.order_src a",
        "rule_group_code": "GR_MID_A", "rule_name": "交易mid",
        "rule_group_en": "DWB_TRADE_MID_F",
    }]


def _mid_b_rules(code="R0001"):
    return [{
        "rule_code": code, "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_detail_mid_f",
        "delete_mode": "1",
        "query_sql": "SELECT a.detail_id, a.product_name FROM ods.detail_src a",
        "rule_group_code": "GR_MID_B", "rule_name": "明细mid",
        "rule_group_en": "DWB_DETAIL_MID_F",
    }]


def _final_rules(code="R0001"):
    return [{
        "rule_code": code, "rule_type": 1, "exec_sequence": 1,
        "target_schema": "dws", "target_table": "dwb_trade_order_f",
        "delete_mode": "1",
        "query_sql": ("SELECT a.order_id, b.product_name "
                      "FROM dws.dwb_trade_mid_f a "
                      "LEFT JOIN dws.dwb_detail_mid_f b ON a.order_id = b.detail_id"),
        "rule_group_code": "GR_FINAL", "rule_name": "最终F",
        "rule_group_en": "DWB_TRADE_ORDER_F",
    }]


def _load_bundles(dirs: list) -> list:
    return [b for b in (_chain_load_child(d) for d in dirs) if b]


def _group_data(bundles: list) -> dict:
    return {b["dir"]: {"rules": b["rules"], "target_fields": b["target_fields"],
                       "group_variables": b["group_variables"], "label": b["label"]}
            for b in bundles}


# ═══════════════════════════════════════════════════════════════
# 1. 闭环建图
# ═══════════════════════════════════════════════════════════════

class TestCollectionGraph:

    def test_three_group_chain(self, tmp_path):
        """mid+mid→F：终点唯一、depth 正确、ods 为外部源表（闭环不外追）。"""
        coll = tmp_path / "collection"
        coll.mkdir()
        dirs = [
            _asset_dir(coll, "DWB_TRADE_MID_F", _mid_a_rules()),
            _asset_dir(coll, "DWB_DETAIL_MID_F", _mid_b_rules()),
            _asset_dir(coll, "DWB_TRADE_ORDER_F", _final_rules()),
        ]
        result = build_collection_graph(_load_bundles(dirs))

        assert len(result["groups"]) == 3
        assert not result["cycle_detected"]
        by_label = {g["rule_group_en"]: g for g in result["groups"]}
        assert by_label["DWB_TRADE_ORDER_F"]["is_endpoint"] is True
        assert by_label["DWB_TRADE_ORDER_F"]["depth"] == 0
        assert by_label["DWB_TRADE_MID_F"]["is_endpoint"] is False
        assert by_label["DWB_TRADE_MID_F"]["depth"] == 1
        # ods 源表在集合内无写入者 → 外部源表，不追溯
        nf = set(result["not_found"])
        assert "ods.order_src" in nf and "ods.detail_src" in nf
        assert "dws.dwb_trade_mid_f" not in nf  # 集合内有写入者，连边

    def test_forest_two_endpoints_shared_upstream(self, tmp_path):
        """森林+菱形：共用上游 S，两个终点 F1/F2；S depth=1。"""
        coll = tmp_path / "collection"
        coll.mkdir()
        s = _asset_dir(coll, "DWB_SHARED_MID", [{
            "rule_code": "SR1", "rule_type": 1, "exec_sequence": 1,
            "target_schema": "dws", "target_table": "dwb_shared_mid_f",
            "delete_mode": "1",
            "query_sql": "SELECT a.k FROM ods.src a",
            "rule_group_code": "GR_S", "rule_group_en": "DWB_SHARED_MID",
        }])
        f1 = _asset_dir(coll, "DWB_FINAL1_F", [{
            "rule_code": "F1R1", "rule_type": 1, "exec_sequence": 1,
            "target_schema": "dws", "target_table": "dwb_final1_f",
            "delete_mode": "1",
            "query_sql": "SELECT a.k FROM dws.dwb_shared_mid_f a",
            "rule_group_code": "GR_F1", "rule_group_en": "DWB_FINAL1_F",
        }])
        f2 = _asset_dir(coll, "DWB_FINAL2_F", [{
            "rule_code": "F2R1", "rule_type": 1, "exec_sequence": 1,
            "target_schema": "dws", "target_table": "dwb_final2_f",
            "delete_mode": "1",
            "query_sql": "SELECT a.k FROM dws.dwb_shared_mid_f a",
            "rule_group_code": "GR_F2", "rule_group_en": "DWB_FINAL2_F",
        }])
        result = build_collection_graph(_load_bundles([s, f1, f2]))

        by_label = {g["rule_group_en"]: g for g in result["groups"]}
        assert by_label["DWB_FINAL1_F"]["is_endpoint"] is True
        assert by_label["DWB_FINAL2_F"]["is_endpoint"] is True
        assert by_label["DWB_SHARED_MID"]["is_endpoint"] is False
        assert by_label["DWB_SHARED_MID"]["depth"] == 1
        assert not result["cycle_detected"]

    def test_cycle_detected(self, tmp_path):
        """A读B写、B读A写：环检出，不崩溃。"""
        coll = tmp_path / "collection"
        coll.mkdir()
        a = _asset_dir(coll, "GRP_A", [{
            "rule_code": "AR1", "rule_type": 1, "exec_sequence": 1,
            "target_schema": "dws", "target_table": "tbl_a",
            "delete_mode": "1",
            "query_sql": "SELECT x FROM dws.tbl_b",
            "rule_group_code": "GR_A", "rule_group_en": "GRP_A",
        }])
        b = _asset_dir(coll, "GRP_B", [{
            "rule_code": "BR1", "rule_type": 1, "exec_sequence": 1,
            "target_schema": "dws", "target_table": "tbl_b",
            "delete_mode": "1",
            "query_sql": "SELECT y FROM dws.tbl_a",
            "rule_group_code": "GR_B", "rule_group_en": "GRP_B",
        }])
        result = build_collection_graph(_load_bundles([a, b]))
        assert result["cycle_detected"] is True

    def test_empty_subdir_skipped_at_load(self, tmp_path):
        """无制品无 yml 的子目录返回 None（装配层披露，图只见有效组）。"""
        empty = tmp_path / "empty_dir"
        empty.mkdir()
        assert _chain_load_child(empty) is None


# ═══════════════════════════════════════════════════════════════
# 2. 合并核（重键 / 整拷）
# ═══════════════════════════════════════════════════════════════

class TestMergeGroupData:

    def _two_group_context(self, tmp_path):
        coll = tmp_path / "collection"
        coll.mkdir()
        dirs = [
            _asset_dir(coll, "DWB_TRADE_MID_F", _mid_a_rules(),
                       target_fields=[{"rule_code": "R0001", "target_field": "order_id",
                                       "source_field": "order_id", "field_type": "bigint"}]),
            _asset_dir(coll, "DWB_TRADE_ORDER_F", _final_rules(),
                       target_fields=[{"rule_code": "R0001", "target_field": "order_id",
                                       "source_field": "order_id", "field_type": "bigint"}]),
        ]
        bundles = _load_bundles(dirs)
        return build_collection_graph(bundles), _group_data(bundles)

    def test_rule_code_collision_rekeyed(self, tmp_path):
        """两组都用 R0001（制品 xlsx 常态）→ 按组前缀重键，码全局唯一，tf 跟随。"""
        graph, gd = self._two_group_context(tmp_path)
        merged_rules, merged_tf, _ = _merge_group_data(graph, gd)

        codes = [r.rule_code for r in merged_rules]
        assert len(codes) == len(set(codes)), f"重键后仍冲突: {codes}"
        assert any("::R0001" in c for c in codes), "冲突码应带组前缀"
        assert set(merged_tf.keys()) == set(codes), "target_fields 键须与重键后码一致"

    def test_unique_codes_untouched(self, tmp_path):
        """码不冲突时不重键（legacy 零回归）。"""
        coll = tmp_path / "collection"
        coll.mkdir()
        dirs = [
            _asset_dir(coll, "DWB_TRADE_MID_F", _mid_a_rules("MA_R1")),
            _asset_dir(coll, "DWB_TRADE_ORDER_F", _final_rules("TC_R1")),
        ]
        bundles = _load_bundles(dirs)
        merged_rules, _, _ = _merge_group_data(build_collection_graph(bundles),
                                               _group_data(bundles))
        assert {r.rule_code for r in merged_rules} == {"MA_R1", "TC_R1"}

    def test_merge_preserves_delete_condition(self, tmp_path):
        """replace 整拷回归：delete_condition 等字段不再被手工构造丢掉。"""
        rule = RawRule(rule_code="R0001", rule_type=1, exec_sequence=1,
                       target_schema="dws", target_table="t1", delete_mode="2",
                       delete_condition="dt < ${P_CYCLE_ID}",
                       query_sql="SELECT 1 FROM ods.s",
                       project_code="PJ1", data_source="DS", business_owner="BO")
        graph = {"groups": [{"dir": "/g1", "rule_group_en": "G1",
                             "target_table": "t1", "all_target_tables": ["t1"],
                             "source_tables": [], "depth": 0}],
                 "not_found": [], "cycle_detected": False, "truncated": False}
        gd = {"/g1": {"rules": [rule], "target_fields": {}, "group_variables": {}, "label": "G1"}}
        merged, _, _ = _merge_group_data(graph, gd)
        assert merged[0].delete_condition == "dt < ${P_CYCLE_ID}"
        assert merged[0].project_code == "PJ1"
        assert merged[0].business_owner == "BO"

    def test_upstream_seq_before_downstream(self, tmp_path):
        """拓扑重编号：上游组 exec_sequence 整体先于下游组。"""
        graph, gd = self._two_group_context(tmp_path)
        merged_rules, _, _ = _merge_group_data(graph, gd)
        # 图里 mid 是上游（depth 1）、F 是终点（depth 0）
        mid = next(r for r in merged_rules if r.target_table == "dwb_trade_mid_f")
        fin = next(r for r in merged_rules if r.target_table == "dwb_trade_order_f")
        assert mid.exec_sequence < fin.exec_sequence


# ═══════════════════════════════════════════════════════════════
# 3. engine DDL 多目录
# ═══════════════════════════════════════════════════════════════

class TestDdlDirList:

    def _ddl_dir(self, base: Path, table: str, field: str) -> Path:
        d = base / f"ddl_{table}"
        d.mkdir(parents=True)
        (d / f"{table}.sql").write_text(
            f"CREATE TABLE {table} (\n  {field} bigint /* 注释_{table} */\n);\n",
            encoding="utf-8")
        return d

    def test_list_merges_across_dirs(self, tmp_path):
        d1 = self._ddl_dir(tmp_path, "tbl_a", "fa")
        d2 = self._ddl_dir(tmp_path, "tbl_b", "fb")
        # 单表只在其中一个目录有定义——list 形态两张表都能命中
        assert "fa" in parse_ddl_for_metadata([d1, d2], "tbl_a")
        assert "fb" in parse_ddl_for_metadata([d1, d2], "tbl_b")
        # 单目录形态不受影响（str 兼容回归）
        assert "fa" in parse_ddl_for_metadata(str(d1), "tbl_a")

    def test_first_dir_wins_on_conflict(self, tmp_path):
        d1 = self._ddl_dir(tmp_path, "tbl_a", "fa")
        d2 = tmp_path / "ddl_dup"
        d2.mkdir()
        (d2 / "tbl_a.sql").write_text(
            "CREATE TABLE tbl_a (\n  fa varchar(10) /* 后来者 */\n);\n", encoding="utf-8")
        meta = parse_ddl_for_metadata([d1, d2], "tbl_a")
        assert meta["fa"]["type"] == "bigint"  # 先到先得，可预期


# ═══════════════════════════════════════════════════════════════
# 4. CLI 端到端
# ═══════════════════════════════════════════════════════════════

class TestChainCollectionCLI:

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(RUN_PY), "analyze-chain", *extra],
            capture_output=True, text=True, encoding="utf-8")

    def _make_full_collection(self, tmp_path):
        coll = tmp_path / "trade_assets"
        coll.mkdir(parents=True)
        _asset_dir(coll, "DWB_TRADE_MID_F", _mid_a_rules(),
                   ddl_sql="CREATE TABLE dwb_trade_mid_f (\n  order_id bigint /* 订单ID */,\n  user_id varchar(50) /* 用户ID */\n);\n")
        _asset_dir(coll, "DWB_DETAIL_MID_F", _mid_b_rules())
        _asset_dir(coll, "DWB_TRADE_ORDER_F", _final_rules(),
                   ddl_sql="CREATE TABLE dwb_trade_order_f (\n  order_id bigint /* 订单ID */,\n  product_name varchar(200) /* 商品名 */\n);\n")
        return coll

    def test_full_chain_deliverables(self, tmp_path):
        coll = self._make_full_collection(tmp_path)
        out = tmp_path / "out"
        r = self._run("--input", str(coll), "--output", str(out))
        assert r.returncode == 0, r.stderr
        chain_dir = out / "chain_trade_assets"
        for f in ("mapping.xlsx", "asset_report.html", "tech_design.md",
                  "_internal/knowledge_draft.json", "_internal/knowledge_summary.md"):
            assert (chain_dir / f).exists(), f"{f} 未产出"

        draft = json.loads((chain_dir / "_internal/knowledge_draft.json").read_text(encoding="utf-8"))
        assert draft["meta"]["is_multi_group"] is True
        labels = [g["name"] for g in draft["meta"]["chain_groups"]]
        assert set(labels) == {"DWB_TRADE_MID_F", "DWB_DETAIL_MID_F", "DWB_TRADE_ORDER_F"}
        # 终点标记 + 深度
        ep = [g for g in draft["meta"]["chain_groups"] if g.get("is_endpoint")]
        assert [g["name"] for g in ep] == ["DWB_TRADE_ORDER_F"]
        # 冲突码已重键（两组 R0001 + 终点 R0001 → 至少一个带 :: 前缀）
        codes = [s["rule_code"] for s in draft["topology"]["steps"]]
        assert len(codes) == len(set(codes)), f"topology 撞码: {codes}"
        # 多目录 DDL：终点表字段类型来自 F 组自带 DDL
        assert draft["meta"]["target_field_types"], "DDL 多目录未生效"

    def test_single_asset_dir_is_one_group_chain(self, tmp_path):
        base = tmp_path / "one"
        d = _asset_dir(base, "SINGLE_F", _final_rules())
        out = tmp_path / "out"
        r = self._run("--input", str(d), "--output", str(out))
        assert r.returncode == 0, r.stderr
        draft = json.loads((out / "chain_SINGLE_F/_internal/knowledge_draft.json")
                           .read_text(encoding="utf-8"))
        assert len(draft["meta"]["chain_groups"]) == 1

    def test_xlsx_file_input_rejected(self, tmp_path):
        xlsx = tmp_path / "pkg.xlsx"
        build_xlsx(str(xlsx), rules=_final_rules())
        r = self._run("--input", str(xlsx), "--output", str(tmp_path / "o"))
        assert r.returncode == 2
        assert "/analyze" in r.stderr
