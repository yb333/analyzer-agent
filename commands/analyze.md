---
description: DWS 制品包分析（分析制品包→生成知识文档+三视图）
---

# /analyze — 制品包分析命令

用户给资产制品，自动分析并生成全部交付物。**一条命令跑到底**：分析 + 三视图一次产出。

## 触发方式

支持多种输入形态，同一个 `/analyze` 命令，脚本自动分流（三形态一个入口）：

### 形态 1：制品目录（推荐）

```
/analyze @制品目录/
/analyze path/to/artifacts/
```

把该资产**全部制品放一个目录**：术加制品包（execution_tasks.xlsx）必需，
LTS 调度 excel / DQ 质量规则 excel / DDL（.sql/.ddl）可选——类型自动识别
（excel 按 sheet 指纹、DDL 按后缀），**用户无需任何附加参数**。

### 形态 2：xlsx 文件（执行平台导出的制品包）

```
/analyze @execution_tasks.xlsx
/analyze path/to/execution_tasks.xlsx
```

单独给术加制品包；LTS/DQ/DDL 需 `--lts/--dq/--ddl-dir` 显式补（可选）。

### 形态 3：代码仓 yml 规则组目录（legacy）

```
/analyze @BFT/BftWideTable/P_TRADE/SUB_TRADE/DWB_TRADE_ORDER_D/
/analyze DWB_TRADE_ORDER_D
```

指向规则组目录即可（表名先让 AI 在代码仓 `BFT/BftWideTable/` 下定位目录）。
工具自动读取目录下所有 `*.yml`（一个 yml = 一条规则）。
> 代码仓结构后续会变、此形态会下线——新能力不依赖它。

## AI 判断规则

| 输入 | AI 判断 | 脚本处理 |
|------|---------|---------|
| 目录（无 `*.yml`） | 制品目录 | 分拣装配（facts/spec 同核），LTS/DQ/DDL/I 视图全自动 |
| 目录（含 `*.yml`） | 代码仓规则组 | `--input 目录/` → read_yml |
| `.xlsx` 文件 | 术加制品包 | `--input xxx.xlsx` → read_excel |
| 表名（非文件非目录） | AI 先找目录 | 找到后同代码仓形态；多个同名让用户选 |

## 执行流程

> **路径约定**：`--output` 固定用 `./output/`（基础目录）。脚本会自动在其下按
> **规则组英文名称**（缺失则回退规则组编码）建子目录。下文 `{output_dir}` 代指
> 该自动创建的子目录，AI 从 Step 1 的输出日志读取其真实路径。
>
> **产物分层**：交付物在 `{output_dir}/` 根；knowledge 过程件在
> `{output_dir}/_internal/`（AI 增强材料都在这里）。

### Step 1: 执行分析脚本（一次全出）

```bash
# 制品目录直入（推荐）
python {skill_dir}/run.py analyze --input {制品目录} --output ./output/

# xlsx / 代码仓目录
python {skill_dir}/run.py analyze --input {input} --output ./output/
```

产出：

```
{output_dir}/
├── mapping.xlsx          — 字段映射（模板 v2：实体级 13 列 + 属性级 15 列，
│                           分组、DDL 元数据、关联方式并入关联条件列）
├── asset_report.html     — 资产说明书（交互式，含血缘图+SQL高亮）
├── tech_design.md        — 技术设计文档（9 章节）
└── _internal/
    ├── knowledge_draft.json   — knowledge 全量（过程件）
    └── knowledge_summary.md   — AI 增强用摘要（过程件）
```

需求 SPEC 单独走 spec 通道：
`python run.py spec --dir {制品目录} --output {路径} [--scenario {场景}]`
（制品目录直入：术加必选 + LTS/DDL 可选；列口径见 docs/17）

### Step 2（可选精修）: AI 补充业务理解后覆盖视图

**契约（渐进式披露）**：AI 从 `{output_dir}/_internal/knowledge_summary.md` 起步
（摘要自足：目标字段清单+DDL注释、每步字段名、WHERE/GROUP BY 原文、加工列
表达式、逻辑块 ON 条件与带出字段）。需要更多细节（某步完整 SQL/某字段血缘/
某表达式）用只读探查入口定向下钻，**不要自己写 python/jq 读大 JSON**：

```bash
python {skill_dir}/run.py inspect --draft {output_dir}/_internal/knowledge_draft.json --step step_1 --part sql
# 选择器: --step X --part sql|blocks|fields|where|joins|ctes | --field 名 | --table 名 | --search 关键字 | --schema
```

AI 基于摘要（+按需 inspect），按以下格式输出自然语言，
保存为 `{output_dir}/_internal/knowledge_ai.md`：

```markdown
# 整体描述
（这张表是什么、干什么、什么粒度，2-3句话）

## step_1
（这步的业务目的和加工逻辑）

## step_2
...

## 关键字段
- 字段名: 业务含义
```

然后重跑 view_generator 覆盖视图（AI 内容注入 html/tech_design）：

```bash
python {skill_dir}/run.py view_generator \
    --input {output_dir}/_internal/knowledge_draft.json \
    --ai-input {output_dir}/_internal/knowledge_ai.md \
    --output {output_dir} \
    --views all
```

### Step 3: 报告结果

向用户展示：

```
分析完成！

目标表：{target_table}
步骤数：{steps} | 字段数：{fields} | 源表数：{sources}
加工模式：{patterns}

已生成：
- mapping.xlsx          — 字段映射（模板 v2，实体级+属性级，CTE穿透）
- asset_report.html     — 资产说明书（交互式，含血缘图+SQL高亮）
- tech_design.md        — 技术设计文档（9 章节）

路径：{output_dir}/
```

## 关键规则

1. **第一步必须执行分析脚本**，不能跳过直接用 AI 分析
2. **一条命令跑到底**：Step 1 已产出全部交付物（含视图）；Step 2 是可选精修
   （弱模型可跳过；要做就按格式写 knowledge_ai.md 后重跑 view_generator）
3. **制品目录形态优先推荐**：用户只需收集制品，不需要挑文件、不需要附加参数
4. **输入自动分流**：目录无 yml → 制品目录分拣；目录有 yml → read_yml；
   .xlsx → read_excel
5. **DDL 发现**：制品目录/yml 场景自动定位；xlsx 单文件场景需 `--ddl-dir` 指定；
   找不到不阻塞（DDL 是可选增强）
6. **输出目录**：`--output` 给基础目录，脚本自动在其下按规则组英文名建子目录；
   交付物在根、knowledge 过程件在 `_internal/`
7. **不要把产出到代码仓内**（污染 git 状态），用 `./output/` 或代码仓外目录

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill。在其他基于 opencode 的 agent 上使用时：
- 将 `.opencode/skills/dws-pipeline-analyzer/` 复制到目标项目
- 将此命令文件复制到 `.opencode/commands/`
- 确保 `run.py` 入口脚本与 `references/` 同级
