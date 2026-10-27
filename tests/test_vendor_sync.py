"""vendor 指纹联锁（ts 草稿契约交付包，权威=消费方 design-dev-agent）。

契约方向与 asset_facts 相反：消费方权威、本仓 vendor（12 章 §11.4）。
本测试逐文件比对 vendor 拷贝与消费方仓权威文件——彼侧变更而本仓未 re-vendor 时 fail
（漂移机器盯，不靠人记）。消费方仓路径不在本机时 skip（内网部署无彼侧源码——
开关式哲学同消费方 test_vendor_sync 对 sql_clean 的处理）。

另含：vendor schema 自身必须是合法 draft-2020-12（进门验可用）、example 与正向样例
过 vendor schema（契约形态锚自洽）。
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PROJECT_ROOT / "dws-pipeline-analyzer" / "vendor" / "ts_draft"
CONSUMER_REPO = Path("/Users/yuanbo/design-dev-agent")

# vendor 文件 → 消费方权威路径（相对消费方仓根）
VENDORED = {
    "ts_draft_v2.schema.json": "skills/opt-pipe/schemas/ts_draft_v2.schema.json",
    "ts_draft_v2.example.json": "skills/opt-pipe/schemas/ts_draft_v2.example.json",
    "ts_sample_forward.json": "skills/opt-pipe/schemas/ts_sample_forward.json",
    "ts-template.json": "skills/dws-design/assets/ts-template.json",
}


def test_vendor_files_present():
    for name in VENDORED:
        assert (VENDOR_DIR / name).exists(), f"vendor 缺文件 {name}（见 VENDOR.md re-vendor 步骤）"
    assert (VENDOR_DIR / "VENDOR.md").exists()


@pytest.mark.skipif(not CONSUMER_REPO.exists(),
                    reason="消费方仓不在本机——联锁跳过（内网形态）")
@pytest.mark.parametrize("name,rel", list(VENDORED.items()))
def test_vendored_matches_consumer_authority(name, rel):
    theirs = CONSUMER_REPO / rel
    if not theirs.exists():
        pytest.skip(f"消费方侧缺 {rel}（权威侧已重构？）——人工核对 re-vendor")
    ours = (VENDOR_DIR / name).read_text(encoding="utf-8")
    assert ours == theirs.read_text(encoding="utf-8"), (
        f"vendor 漂移：{name} 与消费方权威不一致——彼侧契约交付包已变更，"
        f"请 re-vendor（拷贝覆盖 + 更新 VENDOR.md 同步锚）后重跑；形态演进=契约波次")


def test_vendor_schema_is_valid_draft2020():
    from jsonschema import Draft202012Validator
    schema = json.loads((VENDOR_DIR / "ts_draft_v2.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_example_passes_vendor_schema():
    """消费方合格样例过 vendor schema——契约形态锚自洽。"""
    from jsonschema import Draft202012Validator
    schema = json.loads((VENDOR_DIR / "ts_draft_v2.schema.json").read_text(encoding="utf-8"))
    example = json.loads((VENDOR_DIR / "ts_draft_v2.example.json").read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(example))
    assert errors == [], [e.message for e in errors]


def test_forward_sample_shape_anchor():
    """正向样例是全貌对齐靶：核心段存在 + 三桶形态 + tables.fields 三键纯元数据。"""
    fwd = json.loads((VENDOR_DIR / "ts_sample_forward.json").read_text(encoding="utf-8"))
    assert {"meta", "tables", "rules", "data_flow"} <= set(fwd)
    for r in fwd["rules"].values():
        assert set(r["fields"]) == {"processed", "assign", "direct"}
    for t in fwd["tables"].values():
        for f in t["fields"]:
            # 两视图分界：tables.fields 只有三键，加工语义在 rules.fields 三桶
            assert set(f) <= {"target_field", "field_type", "field_comment"}, f
