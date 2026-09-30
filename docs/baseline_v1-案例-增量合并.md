---
status: active
last_reviewed: 2026-08-18
audience: design-dev-agent 消费侧 + 双端评审
---

# baseline_v1 v1.1 案例：增量合并资产（DWB_TRADE_MERGE_D）

> 本文是 v1.0 → v1.1 契约增量的**共识材料**：一个真实形态的增量合并（dm=6）存量资产，
> 展示 write_plan 如何把删除策略按写入类型统一结构化，以及消费方因此省掉什么。
> 契约全文见 [baseline_v1-契约.md](./baseline_v1-契约.md)；案例三件套（源仓/命令/产物）在本目录。

## 一、案例资产

一个典型的两步增量合并链路（模拟代码仓，结构与真实仓一致）：

| 规则 | 写入 | 内容 |
|------|------|------|
| R0001 | dm=1 全量清写 | 订单表 LEFT JOIN 支付表 → 中间表 `dws.tmp_trade_merge` |
| R0002 | **dm=6 增量合并** | 中间表 MERGE 进最终表 `dws.dwb_trade_merge_d`，删除条件列填 ON 谓词 `m.order_id = t.order_id` |

复现命令（本目录下可直接执行）：

```bash
python dws-pipeline-analyzer/run.py analyzer \
    --input docs/baseline_v1-案例/repo/BFT/BftWideTable/P_TRADE/SUB_TRADE/DWB_TRADE_MERGE_D \
    --output docs/baseline_v1-案例/output \
    --export baseline_v1
# stdout: baseline_v1: .../DWB_TRADE_MERGE_D/baseline_v1.json
```

产物：`output/DWB_TRADE_MERGE_D/baseline_v1.json`（本目录随仓留存一份）。

## 二、v1.0 视角：消费方要自己做什么

v1.0 契约对 R0002 的承载是：`delete_mode: "6"`（裸枚举）+ `delete_condition:
"m.order_id = t.order_id"`（裸字符串）+ `merge_on`（仅 dm=6 有的特例字段）。

消费方（design-dev-agent）要安全地做精确变更，自己需要维护一张
**delete_mode → 写入语义** 的版本化映射表（哪个是 MERGE、哪个是分区清写、哪个
DELETE 要 WHERE），并且：

1. **自己解析 delete_condition 文本**：dm=6 时它是 ON 谓词，dm=4/8 时是 WHERE 谓词，
   dm=3/5 时是分区表达式——同一个字段，随 dm 变语义，消费方逐模式写解析；
2. **自己提取谓词里的列**：变更影响围栏比对需要知道 ON 条件涉及哪些列
   （改了 `order_id` 动不动到合并键？），需要再写一层列引用提取；
3. **dm=6 以外的模式没有结构化出口**：契约里只有 merge_on 一个特例，dm=4 的
   WHERE、dm=5 的分区名无处安放，只能回去自己解析原文——即"逐字保真"退化成
   "消费方各自重造解析器"，双端各一份实现，必然漂移。

## 三、v1.1 视角：同一个规则长什么样

```json
{
  "rule_code": "R0002",
  "delete_mode": "6",
  "delete_condition": "m.order_id = t.order_id",
  "merge_on": "m.order_id = t.order_id",
  "write_plan": {
    "kind": "merge_upsert",
    "condition_role": "on_predicate",
    "condition_expr": "m.order_id = t.order_id",
    "condition_columns": ["m.order_id", "t.order_id"],
    "condition_source": "delete_condition"
  }
}
```

消费方直接得到：

- `kind`：写入类型已翻译（`merge_upsert`），不再需要自查 dm 枚举映射——映射表
  按平台代次由 analyzer 侧统一维护（我们解析语料、我们见得多，映射权威归解析侧）；
- `condition_expr`：条件的语义主体（ON 谓词），不再逐模式猜字段形态；
- `condition_columns`：谓词列引用（变更围栏比对可直接用：用户要改 `order_id`
  的加工逻辑 → 命中合并键 → 必须联动评估 MERGE 冲突行为）；
- `condition_source`：该表达式从哪来（删除条件列逐字 / SQL ON 解析 / 交换配置），
  审计可溯源。

而 v1.0 的锚点一个没动：`delete_mode` / `delete_condition` / `merge_on` 原样保留，
围栏比对继续锚原文。**原文做锚点，结构化做消费输入，双轨并存。**

## 四、这不是 dm=6 特例，是全模式对称补齐

write_plan 覆盖 dm 1–9 + 视图 + 未知值（完整映射表见契约文档 §二）。同一个资产里
R0001（dm=1）：

```json
"write_plan": {
  "kind": "full_truncate", "condition_role": "none",
  "condition_expr": null, "condition_columns": [], "condition_source": "none"
}
```

三条纪律（信息守恒，写进契约 §一）在实现层强制：

1. **逐字保真**：query_sql / delete_condition / raw_expr 逐字节拷贝原文（测试断言
   与源 yml 逐字节相等）；
2. **显式空**：解析不出 → `condition_expr: null`（不是空串、不是省略 key），
   xlsx 模式无调度 → `"schedule": null`；
3. **无假信息**：解析不出绝不伪造——dm=4 无删除条件（影响范围不可界定）、
   dm=5 无分区表达式、truncate 却带条件（平台语义未定）等全部进 `warnings[]`
   （type=write_plan_gap）供报告+存档；dm=6 取不到 ON 时**导出直接 fail loud
   不落盘**，与消费方校验器行为镜像。

## 五、消费方同步（约 10 分钟）

1. vendor 拷贝本仓 `dws-pipeline-analyzer/schemas/baseline_v1.schema.json` 覆盖
   `skills/design-dev-shared/schemas/baseline_v1.schema.json`；
2. `baseline_contract.py` 的 `SUPPORTED_VERSIONS` 增加 `"1.1"`；
3. 既有消费代码**无需改动**：write_plan 是可选字段，v1.0 形态产物仍过 v1.1 schema
   （消费方 fixture `baseline_v1_demo_full.json` 已验证通过 v1.1 校验）；
4. 需要时再接入 write_plan（kind / condition_expr / condition_columns 即取即用）。

## 六、案例目录

```
docs/baseline_v1-案例/
├── baseline_v1-案例-增量合并.md   ← 本文
├── repo/                          ← 模拟代码仓（规则yml + DDL + LTS + DQ）
└── output/DWB_TRADE_MERGE_D/
    ├── baseline_v1.json           ← 契约产物（provenance 路径已本地化）
    └── knowledge_draft.json       ← 逆向中间产物（对照用）
```
