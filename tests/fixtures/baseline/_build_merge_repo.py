"""构造 baseline_v1 增量合并（dm=6 MERGE）科普案例的模拟代码仓。

场景：优化要改的存量资产是「增量合并」写入（delete_mode=6，删除条件列填
MERGE ON 谓词）——v1.0 契约只有 merge_on 特例，v1.1 的 write_plan 把
delete_mode+delete_condition 按写入类型统一结构化。本仓就是该案例的语料。

结构（对齐 _build_repo.py 的真实代码仓布局）：
    repo/
    ├── BFT/BftWideTable/P_TRADE/SUB_TRADE/DWB_TRADE_MERGE_D/{R0001,R0002}.yml
    │     R0001: dm=1 全量清写 tmp 中间表（join 两张 ods 源表）
    │     R0002: dm=6 增量合并进最终表（删除条件列 = MERGE ON 谓词）
    ├── DDL/DWS_EDW/dws/table/{tmp_trade_merge,dwb_trade_merge_d}.sql
    ├── LTS/BCNB_DAILY/GROUP_TRADE_MERGE/TASK_TRADE_MERGE_{F,I}.yml
    └── DQ/FIN_DWB/FIN_DWB_COMMEN/dwb_trade_merge_d/DQ_000001.yml

使用:
    from _build_merge_repo import build_merge_repo
    repo = build_merge_repo(tmp_path)   # 返回 {repo_root, group_dir, ...}
"""

import sys
from pathlib import Path

_FIXTURES_COMMON = Path(__file__).resolve().parent.parent / "analyzer"
if str(_FIXTURES_COMMON) not in sys.path:
    sys.path.insert(0, str(_FIXTURES_COMMON))

from _build_yml import build_yml_group  # noqa: E402


# R0002 的 MERGE ON 谓词（删除条件列原文，逐字进契约）
MERGE_ON_CONDITION = "m.order_id = t.order_id"


def build_merge_repo(base_dir):
    """构造增量合并资产 demo 仓，返回关键路径 dict。"""
    repo = Path(base_dir)

    # ── 1. 规则组：R0001 全量清写中间表 → R0002 增量合并最终表 ──
    group_dir = repo / "BFT" / "BftWideTable" / "P_TRADE" / "SUB_TRADE" / "DWB_TRADE_MERGE_D"
    build_yml_group(group_dir, rules=[
        {"rule_code": "R0001", "rule_type": 1, "exec_sequence": 1,
         "target_schema": "dws", "target_table": "tmp_trade_merge", "delete_mode": "1",
         "query_sql": "SELECT o.order_id, o.cust_id, p.pay_amount "
                      "FROM ods.ods_trade_order_di o "
                      "LEFT JOIN ods.ods_trade_pay_di p ON o.order_id = p.order_id "
                      "WHERE o.del = 'N'",
         "rule_name": "交易合并明细", "rule_group_code": "GR_TRADE_MERGE",
         "rule_group_en": "DWB_TRADE_MERGE_D",
         "target_fields": [
             {"rule_code": "R0001", "target_field": "order_id", "source_field": "o.order_id",
              "field_type": "VARCHAR(64)", "remark": "订单ID"},
         ]},
        {"rule_code": "R0002", "rule_type": 1, "exec_sequence": 2,
         "target_schema": "dws", "target_table": "dwb_trade_merge_d", "delete_mode": "6",
         "delete_condition": MERGE_ON_CONDITION,
         "query_sql": "SELECT t.order_id, t.cust_id, t.pay_amount, current_timestamp AS merge_time "
                      "FROM dws.tmp_trade_merge t",
         "rule_name": "增量合并进最终表", "rule_group_code": "GR_TRADE_MERGE",
         "rule_group_en": "DWB_TRADE_MERGE_D",
         "target_fields": [
             {"rule_code": "R0002", "target_field": "order_id", "source_field": "t.order_id",
              "field_type": "VARCHAR(64)", "remark": "订单ID"},
             {"rule_code": "R0002", "target_field": "pay_amount", "source_field": "t.pay_amount",
              "field_type": "DECIMAL(18,2)", "remark": "支付金额"},
         ]},
    ])

    # ── 2. DDL ──
    ddl_table_dir = repo / "DDL" / "DWS_EDW" / "dws" / "table"
    ddl_table_dir.mkdir(parents=True, exist_ok=True)
    (ddl_table_dir / "tmp_trade_merge.sql").write_text(
        "CREATE TABLE dws.tmp_trade_merge (\n  order_id VARCHAR(64)\n);",
        encoding="utf-8")
    (ddl_table_dir / "dwb_trade_merge_d.sql").write_text(
        "CREATE TABLE dws.dwb_trade_merge_d (\n"
        "  order_id VARCHAR(64),\n"
        "  cust_id VARCHAR(64),\n"
        "  pay_amount DECIMAL(18,2),\n"
        "  merge_time TIMESTAMP\n"
        ");\n"
        "COMMENT ON COLUMN dws.dwb_trade_merge_d.order_id IS '订单ID';\n"
        "COMMENT ON COLUMN dws.dwb_trade_merge_d.cust_id IS '客户ID';\n"
        "COMMENT ON COLUMN dws.dwb_trade_merge_d.pay_amount IS '支付金额';\n"
        "COMMENT ON COLUMN dws.dwb_trade_merge_d.merge_time IS '合并时间';\n"
        "COMMENT ON TABLE dws.dwb_trade_merge_d IS '交易增量合并表';\n",
        encoding="utf-8")

    # ── 3. LTS 调度（F + I 任务）──
    lts_group_dir = repo / "LTS" / "BCNB_DAILY" / "GROUP_TRADE_MERGE"
    lts_group_dir.mkdir(parents=True, exist_ok=True)
    (lts_group_dir / "TASK_TRADE_MERGE_F.yml").write_text(
        "'*任务名称': TASK_TRADE_MERGE_F\n"
        "'*任务类型': 周期任务\n"
        "'*任务组名称': GROUP_TRADE_MERGE\n"
        "'*责任人': test_user\n"
        "'*项目名称': BCNB_DAILY\n"
        "调度周期: 0 30 2 * * ?\n"
        "开始时间: 2026/01/01 02:30:00\n"
        "依赖上一周期: 是\n"
        "是否一天多调: 否\n"
        "额外信息（其他sheet页信息）: |\n"
        "  Jobs: |\n"
        "    - '*job名称': Pjob_merge_f\n"
        "      '*job的父节点名称': start\n"
        "      '*job类型': url\n"
        "      job重试次数: '3'\n"
        "      job重试间隔: '60'\n"
        "  taskParams: |\n"
        "    - '*任务名称': TASK_TRADE_MERGE_F\n"
        "      '*任务组名称': GROUP_TRADE_MERGE\n"
        "      '*参数名称': V_GROUP_CODE\n"
        "      '*项目名称': BCNB_DAILY\n"
        "      参数值: GR_TRADE_MERGE\n",
        encoding="utf-8")
    (lts_group_dir / "TASK_TRADE_MERGE_I.yml").write_text(
        "'*任务名称': TASK_TRADE_MERGE_I\n"
        "'*任务类型': 周期任务\n"
        "'*任务组名称': GROUP_TRADE_MERGE\n"
        "'*责任人': test_user\n"
        "'*项目名称': BCNB_DAILY\n"
        "调度周期: 0 40 2 * * ?\n"
        "开始时间: 2026/01/01 02:40:00\n"
        "依赖上一周期: 是\n"
        "是否一天多调: 否\n"
        "额外信息（其他sheet页信息）: |\n"
        "  Jobs: |\n"
        "    - '*job名称': Pjob_merge_i\n"
        "      '*job的父节点名称': start\n"
        "      '*job类型': database\n"
        "      job重试次数: '1'\n"
        "      job重试间隔: '60'\n"
        "  taskParams: |\n"
        "    - '*任务名称': TASK_TRADE_MERGE_I\n"
        "      '*任务组名称': GROUP_TRADE_MERGE\n"
        "      '*参数名称': V_SUCCESS_VALUE\n"
        "      '*项目名称': BCNB_DAILY\n"
        "      参数值: v_code==0\n",
        encoding="utf-8")

    # ── 4. DQ 质量规则 ──
    dq_dir = repo / "DQ" / "FIN_DWB" / "FIN_DWB_COMMEN" / "dwb_trade_merge_d"
    dq_dir.mkdir(parents=True, exist_ok=True)
    (dq_dir / "DQ_000001.yml").write_text(
        "规则Number: DQ_000001\n"
        "规则中文名: 主键空值检查\n"
        "规则中文描述: 合并键不允许为空\n"
        "检查类型: NULL\n"
        "告警级别: '2'\n"
        "SQL: SELECT count(*) FROM fin_dwb.dwb_trade_merge_d WHERE order_id IS NULL\n"
        "目标表: dwb_trade_merge_d\n"
        "规则负责人: test_user\n",
        encoding="utf-8")

    return {
        "repo_root": repo,
        "group_dir": group_dir,
        "ddl_dir": ddl_table_dir,
    }
