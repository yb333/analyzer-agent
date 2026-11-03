"""引擎子查询字段表达式链测试（engine_ctx 装配通道，2026-11-03）。

extract_subquery_field_chains：对停在子查询别名的字段（多列表达式边界 /
JOIN 子查询）产出逐跳表达式链（AST 提取，refs 随行）；只进 engine_ctx，
knowledge 存量零变化（golden 零 diff 另行护栏）。

运行:
    pytest tests/test_subquery_chains.py -v
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dws-pipeline-analyzer" / "references"))

from engine import parse_single_sql, extract_subquery_field_chains

# 用户实测结构（case_31 同构）：tt 层 CASE → b 层 SUM CASE 收敛 → s 带出
SQL_NESTED = """SELECT s.id, s.is_flag
FROM (
    SELECT t1.account_id AS id, b.is_flag,
           ROW_NUMBER() OVER (PARTITION BY t1.account_id ORDER BY t1.last_update_time DESC) rn
    FROM ods.ods_acct_main t1
    LEFT JOIN (
        SELECT tt.account_id,
               CASE WHEN SUM(tt.is_flag) > 0 THEN 'Y' ELSE 'N' END AS is_flag
        FROM (
            SELECT t2.account_id,
                   CASE WHEN t3.tag_id = '1' THEN '1' ELSE '0' END AS is_flag
            FROM ods.ods_acct_pay t2
            LEFT JOIN ods.ods_acct_tag t3 ON t2.id = t3.id AND t3.tag_name = 'Y'
        ) tt
        GROUP BY account_id
    ) b ON t1.account_id = b.account_id
    WHERE b.is_flag IS NOT NULL
) s
WHERE s.rn = 1"""


class TestSubqueryFieldChains:

    def test_two_hop_case_chain(self):
        """跨聚合两跳链：b 层 SUM CASE → tt 层 CASE → 物理根 t3.tag_id。"""
        parsed = parse_single_sql(SQL_NESTED, "dws")
        chains = extract_subquery_field_chains(parsed)
        chain = chains["is_flag"]
        assert [e["alias"] for e in chain] == ["b", "tt"]
        b_hop, tt_hop = chain
        assert b_hop["expression"] == \
            "CASE WHEN SUM(tt.is_flag) > 0 THEN 'Y' ELSE 'N' END"
        assert b_hop["refs"] == [{"alias": "tt", "field": "is_flag"}]
        assert tt_hop["expression"] == \
            "CASE WHEN t3.tag_id = '1' THEN '1' ELSE '0' END"
        assert tt_hop["refs"] == [{"alias": "t3", "field": "tag_id"}]

    def test_penetrated_field_has_no_chain(self):
        """单列直传已被 _penetrate_subquery_columns 穿透（s.id→t1.account_id），
        停在物理别名 → 不产链。"""
        parsed = parse_single_sql(SQL_NESTED, "dws")
        chains = extract_subquery_field_chains(parsed)
        assert "id" not in chains

    def test_sysdate_function_chain(self):
        """纯函数字段（sysdate()）停在 s 层 → 单跳链（表达式即函数体）。"""
        sql = SQL_NESTED.replace(
            "SELECT t1.account_id AS id, b.is_flag,",
            "SELECT t1.account_id AS id, b.is_flag, sysdate() AS dw_last_update_date,",
            1).replace("SELECT s.id, s.is_flag",
                       "SELECT s.id, s.is_flag, s.dw_last_update_date", 1)
        parsed = parse_single_sql(sql, "dws")
        chains = extract_subquery_field_chains(parsed)
        dw = chains.get("dw_last_update_date")
        assert dw and dw[0]["alias"] == "s"
        # sqlglot oracle 方言对 sysdate() 归一渲染为 CURRENT_TIMESTAMP（结构保真）
        assert dw[0]["expression"].upper() in ("SYSDATE", "SYSDATE()",
                                               "CURRENT_TIMESTAMP")
        assert dw[0]["refs"] == []

    def test_no_subquery_empty(self):
        """无子查询 → 空链。"""
        parsed = parse_single_sql(
            "SELECT a.x FROM ods.t a LEFT JOIN ods.u u ON a.id = u.id", "dws")
        assert extract_subquery_field_chains(parsed) == {}

    def test_engine_ctx_wiring(self):
        """analyze_pipeline 全链路：chains 进 engine_ctx，knowledge 无新键。"""
        import io
        import contextlib
        from pipeline_core import run_from_dir
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        shutil.copy(PROJECT_ROOT / "tests" / "fixtures" / "analyzer" /
                    "case_31_real_nested_chain" / "execution_tasks.xlsx",
                    tmp / "et.xlsx")
        from pipeline_core import run_from_dir as _rf
        with contextlib.redirect_stdout(io.StringIO()):
            core = _rf(str(tmp), include_dq=False, channel="spec", entry_title="")
        chains = core["engine_ctx"].get("subquery_field_chains", {})
        assert chains and "is_flag" in chains.get("step_1", {})
        # knowledge 侧零污染（新键只在 engine_ctx）
        for f in core["knowledge"]["field_mappings"]["fields"]:
            assert "subquery_expression" not in f
            for l in f.get("lineage", []):
                assert "subquery_expression_chain" not in l
