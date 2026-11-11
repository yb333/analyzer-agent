"""
pytest 测试：摘要自足化 + draft_inspect 只读探查（v1.21 契约升级）

锁三件事：
1. knowledge_summary 内容自足——名字全给（字段/表/块/CTE）、条件原文、
   加工列表达式、目标字段清单、inspect 指引；旧「详情已省略」退场
2. draft_inspect 各选择器冒烟（函数级 + CLI 子进程退出码 + --json）
3. DRAFT_SCHEMA 双锁——字典路径在真实 draft 中可解析（防文档腐烂）；
   draft 顶层键全被字典覆盖（防漏文档）

运行：
    pytest tests/test_summary_inspect.py -v
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ── 路径配置 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_SKILL = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
RUN_PY = PROJECT_ROOT / "dws-pipeline-analyzer" / "run.py"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"

sys.path.insert(0, str(ANALYZER_SKILL))
sys.path.insert(0, str(FIXTURES_DIR))
sys.path.insert(0, str(FIXTURES_DIR / "cases"))

from analyzer import read_excel, detect_dialect, generate_ai_summary
from engine import analyze_pipeline
import draft_inspect
from _build_xlsx import build_xlsx


# ── Fixtures ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def case_knowledge():
    """两个代表性 case 的完整 knowledge + summary + draft JSON 落盘。

    case_16_many_joins：多 JOIN + WHERE（复杂步骤展开路径）
    case_02_cte_basic：CTE 预聚合（CTE 行/加工列/子块路径）
    """
    out = {}
    for case_name in ("case_16_many_joins", "case_02_cte_basic"):
        xlsx_path = FIXTURES_DIR / case_name / "execution_tasks.xlsx"
        if not xlsx_path.exists():
            mod = __import__(case_name)
            build_xlsx(str(xlsx_path), rules=mod.rules,
                       target_fields=getattr(mod, "target_fields", None),
                       group_variables=getattr(mod, "group_variables", None))
        raw = read_excel(str(xlsx_path))
        sql_texts = [r.query_sql for r in raw["rules"] if r.query_sql]
        knowledge, parsed_map = analyze_pipeline(
            raw["rules"], raw["target_fields"], raw.get("group_variables") or {},
            detect_dialect(sql_texts))
        summary = generate_ai_summary(
            knowledge, raw["rules"], parsed_map, knowledge["topology"],
            knowledge["field_mappings"], knowledge["quality"], knowledge["data_flow"])
        out[case_name] = {
            "knowledge": knowledge,
            "rules": raw["rules"],
            "summary": summary,
        }
    return out


@pytest.fixture(scope="module")
def draft_file(case_knowledge, tmp_path_factory):
    """case_02 的 knowledge 落成 draft JSON 文件（inspect 的输入）。"""
    p = tmp_path_factory.mktemp("draft") / "knowledge_draft.json"
    p.write_text(json.dumps(case_knowledge["case_02_cte_basic"]["knowledge"],
                            ensure_ascii=False), encoding="utf-8")
    return p


# ═══════════════════════════════════════════════════════════════
# 1. 摘要自足化
# ═══════════════════════════════════════════════════════════════

class TestSummarySelfSufficient:
    """摘要必须自足承载 Step 2 任务（块目的+关键字段）的 80%。"""

    def test_header_carries_inspect_guide(self, case_knowledge):
        """头部给 inspect 用法 + 禁止自写 python（契约正文本体）。"""
        s = case_knowledge["case_16_many_joins"]["summary"]
        assert "inspect --draft" in s, "头部应有 inspect 探查指引"
        assert "不要自己写 python" in s, "头部应明令禁止自写 python 读 draft"
        assert "--schema" in s and "--field" in s and "--search" in s

    def test_target_field_names_present(self, case_knowledge):
        """目标字段清单：名字全给（关键字段任务的直接输入）。"""
        s = case_knowledge["case_16_many_joins"]["summary"]
        assert "目标字段(11)" in s
        for name in ("product_id", "attr_1", "attr_9", "product_name"):
            assert name in s, f"目标字段 {name} 应出现在摘要里"

    def test_where_clause_verbatim(self, case_knowledge):
        """WHERE 原文直给（不是只给计数）。"""
        s = case_knowledge["case_16_many_joins"]["summary"]
        assert "- WHERE: t0.del_flag = 'N'" in s, "WHERE 原文应在摘要里且无重复关键字"
        assert "2 条件字段" in s, "特征计数保留"

    def test_blocks_carry_on_and_brought(self, case_knowledge):
        """逻辑块附 ON 条件 + 带出字段（块目的推理的证据）。"""
        s = case_knowledge["case_16_many_joins"]["summary"]
        assert "ON t0.product_id = t1.product_id" in s
        assert "带出: product_id, product_name" in s
        assert "块1:" in s, "块应编号（与文末模板对齐）"

    def test_column_names_not_only_counts(self, case_knowledge):
        """列清单给名字，不再只有「N列(direct=N)」计数。"""
        s = case_knowledge["case_16_many_joins"]["summary"]
        assert "直取列(11)" in s
        assert "attr_8" in s

    def test_cte_line_carries_source_and_expr(self, case_knowledge):
        """CTE 行：来源表 + 内部加工表达式。"""
        s = case_knowledge["case_02_cte_basic"]["summary"]
        assert "CTE agg: 源 ods.sales" in s
        assert "total=SUM(amount)" in s, "CTE 加工表达式应直给"

    def test_expr_column_with_expression(self, case_knowledge):
        """加工列逐个附表达式。"""
        s = case_knowledge["case_02_cte_basic"]["summary"]
        assert "加工列 total = agg.total [aggregate]" in s

    def test_legacy_omit_wording_gone(self, case_knowledge):
        """旧「详情已省略」整段消失的措辞必须退场（降级不消失）。"""
        for case in case_knowledge.values():
            assert "详情已省略" not in case["summary"]

    def test_template_unchanged_shape(self, case_knowledge):
        """文末 AI 输出模板形状不变（view_generator 解析兼容）。"""
        s = case_knowledge["case_16_many_joins"]["summary"]
        assert "# 整体描述" in s
        assert "### 块目的" in s
        assert "## 关键字段" in s
        assert "- 字段名: 业务含义" in s


# ═══════════════════════════════════════════════════════════════
# 2. draft_inspect 冒烟
# ═══════════════════════════════════════════════════════════════

class TestInspectSelectors:
    """函数级：各选择器命中预期内容。"""

    @pytest.fixture(scope="class")
    def draft(self, draft_file):
        return json.loads(draft_file.read_text(encoding="utf-8"))

    def test_step_sql(self, draft):
        lines, _ = draft_inspect.render_step(draft, "step_1", "sql")
        assert "WITH agg AS" in "\n".join(lines)

    def test_step_by_rule_code(self, draft):
        """step 定位也接受 rule_code。"""
        lines, _ = draft_inspect.render_step(draft, "UR002", "summary")
        assert lines is not None

    def test_step_blocks_recursive(self, draft):
        lines, _ = draft_inspect.render_step(draft, "step_1", "blocks")
        text = "\n".join(lines)
        assert "ods.sales" in text, "子块（CTE 内部表）应递归出现"

    def test_step_fields(self, draft):
        lines, _ = draft_inspect.render_step(draft, "step_1", "fields")
        text = "\n".join(lines)
        assert "total [aggregate]" in text
        assert "SUM(amount) AS total" in text, "血缘链表达式应给出"

    def test_field_lineage(self, draft):
        lines, _ = draft_inspect.render_field(draft, "total")
        text = "\n".join(lines)
        assert "经 CTE agg" in text
        assert "物理源" in text

    def test_table_usage(self, draft):
        lines, _ = draft_inspect.render_table(draft, "ods.sales")
        text = "\n".join(lines)
        assert "CTE agg 源表" in text

    def test_search_hits(self, draft):
        lines, _ = draft_inspect.render_search(draft, "SUM(")
        assert "SUM(amount)" in "\n".join(lines)

    def test_schema_renders(self):
        lines, payload = draft_inspect.render_schema()
        assert len(lines) > 10
        assert "data_flow.steps[].data_blocks" in payload


class TestInspectCLI:
    """CLI 级：经 run.py 分发（别名 inspect）、退出码、--json。"""

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(RUN_PY), "inspect", *extra],
            capture_output=True, text=True, encoding="utf-8")

    def test_exit_0_and_markdown(self, draft_file):
        r = self._run("--draft", str(draft_file), "--step", "step_1", "--part", "sql")
        assert r.returncode == 0, r.stderr
        assert "WITH agg AS" in r.stdout

    def test_json_output(self, draft_file):
        r = self._run("--draft", str(draft_file), "--field", "total", "--json")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data[0]["target_field"] == "total"

    def test_unknown_step_exit_1(self, draft_file):
        r = self._run("--draft", str(draft_file), "--step", "nope")
        assert r.returncode == 1
        assert "未找到步骤" in r.stderr

    def test_missing_draft_exit_1(self, tmp_path):
        r = self._run("--draft", str(tmp_path / "no_such.json"))
        assert r.returncode == 1


# ═══════════════════════════════════════════════════════════════
# 3. DRAFT_SCHEMA 双锁
# ═══════════════════════════════════════════════════════════════

def _resolve_schema_path(draft, path):
    """沿字典路径解析；"[]" = 取 list 首元素继续。返回 None=路径不通。"""
    cur = draft
    for seg in path.split("."):
        if seg.endswith("[]"):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(seg[:-2])
            if not isinstance(cur, list) or not cur:
                return None
            cur = cur[0]
        else:
            if not isinstance(cur, dict) or seg not in cur:
                return None
            cur = cur[seg]
    return cur


class TestSchemaLock:
    """① 字典每条路径在真实 draft 中可解析（标注「可选」的条件段除外）；
    ② draft 顶层键全被字典覆盖。"""

    def test_documented_paths_resolve(self, draft_file):
        draft = json.loads(draft_file.read_text(encoding="utf-8"))
        missing = [p for p, desc in draft_inspect.DRAFT_SCHEMA.items()
                   if "可选" not in desc and _resolve_schema_path(draft, p) is None]
        assert not missing, f"schema 字典路径在真实 draft 中不存在（文档腐烂）: {missing}"

    def test_top_level_keys_covered(self, draft_file):
        draft = json.loads(draft_file.read_text(encoding="utf-8"))
        documented_top = {k.split(".")[0] for k in draft_inspect.DRAFT_SCHEMA}
        uncovered = set(draft.keys()) - documented_top
        assert not uncovered, f"draft 顶层键未被 schema 字典覆盖: {uncovered}"
