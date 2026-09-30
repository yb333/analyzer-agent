---
status: active
last_reviewed: 2026-08-18
upstream: design-dev-agent docs/specs/opt/09-契约-baseline_v1.md（v1.0 交接基线）
---

# baseline_v1 契约（权威版，本仓维护）

> 本仓（dws-analyzer-skill）是 baseline_v1 契约的**权威侧**：schema 与本文档随投影代码
> 同仓演进，消费方（design-dev-agent）vendor 带版本拷贝做消费端校验。v1.0 基线见对方
> 仓 `docs/specs/opt/09-契约-baseline_v1.md`；**v1.1 起增量由本仓主导**（变更动机与
> 案例见 `docs/baseline_v1-案例-增量合并.md`）。

## 一、定位

- baseline_v1 = **纯物理事实包**（逆向产物），优化场景外部资产的存量现状表示。
- 文件交接，不跨项目调用脚本；消费方从契约校验开始，缺失/版本不符 fail loud。
- 产物反映线上实跑现状（源文件与平台一致性由交付方保证），消费方不做新鲜度校验。

**三条铁律（信息守恒）**：
1. **逐字保真**：`query_sql` / `delete_condition` / `raw_expr` 逐字节保留解析原文——
   不美化、不重排版、不规范化大小写。围栏比对/SQL 审计锚定这些原文。
2. **显式空**：拿不到的信息 = 显式 `null` 或空数组，不省略 key（xlsx 输入无调度 →
   `"schedule": null`）。缺口显式化，消费方能区分"没有"和"忘了"。
3. **无假信息**：结构化字段只承载**确定性文法解析**结果；解析不出 = `null` + warnings，
   绝不伪造。业务语义推断（增量字段、驱动表、主键、粒度）永不进契约，归人回路。

## 二、v1.1 变更：write_plan（本仓主导）

### 动机

v1.0 对删除策略的承载是"原文逐字 + merge_on 特例"：`delete_mode`/`delete_condition`
逐字携带，dm=6 额外要求 `merge_on` 非空。这个设计不对称——dm=6 的 ON 谓词被结构化了，
dm=4/8 的 WHERE 谓词、dm=3/5 的分区表达式、dm=9 的交换表却没有。而消费方真正需要的
正是这些条件的结构化形态（生成变更 SQL 时必须知道删除边界）。v1.1 用一个统一字段
`write_plan` 把对称性补齐；`merge_on` / `exchange_source_table` 保留（v1.0 消费方兼容）。

### 设计原则

- **双轨制**：原文（`delete_condition`，比对锚点）与结构化（`write_plan`，消费输入）并存，
  与 `query_sql` + `joins/where_clause/group_by` 的既有模式同构。
- **确定性解析 vs 概率性推断**：分界线不是"事实 vs 语义"，而是"文法解析 vs 业务推断"。
  从条件文本提取谓词/列引用是确定性解析（analyzer 本职）；"哪张是驱动表"是推断（禁止）。
- **平台语义版本化**：`write_plan.kind` 的 delete_mode→写入类型映射按
  `asset.platform_generation` 由本仓维护（映射表在 `baseline_export.WRITE_KIND_BY_MODE`）。
  平台迁移（2.0）时换映射表，契约事实字段不变。

### delete_mode → write_plan 映射（platform_generation = 1.0）

| dm | kind | condition_role | delete_condition 的内容形态 |
|----|------|----------------|---------------------------|
| 1 | full_truncate | none | 通常空 |
| 2 | append | none | 空 |
| 3 | subpartition_truncate | subpartition_expr | 子分区名/表达式 |
| 4 | delete_by_condition | where_predicate | WHERE 谓词 |
| 5 | partition_truncate | partition_expr | 分区名/表达式 |
| 6 | merge_upsert | on_predicate | MERGE ON 谓词（→ 同时填 v1.0 的 `merge_on`） |
| 7 | rpt_item | unclear | 平台语义未定，原文如实携带 |
| 8 | update_by_condition | where_predicate | WHERE 谓词 |
| 9 | exchange_partition | exchange_table | 交换配置（→ 同时填 v1.0 的 `exchange_source_table`） |
| （视图步骤） | view | none | — |
| （未知 dm） | unknown | unclear | 原文如实携带 + warning |

`condition_expr` 来源优先级：删除条件列原文（`condition_source: delete_condition`）→
MERGE SQL 的 ON 解析（`condition_source: query_sql`，等价重排版非逐字）→ 交换分区配置
（`exchange_config`）→ `null` + warning。dm=6 取不到时**导出 fail loud 不落盘**
（与消费方校验器镜像）。

## 三、内容规格

```
baseline_v1
├─ version = "1.1"             契约版本（双端校验锚点）
├─ asset                       schema/table/rule_group_code/rule_group_en/dialect/
│                              source_type/analysis_time/platform_generation
├─ provenance[]                实际消费的源文件 {type, path}：rule_yml/execution_tasks/
│                              ddl/lts/dq（路径清单，交付方保证可达）
├─ rules[]                     每规则：rule_code/rule_name/exec_sequence/scenario_id/
│     is_common/target_schema/target_table/delete_mode/delete_condition（逐字）/
│     merge_on（dm=6 必供）/ write_plan（v1.1）/ query_sql（逐字★）/ source_tables[]/
│     joins[]（物理表；子查询假名与 CTE 名不进契约）/ where_clause/group_by[]/
│     is_view_step / exchange_source_table（dm=9）
├─ tables[]                    SQL 涉及全部表 {schema,name}；fields = DDL catalog ∪ 血缘
│                              字段名（大小写不敏感去重；DDL 给 type/comment，血缘给 name，
│                              皆缺则空数组，类型回填归消费方查库）
├─ lineage[]                   {rule_code, target_field, transform_type, raw_expr（逐字）,
│                              physical_sources[]{table,field}（CTE 穿透，裁剪到二元组）}
├─ schedule                    f_tasks/i_tasks/other_tasks（jobs/params 事实原样；
│                              裁冗余 all_tasks 与 lts_file——路径在 provenance）；
│                              xlsx/批量模式 → 显式 null
├─ dq_rules[]                  按 rule_number 排序的 7 字段（编号/名/口径/check_type/
│                              alert_level/sql 原文/target_table）；xlsx 模式 → 显式 null
├─ load_strategy               hint 级原样（非权威，供人参考）
├─ patterns[]                  hint 级加工模式提示
└─ warnings[]                  三类差异事实合流：field_mappings.warnings（原样）+
                               quality.issues（category→type）+ write_plan_gap（本侧
                               交叉验证：如 dm=5 无分区条件、未知 dm、条件与写入类型不符）。
                               消费方式=报告+存档，不参与 patch 决策
```

三级语义分级：**事实**（必填或显式空，逐字保真）/ **结构化**（write_plan——确定性文法翻译，
非推断）/ **hint**（load_strategy、patterns——非权威）。业务语义（增量字段、驱动表、
主键、粒度）**永不进契约**。

## 四、信息守恒对账表（输入字段 → 契约去向）

输入侧每个字段必须落在四类之一：**契约字段 / provenance / warnings / 设计上丢弃（+理由）**。
没有"静默消失"。变更输入格式时同步维护本表。

| 输入字段（xlsx 列 / yml 键） | 去向 |
|---|---|
| 规则编码/执行序列/删除模式/删除条件/查询语句/目标Schema/目标表 | rules[]（逐字） |
| 规则中文名称 | rules[].rule_name |
| 规则类型 | 设计上丢弃（10/11/13/15/12 在读取层已分流；写入语义由 delete_mode 承载） |
| 交换分区来源表 | rules[].exchange_source_table + write_plan（dm=9） |
| TargetFields（目标字段/来源字段/字段类型/备注） | lineage[] + tables[].fields 优先级链；类型经 DDL/血缘落 fields |
| TargetFields.加密方式 | 设计上丢弃（v1.2 候选：如消费方需要安全审计请提案） |
| GroupVariables / 参数变量 | 设计上丢弃（调度参数已随 schedule.params 携带；规则级变量进 v1.2 候选） |
| LTS 任务/调度/jobs/params | schedule（事实原样） |
| DQ 规则 7 字段 | dq_rules[] |
| 项目编码/数据源/业务责任人 | source_type 不可知模式暂丢弃（v1.2 候选：asset.owner 等） |
| 源文件路径（yml/xlsx/DDL/LTS/DQ） | provenance[] |
| 解析交叉验证差异 | warnings[] |

## 五、CLI 与交接

```
python run.py analyzer --input {xlsx或规则组目录} --output {dir} [--ddl-dir D] \
                       --export baseline_v1 [--platform-generation 1.0]
```

- 产物：`{dir}/{规则组英文}/baseline_v1.json`；stdout 单独一行
  `baseline_v1: {绝对路径}`，消费方按该前缀解析定位。
- 自校验后落盘：先过本仓权威 schema（`dws-pipeline-analyzer/schemas/baseline_v1.schema.json`）
  + 语义检查（dm=6 必供 merge_on、write_plan 与 delete_mode 映射一致、provenance 非空），
  不过 fail loud 不写文件。
- 依赖 `jsonschema>=4.0`（仅 export 路径懒加载，不影响既有功能）。
- 命令封装：`/export-baseline`（commands/export-baseline.md，AI 零介入流程）。

## 六、版本与变更协议

- schema 改动 → bump version + 通知消费方同步 vendor 拷贝与校验器；breaking change 提前约定。
- **v1.0 → v1.1 为纯增量**：仅新增可选字段 `rules[].write_plan`（及枚举），未删改任何
  v1.0 字段。v1.0 形态产物通过 v1.1 schema 校验（已验证）。
- 运行时：消费方先对 `version`，不支持即 fail loud。
- 双方各自 CI 校验（不跨仓 import）：本仓断言 export 过权威 schema；消费方断言
  vendor fixture 通过其校验器。

### 消费方同步指南（v1.0 → v1.1，约 10 分钟）

1. vendor 拷贝本仓 `dws-pipeline-analyzer/schemas/baseline_v1.schema.json` 覆盖
   `skills/design-dev-shared/schemas/baseline_v1.schema.json`；
2. `baseline_contract.py`：`SUPPORTED_VERSIONS` 增加 `"1.1"`；
3. （可选）消费 `write_plan`：`kind` 直接给出写入类型（不再需要自查 dm 枚举映射），
   `condition_expr` 给出结构化条件主体，`condition_columns` 给出谓词列（变更影响围栏
   比对可直接用）；不消费也不影响既有 v1.0 读取路径（write_plan 缺席合法）；
4. 现有 v1.0 fixture 无需改动（向后兼容已验证）。

## 七、本仓实施清单（状态）

- [x] `--export baseline_v1` 投影 + 权威 schema + 本契约文档（v1.1）
- [x] provenance 采集（rule_yml / xlsx / ddl / lts / dq 实际消费清单）
- [x] write_plan 全模式结构化（dm 1-9 + 视图 + 未知），merge_on dm=6 fail loud
- [x] `/export-baseline` 命令封装（零 AI 介入 + 人回路指引）
- [ ] 批量 / 链式分析接入（analyze-batch / analyze-chain，按需）
- [ ] 远期可选：补数任务发现、RPT_ITEM（dm=7）语料确认后细化、加密标识/规则变量入契约提案
