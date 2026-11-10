#!/usr/bin/env python3
"""全量/增量双链维度检测契约测试（2026-11-10，样例 = 用户口径特征）。

判据（用户口径）：共享目标表 + 写入方式族差（全量 TRUNCATE/DELETE vs
增量 追加/MERGE）+ 过滤条件特征；规则编码不参与判断。
结构化产出（topology.chains，仅双链挂载）：链归属 + 增量链独有谓词 +
临时表字段差——投影层只消费不判断（承载方案：共底座 + 增量链限制段）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "dws-pipeline-analyzer" / "references"))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "analyzer" / "case_33_dual_chain"


def _core():
    from pipeline_core import run_from_dir
    return run_from_dir(str(FIXTURE), include_dq=False, channel="test", entry_title="")


def test_dual_chain_detected():
    chains = _core()["knowledge"]["topology"]["chains"]
    assert chains["has_dual_chain"] is True
    by_kind = {c["kind"]: c for c in chains["chains"]}
    assert by_kind["full"]["rule_codes"] == ["GR0001", "GR0003"]
    assert by_kind["incremental"]["rule_codes"] == ["GR0002", "GR0004"]


def test_incremental_extras():
    chains = _core()["knowledge"]["topology"]["chains"]
    incr = next(c for c in chains["chains"] if c["kind"] == "incremental")
    preds = [p["predicate"] for p in incr["extra_predicates"]]
    assert any("p_cycle_id" in p and "dw_last_update_date" in p for p in preds), preds
    gaps = [g for g in incr["tmp_field_gap"] if g["target"] == "tmp_dchain"]
    assert gaps and gaps[0]["fields"] == ["dw_last_update_date"]


def test_no_dual_chain_no_key():
    """单链资产不挂 chains 键（缺键 = 无双链，零负载）。"""
    from engine import RawRule, analyze_pipeline, detect_dialect
    sql = "SELECT a.x FROM ods.t1 a"
    rules = [RawRule(rule_code="R1", rule_name="s", rule_type=1, exec_sequence=1,
                     target_schema="dws", target_table="f_d", delete_mode="1",
                     query_sql=sql)]
    knowledge, _ = analyze_pipeline(rules, {}, {}, detect_dialect([sql]),
                                    source_file="x.xlsx")
    assert "chains" not in knowledge["topology"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_projection_incremental_segment():
    """共底座投影：一套行 + 增量链独有谓词带标签段；双链重复体不重复挂载。"""
    from spec_export import build_spec_model
    from mapping_export import build_mapping_model
    core = _core()
    model = build_spec_model(core["knowledge"], core["engine_ctx"])
    # 共底座：只有一个主表行（增量链终端不重复挂载）
    mains = [r for r in model["sheet3"] if r[5] == "主表"]
    assert len(mains) == 1, f"双链重复体应去重，实际 {len(mains)} 个主表行"
    # 挂载基底 = 全量链定义（含审计字段），增量谓词走标签段
    blob = "；".join(str(r[6]) for r in model["sheet3"])
    assert "增量链限制：a.dw_last_update_date >= '${p_cycle_id}'" in blob
    assert "dw_last_update_date >= '${p_cycle_id}'" not in blob.replace(
        "增量链限制：a.dw_last_update_date >= '${p_cycle_id}'", "")  # 无标签外泄漏
    # 全量链共享谓词正常落限定段
    assert "限定：a.del_flag = 'N'" in blob
    # mapping 同源携带
    m = build_mapping_model(core["knowledge"], core["engine_ctx"])
    mblob = "；".join(e["on_condition"] for e in m["entity"])
    assert "增量链限制：" in mblob
    # 无 structural 披露（谓词守恒原子级通过）
    assert not [d for d in model["disclosures"] if d["severity"] == "structural"]


def test_load_strategy_dual_summary():
    """加载策略摘要：meta.load_strategy.dual_chain（html/tech_design 消费）。"""
    ls = _core()["knowledge"]["meta"]["load_strategy"]
    dc = ls.get("dual_chain")
    assert dc and dc["label"] == "全量+增量双链"
    assert dc["full_chain"] == ["GR0001", "GR0003"]
    assert dc["incremental_chain"] == ["GR0002", "GR0004"]


def test_e1_shape_incremental_segments():
    """E1 单表形态（case_22：三步直写同表，无 tmp 无 JOIN）：增量谓词按别名
    落对应源表行的「增量链限制」段——不同源别名（b/c）不互相吞。"""
    from pipeline_core import run_from_dir
    from spec_export import build_spec_model
    fixture = Path(__file__).resolve().parent / "fixtures" / "analyzer" / "case_22_scenario_chain"
    core = run_from_dir(str(fixture), include_dq=False, channel="test", entry_title="")
    model = build_spec_model(core["knowledge"], core["engine_ctx"])
    blob = "；".join(str(r[6]) for r in model["sheet3"])
    assert "增量链限制：b.del_flag = 'N'" in blob
    assert "增量链限制：c.del_flag = 'N'" in blob
    assert not [d for d in model["disclosures"] if d["severity"] == "structural"]
