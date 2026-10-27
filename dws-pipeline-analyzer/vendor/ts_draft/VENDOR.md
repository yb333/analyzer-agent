# vendor: ts 草稿契约交付包（权威=消费方 design-dev-agent）

契约方向与 asset_facts 相反：**消费方权威、本仓 vendor**（12 章 §11.4）。
本目录是消费方仓下列文件的逐字拷贝，`tests/test_vendor_sync.py` 指纹联锁——
消费方变更而本仓未 re-vendor 时 fail（彼侧路径不在本机时 skip，内网形态）。

| 文件 | 来源（消费方仓） | 角色 |
|---|---|---|
| ts_draft_v2.schema.json | skills/opt-pipe/schemas/ | 契约必填面（机器进门验） |
| ts_draft_v2.example.json | skills/opt-pipe/schemas/ | 合格样例（需求形态参照，非生成指令） |
| ts_sample_forward.json | skills/opt-pipe/schemas/ | 正向设计链路真实产出——**全貌对齐靶** |
| ts-template.json | skills/dws-design/assets/ | 完整结构权威（JSONC，schema 未描述字段以此为准） |

- 同步锚：消费方 commit `e3a7882`（2026-10-27）
- re-vendor 步骤：拷贝四文件覆盖 → 更新本锚 → 跑 test_vendor_sync + test_ts_export
- 词表（load_mode 等 ts 域词汇）不在此目录：按本仓标准枚举产（用户决策 2026-10-27），
  消费方遵循本仓枚举
