---
name: dws-pipeline-analyzer
description: |-
  DWS ETL 制品包分析器（facts/spec 双通道版）。把执行平台制品包（xlsx/yml + 可选
  LTS/DDL/DQ）逆向解析后投影为两种产物：asset_facts 事实包（给 agent 读的机器契约）
  与需求SPEC.xlsx（给人读/补的交付件）。

  本仓只开放两个入口：命令入口（/export-facts、/export-spec）与脚本入口
  （run.py facts / run.py spec）。analyzer/view/impact 等其他工作流不在开放范围，
  对应脚本未随本仓分发，请勿尝试调用。

  Use proactively when:
  - 用户提供了 execution_tasks.xlsx / 制品包目录，要出 asset_facts 或需求SPEC
  - agent 编排需要存量资产事实包（优化场景的存量现状输入）
  - 用户说"出SPEC""导出需求规格""事实包""facts"
---

# DWS ETL 制品包分析器（facts / spec 双通道版）

## 依赖检查（首次执行前）

脚本依赖 `openpyxl` 和 `sqlglot`（版本敏感：**必须 28.10.1**，其他版本嵌套子查询
代入链可能断裂）。`run.py` 启动时自检，缺失/版本不符会给出提示：

```bash
python -c "import openpyxl, sqlglot; print('OK')"
pip install -r {skill_dir}/requirements.txt
```

## 入口一：facts（asset_facts 事实包，面向 agent）

制品目录直入，类型自动识别（excel 按 sheet 指纹、.sql/.ddl 后缀即型），
一次调用一次交卷，全程零 AI：

```bash
python {skill_dir}/run.py facts --dir {制品目录} --output {asset_facts.json 绝对路径}
```

- 退出码：**0**=交卷（stdout 读 `asset_facts: {路径}` 与 `self_check: {verdict}`
  定位行）/ **1**=制品问题（stderr 错误清单，走人回路要料）/ **2**=参数错误
- `--format ts_draft` 可选：v2 ts 草稿形态（vendor schema 进门验），默认 asset_facts
- 权威 schema：`schemas/asset_facts.schema.json`（契约版本 1.4）
- 失败即停不落盘；缺料原样上报，**禁止 AI 补值后重跑**
- query_sql / delete_condition / raw_expr 逐字节保真，禁止美化

## 入口二：spec（需求SPEC.xlsx，面向人）

同一套装配/解析链（与 facts 同源），渲染为人向交付件（五 sheet 新模板）：

```bash
python {skill_dir}/run.py spec --dir {制品目录} [--output {路径}] [--scenario {场景}]
```

- 退出码同 facts；stdout 定位行 `spec: {绝对路径}` + `self_check: {verdict}`
- **场景门**：多场景资产 stderr 出 `scenarios: ...`——把清单原样列给用户选，
  带 `--scenario` 重跑，**禁止 agent 替用户选**
- **人工位禁止 AI 代填**：业务背景/使用场景等业务语义列留给人补
- self_check 三态：`pass` 全绿 / `pass_with_noise` 有留空披露（看披露行决定
  是否补料重跑）/ `review` 谓词守恒告警（人工比对 SQL 原文并反馈维护者）
- 模板：`templates/需求SPEC模板.xlsx`（列头指纹校验基准，被改动会 fail loud）

## 两通道的关系

| | facts | spec |
|---|---|---|
| 产物 | asset_facts.json（机器契约） | 需求SPEC.xlsx（人向交付件） |
| 消费方 | agent 编排 | 需求/评审流程中的人 |
| DQ 制品 | 两态披露（未检出进 warnings） | 完全忽略（不读不告警） |

共用同一条「装配 → 解析 → 增强」链（pipeline_core，唯一定义），口径同源。

## 禁止事项

- 修改 facts/spec 产物的任何事实数据（脚本产出，AI 补写=假信息风险）
- 跳过脚本直接用 AI 分析 SQL 出契约/SPEC
- 调用 analyzer / view_generator / batch 等其他脚本入口（未随本仓分发）
