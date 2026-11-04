"""sync_facts_spec 裁剪清单防漂移锁（facts/spec 内网同步通道的范围合同）。

背景（2026-11-03 case_32 双树冒烟实测）：
    engine.py 在解析热路径上**运行时懒加载** view_generator._describe_transform
    （engine.py:3582），模块级 import 图看不出来——纯静态闭包分析会漏掉
    view_generator/usage，裁剪树跑 facts 直接 ModuleNotFoundError。故清单必须
    用真实 fixture 端到端锁住：新增依赖模块而忘改 SKILL_MODULES 时，本测试
    在提交前就红。

覆盖：
1. 清单完整性：SKILL_MODULES/SKILL_ASSETS 引用的文件全部存在于仓库
2. 裁剪树结构：17 模块 + 资产 + 裁剪版 SKILL.md，未收录文件不出现
3. 端到端冒烟：裁剪树上跑 facts / spec（case_01_minimal），退出码 0 + 定位行

运行:
    pytest tests/test_sync_scope.py -v
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sync_facts_spec import (COMMANDS, SKILL_ASSETS, SKILL_DIR, SKILL_MD_SOURCE,
                             SKILL_MODULES, build_trimmed_skill)

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "analyzer" / "case_01_minimal"


class TestManifestComplete:
    """清单引用的源文件必须真实存在（防清单与仓库漂移）。"""

    def test_modules_exist(self):
        for mod in SKILL_MODULES:
            assert (PROJECT_ROOT / SKILL_DIR / "references" / f"{mod}.py").exists(), mod

    def test_assets_exist(self):
        for asset in SKILL_ASSETS:
            assert (PROJECT_ROOT / SKILL_DIR / asset).exists(), asset

    def test_skill_md_source_exists(self):
        assert (PROJECT_ROOT / SKILL_DIR / SKILL_MD_SOURCE).exists()

    def test_commands_exist(self):
        for c in COMMANDS:
            assert (PROJECT_ROOT / "commands" / c).exists(), c


class TestTrimmedTree:
    """裁剪树 = 内网 skills/dws-pipeline-analyzer/ 的最终形态。"""

    def test_structure(self, tmp_path):
        trimmed = build_trimmed_skill(PROJECT_ROOT, tmp_path / SKILL_DIR)
        refs = sorted(p.name for p in (trimmed / "references").glob("*.py"))
        assert refs == sorted(f"{m}.py" for m in SKILL_MODULES)
        # 未收录模块/模板绝不出现（batch/field_search/impact_analyzer/asset_report.html）
        for excluded in ("batch.py", "field_search.py", "impact_analyzer.py",
                         "templates/asset_report.html", "SKILL.facts-spec.md"):
            assert not (trimmed / excluded).exists(), excluded
        # SKILL.md 是裁剪版（只描述两个开放入口），不是全量版
        skill_md = (trimmed / "SKILL.md").read_text(encoding="utf-8")
        assert "facts/spec 双通道版" in skill_md
        assert "影响分析" not in skill_md
        # 随行资产（v1 schema 已随 asset_facts 投影退役删除；vendor schema = ts 契约进门验）
        for asset in ("run.py", "requirements.txt",
                      "templates/需求SPEC模板.xlsx", "vendor/ts_draft/ts_draft_v2.schema.json"):
            assert (trimmed / asset).exists(), asset


class TestEndToEndSmoke:
    """裁剪树端到端：真实 fixture 上 facts / spec 全链路可跑（懒加载依赖锁）。"""

    def _run(self, trimmed, *args):
        return subprocess.run(
            [sys.executable, str(trimmed / "run.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_facts(self, tmp_path):
        trimmed = build_trimmed_skill(PROJECT_ROOT, tmp_path / SKILL_DIR)
        out = tmp_path / "ts_draft.json"
        r = self._run(trimmed, "facts", "--dir", str(FIXTURE), "--output", str(out))
        assert r.returncode == 0, r.stderr
        assert "ts_draft: " in r.stdout
        assert "self_check: " in r.stdout
        assert out.exists()

    def test_spec(self, tmp_path):
        trimmed = build_trimmed_skill(PROJECT_ROOT, tmp_path / SKILL_DIR)
        out = tmp_path / "spec.xlsx"
        r = self._run(trimmed, "spec", "--dir", str(FIXTURE), "--output", str(out))
        assert r.returncode == 0, r.stderr
        assert "spec: " in r.stdout
        assert out.exists()
