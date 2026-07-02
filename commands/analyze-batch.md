---
description: 批量分析多个规则组，生成全部交付件（资产说明书/字段映射/技术设计文档）
---

# /analyze-batch — 批量分析

对多个规则组批量执行分析，每个规则组生成三个交付件（asset_report.html / mapping.xlsx / tech_design.md）。

## 触发方式

```
/analyze-batch @execution_tasks.xlsx
```

用户也可以自然语言触发：
- "批量分析这些表"
- "把所有规则组都生成报告"

## 执行流程

### Step 1: 执行批量分析脚本

```bash
python {skill_dir}/run.py batch \
    --input {input_xlsx} \
    --output {base_dir} \
    [--batch-size 20] \
    [--no-ai]
```

- `--output` 给基础目录，脚本在其下按规则组英文名建子目录
- `--batch-size` 每批处理数量（默认 20），超出自动分批。默认值从 50 下调：单批内
  解析 AST+knowledge 随组数累积，复杂 SQL（多层 CTE/UNION/窗口函数）单组占内存大，
  50 组易触发单进程内存超限；20 为实测安全值。复杂场景可进一步调小（如 10）
- `--no-ai` 跳过 AI 增强（只生成脚本产物，速度快）
- **stdout 只输出简要信息**：逐组详细日志一律写入 `{base_dir}/batch_logs/batch_*.log`，
  stdout 永远只有批次级进度（完成/成功/失败计数）。这是铁律——逐组内容与规则组数成正比，
  打到 stdout 会在大批量时累积超出上游捕获管道上限，导致整个进程被杀（典型表现：前两批
  正常、第三批起步即被杀）。排查某批/某组细节时，读对应 `batch_*.log` 即可

### Step 2: AI 增强（如未跳过）

对每个规则组的 `knowledge_summary.md`，AI 读取后补充业务理解，保存为 `{output_dir}/{规则组英文名}/knowledge_ai.md`。

AI 增强分批进行（每批 5-10 个规则组），避免一次处理过多。

### Step 3: 告知用户结果

脚本输出每个规则组的处理状态，交付件位置：
```
{base_dir}/
├── {规则组1英文名}/
│   ├── knowledge_draft.json
│   ├── knowledge_summary.md
│   ├── mapping.xlsx
│   ├── asset_report.html
│   └── tech_design.md
├── {规则组2英文名}/
│   └── ...
```

## 关键规则

1. **分批处理**：超过 `--batch-size` 的规则组自动分批，每批连续处理（默认每批 20 组）
2. **输出目录**：每个规则组在 `--output` 下建子目录。目录名通常取规则组英文名；
   若存在英文名相同但编码不同的组（如实时区/离线区同名表），自动追加编码去重为
   `{英文名}__{编码}`，避免写进同一目录互相覆盖导致交付件丢失
3. **AI 增强**：默认启用，`--no-ai` 跳过；AI 分批增强（每批 5-10 个）
4. **进度提示**：stdout 只输出批次级进度（避免大批量时 stdout 累积超限），逐组详细
   状态写入 `{base_dir}/batch_logs/batch_*.log`
5. **此命令依赖** `dws-pipeline-analyzer` skill
