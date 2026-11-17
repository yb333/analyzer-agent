---
description: 多规则组链路分析（多个资产的制品目录 → 自动识别依赖 → 全链路一张图）
---

# /analyze-chain — 多规则组链路分析命令

一个资产可能由多个规则组（多个资产目录）协作加工完成（mid→最终F）。本命令把
**一组资产制品目录**作为输入，自动识别组间数据依赖，闭环构建完整链路并一次分析。

## 触发方式

```
/analyze-chain @资产集合目录/          # 推荐：子目录 = 各资产制品目录
/analyze-chain @单个资产制品目录/      # 1 组链（等价单资产，出链路版报告）
/analyze-chain DWB_TRADE_ORDER_F --repo-root 代码仓根目录   # legacy：yml 追溯
"分析这几个资产的完整加工链路" / "把这几个目录串成全链分析"
```

**资产集合目录**（推荐，与 `/analyze` 形态 1 同款制品范式）：

```
资产集合目录/
├── DWB_TRADE_MID/        ← 每个子目录 = 一个资产的制品目录
│   ├── execution_tasks.xlsx         （术加制品包，必需）
│   ├── lts.xlsx / dq.xlsx / DDL     （可选，自动识别）
├── DWB_DETAIL_MID/
└── DWB_TRADE_ORDER_F/
```

### AI 判断规则

| 输入 | AI 判断 | 处理 |
|------|---------|------|
| 目录（子目录含制品包/yml） | 资产集合目录 | 闭环建图：集合内互相连边，自动识别终点 |
| 目录（直接含 execution_tasks.xlsx） | 单资产制品目录 | 1 组链（只要单资产三件套时用 `/analyze`） |
| 最终F表 yml 规则组目录 / 表名 | legacy 代码仓形态 | `--repo-root` 向外追溯（结构后续会变，不投入） |
| 单个 xlsx 文件 | 形态错误 | 提示改用 `/analyze` |

## 执行流程

> **⚠ 直接执行脚本，不要先读内容。** 脚本自动完成装配→建图→合并→分析→报告。

### Step 1: 执行链路分析

```bash
python {skill_dir}/run.py analyze-chain \
    --input {资产集合目录} \
    --output ./output/ \
    [--ddl-dir {DDL目录}]        # 可选，叠加在各组自带 DDL 之上
```

脚本自动完成：
1. 逐子目录装配（制品目录全自动识别 LTS/DQ/DDL；单组装配失败披露后继续，不中断）
2. **闭环建图**：`谁写了这张表` 的写入者索引只来自你给的组——组间数据依赖自动连边，
   不向外追代码仓；写的表无人消费的组 = **终点**（F 层，多终点=森林一张全图）
3. 合并所有组（exec_sequence 按依赖拓扑重编号；跨组 rule_code 冲突自动按组前缀重键）
4. 作为一个整体跑 analyze_pipeline + 生成链路版三件套

### Step 2: 报告结果

```
多规则组链路分析完成！

规则组数：{N}（终点：{终点组名}）
步骤数：{N}
字段数：{N}
目标表：{target_table}

链路（终点在前，缩进=距终点跳数）：
  DWB_TRADE_ORDER_F → 写 dwb_trade_order_f [终点]
      DWB_TRADE_MID_F → 写 dwb_trade_mid_f
      DWB_DETAIL_MID_F → 写 dwb_detail_mid_f

外部源表（集合内无写入者，不追溯）：ods.order_src, ods.detail_src

报告：./output/chain_{集合目录名}/
  - asset_report.html    资产说明书（含多规则组链路标识）
  - mapping.xlsx         字段映射（完整链路，穿透mid表）
  - tech_design.md       技术设计文档
```

## 建图规则

- **串联靠数据依赖**：不靠表名尾缀，靠"谁写了这张表"（与 legacy 追溯同口径）
- **闭环**：依赖只在输入集合内连边；找不到写入者的源表 = 外部源表（多为 ods），列出但不追溯
- **终点**：写的表没被集合内其他组读 = 终点（F 层）；多终点 = 森林，仍出一张全图报告
  （此时调度/DQ/I 视图不挂载——各组按单资产 `/analyze` 查看）
- **环**：A读B写、B读A写 → 检出并披露，拓扑容忍处理，不中断
- **调度/DQ/I 视图**：单终点时取终点组的 LTS/DQ/资产视图（资产视角）；各组自带 DDL
  全部合并参与字段元数据增强
- **I 视图**：制品目录形态由终点组 DDL 自动识别；legacy 形态只对最终 F 表触发

## 与 /analyze 的边界（命令区分）

| | /analyze | /analyze-chain |
|---|---|---|
| 输入 | 单资产制品目录 / 单 xlsx | 资产集合目录 / 单资产目录 / legacy 表名 |
| 语义 | 一个资产的三件套 | 多组依赖连边后的全链路一张图 |
| 判定 | 目录直接含 xlsx → 单资产 | 目录的子目录含制品 → 集合 |

## 关键规则

1. **给目录不要给表名**（制品形态）；表名+`--repo-root` 是 legacy 入口
2. **直接执行脚本**，AI 不预读内容、不自己判断组间依赖（脚本闭环建图）
3. **exec_sequence 重编号**：合并后按拓扑排序（上游在前），保证步骤间依赖正确
4. **单 xlsx 文件会被拒绝**（exit 2）——那是 /analyze 的形态 2

## SKILL 加载

此命令依赖 `dws-pipeline-analyzer` skill。在其他基于 opencode 的 agent 上使用时：
- 将 `dws-pipeline-analyzer/` 复制到目标项目
- 将此命令文件复制到 `commands/`
- 确保 `run.py` 入口脚本与 `references/` 同级
