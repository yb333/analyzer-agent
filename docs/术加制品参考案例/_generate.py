#!/usr/bin/env python3
"""生成术加制品参考案例 execution_tasks.xlsx（模拟真实案例）。

用途：给 design-dev-agent 作为「正向生成制品包」的黄金参考——列头逐字对齐
真实制品（含全角括号等陷阱），覆盖常见规则形态。重新生成：
    python3 docs/术加制品参考案例/_generate.py

案例资产 DWB_CUST_ASSET_D（客户资产汇总，两步加工 + 分区多场景写入）：
    R0001  dm=1   tmp 明细中间表（join 两源表）
    R0002  dm=1   tmp 汇总中间表（GROUP BY；SQL 故意拆在 查询语句1/2 两列，演示拼接规则）
    R0003  dm=5   最终表 当日分区（P_CUR）
    R0004  dm=5   最终表 历史分区（P_HIS）——与 R0003 同执行序列，不同删除条件 → 场景分组
    R0005  rt=12  参数变量行（不构成步骤）
"""

import sys
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "analyzer"
sys.path.insert(0, str(_FIXTURES))

from openpyxl import Workbook

from _build_xlsx import GV_COLUMNS, RULE_COLUMNS, TF_COLUMNS

OUT = Path(__file__).resolve().parent / "execution_tasks_DWB_CUST_ASSET_D.xlsx"

# ── 案例数据 ──────────────────────────────────────────────────────

SQL_R0001 = (
    "SELECT c.cust_id, c.cust_name, a.asset_type, a.balance, a.acct_date "
    "FROM ods.ods_acct_balance_di a "
    "JOIN ods.ods_cust_info_di c ON a.cust_id = c.cust_id "
    "WHERE a.del_flag = 'N' AND a.acct_date = '20260818'"
)
# R0002 演示超长 SQL 分列：查询语句1 存前半段（保留结尾换行），查询语句2 存后半段
SQL_R0002_PART1 = (
    "SELECT cust_id, asset_type, SUM(balance) AS total_balance, "
    "COUNT(1) AS record_cnt FROM dws.tmp_cust_asset_detail t"
)
SQL_R0002_PART2 = (
    " GROUP BY t.cust_id, t.asset_type HAVING SUM(t.balance) > 0"
)
SQL_R0003 = (
    "SELECT s.cust_id, s.asset_type, s.total_balance, 'CUR' AS data_scope "
    "FROM dws.tmp_cust_asset_sum s WHERE s.data_flag = 'D'"
)
SQL_R0004 = (
    "SELECT s.cust_id, s.asset_type, s.total_balance, 'HIS' AS data_scope "
    "FROM dws.tmp_cust_asset_sum s WHERE s.data_flag = 'H'"
)

# 规则行：key 用 _build_xlsx._build_rule_row 认的英文 key + 本脚本扩展（查询语句2）
RULES = [
    dict(rule_code="R0001", rule_type=1, exec_sequence=1, rule_name="资产明细加工",
         target_schema="dws", target_table="tmp_cust_asset_detail",
         delete_mode="1", delete_condition="", query_sql=SQL_R0001),
    dict(rule_code="R0002", rule_type=1, exec_sequence=2, rule_name="资产汇总加工",
         target_schema="dws", target_table="tmp_cust_asset_sum",
         delete_mode="1", delete_condition="",
         query_sql=SQL_R0002_PART1, query_sql2=SQL_R0002_PART2),
    dict(rule_code="R0003", rule_type=1, exec_sequence=3, rule_name="当日分区写入",
         target_schema="dws", target_table="dwb_cust_asset_d",
         delete_mode="5", delete_condition="P_CUR", query_sql=SQL_R0003),
    dict(rule_code="R0004", rule_type=1, exec_sequence=3, rule_name="历史分区写入",
         target_schema="dws", target_table="dwb_cust_asset_d",
         delete_mode="5", delete_condition="P_HIS", query_sql=SQL_R0004),
    dict(rule_code="R0005", rule_type=12, exec_sequence=0, rule_name="P_CYCLE_ID",
         target_schema="", target_table="", delete_mode="", delete_condition="",
         query_sql=""),
]

TARGET_FIELDS = [
    # 来源字段名称：有值必为 s.字段 形态（s 是平台标准别名，与规则 SQL 内表别名无关）；
    # 无源列可指则留空
    dict(rule_code="R0001", target_field="cust_id", source_field="s.cust_id",
         field_type="VARCHAR(64)", remark="客户ID"),
    dict(rule_code="R0001", target_field="cust_name", source_field="s.cust_name",
         field_type="VARCHAR(128)", remark="客户名称"),
    dict(rule_code="R0001", target_field="asset_type", source_field="s.asset_type",
         field_type="VARCHAR(32)", remark="资产类型"),
    dict(rule_code="R0001", target_field="balance", source_field="s.balance",
         field_type="DECIMAL(18,2)", remark="账户余额"),
    dict(rule_code="R0002", target_field="cust_id", source_field="s.cust_id",
         field_type="VARCHAR(64)", remark="客户ID"),
    dict(rule_code="R0002", target_field="total_balance", source_field="s.balance",
         field_type="DECIMAL(18,2)", remark="资产总余额"),
    dict(rule_code="R0002", target_field="record_cnt", source_field="",
         field_type="BIGINT", remark="记录数"),
    # R0003/R0004：真实制品常见的部分登记（R0004 total_balance 缺备注，演示声明缺口）
    dict(rule_code="R0003", target_field="cust_id", source_field="s.cust_id",
         field_type="VARCHAR(64)", remark="客户ID"),
    dict(rule_code="R0003", target_field="total_balance", source_field="s.total_balance",
         field_type="DECIMAL(18,2)", remark="资产总余额"),
    dict(rule_code="R0004", target_field="cust_id", source_field="s.cust_id",
         field_type="VARCHAR(64)", remark="客户ID"),
    dict(rule_code="R0004", target_field="total_balance", source_field="s.total_balance",
         field_type="DECIMAL(18,2)"),
]

GROUP_VARIABLES = [
    dict(rule_code="R0003", var_name="P_CYCLE_ID", default_value="20260818"),
    dict(rule_code="R0004", var_name="P_DATA_DATE", default_value="20260818"),
]


def _rule_row(r: dict) -> list:
    """按 RULE_COLUMNS 顺序构建一行（真实感取值：标识列填值，技术开关列留空）。"""
    group = {
        "类型": 3,
        "项目编码": "P_CUST", "项目中文名": "客户域", "项目英文名": "CUST_DOMAIN",
        "子项目编码": "SUB_CUST_ASSET", "子项目中文名": "客户资产",
        "子项目英文名": "CUST_ASSET",
        "规则组编码": "GR_CUST_ASSET", "规则组中文名称": "客户资产汇总",
        "规则组英文名称": "DWB_CUST_ASSET_D",
        "规则组业务责任人": "zhang_san", "规则组数据源": "DWS_EDW",
        "规则编码": r["rule_code"],
        "规则中文名称": r["rule_name"], "规则英文名称": r["rule_code"],
        "创建方式": 2, "规则类型": r["rule_type"],
        "数据源": "DWS_EDW", "备注": "",
        "(生成的）查询语句1": r.get("query_sql", ""),
        "(生成的）查询语句2": r.get("query_sql2", ""),
        "运行条件": 0, "执行序列": r["exec_sequence"],
        "目标Schema": r["target_schema"], "目标表": r["target_table"],
        "删除模式": r["delete_mode"], "删除条件": r["delete_condition"],
        "业务责任人": "zhang_san",
        "行迁移开关": 0, "并行开关": 0, "数据库类型": "GaussDB", "调度类型": 0,
    }
    return [group.get(col) for col in RULE_COLUMNS]


def main():
    wb = Workbook()

    ws = wb.active
    ws.title = "RULE"
    ws.append(RULE_COLUMNS)
    for r in RULES:
        ws.append(_rule_row(r))

    ws_gv = wb.create_sheet("GroupVariables")
    ws_gv.append(GV_COLUMNS)
    for gv in GROUP_VARIABLES:
        ws_gv.append([gv.get("rule_code"), gv.get("var_name"), "STRING", "系统变量",
                      "STRING", gv.get("default_value"), "是", "STRING",
                      f"{gv['var_name']} 调度周期参数", "是"])

    ws_tf = wb.create_sheet("TargetFields")
    ws_tf.append(TF_COLUMNS)
    for tf in TARGET_FIELDS:
        ws_tf.append([tf.get("rule_code"), tf.get("target_field"),
                      tf.get("source_field"), "0", "", tf.get("alias", ""),
                      tf.get("field_type", ""), tf.get("remark", "")])

    # 其余标准空 sheet（真实制品包固定结构）
    from _build_xlsx import build_xlsx  # 复用列定义
    for sheet_name in ["ModelRelations", "ExtraFields", "SPParams", "Conditions",
                       "MaintenanceParams", "Extract", "ExtractColumn"]:
        ws = wb.create_sheet(sheet_name)

    wb.save(str(OUT))
    print(f"生成: {OUT}")


if __name__ == "__main__":
    main()
