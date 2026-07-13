# 跨资产影响分析 — 交互格式契约（草稿）

> 状态：草稿，待讨论确认后定稿。
>
> 这是跨资产影响分析落地的第一前置条件：格式定不下来，后续全卡住。
> 格式由我们定义，血缘平台按此实现。

---

## 1. 总体流程

```
源变更清单（Excel，格式同单资产）
    ↓
第1层：给定受影响的表名列表
    ↓ 按表名在代码仓定位资产目录（表名=目录名，大小写不敏感）
    ↓ 每个资产跑 analyze_pipeline + filter_and_propagate
第1层产出：每个资产的影响清单（🔴🟡🟢⚪）
    ↓ 提取确认传播的目标字段（🔴🟡的 target_table + target_field）
第2层：拿第1层确认的字段问平台/查依赖
    ↓ 找到下一层依赖这些字段+表的资产
    ↓ 再跑单资产分析
    ... 逐层传播，直到全被过滤或达深度上限
    ↓
最终输出：全链路影响报告（多层级，每层带严重度+路径）
```

---

## 2. 输入清单格式

### 2.1 源变更清单（复用单资产格式，不变）

跟单资产影响分析的变更清单 Excel 完全一致——三 Sheet（表级变更/字段级变更/变动类型说明）。
跨资产场景只是批量的：源变更清单还是一份，只是要分析多个资产。

### 2.2 受影响表名清单（跨资产新增输入）

源变更清单告诉你"什么变了"，但跨资产还需要知道"先分析哪些表"。
这份清单可以由血缘平台产出，也可以用户手动指定。

```yaml
# affected_tables.yml（或 Excel 第四 Sheet / JSON，格式待定）
affected_tables:
  - table: dwb_user_info_f          # 受影响的表名（就是代码仓目录名）
    affected_fields:                 # 该表受影响的字段（平台给的，压平的）
      - user_id                      # 只给字段名，不给严重度/路径（那些我们算）
      - uid_bigint
    reason: "源表 ods.user_src.user_id 类型变化"   # 简要原因（可选，用于追溯）
  - table: dwb_order_f
    affected_fields:
      - total_amount
```

**关键约定**：
- `table` = 资产目录名 = 目标表名（如 `DWB_USER_INFO_F`），大小写不敏感匹配
- `affected_fields` = 该表上受影响的字段名（来自上游传播或平台判定）
- 这份清单的粒度是"表+字段"，不含层级/严重度——那些由我们的分析补充

### 2.3 资产定位规则

```
受影响的表名 DWB_USER_INFO_F
    ↓ 在代码仓 BFT/ 下 rglob 搜索目录名匹配（大小写不敏感）
    ↓ 匹配规则：目录名 == 表名（精确匹配优先）
找到 → BFT/BftWideTable/P_USER/SUB_USER/DWB_USER_INFO_F/规则组英文名/
    ↓ 取规则组目录，跑 analyze_pipeline
找不到 → 标"未定位到资产目录"，进未处理清单，提示用户手动指定路径
```

**拆分资产处理**：一个表拆成多个规则组（加尾缀数字）的场景不做自动合并。
按表名精确匹配，找不到或匹配到多个时让用户手动指定。

---

## 3. 单层分析产出格式

每个受影响资产跑完单资产分析后，产出标准的影响结果。

### 3.1 单资产影响结果

```yaml
asset_result:
  asset: DWB_USER_INFO_F                    # 资产名/目录名
  target_table: dwb_user_info_f             # 目标表
  status: completed                         # completed | not_found | parse_error
  summary:                                  # 统计（同单资产）
    impacted: 2                             # 🔴
    uncertain: 1                            # 🟡
    no_impact: 3                            # 🟢
    not_hit: 5                              # ⚪
  impacts:                                  # 影响明细（🔴🟡，同单资产格式）
    - status: "🔴有影响"
      target_table: dwb_user_info_f
      target_field: uid
      source_table: ods.user_src
      source_field: user_id
      change_type: 字段类型及长度变化
      reason: "源新类型 varchar(50) 与目标 bigint 类型大类不一致"
      hops: "R001/step_1: cast(a.user_id as bigint)"
      steps: step_1
      rule_codes: R001
  propagated_fields:                        # ★ 确认传播到下一层的字段
    - target_table: dwb_user_info_f         # 本资产的目标表（下一层的源表）
      target_field: uid                     # 受影响的目标字段（下一层要看这个字段）
      severity: high                        # high(🔴) | unknown(🟡)，🟢⚪不进这里
      change_summary: "uid 类型可能变化"     # 给下一层的简要说明
```

**`propagated_fields` 是逐层传播的关键**：
- 只有 🔴和🟡 的目标字段进这个列表（🟢⚪截断不传）
- 下一层拿这个列表去找"谁依赖了这些表+字段"

---

## 4. 逐层传播格式

### 4.1 传播输入（上一层 → 查下一层依赖）

```yaml
# 第N层确认传播的字段 → 作为查第N+1层的输入
propagate_query:
  layer: 1                                  # 第几层
  propagated_from:
    - table: dwb_user_info_f                # 上层确认受影响的目标表
      fields:                               # 受影响字段
        - field: uid
          severity: high
          change_summary: "uid 类型可能变化"
        - field: status
          severity: unknown
          change_summary: "status 语义变化待确认"
    - table: dwb_order_f
      fields:
        - field: total_amount
          severity: high
          change_summary: "精度扩大"
```

### 4.2 传播输出（查依赖 → 下一层受影响资产）

这份可以由血缘平台产出，也可以用户从平台导出：

```yaml
# 平台/用户返回：谁直接依赖了上面那些表
affected_assets:
  - table: dwb_user_info_f                  # 被依赖的表（来自 propagate_query）
    dependent_asset: DWB_REPORT_F           # 依赖它的资产（目录名=表名）
    dependent_fields:                       # 哪些字段被引用（压平的）
      - uid                                 # 这个资产用了 uid 字段
      - user_name                           # 也用了 user_name（但 user_name 没受影响，不传）
  - table: dwb_order_f
    dependent_asset: DWB_REPORT_F           # 同一个资产也依赖了 order_f
    dependent_fields:
      - total_amount
```

**注意**：`dependent_fields` 是该资产引用了被影响表的哪些字段。
我们会跟 `propagate_query` 里的受影响字段做交集——
只分析真正受影响的字段（如 uid 受影响、user_name 没受影响就不分析 user_name）。

### 4.3 转成单资产输入

把传播输出转成每个资产的单资产变更清单：

```yaml
# DWB_REPORT_F 的变更清单（自动构造）
asset: DWB_REPORT_F
changes:
  - table: dwb_user_info_f              # 来自 propagate_query.table
    field: uid                          # 交集字段（受影响 且 被引用）
    change_type: 字段类型及长度变化      # 从上层的 change_summary 推导
    before: bigint                      # 上层目标表的 DDL 类型
    after: varchar(50)                  # 变化后的类型（从上层传播）
  - table: dwb_order_f
    field: total_amount
    change_type: 字段类型及长度变化
    before: numeric(10,2)
    after: numeric(18,2)
```

然后对 DWB_REPORT_F 跑单资产分析，产出 3.1 的结果，继续传播。

---

## 5. 终止条件

```
循环终止当：
  1. 某层所有资产的影响都被过滤（propagated_fields 为空）→ 自然收敛
  2. 达到深度上限（默认 5 层）→ 防止自引用无限循环
  3. 出现环（A→B→A）→ 检测到已处理的资产跳过
```

---

## 6. 最终输出格式

### 6.1 多层级影响报告

```yaml
cross_asset_report:
  source_changes:                          # 源变更（原始输入）
    - table: ods.user_src
      field: user_id
      change_type: 字段类型及长度变化

  layers:                                  # 按层级组织
    - layer: 1
      assets:
        - asset: DWB_USER_INFO_F
          impacts: [...]                   # 同单资产 impact 格式
          propagated_to_next: [uid, status]
        - asset: DWB_TRADE_F
          impacts: [...]
          propagated_to_next: [trade_uid]

    - layer: 2
      assets:
        - asset: DWB_REPORT_F
          impacts: [...]
          propagated_to_next: [report_uid]   # 只有这个传到第3层

    - layer: 3
      assets: []                              # 空 → 自然收敛

  summary:                                 # 全链路统计
    total_layers: 3
    total_assets_affected: 3               # 去重计数
    total_impacts: 7                       # 所有层的🔴🟡总和
    max_depth_reached: 2                   # 实际传播深度
    terminated_by: natural_convergence     # natural_convergence | depth_limit | cycle_detected
```

### 6.2 输出产物

```
impact_cross_asset.xlsx
  Sheet1: 影响总览（按层级 × 资产，一图看全链路）
  Sheet2: 影响明细（所有层的🔴🟡，按资产分组）
  Sheet3: 传播路径（树状：源变更→第1层→第2层...）
  Sheet4: 未处理/待确认（找不到目录/解析失败/环检测）
```

---

## 7. 待确认的开放问题

| # | 问题 | 当前草案立场 | 需要确认 |
|---|------|------------|---------|
| 1 | 受影响表名清单的载体格式 | YAML（也可 Excel/JSON） | 平台能产出哪种？ |
| 2 | `dependent_fields`（平台返回的引用字段） | 压平字段名列表 | 平台能提供字段级还是只有表级？ |
| 3 | change_type 在跨层传播时怎么推导 | 从上层的 severity + change_summary 推 | 是否足够？ |
| 4 | before/after 类型在跨层传播时怎么取 | 取上层目标表的 DDL 类型 | DDL 不全时怎么办？ |
| 5 | 环检测策略 | 已处理的资产跳过 | 是否需要更精细的环处理？ |
| 6 | 深度上限 | 默认 5 层 | 合理吗？ |
| 7 | 拆分资产（多规则组） | 不自动合并，手动指定 | 确认不做？ |
