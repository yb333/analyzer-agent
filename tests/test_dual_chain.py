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
