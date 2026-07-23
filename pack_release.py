#!/usr/bin/env python3
"""打包用户发布版 zip。

只含成品（运行必需 + 用户文档），不含开发过程文件。
跑完生成 release/analyzer-agent-release.zip，拿去传内网。

用法:
    python3 pack_release.py [--version v1.0]

排除的开发文件：
  tests/          测试代码
  docs/           设计草稿
  architecture.md 架构设计（内部）
  pack_release.py 打包脚本本身
  .git/ .gitignore .pytest_cache __pycache__ .DS_Store
  交叉矩阵文档等
"""

import os
import sys
import zipfile
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# 用户需要的成品文件/目录（相对于 REPO_ROOT）
# 注意：telemetry-server（运营埋点服务端）不随发布包分发，本地部署即可。
#       客户端 usage.py 在 dws-pipeline-analyzer/references/ 下，随核心包进去。
INCLUDE = [
    "dws-pipeline-analyzer",   # 核心：engine/analyzer/view_generator/impact_analyzer/usage 等
    "commands",                # 命令定义（/analyze 等）
    "README.md",               # 项目说明
    "install.sh",              # macOS/Linux 安装
    "install.bat",             # Windows 安装
    "install.py",              # 安装逻辑
    "sample_rule.yml",         # yml 样例
]

# 明确排除的（即使 INCLUDE 里的目录下也会被过滤掉）
EXCLUDE_PATTERNS = [
    "__pycache__", "*.pyc", ".pytest_cache", ".DS_Store",
    ".git", ".gitignore",
    # 开发文档不给用户（user-guide 给）
    "architecture.md", "docs",
    # 打包脚本本身
    "pack_release.py",
    # telemetry-server 的开发产物（用户自行 npm install）
    "node_modules", "package-lock.json",
    "data",  # SQLite 数据库 + WAL（运行时产物，不打进包）
    # SKILL.md 是给 AI agent 读的元数据，用户不需要直接看
    # 但 install 脚本依赖它存在，所以保留——实际会随 dws-pipeline-analyzer/ 一起进去
]


def should_exclude(path: Path) -> bool:
    """检查路径是否该排除"""
    name = path.name
    parts = path.parts
    for pat in EXCLUDE_PATTERNS:
        if "*" in pat:
            from fnmatch import fnmatch
            if fnmatch(name, pat):
                return True
        else:
            if name == pat or pat in parts:
                return True
    return False


def main():
    version = "v1.0"
    if "--version" in sys.argv:
        idx = sys.argv.index("--version")
        if idx + 1 < len(sys.argv):
            version = sys.argv[idx + 1]

    release_dir = REPO_ROOT / "release"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir()

    zip_name = f"analyzer-agent-{version}.zip"
    zip_path = release_dir / zip_name
    top_dir = f"analyzer-agent"  # 解压后的顶层目录名

    file_count = 0
    total_size = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            src = REPO_ROOT / item
            if not src.exists():
                print(f"[WARN] 不存在，跳过: {item}")
                continue

            if src.is_file():
                if should_exclude(src):
                    continue
                arc = f"{top_dir}/{item}"
                zf.write(src, arc)
                file_count += 1
                total_size += src.stat().st_size
                print(f"  + {item}")
            else:
                # 目录：递归
                for root, dirs, files in os.walk(src):
                    # 过滤目录
                    dirs[:] = [d for d in dirs if not should_exclude(Path(root) / d)]
                    for f in files:
                        fp = Path(root) / f
                        if should_exclude(fp):
                            continue
                        rel = fp.relative_to(REPO_ROOT)
                        arc = f"{top_dir}/{rel}"
                        zf.write(fp, arc)
                        file_count += 1
                        total_size += fp.stat().st_size
                print(f"  + {item}/ (目录)")

    zip_size = zip_path.stat().st_size
    print(f"\n[OK] 打包完成: {zip_path}")
    print(f"  文件数: {file_count}")
    print(f"  原始大小: {total_size / 1024:.0f} KB")
    print(f"  压缩后: {zip_size / 1024:.0f} KB")
    print(f"\n解压后顶层目录: {top_dir}/")
    print(f"传内网: {zip_path}")


if __name__ == "__main__":
    main()
