---
description: ts 草稿导出（存量资产 → 优化场景机器契约，零 AI 介入）
---

# /export-facts — ts 草稿导出命令

把存量资产制品目录逆向解析后投影为 **ts 草稿**（契约 v2.1，ViewContext JSON 渲染、
对齐消费方正向 ts 形态），交给 design-dev-agent 优化场景作为存量现状输入。
v1 asset_facts 投影已于 2026-11-04 退役（消费方 v1 入料路径同日删除，一波换彻底）。

## 与 /analyze 的区别（重要）

| | /analyze | /export-facts |
|---|---|---|
| 产物 | 给人读的交付物（draft + 三视图 + mapping/sdd） | 给机器读的契约（ts 草稿 json） |
| AI 角色 | 必须补充业务理解（knowledge_ai.md） | **零参与**——契约是纯物理事实，任何 AI 补写都是假信息风险 |
| 流程 | 多步 | 1 步纯脚本 |

## 执行（单步，AI 不改写任何产物内容）

```bash
python {skill_dir}/run.py facts --dir {制品目录} --output {ts_draft.json 绝对路径}
```

- 制品类型自动识别（excel 按 sheet 指纹、.sql/.ddl 后缀即型）；术加制品包必需，
  其余可选；混放非制品文件不阻断（进 warnings）
- 退出码：0=交卷（stdout 读 `ts_draft:` 定位行 + `self_check:` 结论行）/
  1=制品问题（stderr 可行动错误清单，上报流程要料）/ 2=参数错误
- 完整调用合同（含错误特征→行动对照）见 docs/14-消费方对接指南.md

## 失败语义（fail loud，AI 必须走人回路，禁止自行编造）

| 错误信息特征 | 人回路动作 |
|---|---|
| `缺必需料：目录中未识别出术加制品包 excel` | 向用户/平台要该资产的术加制品包 |
| `多义：[a.xlsx, b.xlsx]` | 同型制品多个，请用户只保留一个 |
| `rules/xxx: load_mode=merge_into 但 write_condition 为空` | 向用户/平台要该规则的增量合并 ON 配置，补进源制品后重跑 |

## 关键规则

1. **一步纯脚本**：不调用 view_generator、不生成 knowledge_ai.md、不改写产物任何内容
2. **逐字保真**：query_sql / write_condition / direct 串逐字节拷贝，禁止美化
3. **版本**：当前契约 v2.1（vendor schema 进门验），权威在消费方、本仓 vendor
   （`vendor/ts_draft/`，指纹联锁 test_vendor_sync）；映射矩阵见 docs/16

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill（与 /analyze 相同的加载方式）。
