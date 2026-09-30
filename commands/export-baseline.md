---
description: baseline_v1 契约导出（存量资产 → 优化场景机器契约，零 AI 介入）
---

# /export-baseline — baseline_v1 契约导出命令

把存量资产（制品包 xlsx / 代码仓 yml）逆向解析后投影为 **baseline_v1 契约文件**
（纯物理事实包），交给 design-dev-agent 优化场景作为存量现状输入。

## 与 /analyze 的区别（重要）

| | /analyze | /export-baseline |
|---|---|---|
| 产物 | 给人读的交付物（draft + 三视图 + mapping/sdd） | 给机器读的契约（baseline_v1.json） |
| AI 角色 | 必须补充业务理解（knowledge_ai.md） | **零参与**——契约是纯物理事实，任何 AI 补写都是假信息风险 |
| 流程 | 4 步 | 1 步纯脚本 |

本命令与 /analyze 共用同一解析引擎，但**重跑而非复用**旧 draft：解析只要几秒，
换来 provenance（实际消费的源文件清单）与当前源严格一致。契约要求 baseline 反映
线上实跑现状。

## 触发方式

支持与 /analyze 相同的三种输入形态：

```
/export-baseline @execution_tasks.xlsx
/export-baseline path/to/DWB_TRADE_ORDER_D/
/export-baseline DWB_TRADE_MERGE_D        # AI 先在代码仓定位目录
```

## 执行流程（单步，AI 不改写任何产物内容）

```bash
python {skill_dir}/run.py analyzer \
    --input {xlsx文件或规则组目录} \
    --output ./output/ \
    --export baseline_v1 \
    --platform-generation 1.0
```

- `--platform-generation`：平台代次，默认 `1.0`；术加迁移 2.0 时显式传 `2.0`
  （write_plan 的 delete_mode→写入类型映射按代次换版，契约事实字段不变）
- xlsx 场景 DDL 不自动发现，需要时加 `--ddl-dir {DDL目录}`（增强 tables[].fields）

产物：`{output_dir}/baseline_v1.json`。脚本 stdout 单独打印一行
`baseline_v1: {绝对路径}`——**消费方按该行前缀定位产物**，把这行原样转交。

## 失败语义（fail loud，AI 必须走人回路，禁止自行编造）

校验不过时**不落盘**、退出码非 0，stderr 给出违规清单。AI 应把错误原样报告用户，
并按错误类型引导补数，**绝不允许**自行填值后重跑：

| 错误信息特征 | 人回路动作 |
|---|---|
| `delete_mode=6 ... merge_on 为空` | 向用户/平台要该规则的增量合并 ON 配置（删除条件列漏填），补进源 yml/xlsx 后重跑 |
| `query_sql` 相关 schema 违规 | 检查源文件是否被截断/为空 |
| `provenance 为空` | 输入路径形态不对，检查 --input |

## 产物速览（给用户汇报用）

导出成功后向用户展示：

```
baseline_v1 导出完成！

资产：{schema}.{table}（规则组 {rule_group_en}）
规则数：{n} | 表数：{m} | 血缘条目：{k}
写入类型：{各规则 write_plan.kind 汇总，如 full_truncate / merge_upsert}
调度：{有 f/i 任务 | 无（xlsx 输入，显式 null）}
警告：{warnings 条数}

契约文件：{路径}   ← 交给 design-dev-agent 优化场景
```

## 关键规则

1. **一步纯脚本**：不调用 view_generator、不生成 knowledge_ai.md、不改写 baseline_v1.json 任何内容
2. **逐字保真**：query_sql / delete_condition / raw_expr 为解析原文逐字节拷贝，禁止美化
3. **版本**：当前契约版本 1.1（含 write_plan 结构化增量），权威 schema 在
   `dws-pipeline-analyzer/schemas/baseline_v1.schema.json`，口径见 `docs/baseline_v1-契约.md`
4. **失败即停**：校验不过不落盘；merge_on 缺失等问题走人回路，禁止 AI 补值
5. 输出目录约定与 /analyze 相同（`./output/` 基础目录，按规则组英文名建子目录）

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill（与 /analyze 相同的加载方式）。
