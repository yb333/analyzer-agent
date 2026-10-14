"""inputs.py 输入装配层 — 波 1a 结构波守卫测试。

覆盖（docs/13 §四 三条守则的可测面）：
1. 符号同源：analyzer 对源发现函数的 re-export 必须指向 inputs 的同一对象
   （引擎抽离先例：防两处定义悄悄分叉——单一权威生产点）；
2. excel 三件套同源：analyzer / impact_analyzer 与 excel_utils 一致；
3. apply_meta_enrichments 注入契约：key 归属、view_step 从 topology 查找、
   pop view_rule_code 语义、缺省不写 key（draft 形态不变）；
4. build_baseline_export_ctx：provenance 采集与 ctx 键完整性。

运行:
    pytest tests/test_inputs.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

import analyzer
import impact_analyzer
import inputs
import excel_utils


# ── 1. 符号同源（单一权威生产点）──────────────────────────────

def test_reexport_identity_discovery_functions():
    names = (
        # 仓根定位 / DDL 发现
        "_find_repo_root", "_auto_discover_ddl_from_repo", "_find_ddl_file",
        # LTS 家族
        "_discover_lts_from_repo", "_parse_lts_task",
        # DQ 家族
        "_discover_dq_from_repo", "_parse_dq_yml",
        # I 视图家族
        "_extract_view_sql", "_find_view_by_name", "_search_view_by_source",
        "discover_i_view",
        # 装配函数
        "append_i_view_step", "apply_meta_enrichments", "build_baseline_export_ctx",
    )
    for name in names:
        assert hasattr(inputs, name), f"inputs 缺少 {name}"
        assert getattr(analyzer, name) is getattr(inputs, name), \
            f"analyzer.{name} 不是 inputs 的同一对象（定义分叉）"


def test_excel_utils_identity():
    assert analyzer._safe_str is excel_utils.safe_str
    assert analyzer._find_col is excel_utils.find_col
    assert analyzer._get_val is excel_utils.get_val
    assert impact_analyzer._safe_str is excel_utils.safe_str
    assert impact_analyzer._find_col is excel_utils.find_col
    assert impact_analyzer._get_val is excel_utils.get_val


# ── 2. apply_meta_enrichments 注入契约 ─────────────────────────

def _sample_knowledge():
    return {
        "meta": {"target_table": "DWB_TRADE_F"},
        "topology": {"steps": [
            {"step_id": "step_1", "rule_code": "R1"},
            {"step_id": "step_2", "rule_code": "R1_VIEW"},
        ]},
    }


def test_apply_meta_enrichments_injects_all():
    knowledge = _sample_knowledge()
    i_view_info = {"is_view": True, "view_rule_code": "R1_VIEW"}
    inputs.apply_meta_enrichments(
        knowledge, i_view_info,
        schedule_info={"f_tasks": [{"task_name": "T_F"}]},
        dq_rules=[{"rule_number": "1"}],
    )
    meta = knowledge["meta"]
    # view_step 从 topology 按 rule_code 查（不自己算，防 exec_sequence≠step_id 错位）
    assert meta["asset_info"]["view_step"] == "step_2"
    # view_rule_code 是内部交接字段，注入后不残留
    assert "view_rule_code" not in meta["asset_info"]
    assert meta["schedule"] == {"f_tasks": [{"task_name": "T_F"}]}
    assert meta["dq_rules"] == [{"rule_number": "1"}]


def test_apply_meta_enrichments_all_none_is_noop():
    knowledge = _sample_knowledge()
    inputs.apply_meta_enrichments(knowledge, None, None, None)
    # 缺省不写 key（保持既有 draft 形态：excel 输入无 schedule/dq_rules → key 不存在）
    assert "asset_info" not in knowledge["meta"]
    assert "schedule" not in knowledge["meta"]
    assert "dq_rules" not in knowledge["meta"]


def test_apply_meta_enrichments_view_step_missing_rule():
    knowledge = _sample_knowledge()
    i_view_info = {"is_view": True, "view_rule_code": "GONE"}
    inputs.apply_meta_enrichments(knowledge, i_view_info, None, None)
    # topology 里查不到视图步骤 → view_step 空串（仍写入 asset_info）
    assert knowledge["meta"]["asset_info"]["view_step"] == ""


# ── 3. build_baseline_export_ctx ──────────────────────────────

def test_build_export_ctx_xlsx_mode():
    knowledge = {"meta": {}}
    engine_ctx = {
        "table_catalog": {"dws.dwb_trade_f": {"fields": []}},
        "final_target_schema": "dws",
        "final_target_table": "DWB_TRADE_F",
        "rule_group_en": "",
        "ddl_files": ["/repo/DDL/dws/table/dwb_trade_f.sql"],
    }
    ctx = inputs.build_baseline_export_ctx(
        knowledge, engine_ctx,
        is_yml_mode=False,
        consumed_yml_files=[],
        input_path=Path("/tmp/execution_tasks.xlsx"),
        platform_generation="1.0",
        rule_group_en="DWB_TRADE_ORDER_D",
        group_en_fallback="fallback_group",
    )
    assert ctx["source_type"] == "execution_tasks.xlsx"
    assert ctx["platform_generation"] == "1.0"
    assert ctx["rule_group_en"] == "DWB_TRADE_ORDER_D"  # yml 原值优先于 CLI 兜底
    assert ctx["final_target_table"] == "DWB_TRADE_F"
    assert ctx["table_catalog"] == {"dws.dwb_trade_f": {"fields": []}}
    assert ctx["provenance"] == [
        {"type": "execution_tasks", "path": str(Path("/tmp/execution_tasks.xlsx").resolve())},
        {"type": "ddl", "path": "/repo/DDL/dws/table/dwb_trade_f.sql"},
    ]


def test_build_export_ctx_yml_mode_collects_lts_dq_from_meta():
    knowledge = {"meta": {
        "schedule": {"f_tasks": [{"lts_file": "/repo/LTS/T_F.yml"}],
                     "i_tasks": [], "other_tasks": [], "all_tasks": [{"lts_file": "/repo/LTS/T_F.yml"}]},
        "dq_rules": [
            {"rule_number": "2", "dq_file": "/repo/DQ/r2.yml"},
            {"rule_number": "1", "dq_file": "/repo/DQ/r1.yml"},
        ],
    }}
    ctx = inputs.build_baseline_export_ctx(
        knowledge, {},
        is_yml_mode=True,
        consumed_yml_files=["/repo/BFT/G1/a.yml"],
        input_path=Path("/repo/BFT/G1"),
    )
    provenance = [(p["type"], p["path"]) for p in ctx["provenance"]]
    # LTS 去重（f_tasks 与 all_tasks 同文件只记一次）；DQ 按 rule_number 排序采集
    assert provenance == [
        ("rule_yml", "/repo/BFT/G1/a.yml"),
        ("lts", "/repo/LTS/T_F.yml"),
        ("dq", "/repo/DQ/r1.yml"),
        ("dq", "/repo/DQ/r2.yml"),
    ]


def test_build_export_ctx_rule_group_en_fallback_chain():
    # engine_ctx 有值最优先；否则 yml 原值；最后 CLI 兜底
    ctx = inputs.build_baseline_export_ctx(
        {"meta": {}}, {"rule_group_en": "FROM_ENGINE"},
        is_yml_mode=False, consumed_yml_files=[], input_path=Path("x.xlsx"),
        rule_group_en="FROM_YML", group_en_fallback="FROM_CLI",
    )
    assert ctx["rule_group_en"] == "FROM_ENGINE"
    ctx = inputs.build_baseline_export_ctx(
        {"meta": {}}, {},
        is_yml_mode=False, consumed_yml_files=[], input_path=Path("x.xlsx"),
        rule_group_en="FROM_YML", group_en_fallback="FROM_CLI",
    )
    assert ctx["rule_group_en"] == "FROM_YML"
    ctx = inputs.build_baseline_export_ctx(
        {"meta": {}}, {},
        is_yml_mode=False, consumed_yml_files=[], input_path=Path("x.xlsx"),
        rule_group_en="", group_en_fallback="FROM_CLI",
    )
    assert ctx["rule_group_en"] == "FROM_CLI"
