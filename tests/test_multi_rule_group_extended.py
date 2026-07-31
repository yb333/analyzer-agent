"""多规则组链路分析 —— 边角场景扩充用例。

在 test_multi_rule_group.py 的 12 个基础用例之上，系统性覆盖 9 类边角场景。
其中 A/B/C 三类直接锁定本批次修复的 3 个真实 bug：

  B1（已修复）：多目标规则组依赖漏建 —— 规则组内早期步骤写的表被下游读取时，
                merge 阶段曾因只用 max_seq 的 target 而漏建依赖，导致 exec_sequence 错位。
  B2（已修复）：环检测标志失效 —— cycle_detected 曾恒为 False，真实环被静默吞掉。
  B3（已修复）：max_depth 静默截断 —— 超过深度上限时上游被丢弃且无任何信号。

其余 D~I 覆盖菱形依赖、混合源、跨子项目、同表多写入者、_init 过滤、自引用、
删数规则、exchange 分区等真实代码仓会出现的结构。

运行:
    pytest tests/test_multi_rule_group_extended.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(ANALYZER_REF))
sys.path.insert(0, str(FIXTURES))

from _build_yml import build_yml_group
from analyzer import (
    build_target_index, trace_upstream_rule_groups, merge_rule_groups,
)
from engine import analyze_pipeline, _norm_table


# ═══════════════════════════════════════════════════════════════
# 测试工具
# ═══════════════════════════════════════════════════════════════

def _mk_chain(base_dir, spec):
    """构造多规则组代码仓（同子项目）。

    Args:
        base_dir: 临时目录
        spec: [(dirname, rule_group_en, rule_group_code, [rule_dict])]
              rule_dict 用 _R() 构造；target_schema/rule_type=1/delete_mode 等自动补全

    Returns: (repo_root, final_group_dir)
    """
    repo = Path(base_dir) / "repo"
    sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
    sub.mkdir(parents=True)
    for dirname, rgen, rgcode, rules in spec:
        for r in rules:
            r.setdefault("target_schema", "dws")
            r.setdefault("rule_type", 1)
            r.setdefault("delete_mode", "1")
            r.setdefault("rule_group_en", rgen)
            r.setdefault("rule_group_code", rgcode)
        build_yml_group(sub / dirname, rules=rules)
    final_dirname = spec[-1][0]
    return repo, sub / final_dirname


def _R(rule_code, exec_sequence, target_table, query_sql, **extra):
    """构造一条规则 dict。"""
    d = {
        "rule_code": rule_code,
        "exec_sequence": exec_sequence,
        "target_table": target_table,
        "query_sql": query_sql,
    }
    d.update(extra)
    return d


def _group_names(result):
    return {g["rule_group_en"] for g in result["groups"]}


def _depths(result):
    return {g["rule_group_en"]: g["depth"] for g in result["groups"]}


# ═══════════════════════════════════════════════════════════════
# A. 多目标规则组依赖（锁 B1 —— 修复前会失败）
# ═══════════════════════════════════════════════════════════════

class TestMultiTargetGroupDeps:
    """规则组内多步写多张表，下游读早期步骤写的表时依赖必须建立。

    根因（已修复）：merge_rule_groups 的 group_target 曾用 dict[dir]→单个target，
    只记录 max_seq rule 的 target_table，导致早期步骤写的表无法建立组间依赖。
    """

    def test_downstream_reads_early_target_orders_after_upstream(self, tmp_path):
        """GRP_X 内 step1 写 mid_x、step2 写 final_x；GRP_Y 读 mid_x → Y 必须排在 X 之后。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("GRP_X", "GRP_X", "GX", [
                _R("X1", 0, "mid_x", "SELECT a.id FROM ods.src_a a"),
                _R("X2", 1, "final_x", "SELECT a.id FROM dws.mid_x a"),
            ]),
            ("GRP_Y", "GRP_Y", "GY", [
                _R("Y1", 1, "final_y", "SELECT a.id FROM dws.mid_x a"),
            ]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert _group_names(result) == {"GRP_X", "GRP_Y"}

        merged, _, _ = merge_rule_groups(result, repo)
        x_seqs = [r.exec_sequence for r in merged if r.rule_group_en == "GRP_X"]
        y_seqs = [r.exec_sequence for r in merged if r.rule_group_en == "GRP_Y"]
        # 关键断言：下游 Y 的所有 seq 必须大于上游 X 的最大 seq
        assert min(y_seqs) > max(x_seqs), \
            f"GRP_Y 应排在 GRP_X 之后（读其早期写的 mid_x）: X={x_seqs} Y={y_seqs}"

    def test_topology_chains_through_early_target(self, tmp_path):
        """完整链路分析后，步骤间数据依赖经过 mid_x 正确串联。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("GRP_X", "GRP_X", "GX", [
                _R("X1", 0, "mid_x", "SELECT a.id FROM ods.src_a a"),
                _R("X2", 1, "final_x", "SELECT a.id FROM dws.mid_x a"),
            ]),
            ("GRP_Y", "GRP_Y", "GY", [
                _R("Y1", 1, "final_y", "SELECT a.id FROM dws.mid_x a"),
            ]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        merged, tf, gv = merge_rule_groups(result, repo)
        kj, _ = analyze_pipeline(merged, tf, gv, "dws")

        deps = kj["topology"].get("data_dependencies", [])
        # 应存在经过 mid_x 的依赖边
        mid_deps = [d for d in deps if "mid_x" in d.get("intermediate_table", "")]
        assert mid_deps, f"应有经过 mid_x 的数据依赖: {deps}"

    def test_multi_target_group_trace_finds_it(self, tmp_path):
        """trace 阶段：读早期目标表的下游能追溯到多目标规则组。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("GRP_X", "GRP_X", "GX", [
                _R("X1", 0, "mid_x", "SELECT a.id FROM ods.src_a a"),
                _R("X2", 1, "final_x", "SELECT a.id FROM dws.mid_x a"),
            ]),
            ("GRP_Y", "GRP_Y", "GY", [
                _R("Y1", 1, "final_y", "SELECT a.id FROM dws.mid_x a"),
            ]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert "GRP_X" in _group_names(result)

    def test_three_target_group_chain(self, tmp_path):
        """规则组写 3 张表，下游读第 1 张（最早的）也能建立依赖。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("GRP_M", "GRP_M", "GM", [
                _R("M1", 0, "t_a", "SELECT a.id FROM ods.src a"),
                _R("M2", 1, "t_b", "SELECT a.id FROM dws.t_a a"),
                _R("M3", 2, "t_final", "SELECT a.id FROM dws.t_b a"),
            ]),
            ("GRP_N", "GRP_N", "GN", [
                _R("N1", 1, "n_out", "SELECT a.id FROM dws.t_a a"),  # 读最早的 t_a
            ]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        merged, _, _ = merge_rule_groups(result, repo)
        m_seqs = [r.exec_sequence for r in merged if r.rule_group_en == "GRP_M"]
        n_seqs = [r.exec_sequence for r in merged if r.rule_group_en == "GRP_N"]
        assert min(n_seqs) > max(m_seqs), \
            f"GRP_N 读 GRP_M 最早的 t_a，仍应排在 GRP_M 之后: M={m_seqs} N={n_seqs}"


# ═══════════════════════════════════════════════════════════════
# B. 环检测（锁 B2 —— 修复前 cycle_detected 恒为 False）
# ═══════════════════════════════════════════════════════════════

class TestCycleDetection:
    """真实环（A 读 B、B 读 A）必须被检测出来，且不能死循环。

    根因（已修复）：原实现用 len(visited_dirs) < len(groups) 判断环，
    但每个进入 groups 的 dir 必先进入 visited_dirs，该条件恒为 False。
    改为追踪递归栈（pending_dirs）做真实环检测。
    """

    def test_two_group_cycle_detected(self, tmp_path):
        """A 读 B、B 读 A 的互读环 → cycle_detected=True。"""
        repo, _ = _mk_chain(tmp_path, [
            ("GRP_A", "GRP_A", "GA", [
                _R("A1", 1, "tbl_a", "SELECT x.id FROM dws.tbl_b x"),
            ]),
            # _mk_chain 把最后一个当 final，这里手动追溯 GRP_A
        ])
        # 手动构造 B（_mk_chain 默认 final 是最后一个，这里单独建一个 B 组）
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        build_yml_group(sub / "GRP_B", rules=[_R("B1", 1, "tbl_b", "SELECT x.id FROM dws.tbl_a x",
                                                  rule_group_en="GRP_B", rule_group_code="GB",
                                                  target_schema="dws", rule_type=1, delete_mode="1")])
        result = trace_upstream_rule_groups(sub / "GRP_A", repo)
        assert result["cycle_detected"] is True, "A↔B 互读应检测出环"
        # 也不能死循环（groups 数量有界）
        assert len(result["groups"]) <= 2

    def test_three_group_cycle_detected(self, tmp_path):
        """三角环 A→B→C→A → cycle_detected=True。"""
        repo, _ = _mk_chain(tmp_path, [
            ("GRP_A", "GRP_A", "GA", [
                _R("A1", 1, "tbl_a", "SELECT x.id FROM dws.tbl_c x"),
            ]),
        ])
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        build_yml_group(sub / "GRP_B", rules=[_R("B1", 1, "tbl_b", "SELECT x.id FROM dws.tbl_a x",
                                                  rule_group_en="GRP_B", rule_group_code="GB",
                                                  target_schema="dws", rule_type=1, delete_mode="1")])
        build_yml_group(sub / "GRP_C", rules=[_R("C1", 1, "tbl_c", "SELECT x.id FROM dws.tbl_b x",
                                                  rule_group_en="GRP_C", rule_group_code="GC",
                                                  target_schema="dws", rule_type=1, delete_mode="1")])
        result = trace_upstream_rule_groups(sub / "GRP_A", repo)
        assert result["cycle_detected"] is True, "A→B→C→A 三角环应检测出环"

    def test_no_false_cycle_on_diamond(self, tmp_path):
        """菱形依赖（共享上游被两条路径访问）不是环 → cycle_detected=False。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("BASE", "BASE", "GB", [
                _R("B1", 1, "base_f", "SELECT a.id FROM ods.src a"),
            ]),
            ("MID1", "MID1", "GM1", [
                _R("M1", 1, "mid1_f", "SELECT a.id FROM dws.base_f a"),
            ]),
            ("MID2", "MID2", "GM2", [
                _R("M2", 1, "mid2_f", "SELECT a.id FROM dws.base_f a"),
            ]),
            ("FIN", "FIN", "GF", [
                _R("F1", 1, "fin_f",
                   "SELECT a.id FROM dws.mid1_f a LEFT JOIN dws.mid2_f b ON a.id=b.id"),
            ]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert result["cycle_detected"] is False, "菱形依赖不是环"


# ═══════════════════════════════════════════════════════════════
# C. 深度截断可观测（锁 B3 —— 修复前 truncated 字段不存在/不上报）
# ═══════════════════════════════════════════════════════════════

class TestDepthTruncation:
    """超过 max_depth 时上游被截断，必须通过 truncated 字段上报。

    根因（已修复）：原实现 depth > max_depth 时静默 return，调用方无法区分
    "自然到顶 ODS" 和 "被深度截断"。新增 truncated 字段标记截断。
    """

    @staticmethod
    def _build_n_level_chain(base_dir, n):
        """建 n 层链路：lv0(读ods)→lv1→...→lv{n-1}(final)。"""
        repo = Path(base_dir) / "repo"
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        sub.mkdir(parents=True)
        for i in range(n):
            if i == 0:
                sql = "SELECT a.id FROM ods.src a"
            else:
                sql = f"SELECT a.id FROM dws.lv{i-1} a"
            build_yml_group(sub / f"L{i}", rules=[_R(
                f"R{i}", 1, f"lv{i}", sql,
                rule_group_en=f"L{i}", rule_group_code=f"C{i}",
                target_schema="dws", rule_type=1, delete_mode="1",
            )])
        return repo, sub / f"L{n-1}"

    def test_within_depth_no_truncation(self, tmp_path):
        """7 层链路（depth 0..6）在 max_depth=6 下不截断。"""
        repo, final_dir = self._build_n_level_chain(tmp_path, 7)
        result = trace_upstream_rule_groups(final_dir, repo, max_depth=6)
        assert len(result["groups"]) == 7
        assert result["truncated"] is False

    def test_beyond_depth_truncation_reported(self, tmp_path):
        """8 层链路（depth 0..7）在 max_depth=6 下，第 7 层被截断且 truncated=True。"""
        repo, final_dir = self._build_n_level_chain(tmp_path, 8)
        result = trace_upstream_rule_groups(final_dir, repo, max_depth=6)
        # 第 7 层（lv7 的上游 lv0..lv6 中，lv7 自己 depth=0，lv0 depth=7 被截断）
        assert len(result["groups"]) < 8, "超过深度的层应被截断"
        assert result["truncated"] is True, "截断必须通过 truncated 字段上报"

    def test_truncation_field_exists(self, tmp_path):
        """返回值始终包含 truncated 字段（即使正常不截断）。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("MID", "MID", "GM", [_R("M1", 1, "mid_f", "SELECT a.id FROM ods.s a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.mid_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert "truncated" in result
        assert result["truncated"] is False


# ═══════════════════════════════════════════════════════════════
# D. 菱形依赖（共享上游只访问一次）
# ═══════════════════════════════════════════════════════════════

class TestDiamondDependency:
    """BASE→{MID1,MID2}→FIN，共享上游 BASE 只被访问一次，但依赖正确建立。"""

    def test_shared_upstream_visited_once(self, tmp_path):
        repo, final_dir = _mk_chain(tmp_path, [
            ("BASE", "BASE", "GB", [_R("B1", 1, "base_f", "SELECT a.id FROM ods.src a")]),
            ("MID1", "MID1", "GM1", [_R("M1", 1, "mid1_f", "SELECT a.id FROM dws.base_f a")]),
            ("MID2", "MID2", "GM2", [_R("M2", 1, "mid2_f", "SELECT a.id FROM dws.base_f a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f",
                "SELECT a.id FROM dws.mid1_f a LEFT JOIN dws.mid2_f b ON a.id=b.id")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        names = _group_names(result)
        assert names == {"BASE", "MID1", "MID2", "FIN"}
        # BASE 只出现一次
        base_count = sum(1 for g in result["groups"] if g["rule_group_en"] == "BASE")
        assert base_count == 1, f"共享上游 BASE 应只访问一次，实际 {base_count} 次"

    def test_diamond_depths(self, tmp_path):
        """菱形各层 depth 正确：FIN=0, MID1/MID2=1, BASE=2。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("BASE", "BASE", "GB", [_R("B1", 1, "base_f", "SELECT a.id FROM ods.src a")]),
            ("MID1", "MID1", "GM1", [_R("M1", 1, "mid1_f", "SELECT a.id FROM dws.base_f a")]),
            ("MID2", "MID2", "GM2", [_R("M2", 1, "mid2_f", "SELECT a.id FROM dws.base_f a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f",
                "SELECT a.id FROM dws.mid1_f a LEFT JOIN dws.mid2_f b ON a.id=b.id")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        depths = _depths(result)
        assert depths["FIN"] == 0
        assert depths["MID1"] == 1
        assert depths["MID2"] == 1
        assert depths["BASE"] == 2

    def test_diamond_merge_orders_base_first(self, tmp_path):
        """合并后 BASE 的 seq 整体最小，FIN 的 seq 最大。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("BASE", "BASE", "GB", [_R("B1", 1, "base_f", "SELECT a.id FROM ods.src a")]),
            ("MID1", "MID1", "GM1", [_R("M1", 1, "mid1_f", "SELECT a.id FROM dws.base_f a")]),
            ("MID2", "MID2", "GM2", [_R("M2", 1, "mid2_f", "SELECT a.id FROM dws.base_f a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f",
                "SELECT a.id FROM dws.mid1_f a LEFT JOIN dws.mid2_f b ON a.id=b.id")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        merged, _, _ = merge_rule_groups(result, repo)
        base_seqs = [r.exec_sequence for r in merged if r.rule_group_en == "BASE"]
        fin_seqs = [r.exec_sequence for r in merged if r.rule_group_en == "FIN"]
        assert max(base_seqs) < min(fin_seqs), \
            f"BASE 应排在 FIN 之前: BASE={base_seqs} FIN={fin_seqs}"


# ═══════════════════════════════════════════════════════════════
# E. 混合源（一个组既读 mid 又读 ods）
# ═══════════════════════════════════════════════════════════════

class TestMixedSources:
    """一个规则组既读代码仓内 mid 表又读 ods 源表：mid 被追溯，ods 进 not_found。"""

    def test_mid_traced_and_ods_in_not_found(self, tmp_path):
        repo, final_dir = _mk_chain(tmp_path, [
            ("UP", "UP", "GU", [_R("U1", 1, "up_f", "SELECT a.id FROM ods.up_src a")]),
            ("MID", "MID", "GM", [_R("M1", 1, "mid_f",
                "SELECT a.id FROM dws.up_f a LEFT JOIN ods.extra b ON a.id=b.id")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.mid_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert "UP" in _group_names(result), "代码仓内的 up_f 应被追溯"
        assert "ods.extra" in result["not_found"], "ods.extra 应进 not_found"

    def test_only_ods_no_upstream(self, tmp_path):
        """只读 ods 的规则组，追溯只返回自己，ods 进 not_found。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f", "SELECT a.id FROM ods.only_src a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert len(result["groups"]) == 1
        assert "ods.only_src" in result["not_found"]


# ═══════════════════════════════════════════════════════════════
# F. 跨子项目追溯（project 级回退）
# ═══════════════════════════════════════════════════════════════

class TestCrossSubProject:
    """mid 规则组在同项目的不同子项目下时，通过 project 级索引回退找到。"""

    def test_mid_in_sibling_sub_project(self, tmp_path):
        """mid 在 P_TRADE/SUB_MID，final 在 P_TRADE/SUB_ORDER → project 级回退找到。"""
        repo = tmp_path / "repo"
        sub_order = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_ORDER"
        sub_mid = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_MID"
        sub_order.mkdir(parents=True)
        sub_mid.mkdir(parents=True)

        build_yml_group(sub_mid / "DWB_MID_F", rules=[_R(
            "M1", 1, "dwb_mid_f", "SELECT a.id FROM ods.src a",
            rule_group_en="DWB_MID_F", rule_group_code="GM",
            target_schema="dws", rule_type=1, delete_mode="1")])
        build_yml_group(sub_order / "DWB_FINAL_F", rules=[_R(
            "F1", 1, "dwb_final_f", "SELECT a.id FROM dws.dwb_mid_f a",
            rule_group_en="DWB_FINAL_F", rule_group_code="GF",
            target_schema="dws", rule_type=1, delete_mode="1")])

        result = trace_upstream_rule_groups(sub_order / "DWB_FINAL_F", repo)
        assert "DWB_MID_F" in _group_names(result), "同项目不同子项目的 mid 应通过回退找到"

    def test_mid_in_different_project_not_found(self, tmp_path):
        """mid 在完全不同的项目 P_OTHER 下 → 超出回退范围，进 not_found（设计边界）。"""
        repo = tmp_path / "repo"
        sub_other = repo / "BFT" / "BftWideTable" / "P_OTHER" / "SUB_X"
        sub_trade = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_ORDER"
        sub_other.mkdir(parents=True)
        sub_trade.mkdir(parents=True)

        build_yml_group(sub_other / "DWB_MID_F", rules=[_R(
            "M1", 1, "dwb_mid_f", "SELECT a.id FROM ods.src a",
            rule_group_en="DWB_MID_F", rule_group_code="GM",
            target_schema="dws", rule_type=1, delete_mode="1")])
        build_yml_group(sub_trade / "DWB_FINAL_F", rules=[_R(
            "F1", 1, "dwb_final_f", "SELECT a.id FROM dws.dwb_mid_f a",
            rule_group_en="DWB_FINAL_F", rule_group_code="GF",
            target_schema="dws", rule_type=1, delete_mode="1")])

        result = trace_upstream_rule_groups(sub_trade / "DWB_FINAL_F", repo)
        # 回退只到 project 级（P_TRADE），跨项目找不到 → 在 not_found
        assert "DWB_MID_F" not in _group_names(result)
        assert any("dwb_mid_f" in t for t in result["not_found"])


# ═══════════════════════════════════════════════════════════════
# G. 同表多写入者
# ═══════════════════════════════════════════════════════════════

class TestSameTableMultipleWriters:
    """两个规则组都写同一张表时，下游读取应追溯到两个写入者。"""

    def test_both_writers_traced(self, tmp_path):
        repo, final_dir = _mk_chain(tmp_path, [
            ("MID_A", "MID_A", "GA", [_R("MA", 1, "dwb_mid_f", "SELECT a.id FROM ods.srca a")]),
            ("MID_B", "MID_B", "GB", [_R("MB", 1, "dwb_mid_f", "SELECT a.id FROM ods.srcb a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.dwb_mid_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        names = _group_names(result)
        assert "MID_A" in names and "MID_B" in names, \
            f"两个写入者都应被追溯: {names}"


# ═══════════════════════════════════════════════════════════════
# H. _init 规则组过滤
# ═══════════════════════════════════════════════════════════════

class TestInitGroupFiltering:
    """_init 后缀的规则组（初始化数据）被跳过；含 INIT 但非后缀的正常组不误杀。"""

    def test_init_group_skipped(self, tmp_path):
        """rule_group_en 以 _init 结尾的组被跳过。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("DWB_MID_INIT", "DWB_MID_INIT", "GI",
             [_R("I1", 1, "dwb_mid_f", "SELECT a.id FROM ods.src a")]),
            ("DWB_FINAL_F", "DWB_FINAL_F", "GF",
             [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.dwb_mid_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert "DWB_MID_INIT" not in _group_names(result), "_init 后缀组应被跳过"

    def test_init_suffix_in_dir_name_skipped(self, tmp_path):
        """目录名以 _init 结尾（即使 rule_group_en 不带）也被跳过。"""
        repo = tmp_path / "repo"
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        sub.mkdir(parents=True)
        # 目录名以 _init 结尾，但 rule_group_en 是正常名（模拟两者不一致的容错）
        build_yml_group(sub / "DWB_MID_INIT", rules=[_R(
            "I1", 1, "dwb_mid_f", "SELECT a.id FROM ods.src a",
            rule_group_en="NORMAL_NAME", rule_group_code="GI",
            target_schema="dws", rule_type=1, delete_mode="1")])
        build_yml_group(sub / "DWB_FINAL_F", rules=[_R(
            "F1", 1, "fin_f", "SELECT a.id FROM dws.dwb_mid_f a",
            rule_group_en="DWB_FINAL_F", rule_group_code="GF",
            target_schema="dws", rule_type=1, delete_mode="1")])
        result = trace_upstream_rule_groups(sub / "DWB_FINAL_F", repo)
        assert "NORMAL_NAME" not in _group_names(result), "目录名 _init 后缀也应被跳过"

    def test_init_in_middle_not_false_positive(self, tmp_path):
        """DWB_ORDER_INIT_F（含 INIT 但后缀是 _F）是正常业务组，不误杀。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("DWB_ORDER_INIT_F", "DWB_ORDER_INIT_F", "GOI",
             [_R("OI", 1, "dwb_order_mid_f", "SELECT a.id FROM ods.src a")]),
            ("DWB_FINAL_F", "DWB_FINAL_F", "GF",
             [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.dwb_order_mid_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert "DWB_ORDER_INIT_F" in _group_names(result), \
            "含 INIT 但后缀非 _init 的正常组不应被误杀"


# ═══════════════════════════════════════════════════════════════
# I. 边界 / 健壮性
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    """单规则组、自引用、删数规则、exchange 分区等边界。"""

    def test_single_group_no_upstream(self, tmp_path):
        """无上游的单规则组，追溯只返回自己。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("DWB_SIMPLE_F", "DWB_SIMPLE_F", "GR1",
             [_R("R1", 1, "dwb_simple_f", "SELECT a.id FROM ods.src_a a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert len(result["groups"]) == 1
        assert result["groups"][0]["rule_group_en"] == "DWB_SIMPLE_F"
        assert result["cycle_detected"] is False
        assert result["truncated"] is False

    def test_self_reference_no_infinite_loop(self, tmp_path):
        """规则组读自己写的表（自引用）→ 不死循环，且构成自环。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("DWB_FINAL_F", "DWB_FINAL_F", "GF",
             [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.fin_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        # 不死循环
        assert len(result["groups"]) == 1
        # 自引用构成自环，应被检测为环
        assert result["cycle_detected"] is True

    def test_delete_rule_not_crash(self, tmp_path):
        """含删数规则（rule_type=2）的规则组追溯不崩溃。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("DWB_FINAL_F", "DWB_FINAL_F", "GF", [
                _R("D1", 0, "fin_f", "DELETE FROM fin_f", rule_type=2),
                _R("F1", 1, "fin_f", "SELECT a.id FROM ods.src a"),
            ]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        assert len(result["groups"]) == 1

    def test_trace_returns_complete_structure(self, tmp_path):
        """返回值结构完整：含 groups/not_found/cycle_detected/truncated 四个字段。"""
        repo, final_dir = _mk_chain(tmp_path, [
            ("MID", "MID", "GM", [_R("M1", 1, "mid_f", "SELECT a.id FROM ods.s a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.mid_f a")]),
        ])
        result = trace_upstream_rule_groups(final_dir, repo)
        for key in ("groups", "not_found", "cycle_detected", "truncated"):
            assert key in result, f"返回值缺少 {key} 字段"


# ═══════════════════════════════════════════════════════════════
# J. 索引健壮性（build_target_index 边界）
# ═══════════════════════════════════════════════════════════════

class TestTargetIndexRobustness:
    """build_target_index 的边界行为。"""

    def test_index_handles_duplicate_targets(self, tmp_path):
        """两个规则组写同一张表 → 索引该表对应两个写入者。"""
        repo, _ = _mk_chain(tmp_path, [
            ("MID_A", "MID_A", "GA", [_R("MA", 1, "shared_tbl", "SELECT a.id FROM ods.a a")]),
            ("MID_B", "MID_B", "GB", [_R("MB", 1, "shared_tbl", "SELECT a.id FROM ods.b a")]),
            ("FIN", "FIN", "GF", [_R("F1", 1, "fin_f", "SELECT a.id FROM dws.shared_tbl a")]),
        ])
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        index = build_target_index(repo, sub)
        writers = index.get("shared_tbl", [])
        assert len(writers) == 2, f"shared_tbl 应有 2 个写入者: {len(writers)}"

    def test_empty_repo_returns_empty_index(self, tmp_path):
        """空代码仓（无 yml）→ 索引为空。"""
        repo = tmp_path / "repo"
        sub = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE"
        sub.mkdir(parents=True)
        index = build_target_index(repo, sub)
        assert index == {}
