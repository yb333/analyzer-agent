#!/usr/bin/env python3
"""sync_facts_spec — 一键同步本仓 facts/spec 双通道能力到内部仓的 .opencode/

只同步两个入口（命令 /export-facts、/export-spec + 脚本 run.py facts/spec）所需的
最终执行文件，不含 docs/tests/telemetry 等开发侧内容；SKILL.md 用裁剪版
（SKILL.facts-spec.md，只描述两个开放入口的合同）。

源 = 本仓本地 <SRC_BRANCH> 分支当前内容（用户自己 git pull，工具不 fetch 远端）。
内部仓一切本地状态不保留（工作区改动/本地提交/失败遗留一律 reset 对齐远端）。

结构对齐 design-dev-agent 仓的 sync_to_team.py（同一目标仓、同一套纪律）：
  dws-pipeline-analyzer/（27 文件裁剪集）
      → .opencode/skills/dws-pipeline-analyzer/   逐文件镜像（含删除）
  commands/export-facts.md、export-spec.md
      → .opencode/commands/                       同名覆盖（不删别人的）

与 sync_to_team.py 的差异（我们更简单）：
  - 无 config 快照保护——skill 无 example→真实 config 对，无内网侧可变状态
  - 单仓单源——无双源（--config-analyzer）逻辑
  - 多一层 include-list 裁剪——skill 目录整体上会带 analyze/impact 等未开放
    工作流（对应脚本与模板不上内网）；清单由 tests/test_sync_scope.py 用真实
    fixture 锁住，防止新增模块后清单漂移

与 design-dev-agent 的 sync_to_team 通道互不干扰：它的 --config-analyzer 是
opt-in，不配置就只同步它自己的 skills/agents/commands，与我们的路径零重叠；
两边先后跑、各提各的 commit（mirror 删除只删 git 已跟踪且不在我们源里的文件，
别人放的未跟踪文件绝不碰）。

入口：sync_facts_spec.sh（开发环境测试）/ sync_facts_spec.bat（内网 Windows
实际运行），两者都是透传参数的薄壳。

用法：
  sync_facts_spec.sh                                # 同步（用已存配置）
  sync_facts_spec.sh /path/to/internal/repo         # 指定内部仓路径，本次生效
  sync_facts_spec.sh --config /path/to/repo         # 保存配置（含其他已生效选项）后退出
  sync_facts_spec.sh --src-branch main --team-branch 8.12   # 分支覆盖，本次生效
  sync_facts_spec.sh --switch 8.12                  # 内网仓切分支并记住配置（只切换，不同步）

配置 ~/.dws-facts-spec-sync.conf（优先级：CLI 参数 > 配置文件 > 默认值）：
  TEAM_REPO=/path/to/internal/repo   # 内部仓本地克隆路径（必填）
  SRC_BRANCH=main                    # 源仓分支（读本地该分支当前内容）
  TEAM_BRANCH=                       # 内部仓分支校验，空=用当前 checkout 分支
"""

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

CONFIG_NAME = ".dws-facts-spec-sync.conf"
SKILL_DIR = "dws-pipeline-analyzer"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_FILES = {".DS_Store"}
EXCLUDE_SUFFIX = (".pyc",)

# references/ 模块裁剪清单（17/21）：facts/spec 双通道运行时闭包。
# 注意 view_generator/usage 必须随行——engine.py 在解析热路径上运行时懒加载
# view_generator._describe_transform，模块级 import 图看不出来（2026-11-03
# case_32 双树冒烟实测）。tests/test_sync_scope.py 用真实 fixture 锁此清单。
SKILL_MODULES = [
    "analyzer", "baseline_export", "crosscheck", "engine", "excel_utils",
    "facts", "inputs", "pipeline_core", "schedule_match", "schedule_recovery",
    "spec", "spec_export", "spec_extract", "ts_export", "viewmodel",
    "view_generator", "usage",
]
# references/ 之外的随行资产（相对 skill 根；目录=整目录收录）
SKILL_ASSETS = [
    "run.py",
    "requirements.txt",                       # 依赖契约（sqlglot==28.10.1 版本锁声明）
    "schemas/asset_facts.schema.json",        # facts 权威 schema
    "templates/需求SPEC模板.xlsx",             # spec 渲染模板（列头指纹校验基准）
    "vendor/ts_draft",                        # facts --format ts_draft 进门验 schema + 对齐靶
]
# 内网 SKILL.md = 源仓裁剪版（只描述两个开放入口）；全量版会宣传未分发的工作流
SKILL_MD_SOURCE = "SKILL.facts-spec.md"
COMMANDS = ["export-facts.md", "export-spec.md"]


def fail(msg: str):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def run_git(args, cwd=None, capture=False):
    kwargs = {"cwd": str(cwd)} if cwd else {}
    if capture:
        kwargs.update(capture_output=True, text=True, encoding="utf-8", errors="replace")
    return subprocess.run(["git"] + args, **kwargs)


def load_config(path: Path) -> dict:
    cfg = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def save_config(path: Path, cfg: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in cfg.items()), encoding="utf-8"
    )


def excluded(rel: Path) -> bool:
    return (
        any(part in EXCLUDE_DIRS for part in rel.parts)
        or rel.name in EXCLUDE_FILES
        or rel.suffix in EXCLUDE_SUFFIX
    )


def mirror_dir(src: Path, dst: Path, tracked: set, repo_prefix: str):
    """镜像 src → dst：复制新增/变更，删除 dst 中 src 没有的。

    只删除 git 已跟踪的文件（tracked 含 repo_prefix 前缀的仓库相对路径）——
    别人放在目录里的未跟踪文件绝不碰。
    """
    dst.mkdir(parents=True, exist_ok=True)
    src_files = set()
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if excluded(rel) or not p.is_file():
            continue
        src_files.add(rel)
        target = dst / rel
        if not target.exists() or target.read_bytes() != p.read_bytes():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    for p in sorted(dst.rglob("*"), reverse=True):
        rel = p.relative_to(dst)
        if excluded(rel):
            continue
        if p.is_file():
            if rel not in src_files and f"{repo_prefix}/{rel.as_posix()}" in tracked:
                p.unlink()
        elif not any(p.iterdir()):
            p.rmdir()


def copy_file(src: Path, dst: Path):
    """单文件覆盖式拷贝。"""
    if not src.exists():
        fail(f"源文件不存在: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        shutil.copy2(src, dst)


def build_trimmed_skill(export_dir: Path, trimmed: Path):
    """git archive 导出树 → facts/spec 裁剪集（SKILL.md 换裁剪版）。

    trimmed 即内网 skills/dws-pipeline-analyzer/ 的最终形态：
    17 个 references 模块 + 5 项资产 + 裁剪版 SKILL.md。
    """
    skill_src = export_dir / SKILL_DIR
    (trimmed / "references").mkdir(parents=True)
    for mod in SKILL_MODULES:
        src = skill_src / "references" / f"{mod}.py"
        if not src.exists():
            fail(f"裁剪清单引用了不存在的模块: {src}（清单与仓库漂移，先修 sync_facts_spec.py）")
        shutil.copy2(src, trimmed / "references" / f"{mod}.py")
    for asset in SKILL_ASSETS:
        src = skill_src / asset
        if not src.exists():
            fail(f"裁剪清单引用了不存在的资产: {src}（清单与仓库漂移，先修 sync_facts_spec.py）")
        dst = trimmed / asset
        if src.is_dir():
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns(*EXCLUDE_DIRS, *EXCLUDE_FILES))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    # 内网 SKILL.md = 裁剪版（只描述 facts/spec 两个开放入口）
    copy_file(skill_src / SKILL_MD_SOURCE, trimmed / "SKILL.md")
    return trimmed


def switch_team_branch(team_repo: Path, branch: str):
    """内部仓切到 branch（单分支克隆免疫，依据 FETCH_HEAD——同 sync_to_team）。"""
    r = run_git(["fetch", "origin", branch], cwd=team_repo, capture=True)
    remote_ok = r.returncode == 0
    if not remote_ok:
        err_lines = [l for l in (r.stderr or "").splitlines() if l.strip()]
        print(f"  [WARN] fetch origin {branch} 失败: "
              f"{err_lines[-1][:120] if err_lines else '无输出'}（本地已有该分支时仍可切换）")
    r = run_git(["checkout", "-f", branch], cwd=team_repo)
    if r.returncode != 0 and remote_ok:
        r = run_git(["checkout", "-b", branch, "FETCH_HEAD"], cwd=team_repo)
    if r.returncode != 0:
        if remote_ok:
            fail(f"切换到 {branch} 失败：本地建分支出错（fetch 已成功，可手动 "
                 f"git checkout -b {branch} FETCH_HEAD 看真实报错）")
        fail(f"切换到 {branch} 失败：本地没有这个分支，远端也取不到"
             f"（fetch 失败原因见上方 WARN；确认内部远端已建 {branch} 且网络可用）")


def run_menu() -> list | None:
    """交互菜单：返回要执行的参数列表（[] = 直接同步），None = 退出。"""
    print()
    print("═" * 46)
    print("  sync_facts_spec — 请选择要做什么")
    print("═" * 46)
    print("  [1] 同步（facts/spec 能力 → 内网仓提交推送）")
    print("  [2] 只拉别人的提交（对齐远端，不做我们的同步）")
    print("  [3] 切换内网仓分支（只切换不同步，切完回菜单）")
    print("  [4] 配置内部仓路径")
    print("  [0] 退出")
    print("─" * 46)
    try:
        choice = input("  选择: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice == "1":
        return []
    if choice == "2":
        return ["--pull"]
    if choice == "3":
        try:
            b = input("  切到哪个分支（如 master / 8.12）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return ["--switch", b] if b else None
    if choice == "4":
        try:
            repo = input("  内部仓本地克隆路径: ").strip()
            branch = input("  内部仓分支校验（直接回车=不校验）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not repo:
            return None
        args = ["--config", repo]
        if branch:
            args += ["--team-branch", branch]
        return args
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步本仓 facts/spec 能力到内部仓 .opencode/")
    parser.add_argument("team_repo", nargs="?", help="内部仓路径（本次生效）")
    parser.add_argument("--config", metavar="TEAM_REPO", help="保存配置（含其他已生效选项）后退出")
    parser.add_argument("--src-branch", help="源仓分支（默认 main）")
    parser.add_argument("--team-branch", help="内部仓分支校验（空=用当前 checkout 分支）")
    parser.add_argument("--switch", metavar="BRANCH",
                        help="内部仓切到指定分支并更新配置（只切换：不同步不提交）")
    parser.add_argument("--pull", action="store_true",
                        help="只拉别人的提交：内部仓对齐远端，不 mirror 不提交不推送")
    parser.add_argument("--menu", action="store_true", help="交互菜单（bat 双击默认入口）")
    return parser


def managed_paths() -> list:
    """我们产出条目的精确清单——同步/提交只动这些路径，别人的内容全程不碰。"""
    return (
        [f".opencode/skills/{SKILL_DIR}"]
        + [f".opencode/commands/{c}" for c in COMMANDS]
    )


def execute_action(args, config_path: Path | None = None) -> int:
    """执行单个动作（config 保存 / 切分支 / 拉取 / 同步），完成即返回。"""
    src_repo = Path(__file__).resolve().parent
    if config_path is None:
        config_path = Path.home() / CONFIG_NAME
    cfg = load_config(config_path)

    team_repo = args.team_repo or cfg.get("TEAM_REPO", "")
    src_branch = args.src_branch or cfg.get("SRC_BRANCH", "") or "main"
    team_branch = args.team_branch or cfg.get("TEAM_BRANCH", "")

    if args.config:
        team_repo = args.config or team_repo
        save_config(config_path, {
            "TEAM_REPO": team_repo,
            "SRC_BRANCH": src_branch,
            "TEAM_BRANCH": team_branch,
        })
        print(f"[OK] 已保存配置: 内部仓={team_repo}（源分支: {src_branch}，内部仓分支: {team_branch or '不校验'}）")
        return 0

    if not team_repo:
        fail("未指定内部仓路径（用法: sync_facts_spec.sh --config /path/to/internal/repo）")
    team_repo = Path(team_repo).expanduser().resolve()
    if not (team_repo / ".git").exists():
        fail(f"不是 git 仓库: {team_repo}")

    if args.switch:
        branch = args.switch
        print(f"[切分支] 内部仓 → {branch}")
        switch_team_branch(team_repo, branch)
        team_branch = branch
        save_config(config_path, {
            "TEAM_REPO": str(team_repo),
            "SRC_BRANCH": src_branch,
            "TEAM_BRANCH": team_branch,
        })
        print(f"[OK] 已切换到 {branch} 并更新配置（只切换：不提交不推送；要同步回菜单选 [1] 或重跑一次）")
        return 0

    if args.pull:
        # 只拉别人的提交：fetch + 本地对齐远端，不 mirror / 不提交 / 不推送。
        print("[拉取] 内部仓对齐远端（只拉别人的提交，不做同步）...")
        cleanup_git_states(team_repo)
        r = run_git(["symbolic-ref", "--short", "HEAD"], cwd=team_repo, capture=True)
        cur = r.stdout.strip()
        if not cur:
            fail("内部仓处于 detached HEAD，请先 git checkout <分支>")
        if team_branch and cur != team_branch:
            fail(f"内部仓当前分支是 {cur}，配置要求 {team_branch}（或用 --switch 切换）")
        old_head = run_git(["rev-parse", "--short", "HEAD"], cwd=team_repo,
                           capture=True).stdout.strip()
        if run_git(["fetch", "origin", cur], cwd=team_repo).returncode != 0:
            fail("fetch 内部远端失败，请检查网络")
        r = run_git(["log", "HEAD..FETCH_HEAD", "--oneline"], cwd=team_repo, capture=True)
        incoming = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if run_git(["reset", "--hard", "FETCH_HEAD"], cwd=team_repo).returncode != 0:
            fail("reset 到远端最新失败")
        if incoming:
            print(f"  拉到 {len(incoming)} 个新提交:")
            for l in incoming[:20]:
                print(f"    {l}")
            if len(incoming) > 20:
                print(f"    ...（共 {len(incoming)} 个）")
        else:
            print("  远端无新提交，本地已是最新")
        print(f"[OK] 已对齐远端（{old_head} → "
              f"{run_git(['rev-parse', '--short', 'HEAD'], cwd=team_repo, capture=True).stdout.strip()}），未产生任何本地提交")
        return 0

    print("=" * 60)
    print("  同步 facts/spec 能力 → 内部仓 .opencode/")
    print("=" * 60)
    print(f"源:  {src_repo} (本地分支 {src_branch})")
    print(f"目标: {team_repo}/.opencode/")
    print()

    with tempfile.TemporaryDirectory(prefix="dws-facts-spec-sync-") as tmp:
        return do_sync(src_repo, Path(tmp), team_repo, src_branch, team_branch)


def cleanup_git_states(team_repo: Path):
    """上次失败遗留的 rebase/merge 冲突现场：自动放弃回退（同 sync_to_team）。"""
    def git_state_present(name: str) -> bool:
        r = run_git(["rev-parse", "-q", "--verify", name], cwd=team_repo, capture=True)
        if r.returncode == 0 and r.stdout.strip():
            return True
        r = run_git(["rev-parse", "--git-path", name], cwd=team_repo, capture=True)
        return (r.returncode == 0 and bool(r.stdout.strip())
                and (team_repo / r.stdout.strip()).exists())

    for state, abort_cmd in (
        ("rebase-merge", ["rebase", "--abort"]),
        ("rebase-apply", ["rebase", "--abort"]),
        ("MERGE_HEAD", ["merge", "--abort"]),
    ):
        if git_state_present(state):
            print(f"  检测到未完成的 {abort_cmd[0]} 现场（上次冲突遗留），自动放弃回退")
            if run_git(abort_cmd, cwd=team_repo).returncode != 0:
                fail(f"放弃 {abort_cmd[0]} 现场失败，请人工处理: git {' '.join(abort_cmd)}")


def do_sync(src_repo: Path, tmp: Path, team_repo: Path, src_branch: str, team_branch: str) -> int:
    # ── Step 1: 读源仓本地分支当前内容（用户自己 pull，工具不 fetch 远端）──
    print(f"[Step 1] 读取源仓本地分支 ({src_branch})...")
    r = run_git(["rev-parse", "--short", src_branch], cwd=src_repo, capture=True)
    if r.returncode != 0 or not r.stdout.strip():
        fail(f"源仓本地分支不存在: {src_branch}（请先 git pull）")
    src_hash = r.stdout.strip()
    r = run_git(["log", "-1", "--format=%s", src_branch], cwd=src_repo, capture=True)
    src_subject = r.stdout.strip()
    print(f"  源提交: {src_hash} {src_subject}")

    export_dir = tmp / "src"
    export_dir.mkdir()
    r = subprocess.run(
        ["git", "-C", str(src_repo), "archive", src_branch, "--",
         SKILL_DIR] + [f"commands/{c}" for c in COMMANDS],
        capture_output=True,
    )
    if r.returncode != 0:
        fail(f"git archive 失败: {r.stderr.decode('utf-8', errors='replace')[:200]}")
    with tarfile.open(fileobj=BytesIO(r.stdout)) as tf:
        tf.extractall(export_dir)

    # ── 裁剪：facts/spec 27 文件集（模块+资产+裁剪版 SKILL.md）──
    trimmed = build_trimmed_skill(export_dir, tmp / "trimmed" / SKILL_DIR)
    n_files = sum(1 for p in trimmed.rglob("*") if p.is_file())
    print(f"  裁剪集: {len(SKILL_MODULES)} 模块 + 资产，共 {n_files} 文件（SKILL.md = 裁剪版）")
    print()

    managed = managed_paths()
    skill_target = f".opencode/skills/{SKILL_DIR}"

    # ── Step 2: 校验内部仓状态（现场回退 + 分支）──
    print("[Step 2] 校验内部仓...")
    cleanup_git_states(team_repo)
    r = run_git(["symbolic-ref", "--short", "HEAD"], cwd=team_repo, capture=True)
    cur_branch = r.stdout.strip()
    if not cur_branch:
        fail("内部仓处于 detached HEAD（HEAD 指在提交上、不在任何分支上）\n"
             "  处理: 在内部仓运行 git checkout <分支> 回到分支后重跑")
    if team_branch and cur_branch != team_branch:
        fail(f"内部仓当前分支是 {cur_branch}，配置要求 {team_branch}（用 --switch 切换，或手动 checkout）")

    oc = team_repo / ".opencode"
    (oc / "skills").mkdir(parents=True, exist_ok=True)
    (oc / "commands").mkdir(parents=True, exist_ok=True)

    # 同步主体可整体重试：push 被拒（远端在窗口期又进了新提交）就整个重来一轮。
    done = False
    for attempt in range(1, 4):
        if attempt > 1:
            print(f"[重试 {attempt}/3] push 被拒，重新对齐远端再来一轮...")
            print("  丢弃本轮生成的 sync 提交（可再生成）")

        # fetch 先行；对齐目标用 FETCH_HEAD（单分支克隆免疫，同 sync_to_team）
        if run_git(["fetch", "origin", cur_branch], cwd=team_repo).returncode != 0:
            fail("fetch 内部远端失败，请检查网络")

        if attempt == 1:
            r = run_git(["status", "--porcelain", "-uno"], cwd=team_repo, capture=True)
            dirty = [l for l in r.stdout.splitlines() if l.strip()]
            r = run_git(["rev-list", "--count", "FETCH_HEAD..HEAD"],
                        cwd=team_repo, capture=True)
            ahead = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
            if ahead > 0:
                r = run_git(["log", "FETCH_HEAD..HEAD", "--format=%s"],
                            cwd=team_repo, capture=True)
                print(f"  本地有 {ahead} 个领先提交（本地状态不保留），对齐远端:")
                for s in [l.strip() for l in r.stdout.splitlines() if l.strip()]:
                    print(f"    {s}")
            if dirty:
                print(f"  本地有 {len(dirty)} 个工作区改动（本地状态不保留），对齐远端:")
                for l in dirty[:10]:
                    print(f"    {l}")
                if len(dirty) > 10:
                    print(f"    ...（共 {len(dirty)} 个）")

        # 远端新增文件撞本地未跟踪文件的先清掉（以远端为准，不清会挡住 reset）
        r = run_git(["diff", "--name-only", "--diff-filter=A", "HEAD", "FETCH_HEAD"],
                    cwd=team_repo, capture=True)
        added = {l.strip().strip('"') for l in r.stdout.splitlines() if l.strip()}
        if added:
            r = run_git(["ls-files", "--others", "--exclude-standard"],
                        cwd=team_repo, capture=True)
            clash = added & {l.strip().strip('"') for l in r.stdout.splitlines() if l.strip()}
            for f in sorted(clash):
                (team_repo / f).unlink(missing_ok=True)
        if run_git(["reset", "--hard", "FETCH_HEAD"], cwd=team_repo).returncode != 0:
            fail("reset 到远端最新（FETCH_HEAD）失败")

        # ── mirror 源头内容到我们的条目 ──
        r = run_git(["ls-files", "--"] + managed, cwd=team_repo, capture=True)
        tracked = set(r.stdout.splitlines())
        mirror_dir(trimmed, oc / "skills" / SKILL_DIR,
                   tracked=tracked, repo_prefix=skill_target)
        for c in COMMANDS:
            copy_file(export_dir / "commands" / c, oc / "commands" / c)

        # ── 限定路径单 commit ──
        if run_git(["add", "-A", "--"] + managed, cwd=team_repo).returncode != 0:
            fail("git add 失败")
        if run_git(["diff", "--cached", "--quiet"], cwd=team_repo).returncode == 0:
            print("  无变更，内部仓内容已是最新。")
            done = True
            break
        r = run_git(["diff", "--cached", "--name-status"], cwd=team_repo, capture=True)
        changed = [l for l in r.stdout.splitlines() if l.strip()]
        print(f"  提交（{len(changed)} 个文件变更）...")
        for line in changed[:20]:
            print(f"  {line}")
        if len(changed) > 20:
            print(f"  ...（共 {len(changed)} 个）")
        # 提交信息 = 实际推送内容对应的源仓提交：对每个变更文件反查源仓上
        # "最后改动它的提交"，去重。不依赖任何基线/内网历史——推了什么，
        # 名字就说什么。（目标 SKILL.md 反查源仓 SKILL.facts-spec.md）
        entries = []
        for l in changed:
            p = l.split(maxsplit=2)[-1].strip('"')
            src_path = p[len(".opencode/skills/"):] if p.startswith(".opencode/skills/") else p
            if src_path == f"{SKILL_DIR}/SKILL.md":
                src_path = f"{SKILL_DIR}/{SKILL_MD_SOURCE}"
            rr = run_git(["log", "-1", "--format=%h %s", src_branch, "--", src_path],
                         cwd=src_repo, capture=True)
            if rr.returncode == 0 and rr.stdout.strip():
                entries.append(rr.stdout.strip())
        entries = list(dict.fromkeys(entries))  # 去重保序
        tag = f"dws-analyzer@{src_hash}"
        if len(entries) >= 2:
            commit_args = ["-m", f"sync: {tag} facts/spec 能力更新 {len(entries)} 项",
                           "-m", "\n".join(f"* {e}" for e in entries)]
            print(f"  信息: 能力更新 {len(entries)} 项（正文含逐条清单）")
        elif len(entries) == 1:
            subject = entries[0].split(" ", 1)[-1]
            commit_args = ["-m", f"sync: {tag} {subject}"]
        else:
            commit_args = ["-m", f"sync: {tag} facts/spec 更新"]
        if run_git(["commit"] + commit_args, cwd=team_repo).returncode != 0:
            fail("commit 失败")

        # ── push（显式 refspec 不依赖 upstream；只有竞争性被拒才重试）──
        print("  推送到内部远端...")
        r = run_git(["push", "origin", f"HEAD:{cur_branch}"],
                    cwd=team_repo, capture=True)
        if r.returncode == 0:
            done = True
        else:
            err = ((r.stderr or "") + (r.stdout or "")).strip()
            competitive = any(k in err for k in ("fetch first", "non-fast-forward", "stale info"))
            if not competitive:
                print(err)
                fail("push 失败（非竞争原因）——见上方 git 原始输出；"
                     "常见：分支受保护（找管理员开直推权限）、push 凭据/权限问题")
            print(err.splitlines()[-1] if err else "  push 被拒")
    if not done:
        fail("push 连续 3 轮竞争被拒（远端推进太快）——稍后直接重跑即可自愈；"
             "持续失败请在内部仓手动 git push 看真实报错")

    print()
    print("=" * 60)
    print(f"  ✅ 同步完成: dws-analyzer@{src_hash} → {cur_branch}")
    print("=" * 60)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.dont_write_bytecode = True

    parser = build_parser()
    args = parser.parse_args()
    if args.menu:
        # 动作完成回菜单（每轮重读配置）；出错 fail() 直接退出——bat 末尾有 pause
        while True:
            extra = run_menu()
            if extra is None:
                return 0
            execute_action(parser.parse_args(extra))
    return execute_action(args)


if __name__ == "__main__":
    sys.exit(main())
