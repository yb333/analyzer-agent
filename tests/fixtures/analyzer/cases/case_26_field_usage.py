"""Case 26: 字段使用信息 — 关联键 + 过滤 + 分组。

验证:
- join_usage: proj_id 出现在 ON 条件
- where_usage: status 出现在 WHERE
- groupby_usage: contract_no 出现在 GROUP BY
- 辅助字段: proj_id 不写入目标表但用作关联键
"""

rules = [
    {
        "rule_code": "UR026", "rule_type": 1, "exec_sequence": 0,
        "target_schema": "dws", "target_table": "dwb_usage_test_f",
        "delete_mode": "1", "delete_condition": "",
        "query_sql": """SELECT
    t.contract_no,
    t.amount,
    f.proj_name,
    SUM(t.amount) AS total_amount,
    'N' AS del_flag
FROM ods.main_tbl t
LEFT JOIN dim.dim_proj f ON t.proj_id = f.proj_id AND f.del_flag = 'N'
WHERE t.contract_no IS NOT NULL AND t.status = 'A'
GROUP BY t.contract_no, t.amount, f.proj_name""",
        "rule_group_code": "GR001", "rule_name": "字段使用测试",
    },
]

target_fields = [
    {"rule_code": "UR026", "target_field": "contract_no", "source_field": "contract_no"},
    {"rule_code": "UR026", "target_field": "amount", "source_field": "amount"},
    {"rule_code": "UR026", "target_field": "proj_name", "source_field": "proj_name"},
    {"rule_code": "UR026", "target_field": "total_amount", "source_field": "amount"},
    {"rule_code": "UR026", "target_field": "del_flag", "source_field": ""},
]

group_variables = []
