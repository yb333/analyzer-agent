---
description: asset_facts 事实包导出（存量资产 → 优化场景机器契约，零 AI 介入）
---

# /export-facts — asset_facts 事实包导出命令

把存量资产（制品包 xlsx / 代码仓 yml）逆向解析后投影为 **asset_facts 事实包**
（纯物理事实包，v1.3 前名 baseline_v1），交给 design-dev-agent 优化场景作为
存量现状输入。

## 与 /analyze 的区别（重要）

| | /analyze | /export-facts |
|---|---|---|
| 产物 | 给人读的交付物（draft + 三视图 + mapping/sdd） | 给机器读的契约（asset_facts.json） |
| AI 角色 | 必须补充业务理解（knowledge_ai.md） | **零参与**——契约是纯物理事实，任何 AI 补写都是假信息风险 |
| 流程 | 4 步 | 1 步纯脚本 |

本命令与 /analyze 共用同一解析引擎，但**重跑而非复用**旧 draft：解析只要几秒，
换来 provenance（实际消费的源文件清单）与当前源严格一致。

## 两种形态

**面向 agent（标准合同，推荐）**——制品目录直入，类型自动识别（excel 按 sheet
指纹、.sql/.ddl 后缀即型），DQ 未检出走两态披露：

```bash
python {skill_dir}/run.py facts --dir {制品目录} --output {asset_facts.json 绝对路径}
```

退出码：0=交卷（stdout 读 `asset_facts: {路径}` 定位行）/ 1=制品问题（stderr
错误清单，上报流程要料）/ 2=参数错误（修参数重调）。完整合同见 docs/14。

**面向人（--input 形态）**——显式指认输入，适合 yml 代码仓资产或人工逐件确认：

```
/export-facts @execution_tasks.xlsx
/export-facts path/to/DWB_TRADE_ORDER_D/
```

```bash
python {skill_dir}/run.py analyzer \
    --input {xlsx文件或规则组目录} \
    --output ./output/ \
    --export asset_facts \
    --platform-generation 1.0
```

- `--platform-generation`：平台代次，默认 `1.0`；术加迁移 2.0 时显式传 `2.0`
- 可选源参数：`--ddl-dir {DDL目录}`（补字段类型/物理属性）、`--lts {LTS excel}`、
  `--dq {DQ excel}`（均优先于代码仓自动发现）

产物：`{output_dir}/asset_facts.json`；stdout 单独打印一行
`asset_facts: {绝对路径}`——消费方按该行前缀定位产物。

## 失败语义（fail loud，AI 必须走人回路，禁止自行编造）

校验不过时**不落盘**、退出码非 0，stderr 给出违规清单。AI 应把错误原样报告用户，
并按错误类型引导补数，**绝不允许**自行填值后重跑：

| 错误信息特征 | 人回路动作 |
|---|---|
| `delete_mode=6 ... merge_on 为空` | 向用户/平台要该规则的增量合并 ON 配置（删除条件列漏填），补进源制品后重跑 |
| `query_sql` 相关 schema 违规 | 检查源文件是否被截断/为空 |
| `缺必需料：目录中未识别出术加制品包` | 确认制品目录里是否有含 RULE+TargetFields sheet 的 excel |
| `多义：...` | 同型制品多个，请用户只保留一个 |

## 关键规则

1. **一步纯脚本**：不调用 view_generator、不生成 knowledge_ai.md、不改写产物任何内容
2. **逐字保真**：query_sql / delete_condition / raw_expr 为解析原文逐字节拷贝，禁止美化
3. **版本**：当前契约版本 1.3（更名 asset_facts + DQ 两态 + 分拣披露），权威 schema 在
   `dws-pipeline-analyzer/schemas/asset_facts.schema.json`，口径见 `docs/asset_facts-契约.md`
4. **失败即停**：校验不过不落盘；缺料走人回路，禁止 AI 补值
5. 输出目录约定与 /analyze 相同（`./output/` 基础目录，按规则组英文名建子目录；facts 入口例外——产物落 --output 精确路径）

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill（与 /analyze 相同的加载方式）。
