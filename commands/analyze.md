---
description: DWS 制品包分析（分析制品包→生成知识文档+三视图）
---

# /analyze — 制品包分析命令

用户提供执行平台制品包（execution_tasks.xlsx），自动分析并生成全部交付物。

## 触发方式

```
/analyze                              # 分析当前工作目录下的制品包
/analyze @execution_tasks.xlsx        # 指定文件
/analyze path/to/execution_tasks.xlsx # 指定路径
```

AI 也可以在用户拖入 xlsx 或说"分析这个表""帮我看看这个ETL"时自动触发此流程。

## 执行流程（2 步，全自动）

### Step 1: 分析 + AI 增强 + 生成全部视图

```bash
# 1. 自动定位文件（按优先级）
# - 用户指定的路径
# - 当前目录下 *execution_tasks*.xlsx
# - docs/output/*/09_export/execution_tasks.xlsx

# 2. 自动检测 DDL 目录（同级的 04_ddl/）
# - 有则自动传入 --ddl-dir
# - 没有则跳过（字段类型/中文名留空）

# 3. 执行分析脚本
dws-run analyzer analyze \
    --input {input_xlsx} \
    --output {output_dir} \
    [--ddl-dir {ddl_dir}]

# 4. AI 读取 knowledge_draft.json，补充 L4 业务逻辑 + L5 洞察
# 5. 保存为 knowledge_final.json

# 6. 生成全部 3 个视图
dws-run analyzer view_generator \
    --input knowledge_final.json \
    --output {output_dir} \
    --views all
```

### Step 2: 报告结果

向用户展示：

```
分析完成！

目标表：{target_table}
步骤数：{steps} | 字段数：{fields} | 源表数：{sources}
加工模式：{patterns}

已生成：
- mapping.xlsx          — 字段映射（实体级+属性级，CTE穿透）
- asset_report.html     — 资产说明书（交互式，含血缘图+SQL高亮）
- tech_design.md        — 技术设计文档（9 章节）

路径：{output_dir}/analyzer/views/
```

## 关键规则

1. **第一步必须执行分析脚本**，不能跳过直接用 AI 分析
2. **视图全部自动生成**，不询问用户选哪些
3. **DDL 自动检测**，同级 04_ddl/ 有就读，没有就跳过
4. **AI 补充的是业务理解**（L4+L5），不能修改脚本产出的事实数据（L1-L3）
5. **输出目录**与输入文件同级（`{input_dir}/analyzer/`）

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill。在其他基于 opencode 的 agent 上使用时：
- 将 `.opencode/skills/dws-pipeline-analyzer/` 复制到目标项目
- 将此命令文件复制到 `.opencode/commands/`
- 确保已安装 `dws-run`（skill 入口脚本）
