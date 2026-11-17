#!/usr/bin/env python3
"""dws-pipeline-analyzer — 制品包分析器（数据层 + CLI）。

三层架构（详见 architecture.md）：
    ① 数据层（analyzer.py）— 本模块：read_excel / CLI
    ② 理解引擎（engine.py）— SQL 理解与血缘解析
    ③ 任务层 — 文档化 / 字段检索 / 关联影响分析 / ...

本模块职责：
    - read_excel()  读取 execution_tasks.xlsx → rules / target_fields / 配置
    - main()        CLI 入口，调 engine.analyze_pipeline + 写文件
    - _generate_ai_summary()  AI 兜底摘要生成

Usage:
    python analyzer.py --input execution_tasks.xlsx --output docs/output/
    python analyzer.py --input execution_tasks.xlsx --output docs/ --ddl-dir ddl/

Author: 院博
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import openpyxl
except ImportError:
    print("错误: 需要 openpyxl。pip install openpyxl", file=sys.stderr)
    sys.exit(1)

try:
    import yaml  # noqa: F401（read_yml 内部 import，这里只做启动检查）
except ImportError:
    print("提示: PyYAML 未安装，代码仓 yml 输入将不可用。pip install pyyaml", file=sys.stderr)

# 引擎层（单向依赖：analyzer → engine）
import re
from engine import (
    analyze_pipeline,
    detect_dialect, parse_single_sql,
    ParsedSQL, RawRule,
    SELECT_RULE_TYPES, RULE_TYPE_MAP,
    _strip_dws_clauses, _replace_placeholders,
    _normalize_table_name, _norm_table, _is_intermediate_table,
    _table_match, _infer_layer, _clean_name,
)

# 输入装配层（单向依赖：analyzer → inputs；源发现与装配的唯一归属，见 docs/13）
# re-export 保证既有 `from analyzer import _discover_* / discover_i_view` 继续可用
# （过渡期，新代码请直接 from inputs import）。
from inputs import (  # noqa: F401
    _find_repo_root, _auto_discover_ddl_from_repo, _find_ddl_file,
    _discover_lts_from_repo, _parse_lts_task,
    _discover_dq_from_repo, _parse_dq_yml,
    _extract_view_sql, _find_view_by_name, _search_view_by_source, discover_i_view,
    append_i_view_step, apply_meta_enrichments, build_baseline_export_ctx,
    read_lts_excel, read_dq_excel,
)

# excel 列定位/取值三件套（唯一定义处在 excel_utils，此处保留旧名）
from excel_utils import safe_str as _safe_str, find_col as _find_col, get_val as _get_val  # noqa: F401

# ═══════════════════════════════════════════════════════════════
# Excel 列映射（read_excel 专用）
# ═══════════════════════════════════════════════════════════════

RULE_COLUMNS_MAP = {
    "rule_code": "规则编码",
    "exec_sequence": "执行序列",
    "target_schema": "目标Schema",
    "target_table": "目标表",
    "delete_mode": "删除模式",
    "delete_condition": "删除条件",
    "query_sql": "(生成的）查询语句1",
    "project_code": "项目编码",
    "data_source": "数据源",
    "business_owner": "业务责任人",
    "rule_group_code": "规则组编码",
    "rule_group_en": "规则组英文名称",
    "rule_type": "规则类型",
    "rule_name": "规则中文名称",
    "exchange_source_table": "交换分区来源表",
}

# 规则类型语义映射
RULE_TYPE_MAP = {
    1: "取数规则",
    2: "删数规则",
    3: "备份规则",
    4: "查询规则",
    5: "逻辑视图",
    6: "物理视图",
    7: "度量规则",
    8: "物理表规则",
    9: "分区交换",
    10: "SP规则",
    11: "API规则",
    12: "参数变量",
    13: "维护类",
    14: "Spark取数",
    15: "判断类",
}

# SELECT 类规则（需要完整解析 SQL + 字段映射 + 血缘）
SELECT_RULE_TYPES = {1, 14}

# 记录类规则（不解析 SQL，但记录操作信息）
RECORD_RULE_TYPES = {2, 9}

# 参数变量（记录到 variables，不算 step）
VARIABLE_RULE_TYPES = {12}

# 删除模式语义映射
DELETE_MODE_MAP = {
    "1": "TRUNCATE TABLE",
    "2": "NO DELETE (追加)",
    "3": "TRUNCATE SUBPARTITION",
    "4": "DELETE",
    "5": "TRUNCATE PARTITION",
    "6": "MERGE INTO",
    "7": "RPT_ITEM",
}

# 分区级删除模式（有分区场景标识）
PARTITION_DELETE_MODES = {"3", "5"}

# TargetFields sheet 列名
TF_COLUMNS_MAP = {
    "rule_code": "规则编码",
    "target_field": "目标字段名称",
    "source_field": "来源字段名称",
    "encryption": "加密方式",
    "alias": "别名",
    "field_type": "字段类型",
    "remark": "备注",
}

# GroupVariables sheet 列名
GV_COLUMNS_MAP = {
    "rule_code": "规则编码",
    "var_name": "动态参数/变量名",
    "default_value": "变量默认值",
}

# ═══════════════════════════════════════════════════════════════
# read_excel 专用辅助函数（其余工具函数已在 engine.py；
# 三件套 _safe_str/_find_col/_get_val 已收拢至 excel_utils 并在顶部 re-export）
# ═══════════════════════════════════════════════════════════════

def _read_query_sql(row: tuple, col_idx: dict) -> str:
    """读取查询语句（支持多列拼接）。

    超长 SQL 会分散在「查询语句1」「查询语句2」... 多列。
    按列序号拼接非空内容，最后一个非空列去掉末尾 \\r\\n。
    不 strip 中间列（保留列内的空格和换行，避免拼接时 SQL 断裂）。
    """
    import re as _re
    # 找所有「查询语句N」列，按 N 排序
    sql_cols = []
    for col_name, idx in col_idx.items():
        m = _re.match(r'.*查询语句\s*(\d+)', str(col_name))
        if m:
            sql_cols.append((int(m.group(1)), idx))
    sql_cols.sort()

    if not sql_cols:
        return ""

    # 直接读取原始值（不走 _get_val 的 strip），保留列内空格换行
    parts = []
    for _, idx in sql_cols:
        if idx is None or idx >= len(row):
            continue
        val = row[idx]
        if val is None:
            continue
        val_str = str(val)
        if val_str.strip():  # 整列只有空白/换行则跳过，但保留非空内容
            parts.append(val_str)

    if not parts:
        return ""

    # 完整拼接，保留所有原始字符（包括末尾 \r\n，它是完整 SQL 的一部分）
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════
# Step 1: read_excel()
# ═══════════════════════════════════════════════════════════════

def read_excel(excel_path: str) -> dict:
    """读取制品包 Excel，返回结构化数据。

    Returns: {
        "rules": [RawRule, ...],
        "target_fields": {"规则编码": [RawTargetField, ...]},
        "group_variables": {"规则编码": [RawGroupVariable, ...]},
        "variables": ["P_CYCLE_ID", ...],
        "rule_group_code": "GR123456",
    }
    """
    # read_only=False 更可靠（部分 Excel 文件在 read_only=True 时列读取不完整）
    wb = openpyxl.load_workbook(excel_path, read_only=False, data_only=True)
    result = {
        "rules": [],
        "target_fields": {},
        "group_variables": {},
        "variables": [],
        "rule_group_code": "",
        "rule_group_en": "",
    }

    # ── RULE sheet ──
    if "RULE" not in wb.sheetnames:
        print("错误: Excel 中没有 RULE sheet", file=sys.stderr)
        wb.close()
        return result

    ws = wb["RULE"]
    col_idx = {}
    for cell in next(ws.iter_rows(min_row=1, max_row=1, values_only=False)):
        if cell.value:
            col_idx[cell.value.strip()] = cell.column - 1

    # 映射列名到索引
    ci = {k: _find_col(col_idx, v) for k, v in RULE_COLUMNS_MAP.items()}
    col_rule_type = ci.get("rule_type")

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue

        rule_type_str = _get_val(row, col_rule_type)
        try:
            rt = int(float(rule_type_str)) if rule_type_str else 0
        except (ValueError, TypeError):
            rt = 0

        query = _read_query_sql(row, col_idx)
        exec_seq_str = _get_val(row, ci.get("exec_sequence"))
        try:
            # int(float()) 兼容数值、字符串 "1"、字符串 "1.0" 三种格式
            exec_seq = int(float(exec_seq_str)) if exec_seq_str else 0
        except (ValueError, TypeError):
            exec_seq = 0

        # 类型 12（参数变量）→ 记录到 variables
        if rt in VARIABLE_RULE_TYPES:
            var_name = _get_val(row, ci.get("rule_name")) or _get_val(row, ci.get("rule_code"))
            if var_name:
                result["variables"].append(var_name)
            continue

        # 类型 10/11/13/15（SP/API/维护/判断）→ 完全跳过
        if rt in {10, 11, 13, 15}:
            continue

        # 类型 1/14（取数/Spark取数）→ 完整解析，必须有 SQL
        # 类型 2/9（删数/分区交换）→ 记录操作，SQL 可选
        # 其他类型 → 记录但不解析

        rule = RawRule(
            rule_code=_get_val(row, ci.get("rule_code")),
            rule_name=_get_val(row, ci.get("rule_name")),
            rule_type=rt,
            exec_sequence=exec_seq,
            target_schema=_get_val(row, ci.get("target_schema")),
            target_table=_get_val(row, ci.get("target_table")),
            delete_mode=_get_val(row, ci.get("delete_mode")),
            delete_condition=_get_val(row, ci.get("delete_condition")),
            query_sql=query.strip() if query else "",
            project_code=_get_val(row, ci.get("project_code")),
            data_source=_get_val(row, ci.get("data_source")),
            business_owner=_get_val(row, ci.get("business_owner")),
            rule_group_code=_get_val(row, ci.get("rule_group_code")),
            rule_group_en=str(_get_val(row, ci.get("rule_group_en")) or "").strip(),
            exchange_source_table=_get_val(row, ci.get("exchange_source_table")),
        )

        # SELECT 类规则必须有 SQL
        if rt in SELECT_RULE_TYPES and not rule.query_sql:
            continue

        result["rules"].append(rule)

        if rule.rule_group_code and not result["rule_group_code"]:
            result["rule_group_code"] = rule.rule_group_code

        # 规则组英文名称（取第一个非空值，作为输出目录名）
        rule_group_en = _get_val(row, ci.get("rule_group_en"))
        if rule_group_en and not result["rule_group_en"]:
            result["rule_group_en"] = str(rule_group_en).strip()

    # ── TargetFields sheet ──
    if "TargetFields" in wb.sheetnames:
        ws_tf = wb["TargetFields"]
        tf_col_idx = {}
        for cell in next(ws_tf.iter_rows(min_row=1, max_row=1, values_only=False)):
            if cell.value:
                tf_col_idx[cell.value.strip()] = cell.column - 1

        tf_ci = {k: _find_col(tf_col_idx, v) for k, v in TF_COLUMNS_MAP.items()}

        for tf_row in ws_tf.iter_rows(min_row=2, values_only=True):
            if not tf_row:
                continue
            rc = _get_val(tf_row, tf_ci.get("rule_code"))
            tf = RawTargetField(
                rule_code=rc,
                target_field=_get_val(tf_row, tf_ci.get("target_field")),
                source_field=_get_val(tf_row, tf_ci.get("source_field")),
                encryption=_get_val(tf_row, tf_ci.get("encryption")),
                alias=_get_val(tf_row, tf_ci.get("alias")),
                field_type=_get_val(tf_row, tf_ci.get("field_type")),
                remark=_get_val(tf_row, tf_ci.get("remark")),
            )
            if rc:
                result["target_fields"].setdefault(rc, []).append(tf)

    # ── GroupVariables sheet ──
    if "GroupVariables" in wb.sheetnames:
        ws_gv = wb["GroupVariables"]
        gv_col_idx = {}
        for cell in next(ws_gv.iter_rows(min_row=1, max_row=1, values_only=False)):
            if cell.value:
                gv_col_idx[cell.value.strip()] = cell.column - 1

        gv_ci = {k: _find_col(gv_col_idx, v) for k, v in GV_COLUMNS_MAP.items()}
        all_vars = set()

        for gv_row in ws_gv.iter_rows(min_row=2, values_only=True):
            if not gv_row:
                continue
            rc = _get_val(gv_row, gv_ci.get("rule_code"))
            var_name = _get_val(gv_row, gv_ci.get("var_name"))
            gv = RawGroupVariable(
                rule_code=rc,
                var_name=var_name,
                default_value=_get_val(gv_row, gv_ci.get("default_value")),
            )
            if rc:
                result["group_variables"].setdefault(rc, []).append(gv)
            if var_name:
                all_vars.add(var_name)

        result["variables"] = sorted(all_vars)

    # v1.2 provenance：实际读到的 sheet 清单（文件+sheet 定位，装配层采集用；
    # 私有 key，main 读出后不流入 draft）
    result["_source_sheets"] = [s for s in ("RULE", "TargetFields", "GroupVariables")
                                if s in wb.sheetnames]

    wb.close()
    return result


# ═══════════════════════════════════════════════════════════════
# Step 1b: read_yml() — 代码仓 yml 加载（和 read_excel 产出完全一致）
# ═══════════════════════════════════════════════════════════════

# yml key → RawRule 字段的映射（和 RULE_COLUMNS_MAP 对应，值是 yml 里的中文 key）
# 注意查询语句的 key 容错：yml 用半角括号，Excel 用全角括号+后缀1
_YML_RULE_KEY_ALIASES = {
    "rule_code": ["规则编码"],
    "rule_name": ["规则中文名称"],
    "rule_type": ["规则类型"],
    "exec_sequence": ["执行序列"],
    "target_schema": ["目标Schema", "目标schema", "目标SCHEMA"],
    "target_table": ["目标表"],
    "delete_mode": ["删除模式"],
    "delete_condition": ["删除条件"],
    "query_sql": ["(生成的)查询语句", "(生成的）查询语句", "(生成的)查询语句1", "(生成的）查询语句1"],
    "project_code": ["项目编码"],
    "data_source": ["数据源"],
    "business_owner": ["业务责任人"],
    "rule_group_code": ["规则组编码"],
    "rule_group_en": ["规则组英文名称"],
    "exchange_source_table": ["交换分区来源表"],
}

_YML_TF_KEY_ALIASES = {
    "rule_code": ["规则编码"],
    "target_field": ["目标字段名称"],
    "source_field": ["来源字段名称"],
    "encryption": ["加密方式"],
    "alias": ["别名"],
    "field_type": ["字段类型"],
    "remark": ["备注"],
}

_YML_GV_KEY_ALIASES = {
    "rule_code": ["规则编码"],
    "var_name": ["动态参数/变量名", "变量名"],
    "default_value": ["变量默认值", "默认值"],
}


def _yml_get(d: dict, field: str, aliases: dict) -> str:
    """从 yml dict 按 alias 列表取值（容错多种 key 写法），返回字符串。"""
    for key in aliases.get(field, []):
        if key in d:
            val = d[key]
            return "" if val is None else str(val)
    return ""


def _parse_int(val: str) -> int:
    """字符串转 int（兼容 '1'/'1.0'/1 等格式，和 read_excel 的转换一致）。"""
    try:
        return int(float(val)) if val else 0
    except (ValueError, TypeError):
        return 0


def _yml_read_query_sql(data: dict) -> str:
    """从 yml dict 读取查询语句（支持多段拼接，和 read_excel 的 _read_query_sql 对齐）。

    真实 yml 里超长 SQL 会被拆成多条（和 Excel 多列一样）：
        (生成的)查询语句1: |-   ← 第一段
          WITH agg AS (...
        (生成的)查询语句2: |-   ← 第二段
          FROM ods.t WHERE...)

    本函数找所有「查询语句N」key，按 N 排序拼接。
    key 格式容错：半角/全角括号、有无后缀数字、| literal block scalar。
    """
    import re
    sql_parts = []  # [(序号, 内容)]
    for key, val in data.items():
        key_str = str(key)
        # 匹配「查询语句N」或「查询语句」（无数字视为1）
        m = re.match(r'.*查询语句\s*(\d*)', key_str)
        if m:
            num = int(m.group(1)) if m.group(1) else 1
            val_str = "" if val is None else str(val)
            if val_str.strip():
                sql_parts.append((num, val_str))
    if not sql_parts:
        return ""
    sql_parts.sort(key=lambda x: x[0])
    return "".join(part for _, part in sql_parts)


def read_yml(yml_dir: str, consumed_files: list = None) -> dict:
    """读取代码仓规则组目录下的 yml 文件，返回和 read_excel 完全一致的结构。

    一个规则组目录下有多个 *.yml 文件（一个 yml = 一条规则）。
    本函数遍历目录下所有 yml，合并为一个规则组，产出结构同 read_excel：
        {rules, target_fields, group_variables, variables, rule_group_code, rule_group_en}

    consumed_files: 可选出参列表，传入后把实际读取的 yml 绝对路径追加进去
        （ts_draft provenance 用）；不传时行为不变。

    yml 格式（详见 sample_rule.yml）：
        顶层 = RULE sheet 字段（中文 key）
        额外信息（其他sheet页信息）.TargetFields = TargetFields sheet
        额外信息（其他sheet页信息）.GroupVariables = GroupVariables sheet
    """
    import yaml

    yml_path = Path(yml_dir)
    result = {
        "rules": [],
        "target_fields": {},
        "group_variables": {},
        "variables": [],
        "rule_group_code": "",
        "rule_group_en": "",
    }

    # 收集目录下所有 yml 文件（一个 yml = 一条规则）
    yml_files = sorted(yml_path.glob("*.yml")) + sorted(yml_path.glob("*.yaml"))
    if not yml_files:
        print(f"错误: 目录下没有 yml 文件: {yml_dir}", file=sys.stderr)
        return result

    all_vars = set()

    for yf in yml_files:
        if consumed_files is not None:
            consumed_files.append(str(yf.resolve()))
        try:
            data = yaml.safe_load(yf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [yml解析错误] {yf.name}: {e}", file=sys.stderr)
            continue
        if not data or not isinstance(data, dict):
            continue

        # ── 解析 RULE 主信息 → RawRule ──
        rt = _parse_int(_yml_get(data, "rule_type", _YML_RULE_KEY_ALIASES))

        # 类型 12（参数变量）→ 记录到 variables，不作为规则
        if rt in VARIABLE_RULE_TYPES:
            var_name = _yml_get(data, "rule_name", _YML_RULE_KEY_ALIASES) or \
                       _yml_get(data, "rule_code", _YML_RULE_KEY_ALIASES)
            if var_name:
                all_vars.add(var_name)
            continue

        # 类型 10/11/13/15（SP/API/维护/判断）→ 跳过（和 read_excel 一致）
        if rt in {10, 11, 13, 15}:
            continue

        rule = RawRule(
            rule_code=_yml_get(data, "rule_code", _YML_RULE_KEY_ALIASES),
            rule_name=_yml_get(data, "rule_name", _YML_RULE_KEY_ALIASES),
            rule_type=rt,
            exec_sequence=_parse_int(_yml_get(data, "exec_sequence", _YML_RULE_KEY_ALIASES)),
            target_schema=_yml_get(data, "target_schema", _YML_RULE_KEY_ALIASES),
            target_table=_yml_get(data, "target_table", _YML_RULE_KEY_ALIASES),
            delete_mode=_yml_get(data, "delete_mode", _YML_RULE_KEY_ALIASES),
            delete_condition=_yml_get(data, "delete_condition", _YML_RULE_KEY_ALIASES),
            query_sql=_yml_read_query_sql(data).strip(),
            project_code=_yml_get(data, "project_code", _YML_RULE_KEY_ALIASES),
            data_source=_yml_get(data, "data_source", _YML_RULE_KEY_ALIASES),
            business_owner=_yml_get(data, "business_owner", _YML_RULE_KEY_ALIASES),
            rule_group_code=_yml_get(data, "rule_group_code", _YML_RULE_KEY_ALIASES),
            rule_group_en=_yml_get(data, "rule_group_en", _YML_RULE_KEY_ALIASES).strip(),
            exchange_source_table=_yml_get(data, "exchange_source_table", _YML_RULE_KEY_ALIASES),
        )

        # SELECT 类规则必须有 SQL
        if rt in SELECT_RULE_TYPES and not rule.query_sql:
            continue

        result["rules"].append(rule)

        if rule.rule_group_code and not result["rule_group_code"]:
            result["rule_group_code"] = rule.rule_group_code
        if rule.rule_group_en and not result["rule_group_en"]:
            result["rule_group_en"] = rule.rule_group_en

        # ── 解析额外信息（TargetFields / GroupVariables，有则读，无则跳过）──
        # 容错：真实 yml 里额外信息/TargetFields/GroupVariables 的冒号后可能有 |
        # （literal block scalar），导致 YAML 把它们解析成字符串而非 dict/list。
        # 遇到字符串时再 yaml.safe_load 一次，解析出真实结构。
        extra = data.get("额外信息（其他sheet页信息）") or data.get("额外信息") or {}
        if isinstance(extra, str):
            try:
                extra = yaml.safe_load(extra) or {}
            except Exception:
                extra = {}
        if not isinstance(extra, dict):
            extra = {}

        rc = rule.rule_code

        # TargetFields（值可能因 | 变成字符串，需再解析）
        tf_list = extra.get("TargetFields") or []
        if isinstance(tf_list, str):
            try:
                tf_list = yaml.safe_load(tf_list) or []
            except Exception:
                tf_list = []
        if isinstance(tf_list, list):
            for tf_item in tf_list:
                if not isinstance(tf_item, dict):
                    continue
                tf = RawTargetField(
                    rule_code=_yml_get(tf_item, "rule_code", _YML_TF_KEY_ALIASES) or rc,
                    target_field=_yml_get(tf_item, "target_field", _YML_TF_KEY_ALIASES),
                    source_field=_yml_get(tf_item, "source_field", _YML_TF_KEY_ALIASES),
                    encryption=_yml_get(tf_item, "encryption", _YML_TF_KEY_ALIASES),
                    alias=_yml_get(tf_item, "alias", _YML_TF_KEY_ALIASES),
                    field_type=_yml_get(tf_item, "field_type", _YML_TF_KEY_ALIASES),
                    remark=_yml_get(tf_item, "remark", _YML_TF_KEY_ALIASES),
                )
                tf_rc = tf.rule_code or rc
                if tf_rc:
                    result["target_fields"].setdefault(tf_rc, []).append(tf)

        # GroupVariables（值同样可能因 | 变成字符串，需再解析）
        gv_list = extra.get("GroupVariables") or []
        if isinstance(gv_list, str):
            try:
                gv_list = yaml.safe_load(gv_list) or []
            except Exception:
                gv_list = []
        if isinstance(gv_list, list):
            for gv_item in gv_list:
                if not isinstance(gv_item, dict):
                    continue
                gv = RawGroupVariable(
                    rule_code=_yml_get(gv_item, "rule_code", _YML_GV_KEY_ALIASES) or rc,
                    var_name=_yml_get(gv_item, "var_name", _YML_GV_KEY_ALIASES),
                    default_value=_yml_get(gv_item, "default_value", _YML_GV_KEY_ALIASES),
                )
                gv_rc = gv.rule_code or rc
                if gv_rc:
                    result["group_variables"].setdefault(gv_rc, []).append(gv)
                if gv.var_name:
                    all_vars.add(gv.var_name)

    result["variables"] = sorted(all_vars)
    return result


# ═══════════════════════════════════════════════════════════════
# 源发现与装配（LTS/DQ/I 视图/DDL/仓根定位 + 装配函数）已平移至 inputs.py
# （输入装配层，docs/13 波 1a；analyzer 顶部已 re-export，行为零变）。
# ═══════════════════════════════════════════════════════════════

def generate_ai_summary(knowledge, rules, parsed_map, topology, field_mappings, quality, data_flow) -> str:
    """公开 API（batch 等外部模块用）。内部逻辑同 _generate_ai_summary。"""
    return _generate_ai_summary(knowledge, rules, parsed_map, topology,
                                field_mappings, quality, data_flow)


def _trunc_text(s: str, n: int) -> str:
    """文本截断：超长加省略号（摘要/inspect 里所有原文片段统一走这里）。"""
    s = (s or "").strip()
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def _cond_text(clause: str, n: int, keyword: str = "WHERE") -> str:
    """条件原文展示：剥掉行首 SQL 关键字（WHERE/HAVING）再截断，避免「WHERE: WHERE …」。"""
    s = (clause or "").strip()
    s = re.sub(rf"^\s*{keyword}\s+", "", s, flags=re.IGNORECASE)
    return _trunc_text(s, n)


def _ai_col_names(cols, cap: int = 30) -> str:
    """列名清单（名字全给原则：默认 30 个，超出提示总数走 inspect）。"""
    names = [c.alias or _trunc_text(c.expression, 30) for c in cols]
    shown = ", ".join(names[:cap])
    if len(names) > cap:
        shown += f", …共{len(names)}列"
    return shown


def _ai_block_desc(blk: dict) -> str:
    """逻辑块单行描述：表(别名) JOIN类型 ON条件 [操作] 带出字段。"""
    table = blk.get("table", "")
    alias = blk.get("alias", "")
    jt = blk.get("join_type", "")
    on_cond = blk.get("on_condition", "")
    ops = blk.get("ops", [])
    brought = [f["name"] if isinstance(f, dict) else f
               for f in (blk.get("brought_fields") or [])]
    seg = table + (f"({alias})" if alias else "")
    if jt and jt != "FROM":
        seg += f" {jt}"
    if on_cond:
        seg += f" ON {_trunc_text(on_cond, 60)}"
    if ops:
        seg += f" [{', '.join(ops[:3])}]"
    if brought:
        seg += f" 带出: {_trunc_text(', '.join(brought), 100)}"
    return seg


def _generate_ai_summary(knowledge, rules, parsed_map, topology, field_mappings, quality, data_flow) -> str:
    """生成 AI 增强用的自足摘要 markdown。

    契约（2026-11-11 渐进式披露升级，替代旧"只读 2-4KB 摘要、不读大 JSON"）：
    - 摘要自足承载 Step 2 任务（块目的+关键字段）的 80%——**名字全给**
      （字段/表/块/CTE），**短内容直给**（条件原文、加工列表达式，超长截断）
    - 长内容（完整 SQL/全量血缘/全块递归）走 `run.py inspect` 定向下钻，
      摘要头部给出用法——AI 不需要也不应该自己写 python 读大 JSON
      （Windows PowerShell 下编码/引号不稳，且易读错结构）
    """
    run_py = Path(__file__).resolve().parent.parent / "run.py"
    lines = []
    lines.append("# ETL 分析摘要（AI 增强用）")
    lines.append("")
    lines.append("> AI 请基于以下信息，补充每个步骤的业务目的和加工逻辑：")
    lines.append("> 1. 每个块的业务目的（块目的）——每块已给出表/关联条件/带出字段")
    lines.append("> 2. 整个步骤的业务目的（块目的的组合概括）")
    lines.append("> 3. 关键字段的业务含义（目标字段清单+DDL注释已列出）")
    lines.append("> 输出格式见文末模板，保存为 knowledge_ai.md")
    lines.append("> 需要更多细节（某步完整SQL/某字段血缘/某表达式）时用只读探查入口，")
    lines.append("> **不要自己写 python/jq 读 knowledge_draft.json**（跨平台不稳且易读错结构）：")
    lines.append(f">   python {run_py} inspect --draft <本文件同目录>/knowledge_draft.json --step step_1 --part sql")
    lines.append(">   选择器: --step X --part sql|blocks|fields|where|joins|ctes | --field 名 | --table 名 | --search 关键字 | --schema")
    lines.append("")

    # ── 基本信息 ──
    meta = knowledge.get("meta", {})
    lines.append(f"## 基本信息")
    lines.append(f"- 目标表: {meta.get('target_table', '')}")
    if meta.get("table_comment"):
        lines.append(f"- 表注释: {meta['table_comment']}")
    lines.append(f"- 规则数: {len(rules)}")
    lines.append(f"- 加工模式: {', '.join(p.get('label','') for p in meta.get('patterns', []))}")

    # 目标字段清单（名字全给 + DDL 类型/注释——「关键字段」任务的直接输入）
    tft = meta.get("target_field_types", {}) or {}
    tfc = meta.get("target_field_comments", {}) or {}
    field_names = list(tfc.keys()) or list(tft.keys())
    if not field_names:
        # 无 DDL 时回退 field_mappings 的目标字段（去重保序）
        seen = set()
        field_names = [f["target_field"] for f in field_mappings.get("fields", [])
                       if f.get("target_field")
                       and not (f["target_field"].lower() in seen or seen.add(f["target_field"].lower()))]
    if field_names:
        lines.append(f"- 目标字段({len(field_names)}):")
        segs = []
        for n in field_names:
            seg = n + (f"({tft[n]})" if tft.get(n) else "") + (f"[{tfc[n]}]" if tfc.get(n) else "")
            segs.append(seg)
        # 宽表折行（≤96字符/行）而不是逐字段占行
        cur = ""
        for seg in segs:
            if cur and len(cur) + 2 + len(seg) > 96:
                lines.append(f"  - {cur}")
                cur = seg
            else:
                cur = f"{cur}, {seg}" if cur else seg
        if cur:
            lines.append(f"  - {cur}")
    lines.append("")

    # ── 场景结构 ──
    scenarios = topology.get("scenarios", [])
    if scenarios:
        lines.append("## 场景结构")
        for sc in scenarios:
            label = "公共步骤" if sc.get("is_common") else sc["name"]
            lines.append(f"- {label}: {sc['rule_count']} 个规则 ({', '.join(sc['rule_codes'])})")
        lines.append("")

    # ── 步骤详情（名字全给、内容截断；超阈值降级为压缩行而非整段消失）──
    lines.append("## 步骤详情")
    SUMMARY_MAX_LINES = 300  # 总量控制阈值（4-10KB 预算）
    for rule in rules:
        parsed = parsed_map.get(rule.rule_code)
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        sid = step["step_id"] if step else ""
        rt_label = RULE_TYPE_MAP.get(rule.rule_type, "")
        dm_label = DELETE_MODE_MAP.get((rule.delete_mode or "").strip(), "")
        dc = rule.delete_condition or ""

        # 总量控制：超阈值降级为压缩行（表链+列数+探查指引），不再整段消失
        if len(lines) > SUMMARY_MAX_LINES:
            lines.append(f"### {sid} ({rule.rule_code}) {rule.rule_name or ''}")
            if parsed:
                src_tables = [j.source_table for j in parsed.source_tables]
                lines.append(f"- {rt_label}: {' → '.join(src_tables[:3])} → {rule.target_table}"
                             f" | {len(parsed.select_columns)}列 | 超出摘要阈值已压缩"
                             f"（细节: inspect --step {sid}）")
            else:
                lines.append(f"- 超出摘要阈值已压缩（细节: inspect --step {sid}）")
            lines.append("")
            continue

        # 兜底描述
        auto_desc = next((d for d in knowledge.get("business_logic", {}).get("step_descriptions", [])
                         if d.get("step_id") == sid), {})

        lines.append(f"### {sid} ({rule.rule_code}) {rule.rule_name or ''}")

        # 判断步骤复杂度（决定展开程度）
        join_count = len([j for j in parsed.source_tables]) if parsed else 0
        cte_count = len(parsed.ctes) if parsed else 0
        is_simple = join_count <= 1 and cte_count == 0  # 单表直取 = 简单

        if is_simple and parsed:
            # 简单步骤压缩：来源→目标+列数+字段名+写入方式+过滤，一行搞定
            src_tables = [j.source_table for j in parsed.source_tables]
            tt_dist = Counter(c.transform_type for c in parsed.select_columns)
            tt_str = ", ".join(f"{k}={v}" for k, v in tt_dist.most_common())
            line = f"- {rt_label}: {' → '.join(src_tables[:3])} → {rule.target_table}"
            line += f" | {len(parsed.select_columns)}列({tt_str})"
            line += f": {_ai_col_names(parsed.select_columns)}"
            line += f" | {dm_label}" + (f"[{_trunc_text(dc, 60)}]" if dc else "")
            if parsed.where_clause:
                line += f" | WHERE {_cond_text(parsed.where_clause, 160)}"
            lines.append(line)
        else:
            # 复杂步骤展开（CTE/条件原文/字段清单/逻辑块带出——名字全给）
            lines.append(f"- 类型: {rt_label} | 写入: {dm_label}" + (f" [{_trunc_text(dc, 80)}]" if dc else ""))
            if rule.rule_type == 9 and rule.exchange_source_table:
                lines.append(f"- 分区交换: {rule.target_table} → {rule.exchange_source_table}")
            if parsed:
                src_tables = [j.source_table for j in parsed.source_tables]
                src_str = ", ".join(src_tables[:8]) + (f" (+{len(src_tables) - 8})" if len(src_tables) > 8 else "")
                lines.append(f"- 来源表: {src_str}")
                # CTE：名字+来源表+WHERE（CTE 的过滤语义常被漏看）
                if parsed.ctes:
                    for cte in parsed.ctes[:6]:
                        cte_srcs = [ct.get("name", "") if isinstance(ct, dict) else str(ct)
                                    for ct in cte.source_tables[:3]]
                        seg = f"- CTE {cte.name}: 源 {', '.join(s for s in cte_srcs if s)}"
                        # CTE 内部加工表达式（块目的推理的核心证据）
                        cte_exprs = [f for f in (cte.fields or [])
                                     if isinstance(f, dict)
                                     and f.get("transform_type") not in ("direct", "", None)]
                        if cte_exprs:
                            seg += " | " + ", ".join(
                                f"{f.get('name', '')}={_trunc_text(f.get('expression', ''), 60)}"
                                for f in cte_exprs[:3])
                        if cte.where_clause:
                            seg += f" | WHERE {_cond_text(cte.where_clause, 120)}"
                        lines.append(seg)
                    if len(parsed.ctes) > 6:
                        lines.append(f"- …共 {len(parsed.ctes)} 个 CTE（inspect --step {sid} --part ctes）")
                # 条件原文（过滤/分组/聚合后过滤——块目的推理的直接证据）
                if parsed.where_clause:
                    lines.append(f"- WHERE: {_cond_text(parsed.where_clause, 240)}")
                if parsed.group_by:
                    lines.append(f"- GROUP BY: {_trunc_text(', '.join(parsed.group_by), 200)}")
                if parsed.having_clause:
                    lines.append(f"- HAVING: {_cond_text(parsed.having_clause, 160, 'HAVING')}")
                # 字段清单：直取列只列名；加工列逐个附表达式
                direct_cols = [c for c in parsed.select_columns if c.transform_type == "direct"]
                expr_cols = [c for c in parsed.select_columns if c.transform_type != "direct"]
                if direct_cols:
                    lines.append(f"- 直取列({len(direct_cols)}): {_trunc_text(_ai_col_names(direct_cols, 40), 240)}")
                for c in expr_cols[:15]:
                    nm = c.alias or _trunc_text(c.expression, 30)
                    lines.append(f"- 加工列 {nm} = {_trunc_text(c.expression, 90)} [{c.transform_type}]")
                if len(expr_cols) > 15:
                    lines.append(f"- …共 {len(expr_cols)} 个加工列（inspect --step {sid} --part fields）")
                # SQL 关键特征计数
                features = []
                if join_count > 1:
                    features.append(f"{join_count} JOIN")
                if cte_count:
                    features.append(f"{cte_count} CTE")
                where_count = len(parsed.where_usage) if hasattr(parsed, 'where_usage') else 0
                if where_count:
                    features.append(f"{where_count} 条件字段")
                if parsed.union_branches:
                    features.append(f"{len(parsed.union_branches)} UNION分支")
                if features:
                    lines.append(f"- SQL特征: {', '.join(features)}")

            # SQL 注释（如果有，帮助 AI 理解业务含义）
            if rule.query_sql and parsed and not parsed.parse_error:
                comments = re.findall(r'/\*\s*(.*?)\s*\*/', rule.query_sql)
                if comments:
                    lines.append(f"- SQL注释: {'; '.join(comments[:3])}")

            # 逻辑块（编号与文末模板对齐；每块附关联条件与带出字段）
            df_step = next((s for s in data_flow.get("steps", []) if s.get("step_id") == sid), None)
            if df_step and df_step.get("data_blocks"):
                blocks = df_step["data_blocks"]
                lines.append(f"- 逻辑块({len(blocks)}):")
                emitted = 0
                for idx, blk in enumerate(blocks[:8]):
                    if emitted >= 12:
                        lines.append(f"  - …其余块见 inspect --step {sid} --part blocks")
                        break
                    lines.append(f"  - 块{idx + 1}: {_ai_block_desc(blk)}")
                    emitted += 1
                    for child in (blk.get("children") or [])[:3]:
                        if emitted >= 12:
                            break
                        lines.append(f"    └ {_ai_block_desc(child)}")
                        emitted += 1
                if len(blocks) > 8:
                    lines.append(f"  - …共 {len(blocks)} 个顶层块")

        # 兜底描述（脚本已生成）
        if auto_desc.get("purpose"):
            lines.append(f"- 脚本兜底: {auto_desc['purpose']}")
        lines.append("")

    # ── 质量问题 ──
    issues = quality.get("issues", [])
    if issues:
        lines.append("## 质量问题")
        for iss in issues[:10]:
            lines.append(f"- [{iss.get('severity','')}] {iss.get('title','')}")
        lines.append("")

    # ── AI 输出模板 ──
    lines.append("---")
    lines.append("")
    lines.append("## AI 输出模板（按此格式输出，保存为 knowledge_ai.md）")
    lines.append("")
    lines.append("```markdown")
    lines.append("# 整体描述")
    lines.append("（2-3句话描述这个ETL是干什么的）")
    lines.append("")
    for rule in rules:
        step = next((s for s in topology["steps"] if s["rule_code"] == rule.rule_code), None)
        sid = step["step_id"] if step else ""
        lines.append(f"## {sid}")
        lines.append(f"（基于以下逻辑块推理这步的业务目的，1-2句话）")
        lines.append(f"### 块目的")
        lines.append(f"（为每个块补充业务目的，格式: - 块N (角色 表): 目的）")
        # 列出该步骤的逻辑块，让 AI 为每个块补充目的
        df_step_tpl = next((s for s in data_flow.get("steps", []) if s.get("step_id") == sid), None)
        if df_step_tpl and df_step_tpl.get("data_blocks"):
            for idx, blk in enumerate(df_step_tpl["data_blocks"]):
                _append_block_template(lines, blk, idx, indent=0)
        lines.append("")
    lines.append("## 关键字段")
    lines.append("- 字段名: 业务含义")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="dws-pipeline-analyzer — 制品包深度分析器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", default="", help="execution_tasks.xlsx 文件路径")
    parser.add_argument("--output", default="", help="输出基础目录（脚本会在此目录下按规则组英文名称建子目录）")
    parser.add_argument("--dialect", default="", help="SQL 方言 (oracle/dws/auto)，默认自动检测")
    parser.add_argument("--ddl-dir", default="", help="DDL 文件目录（可选，用于补充字段类型）")
    parser.add_argument("--lts", default="",
                        help="LTS 调度任务 excel 路径（平台导出三 sheet：tasks/jobs/taskParams；"
                             "xlsx/yml 场景均可，优先于代码仓自动发现）")
    parser.add_argument("--dq", default="",
                        help="DQ 质量规则 excel 路径（平台导出，主 sheet「规则基础信息」；"
                             "xlsx/yml 场景均可，优先于代码仓自动发现）")
    args = parser.parse_args()

    if not args.input:
        parser.error("--input 必填（agent 编排调用请用 facts 入口：run.py facts --dir X --output Y）")

    # ── 运营埋点：记录开始时刻 + 预备上下文 ──
    import time as _t_usage
    import uuid as _uuid_usage
    _t0_usage = _t_usage.time()
    _trace_id = f"trace_{int(_t0_usage)}_{_uuid_usage.uuid4().hex[:8]}"
    _usage_exc = None  # 捕获异常类型（成功时为 None）

    input_path = Path(args.input)
    base_output_dir = Path(args.output)

    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)


    # ── 输入形态自识别（三形态一个入口，2026-11-04）──
    #   制品目录（无 yml）→ 分拣装配（pipeline_core，facts/spec 同核）
    #   代码仓规则组目录（含 yml）→ read_yml（legacy，代码仓结构后续会变、届时下线）
    #   xlsx 文件 → read_excel
    mode = _detect_input_mode(input_path)
    if mode == "artifact_dir":
        # 制品目录直入：LTS/DQ/DDL/I 视图全自动识别，用户无需任何附加参数
        print(f"=== dws-pipeline-analyzer（制品目录直入）===")
        print(f"输入: {input_path}")
        print(f"输出基础目录: {base_output_dir}")
        print()
        _finish_analysis(args, _assemble_artifact_dir(input_path), base_output_dir,
                         _t0_usage, _trace_id)
        return
    print(f"=== dws-pipeline-analyzer ===")
    print(f"输入: {input_path}（{'代码仓 yml' if mode == 'yml_dir' else 'xlsx'}）")
    print(f"输出基础目录: {base_output_dir}")
    print()

    # ── Step 1: 读取输入 ──
    # 输入分流：.xlsx 文件 → read_excel；目录 → read_yml（代码仓 yml，legacy）
    is_yml_mode = (mode == "yml_dir")
    consumed_yml_files = []  # 实际读取的 yml 清单（ts_draft provenance 用）
    input_sheets = None      # xlsx 实际读到的 sheet 清单（v1.2 provenance[].sheets 用）
    if is_yml_mode:
        print("Step 1: 读取代码仓 yml...")
        raw = read_yml(str(input_path), consumed_files=consumed_yml_files)
    else:
        print("Step 1: 读取制品包 Excel...")
        raw = read_excel(str(input_path))
        input_sheets = raw.pop("_source_sheets", None)
    rules = raw["rules"]

    # 确定输出目录：基础目录 / 规则组英文名称（兜底用规则组编码或 output）
    group_en = (raw.get("rule_group_en") or "").strip()
    if not group_en:
        group_en = (raw.get("rule_group_code") or "").strip() or "output"
    # 清理目录名（去掉非法字符）
    safe_group_en = re.sub(r'[<>:"/\\|?*\s]', "_", group_en)
    output_dir = base_output_dir / safe_group_en
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {output_dir}")
    print()

    if not rules and not is_yml_mode:
        # 详细诊断
        print("错误: 未找到有效的 RULE 行", file=sys.stderr)
        print("", file=sys.stderr)
        print("诊断信息:", file=sys.stderr)

        # 检查 RULE sheet 是否存在
        wb_diag = openpyxl.load_workbook(str(input_path), read_only=False, data_only=True)
        if "RULE" not in wb_diag.sheetnames:
            print(f"  - Excel 中没有 'RULE' sheet，实际 sheet: {wb_diag.sheetnames}", file=sys.stderr)
        else:
            ws_diag = wb_diag["RULE"]
            diag_headers = []
            for cell in next(ws_diag.iter_rows(min_row=1, max_row=1, values_only=False)):
                if cell.value:
                    diag_headers.append(str(cell.value).strip())

            data_rows = 0
            skipped_no_sql = 0
            skipped_rule_type = []
            for row in ws_diag.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                data_rows += 1

            # 找规则类型列
            rt_col_idx = None
            for h_idx, h in enumerate(diag_headers):
                if "规则类型" in h:
                    rt_col_idx = h_idx
                    break

            # 找查询语句列
            sql_col_idx = None
            for h_idx, h in enumerate(diag_headers):
                if "查询语句" in h:
                    sql_col_idx = h_idx
                    break

            print(f"  - RULE sheet 存在，数据行数: {data_rows}", file=sys.stderr)
            print(f"  - 表头列数: {len(diag_headers)}", file=sys.stderr)

            if rt_col_idx is None:
                print(f"  - [FAIL] 找不到 '规则类型' 列！表头: {diag_headers[:10]}", file=sys.stderr)
            else:
                print(f"  - 规则类型列 idx={rt_col_idx} ('{diag_headers[rt_col_idx]}')", file=sys.stderr)
                # 检查规则类型值
                rt_values = set()
                for row in ws_diag.iter_rows(min_row=2, values_only=True):
                    if row and rt_col_idx < len(row):
                        rt_val = row[rt_col_idx]
                        rt_values.add(str(rt_val) if rt_val is not None else "None")
                print(f"  - 规则类型值: {rt_values}", file=sys.stderr)

            if sql_col_idx is None:
                print(f"  - [FAIL] 找不到 '查询语句' 列！表头含'查询': {[h for h in diag_headers if '查询' in h or 'sql' in h.lower()]}", file=sys.stderr)
            else:
                print(f"  - 查询语句列 idx={sql_col_idx} ('{diag_headers[sql_col_idx]}')", file=sys.stderr)
                # 检查 SQL 是否为空
                empty_sql = 0
                for row in ws_diag.iter_rows(min_row=2, values_only=True):
                    if row and sql_col_idx < len(row):
                        sql_val = row[sql_col_idx]
                        if not sql_val or not str(sql_val).strip():
                            empty_sql += 1
                if empty_sql > 0:
                    print(f"  - [WARN] {empty_sql} 行的 SQL 为空", file=sys.stderr)

        wb_diag.close()
        sys.exit(1)

    print(f"  RULE 行: {len(rules)}")
    print(f"  TargetFields: {sum(len(v) for v in raw['target_fields'].values())} 行")
    print(f"  GroupVariables: {sum(len(v) for v in raw['group_variables'].values())} 行")
    print(f"  变量: {raw['variables']}")
    print()

    # ── Step 2: 方言检测 ──
    dialect = args.dialect
    if not dialect or dialect == "auto":
        sql_texts = [r.query_sql for r in rules if r.query_sql]
        dialect = detect_dialect(sql_texts)
    print(f"Step 2: 方言 = {dialect}")
    print()

    # ── Step 2b: I 视图发现（yml 场景，资产是 I 视图不是 F 表）──
    # F 表是加工底表，I 视图是对外资产。发现 I 视图后追加为链路最后一步（F→I），
    # 使完整链路为 ODS→F表→I视图。找不到 I 视图→以 F 表为终点（容错，不阻塞）。
    # 发现+追加+记录全在 inputs.append_i_view_step（装配层）；i_view_info 后续经
    # apply_meta_enrichments 注入 knowledge.meta.asset_info
    i_view_info = None
    if is_yml_mode and rules:
        i_view_info = append_i_view_step(raw, input_path)
        rules = raw["rules"]

    # ── DDL 发现 ──
    # yml 场景：从代码仓根定位 DDL/{DWS_EDW|DWS_RT_EDW}/{schema}/table/{target_table}.sql
    # xlsx 场景：不自动发现（xlsx 是临时导出，DDL 位置不统一），需 --ddl-dir 显式指定
    ddl_dir = args.ddl_dir
    _t_ddl = 0
    if not ddl_dir and is_yml_mode:
        _t_ddl0 = _t_usage.time()
        ddl_dir = _auto_discover_ddl_from_repo(input_path, rules)
        _t_ddl = round(_t_usage.time() - _t_ddl0, 2)

    # LTS 调度来源：--lts 显式 excel（loader，优先）→ yml 场景代码仓自动发现
    schedule_info = None
    _t_lts = 0
    if args.lts:
        _t_lts0 = _t_usage.time()
        schedule_info = read_lts_excel(args.lts, raw.get("rule_group_code") or "")
        _t_lts = round(_t_usage.time() - _t_lts0, 2)
        if schedule_info:
            f_names = [t["task_name"] for t in schedule_info.get("f_tasks", [])]
            i_names = [t["task_name"] for t in schedule_info.get("i_tasks", [])]
            print(f"  [LTS] 调度任务(excel): F={f_names} I={i_names} ({_t_lts}s)")
        else:
            print(f"  [LTS] excel 未读到任务: {args.lts}")
    elif is_yml_mode and raw.get("rule_group_code"):
        try:
            _t_lts0 = _t_usage.time()
            schedule_info = _discover_lts_from_repo(input_path, raw["rule_group_code"])
            _t_lts = round(_t_usage.time() - _t_lts0, 2)
            if schedule_info:
                f_names = [t["task_name"] for t in schedule_info.get("f_tasks", [])]
                i_names = [t["task_name"] for t in schedule_info.get("i_tasks", [])]
                print(f"  [LTS] 调度任务: F={f_names} I={i_names} ({_t_lts}s)")
        except Exception:
            pass

    # ── Step 3~7: 核心解析（与批量路径共用 analyze_pipeline，避免两套逻辑漂移）──
    print("Step 3-7: 解析 + 组装 knowledge...")
    # 引擎导出上下文（table_catalog 等，不落盘）。引擎接口不知道消费场景，
    # context_out 始终导出、由装配层持有，投影（ts_draft 等）按需消费。
    engine_ctx = {}
    knowledge, parsed_map = analyze_pipeline(
        rules, raw["target_fields"], raw["group_variables"], dialect,
        ddl_dir=ddl_dir, source_file=input_path.name,
        rule_group_code=raw["rule_group_code"],
        context_out=engine_ctx,
    )
    # 从 knowledge 取回 AI summary 需要的中间结构
    topology = knowledge["topology"]
    data_flow = knowledge["data_flow"]
    field_mappings = knowledge["field_mappings"]
    quality = knowledge["quality"]
    target_name = knowledge["meta"]["target_table"]
    # DQ 来源：--dq 显式 excel（loader，优先）→ yml 场景代码仓自动发现
    dq_rules = None
    _t_dq = 0
    if args.dq:
        _t_dq0 = _t_usage.time()
        dq_rules = read_dq_excel(args.dq)
        _t_dq = round(_t_usage.time() - _t_dq0, 2)
        if dq_rules:
            print(f"  [DQ] 发现 {len(dq_rules)} 条质量检查规则(excel) ({_t_dq}s)")
        else:
            print(f"  [DQ] excel 未读到有效规则: {args.dq}")
    elif is_yml_mode and target_name:
        try:
            _t_dq0 = _t_usage.time()
            dq_rules = _discover_dq_from_repo(input_path, target_name)
            _t_dq = round(_t_usage.time() - _t_dq0, 2)
            if dq_rules:
                print(f"  [DQ] 发现 {len(dq_rules)} 条质量检查规则 ({_t_dq}s)")
        except Exception:
            pass
    # 装配信息注入 meta（注入有主：asset_info/schedule/dq_rules 唯一写入点在 inputs）
    apply_meta_enrichments(knowledge, i_view_info, schedule_info, dq_rules)
    stats = field_mappings["statistics"]
    print(f"  步骤数: {len(rules)}, 字段数: {stats['total_in_sql']}, "
          f"问题数: {len(quality['issues'])}")
    print()

    _finish_analysis(
        args,
        {"raw": raw, "rules": rules, "knowledge": knowledge,
         "parsed_map": parsed_map, "engine_ctx": engine_ctx,
         "input_sheets": input_sheets, "consumed_yml_files": consumed_yml_files,
         "is_yml_mode": is_yml_mode,
         "input_type": "yml_dir" if is_yml_mode else "xlsx",
         "input_path": input_path},
        base_output_dir, _t0_usage, _trace_id,
        t_ddl=_t_ddl, t_lts=_t_lts)


def _detect_input_mode(input_path: Path) -> str:
    """输入形态自识别：artifact_dir（制品目录）/ yml_dir（代码仓，legacy）/ xlsx。

    识别职责在脚本侧（用户只管把资产制品放一个目录，参考 facts 自识别）：
    - 目录含 *.yml/*.yaml → 代码仓规则组（read_yml；代码仓结构后续会变，届时下线）
    - 目录无 yml → 制品目录（assemble_artifact_dir 分拣，facts/spec 同核）
    - 文件 → xlsx 制品包（read_excel）
    """
    if input_path.is_dir():
        has_yml = any(input_path.glob("*.yml")) or any(input_path.glob("*.yaml"))
        return "yml_dir" if has_yml else "artifact_dir"
    return "xlsx"


def _assemble_artifact_dir(input_path: Path) -> dict:
    """制品目录直入：分拣→读取→引擎→增强一条链（pipeline_core 唯一定义）。

    与 facts/spec 同核：术加必需（硬闸），LTS/DQ/DDL/I 视图全自动识别。
    """
    from pipeline_core import run_from_dir, AssemblyError
    try:
        core = run_from_dir(str(input_path), include_dq=True, channel="analyze",
                            entry_title="")
    except AssemblyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    return {
        "raw": core["raw"], "rules": core["rules"],
        "knowledge": core["knowledge"], "parsed_map": core["parsed_map"],
        "engine_ctx": core["engine_ctx"], "input_sheets": core["input_sheets"],
        "consumed_yml_files": [], "is_yml_mode": False,
        "input_type": "artifact_dir",
        "input_path": core["input_path"],   # 术加制品包路径（provenance 用）
    }


def _finish_analysis(args, bundle: dict, base_output_dir: Path,
                     t0: float, trace_id: str,
                     t_ddl: float = 0.0, t_lts: float = 0.0):
    """共尾：输出目录 + 过程件落 _internal + 视图一条命令跑到底 + 埋点。

    产物分层（2026-11-04 目录规整）：交付物（mapping.xlsx / asset_report.html /
    tech_design.md）在输出根目录；knowledge 过程件（knowledge_draft.json /
    knowledge_summary.md / knowledge_ai.md）进 _internal/——AI 增强从必做中间步
    降级为可选精修（写 knowledge_ai.md 后重跑 view_generator 覆盖）。
    """
    import time as _t

    knowledge = bundle["knowledge"]
    raw = bundle["raw"]
    rules = bundle["rules"]
    parsed_map = bundle["parsed_map"]
    engine_ctx = bundle["engine_ctx"]
    input_path = bundle["input_path"]
    is_yml_mode = bundle["is_yml_mode"]

    topology = knowledge["topology"]
    data_flow = knowledge["data_flow"]
    field_mappings = knowledge["field_mappings"]
    quality = knowledge["quality"]
    target_name = knowledge["meta"]["target_table"]
    stats = field_mappings["statistics"]

    # 性能诊断：读各阶段耗时（在写 JSON 前读，_timings 不写进 JSON）
    timings = knowledge.get("meta", {}).get("_timings", {})
    _t_parse = round(sum(timings.values()), 2) if timings else 0
    knowledge["meta"].pop("_timings", None)

    # 输出目录：基础目录/规则组英文名；过程件进 _internal/
    group_en = (raw.get("rule_group_en") or "").strip()
    if not group_en:
        group_en = (raw.get("rule_group_code") or "").strip() or "output"
    safe_group_en = re.sub(r'[<>:"/\\|?*\s]', "_", group_en)
    output_dir = base_output_dir / safe_group_en
    internal_dir = output_dir / "_internal"
    internal_dir.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {output_dir}")

    _t_json0 = _t.time()
    output_file = internal_dir / "knowledge_draft.json"
    output_file.write_text(
        json.dumps(knowledge, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )

    # ── 生成 AI 增强用摘要 ──
    summary_file = internal_dir / "knowledge_summary.md"
    summary_text = _generate_ai_summary(knowledge, rules, parsed_map, topology,
                                        field_mappings, quality, data_flow)
    summary_file.write_text(summary_text, encoding="utf-8", newline="\n")
    _t_json_summary = round(_t.time() - _t_json0, 2)

    # ── 视图一条命令跑到底（2026-11-04：analyze 不再依赖后续 AI 中间步）──
    print("Step 8: 生成视图...")
    _t_view0 = _t.time()
    _view_exc = None
    try:
        from view_generator import (generate_mapping, generate_asset_report,
                                    generate_tech_design)
        generate_mapping(knowledge, str(output_dir), engine_ctx=engine_ctx)
        generate_asset_report(knowledge, str(output_dir))
        generate_tech_design(knowledge, str(output_dir))
    except Exception as e:
        _view_exc = e
        print(f"  [WARN] 视图生成失败: {e}", file=sys.stderr)
    _t_view = round(_t.time() - _t_view0, 2)


    print(f"\n=== 完成 ===")
    print(f"输出: {output_dir}")
    print(f"  交付物: mapping.xlsx / asset_report.html / tech_design.md")
    print(f"  过程件: _internal/knowledge_draft.json, _internal/knowledge_summary.md")
    print(f"目标表: {target_name}")
    print(f"步骤数: {len(rules)}")
    print(f"字段数: {stats['total_in_sql']}")
    print(f"问题数: {len(quality['issues'])}")

    # 性能诊断：输出慢阶段（>0.5s 的才显示，避免正常情况刷屏）
    slow = sorted(timings.items(), key=lambda x: -x[1])
    slow_filtered = [(n, t) for n, t in slow if t > 0.5]
    if t_ddl > 0.5:
        slow_filtered.append(("ddl_discovery", t_ddl))
    if t_lts > 0.5:
        slow_filtered.append(("lts_discovery", t_lts))
    if _t_view > 0.5:
        slow_filtered.append(("view_generation", _t_view))
    slow_filtered.sort(key=lambda x: -x[1])
    total_t = sum(timings.values()) if timings else 0
    if slow_filtered:
        all_t = total_t + t_ddl + t_lts + _t_view
        print(f"\n分析耗时: {all_t:.1f}s（慢阶段:）")
        for name, t in slow_filtered:
            print(f"  {name}: {t:.1f}s")
    elif t_ddl > 0 or t_lts > 0:
        all_t = total_t + t_ddl + t_lts
        print(f"\n分析耗时: {all_t:.1f}s")

    print(f"\n可选精修: AI 读 _internal/knowledge_summary.md 补业务理解，"
          f"写 _internal/knowledge_ai.md 后重跑 view_generator 覆盖视图：")
    print(f"  python run.py view_generator --input {output_file} "
          f"--ai-input {internal_dir / 'knowledge_ai.md'} "
          f"--output {output_dir} --views all")

    # ── 运营埋点：记录本次命令执行（在所有产出落盘之后） ──
    try:
        from usage import record as _record_usage
        _record_usage({
            "command": "analyze",
            "input_type": bundle["input_type"],
            "asset": safe_group_en,
            "trace_id": trace_id,
            "target_table": target_name,
            "rule_count": len(rules),
            "field_count": stats.get("total_in_sql", 0) if isinstance(stats, dict) else 0,
            "elapsed_sec": round(_t.time() - t0, 2),
            "elapsed_detail": {
                "parse": _t_parse,
                "ddl_discovery": t_ddl,
                "lts_discovery": t_lts,
                "write_json_and_summary": _t_json_summary,
                "view_generation": _t_view,
            },
            "status": "partial" if _view_exc else "ok",
            "error_type": "view_generation_error" if _view_exc else "",
            "error_message": str(_view_exc) if _view_exc else "",
            "quality_issues": len(quality["issues"]) if isinstance(quality, dict) and "issues" in quality else 0,
        })
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
# 多规则组链路分析（/analyze-chain）
# ═══════════════════════════════════════════════════════════════


def build_target_index(repo_root, scope_dir=None):
    """扫描代码仓，建 {target_table → [规则组目录]} 索引。

    Args:
        repo_root: 代码仓根目录（含 BFT/）
        scope_dir: 搜索范围（子项目目录），None 则扫整个 BFT/BftWideTable

    Returns:
        {norm_target_table: [{dir, schema, rule_group_en, rule_group_code}]}
    """
    import yaml as _yaml
    from engine import _norm_table

    repo = Path(repo_root)
    bft = repo / "BFT" / "BftWideTable"
    if scope_dir:
        # 在指定子项目目录下扫
        search_root = Path(scope_dir)
    else:
        search_root = bft

    if not search_root.is_dir():
        return {}

    index = {}
    for yml_file in search_root.rglob("*.yml"):
        group_dir = yml_file.parent
        try:
            data = _yaml.safe_load(yml_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data or not isinstance(data, dict):
            continue

        target_table = _yml_get(data, "target_table", _YML_RULE_KEY_ALIASES)
        target_schema = _yml_get(data, "target_schema", _YML_RULE_KEY_ALIASES)
        rule_group_en = _yml_get(data, "rule_group_en", _YML_RULE_KEY_ALIASES).strip()
        rule_group_code = _yml_get(data, "rule_group_code", _YML_RULE_KEY_ALIASES)

        if not target_table:
            continue

        key = _norm_table(target_table)
        index.setdefault(key, []).append({
            "dir": str(group_dir),
            "schema": target_schema,
            "rule_group_en": rule_group_en,
            "rule_group_code": rule_group_code,
            "target_table": target_table,
        })

    return index


def trace_upstream_rule_groups(
    final_group_dir, repo_root, max_depth=6,
):
    """从最终 F 表规则组出发，递归追溯所有上游 mid/tmp 规则组。

    算法：
      1. 分析最终规则组 → 取穿透后的源表
      2. 筛出"被代码仓里某规则组写的"表（在索引里能找到的）
      3. 对每个上游规则组递归 Step 1-2
      4. 直到源表都是 ods（索引里找不到写入者）

    Args:
        final_group_dir: 最终 F 表规则组目录
        repo_root: 代码仓根目录

    Returns:
        {
            "groups": [{dir, rule_group_en, target_table, source_tables, depth}],
            "not_found": [表名列表],  # 源表找不到写入者（可能是 ods 源表）
            "cycle_detected": bool,   # 是否发现环（A读B、B读A 这类循环依赖）
            "truncated": bool,        # 是否因超过 max_depth 而提前截断（被截断的上游未追溯）
        }
    """
    from engine import analyze_pipeline, _norm_table
    from analyzer import read_yml, detect_dialect

    # 确定搜索范围：先子项目，后项目级
    final_path = Path(final_group_dir).resolve()
    # 子项目 = 规则组目录的父目录（如 SUB_TRADE）
    sub_project_dir = final_path.parent
    # 项目 = 子项目的父目录（如 P_TRADE）
    project_dir = sub_project_dir.parent

    # 先在子项目下建索引
    sub_index = build_target_index(repo_root, sub_project_dir)

    groups = []
    visited_dirs = set()
    pending_dirs = set()  # 当前递归栈中的目录（用于真正的环检测）
    not_found_tables = set()
    cycle_detected = [False]  # 用 list 包裹，便于闭包内赋值
    truncated = [False]       # 是否因 max_depth 截断

    def _trace(group_dir, depth):
        group_dir_resolved = str(Path(group_dir).resolve())
        if group_dir_resolved in pending_dirs:
            # 命中当前递归栈 → 真实环（A读B、B读A）
            cycle_detected[0] = True
            return
        if group_dir_resolved in visited_dirs:
            return  # 已被其他路径访问过（菱形依赖等，非环）
        if depth > max_depth:
            # 超过最大深度，记录截断但不中断整体追溯
            truncated[0] = True
            return
        visited_dirs.add(group_dir_resolved)
        pending_dirs.add(group_dir_resolved)
        try:
            # 读规则组
            raw = read_yml(group_dir)
            if not raw.get("rules"):
                return

            rules = raw["rules"]
            rule_group_en = raw.get("rule_group_en", "")
            dialect = detect_dialect(rules)

            # 分析 → 取穿透后的源表
            target_fields = raw.get("target_fields", {})
            group_variables = raw.get("group_variables", {})
            _, parsed_map = analyze_pipeline(rules, target_fields, group_variables, dialect)

            # 收集所有步骤的穿透源表
            source_tables = set()
            for rule in rules:
                parsed = parsed_map.get(rule.rule_code)
                if not parsed:
                    continue
                for j in parsed.source_tables:
                    if not j.source_table or j.source_table.startswith("(subquery:"):
                        continue
                    source_tables.add(j.source_table)
                # CTE 内部表也要收
                for cte in parsed.ctes:
                    for t in cte.source_tables:
                        tname = t.get("name", "")
                        if tname:
                            source_tables.add(tname)

            # 目标表（这个规则组写的）
            target_table = ""
            all_target_tables = set()
            if rules:
                max_seq_rule = max(rules, key=lambda r: r.exec_sequence)
                target_table = max_seq_rule.target_table
                for r in rules:
                    if r.target_table:
                        all_target_tables.add(r.target_table.lower())

            groups.append({
                "dir": str(group_dir),
                "rule_group_en": rule_group_en,
                "target_table": target_table,
                "all_target_tables": sorted(all_target_tables),
                "source_tables": list(source_tables),
                "depth": depth,
            })

            # 对每个源表，找写入者
            for src_table in source_tables:
                # 归一化：去 schema 前缀（dws.dwb_trade_mid_f → dwb_trade_mid_f）
                # 索引 key 是 _norm_table(target_table)，yml 里 target_table 不带 schema
                table_part = src_table.split(".")[-1] if "." in src_table else src_table
                key = _norm_table(table_part)
                writers = sub_index.get(key, [])
                if not writers:
                    # 子项目找不到，试项目级
                    if not hasattr(_trace, "_project_index"):
                        _trace._project_index = build_target_index(repo_root, project_dir)
                    writers = _trace._project_index.get(key, [])

                if not writers:
                    not_found_tables.add(src_table)
                    continue

                for writer in writers:
                    # 跳过 _init 规则组（初始化数据，不属于日常加工链路）
                    writer_name = writer.get("rule_group_en", "")
                    writer_dir_name = Path(writer["dir"]).name
                    if writer_name.lower().endswith("_init") or writer_dir_name.lower().endswith("_init"):
                        continue
                    _trace(writer["dir"], depth + 1)
        finally:
            # 弹出当前栈帧：保证 pending_dirs 准确反映"正在递归中"的路径
            pending_dirs.discard(group_dir_resolved)

    _trace(final_group_dir, 0)

    return {
        "groups": groups,
        "not_found": sorted(not_found_tables),
        "cycle_detected": cycle_detected[0],
        "truncated": truncated[0],
    }


def merge_rule_groups(groups_info, repo_root, ddl_dir=""):
    """合并多个规则组的 rules，重新编号 exec_sequence（legacy yml 入口的包装）。

    读取侧 read_yml 逐组读；核心逻辑在 _merge_group_data（collection 入口
    直取 bundle，不重复读盘）。
    """
    groups = groups_info["groups"]
    group_data = {}
    for g in groups:
        raw = read_yml(g["dir"])
        group_data[g["dir"]] = {
            "rules": raw.get("rules", []),
            "target_fields": raw.get("target_fields", {}),
            "group_variables": raw.get("group_variables", {}),
            "label": g.get("rule_group_en") or Path(g["dir"]).name,
        }
    return _merge_group_data(groups_info, group_data)


def _merge_group_data(groups_info: dict, group_data: dict):
    """合并核（collection / legacy 两条 chain 入口共用）。

    - 组间依赖：A 写的表被 B 的源表引用 → B 依赖 A（groups_info.groups[].source_tables）
    - exec_sequence 拓扑重编号：被依赖的（上游）排前面；互相不依赖的组 seq 可重叠
    - rule_code 跨组冲突 → 按组前缀重键 `{组}::{码}`（制品 xlsx 每组常从 R0001
      起编，不重键会串组）；target_fields/group_variables 跟随重键
    - rules 用 dataclasses.replace 整拷（修存量缺陷：旧手工逐字段构造丢
      delete_condition/project_code/data_source/business_owner）

    Returns: (merged_rules, merged_target_fields, merged_group_variables)
    """
    import dataclasses as _dc
    from engine import _norm_table

    groups = groups_info["groups"]
    if not groups:
        return [], {}, {}

    # 组标签（重键前缀用）：rule_group_en 优先，回退目录名
    label_of = {g["dir"]: (g.get("rule_group_en")
                           or group_data.get(g["dir"], {}).get("label")
                           or Path(g["dir"]).name)
                for g in groups}

    # 每组写过的全部 target_table（归一化；一组可多步写多表，早期 mid 表也算）
    group_targets = {}
    for g in groups:
        targets = set()
        for r in group_data.get(g["dir"], {}).get("rules", []):
            if r.target_table:
                targets.add(_norm_table(r.target_table))
        if targets:
            group_targets[g["dir"]] = targets

    # 组间依赖：B 的源表命中 A 写的表 → B 依赖 A
    group_deps = {g["dir"]: set() for g in groups}
    for g in groups:
        for src_table in g.get("source_tables", []):
            table_part = src_table.split(".")[-1] if "." in src_table else src_table
            src_key = _norm_table(table_part)
            for other_dir, other_targets in group_targets.items():
                if src_key in other_targets and other_dir != g["dir"]:
                    group_deps[g["dir"]].add(other_dir)

    # 按依赖拓扑排序（被依赖的先处理；环容忍——temp_mark 直接跳过）
    sorted_dirs = []
    visited = set()
    temp_mark = set()

    def _topo_sort(dir_path):
        if dir_path in visited:
            return
        if dir_path in temp_mark:
            return  # 环检测：容忍不中断
        temp_mark.add(dir_path)
        for dep_dir in group_deps.get(dir_path, set()):
            _topo_sort(dep_dir)
        temp_mark.discard(dir_path)
        visited.add(dir_path)
        sorted_dirs.append(dir_path)

    for g in groups:
        _topo_sort(g["dir"])

    # 每组偏移量 = max(依赖组的全局最大 seq) + 1（无依赖从 0 起，组间可重叠）
    group_offset = {}
    group_global_max = {}
    for dir_path in sorted_dirs:
        rules = group_data.get(dir_path, {}).get("rules", [])
        if not rules:
            group_offset[dir_path] = 0
            group_global_max[dir_path] = 0
            continue
        max_seq_in_group = max(r.exec_sequence for r in rules)
        deps = group_deps.get(dir_path, set())
        offset = max((group_global_max.get(d, 0) for d in deps), default=-1) + 1
        group_offset[dir_path] = offset
        group_global_max[dir_path] = offset + max_seq_in_group

    # rule_code 跨组冲突检测 → 重键表 {dir: {old_code: new_code}}
    code_owner = {}
    collided = set()
    for g in groups:
        for r in group_data.get(g["dir"], {}).get("rules", []):
            if r.rule_code in code_owner and code_owner[r.rule_code] != g["dir"]:
                collided.add(r.rule_code)
            code_owner[r.rule_code] = g["dir"]
    rekey = {}
    for g in groups:
        d = g["dir"]
        for r in group_data.get(d, {}).get("rules", []):
            if r.rule_code in collided:
                rekey.setdefault(d, {})[r.rule_code] = f"{label_of[d]}::{r.rule_code}"

    # 合并 rules（replace 整拷 + 偏移 + 重键）
    merged_rules = []
    for g in groups:
        d = g["dir"]
        offset = group_offset.get(d, 0)
        rk = rekey.get(d, {})
        for rule in group_data.get(d, {}).get("rules", []):
            merged_rules.append(_dc.replace(
                rule, exec_sequence=rule.exec_sequence + offset,
                rule_code=rk.get(rule.rule_code, rule.rule_code)))

    # 合并 target_fields / group_variables（{rule_code: list}；重键跟随）
    merged_target_fields = {}
    merged_group_variables = {}
    for g in groups:
        d = g["dir"]
        rk = rekey.get(d, {})
        for rc, tfs in (group_data.get(d, {}).get("target_fields") or {}).items():
            merged_target_fields.setdefault(rk.get(rc, rc), []).extend(tfs)
        for rc, gvs in (group_data.get(d, {}).get("group_variables") or {}).items():
            merged_group_variables.setdefault(rk.get(rc, rc), []).extend(gvs)

    return merged_rules, merged_target_fields, merged_group_variables


# ═══════════════════════════════════════════════════════════════
# collection 形态（资产集合目录）：逐子目录装配 → 闭环建图 → 合并
# 输入契约（v1.30）：父目录下每个子目录 = 一个资产的制品目录（推荐，与
# /analyze 形态 1 同款）；yml 子目录顺带兼容（legacy 日落中，不投入）。
# 依赖只在集合内连边（闭环），找不到写入者的源表 = 外部源表（不外追）。
# ═══════════════════════════════════════════════════════════════

def _chain_collect_sources(rules, parsed_map) -> set:
    """收集一组规则穿透后的全部源表（含 CTE 内部表；trace/collection 共用口径）。"""
    sources = set()
    for rule in rules:
        parsed = parsed_map.get(rule.rule_code)
        if not parsed:
            continue
        for j in parsed.source_tables:
            if not j.source_table or j.source_table.startswith("(subquery:"):
                continue
            sources.add(j.source_table)
        for cte in parsed.ctes:
            for t in cte.source_tables:
                tname = t.get("name", "") if isinstance(t, dict) else ""
                if tname:
                    sources.add(tname)
    return sources


def _chain_load_child(child_dir: Path):
    """collection 单个子目录 → bundle；无制品无 yml 的子目录返回 None（调用方披露）。"""
    mode = _detect_input_mode(child_dir)
    if mode == "xlsx":
        return None
    if mode == "artifact_dir":
        # 预闸：目录里连 xlsx 都没有 → 不是资产目录（不触发装配异常，交调用方披露）
        if not any(child_dir.glob("*.xlsx")):
            return None
        from pipeline_core import run_from_dir
        core = run_from_dir(str(child_dir), include_dq=True, channel="analyze-chain")
        raw = core["raw"]
        return {
            "dir": str(child_dir),
            "label": raw.get("rule_group_en") or child_dir.name,
            "mode": "artifact_dir",
            "rules": core["rules"],
            "target_fields": raw.get("target_fields", {}),
            "group_variables": raw.get("group_variables", {}),
            "rule_group_code": raw.get("rule_group_code", ""),
            "source_tables": _chain_collect_sources(core["rules"], core["parsed_map"]),
            "target_tables": {r.target_table.lower() for r in core["rules"] if r.target_table},
            "ddl_stage_dir": core.get("ddl_stage_dir", ""),
            "schedule_info": core["schedule_info"],
            "dq_rules": core["dq_rules"],
            "asset_info": (core["knowledge"].get("meta", {}) or {}).get("asset_info"),
        }
    # yml_dir（legacy 顺带兼容）
    raw = read_yml(child_dir)
    if not raw.get("rules"):
        return None
    dialect = detect_dialect(raw["rules"])
    _, parsed_map = analyze_pipeline(
        raw["rules"], raw.get("target_fields", {}),
        raw.get("group_variables", {}), dialect)
    return {
        "dir": str(child_dir),
        "label": raw.get("rule_group_en") or child_dir.name,
        "mode": "yml_dir",
        "rules": raw["rules"],
        "target_fields": raw.get("target_fields", {}),
        "group_variables": raw.get("group_variables", {}),
        "rule_group_code": raw.get("rule_group_code", ""),
        "source_tables": _chain_collect_sources(raw["rules"], parsed_map),
        "target_tables": {r.target_table.lower() for r in raw["rules"] if r.target_table},
        "ddl_stage_dir": "", "schedule_info": None, "dq_rules": None, "asset_info": None,
    }


def build_collection_graph(bundles: list) -> dict:
    """闭环建组间依赖图（写入者索引只来自给定组集合，不外追代码仓）。

    Returns（与 trace_upstream_rule_groups 同构，后续流程共用）:
        groups: [{dir, rule_group_en, rule_group_code, target_table,
                  all_target_tables, source_tables, depth, is_endpoint}]
        not_found: 集合内无写入者的源表（外部源表，不追溯）
        cycle_detected / truncated（恒 False，闭环无深度截断）
    depth 语义与 trace 一致：终点=0，上游=距最近终点的跳数。
    """
    from engine import _norm_table

    def _bare_key(table: str) -> str:
        part = table.split(".")[-1] if "." in table else table
        return _norm_table(part)

    # 写入者索引 {key: [bundle]}
    index: dict = {}
    for b in bundles:
        for t in b["target_tables"]:
            index.setdefault(_bare_key(t), []).append(b)

    # 组间依赖（B 源表命中 A 写的表）+ 外部源表
    deps = {}        # {dir: set(上游 dir)}
    consumers = {}   # {dir: set(下游 dir)}
    not_found = set()
    for b in bundles:
        dset = set()
        for src in b["source_tables"]:
            writers = [w for w in index.get(_bare_key(src), []) if w["dir"] != b["dir"]]
            if not writers:
                not_found.add(src)
                continue
            for w in writers:
                dset.add(w["dir"])
                consumers.setdefault(w["dir"], set()).add(b["dir"])
        deps[b["dir"]] = dset

    # 终点：写的表没被集合内其他组读（多终点=森林，一张全图报告）
    endpoint_dirs = {b["dir"] for b in bundles if not consumers.get(b["dir"])}

    # 环检测（DFS temp-mark）
    cycle = [False]
    state = {}  # dir -> 1(在栈) / 2(完成)

    def _dfs(d):
        if state.get(d) == 1:
            cycle[0] = True
            return
        if state.get(d) == 2:
            return
        state[d] = 1
        for up in deps.get(d, ()):
            _dfs(up)
        state[d] = 2

    for b in bundles:
        _dfs(b["dir"])

    # depth：从终点沿依赖边反向 BFS（距最近终点的跳数，终点 0）
    from collections import deque
    depth = {}
    q = deque(endpoint_dirs)
    for d in endpoint_dirs:
        depth[d] = 0
    while q:
        cur = q.popleft()
        for up in deps.get(cur, ()):
            if up not in depth or depth[cur] + 1 < depth[up]:
                depth[up] = depth[cur] + 1
                q.append(up)

    groups = []
    for b in bundles:
        tgt = ""
        if b["rules"]:
            tgt = max(b["rules"], key=lambda r: r.exec_sequence).target_table
        groups.append({
            "dir": b["dir"], "rule_group_en": b["label"],
            "rule_group_code": b.get("rule_group_code", ""),
            "target_table": tgt,
            "all_target_tables": sorted(b["target_tables"]),
            "source_tables": sorted(b["source_tables"]),
            "depth": depth.get(b["dir"], 0),
            "is_endpoint": b["dir"] in endpoint_dirs,
        })
    groups.sort(key=lambda g: (-g["depth"], g["rule_group_en"]))
    return {"groups": groups, "not_found": sorted(not_found),
            "cycle_detected": cycle[0], "truncated": False}


def _chain_finish(*, merged_rules, merged_target_fields, merged_group_variables,
                  ddl_dir, schedule_info, dq_rules, dq_declared_absent, asset_info,
                  chain_groups, output_base: Path, safe_name: str,
                  t0, trace_id, input_type, asset_label, group_count,
                  timings, dq_discover=None):
    """chain 共尾：合并分析 → meta 注入 → 过程件 + 三件套 → 埋点（两入口共用）。"""
    import time as _t_usage
    from inputs import apply_meta_enrichments

    # ── 分析（合并后整体作为一个 pipeline；rule_group_code=CHAIN）──
    print(f"\n[Step 3] 分析完整链路...")
    dialect = detect_dialect(merged_rules)
    knowledge, parsed_map = analyze_pipeline(
        merged_rules, merged_target_fields, merged_group_variables, dialect,
        ddl_dir=ddl_dir, source_file="", rule_group_code="CHAIN",
    )

    # 多规则组链路标记（报告区分用）
    knowledge["meta"]["is_multi_group"] = True
    knowledge["meta"]["chain_groups"] = chain_groups

    # legacy 入口的 DQ 仓内发现（依赖 target_table，须在 analyze 之后）
    if dq_rules is None and dq_discover:
        try:
            dq_rules = dq_discover(knowledge.get("meta", {}).get("target_table", "")) or None
            if dq_rules:
                print(f"  [DQ] 发现 {len(dq_rules)} 条质量检查规则")
        except Exception:
            dq_rules = None

    # 调度 / DQ / I 视图注入（终点组语义；唯此一处写这些 key）
    apply_meta_enrichments(knowledge, dict(asset_info) if asset_info else None,
                           schedule_info, dq_rules,
                           dq_declared_absent=dq_declared_absent)

    # ── 输出 ──
    output_dir = Path(output_base) / safe_name
    internal_dir = output_dir / "_internal"
    internal_dir.mkdir(parents=True, exist_ok=True)

    # 在 pop 前读 _timings（运营埋点用）
    _chain_timings = knowledge.get("meta", {}).get("_timings", {})
    _t_parse = round(sum(_chain_timings.values()), 2) if _chain_timings else 0
    knowledge["meta"].pop("_timings", None)

    _t_json0 = _t_usage.time()
    (internal_dir / "knowledge_draft.json").write_text(
        json.dumps(knowledge, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )

    # AI 摘要（v1.22 渐进式披露契约：自足摘要，细节走 run.py inspect）
    summary_text = _generate_ai_summary(
        knowledge, merged_rules, parsed_map,
        knowledge["topology"], knowledge["field_mappings"],
        knowledge["quality"], knowledge["data_flow"],
    )
    (internal_dir / "knowledge_summary.md").write_text(summary_text, encoding="utf-8", newline="\n")
    _t_json_summary = round(_t_usage.time() - _t_json0, 2)

    # ── 三件套 ──
    print(f"\n[Step 4] 生成视图...")
    _t_view0 = _t_usage.time()
    _view_exc = None
    try:
        from view_generator import (generate_mapping, generate_asset_report,
                                    generate_tech_design)
        generate_mapping(knowledge, output_dir)
        generate_asset_report(knowledge, output_dir)
        generate_tech_design(knowledge, output_dir)
    except Exception as e:
        _view_exc = e
        print(f"  [WARN] 视图生成失败: {e}", file=sys.stderr)
    _t_view = round(_t_usage.time() - _t_view0, 2)

    stats = knowledge["field_mappings"]["statistics"]
    target_table = knowledge["meta"]["target_table"]
    print(f"\n=== 完成 ===")
    print(f"输出目录: {output_dir}")
    print(f"规则组数: {group_count}")
    print(f"步骤数: {len(merged_rules)}")
    print(f"字段数: {stats['total_in_sql']}")
    print(f"目标表: {target_table}")
    print(f"\n已生成:")
    print(f"  - mapping.xlsx / asset_report.html / tech_design.md")
    print(f"  - _internal/knowledge_draft.json, _internal/knowledge_summary.md")

    # ── 运营埋点：记录本次链路分析 ──
    try:
        from usage import (record as _record_usage,
                           _error_message as _msg_err)
        _record_usage({
            "command": "analyze-chain",
            "input_type": input_type,
            "asset": asset_label,
            "trace_id": trace_id,
            "target_table": target_table,
            "rule_count": group_count,
            "field_count": stats.get("total_in_sql", 0) if isinstance(stats, dict) else 0,
            "elapsed_sec": round(_t_usage.time() - t0, 2),
            "elapsed_detail": {
                "parse": _t_parse,
                "ddl_discovery": timings.get("ddl_discovery", 0),
                "lts_discovery": timings.get("lts_discovery", 0),
                "write_json_and_summary": _t_json_summary,
                "view_generation": _t_view,
            },
            "status": "partial" if _view_exc else "ok",
            "error_type": "view_generation_error" if _view_exc else "",
            "error_message": _msg_err(_view_exc),
            "quality_issues": len(knowledge.get("quality", {}).get("issues", [])),
        })
    except Exception:
        pass


def _run_chain_collection(args, input_path: Path, t0, trace_id):
    """collection 形态：逐子目录装配 → 闭环建图 → 合并 → 共尾。"""
    import time as _t_usage

    print(f"=== 多规则组链路分析（资产集合目录，闭环建图） ===")
    print(f"输入: {input_path}")

    # 子目录集合：父目录直接含 xlsx = 单资产（1 组链）；否则逐子目录入集
    if any(input_path.glob("*.xlsx")):
        child_dirs = [input_path]
        print("  单资产制品目录（1 组链；只需单资产三件套时用 /analyze 即可）")
    else:
        child_dirs = [c for c in sorted(input_path.iterdir())
                      if c.is_dir() and _detect_input_mode(c) in ("artifact_dir", "yml_dir")]

    bundles = []
    skipped = []
    for c in child_dirs:
        print(f"\n── 装配子目录: {c.name} ──")
        try:
            b = _chain_load_child(c)
        except Exception as e:
            # 单组装配失败（缺术加等）不中断其他组：披露后继续（闭环语义=用户给什么算什么）
            print(f"  [WARN] 装配失败，跳过: {e}", file=sys.stderr)
            skipped.append(c.name)
            continue
        if b is None:
            skipped.append(c.name)
            continue
        bundles.append(b)
    if skipped:
        print(f"\n[分拣] 未入图子目录: {', '.join(skipped)}")
    if not bundles:
        print("[ERROR] 集合内无有效资产（每个子目录需含 execution_tasks.xlsx 制品目录或 *.yml）",
              file=sys.stderr)
        sys.exit(1)

    # 闭环建图
    print(f"\n[Step 1] 闭环依赖建图（{len(bundles)} 组）...")
    result = build_collection_graph(bundles)
    endpoints = [g for g in result["groups"] if g.get("is_endpoint")]
    print(f"  终点(写的表无人消费): {'、'.join(g['rule_group_en'] for g in endpoints) or '(无——疑似环)'}")
    for g in result["groups"]:
        indent = "    " * g["depth"]
        tag = " [终点]" if g.get("is_endpoint") else ""
        tables = ', '.join(g["all_target_tables"]) or g["target_table"]
        print(f"  {indent}{g['rule_group_en']} → 写 {tables}{tag}")
    if result["cycle_detected"]:
        print("  [WARN] 检测到组间循环依赖（A读B、B读A）——拓扑容忍处理，结果供人工核对")
    if result["not_found"]:
        print(f"  外部源表(集合内无写入者，不追溯): {', '.join(result['not_found'])}")

    # 合并
    print(f"\n[Step 2] 合并规则组...")
    group_data = {b["dir"]: {"rules": b["rules"],
                             "target_fields": b["target_fields"],
                             "group_variables": b["group_variables"],
                             "label": b["label"]}
                  for b in bundles}
    merged_rules, merged_tf, merged_gv = _merge_group_data(result, group_data)
    print(f"  合并后 {len(merged_rules)} 条规则（exec_sequence 已按拓扑重编号）")

    # DDL：各组制品自带 stage 目录 + 显式 --ddl-dir，全并（engine 支持 str|list）
    ddl_dirs = [b["ddl_stage_dir"] for b in bundles if b.get("ddl_stage_dir")]
    if args.ddl_dir:
        ddl_dirs.append(args.ddl_dir)
    ddl_dir = ddl_dirs or ""

    # LTS/DQ/I 视图：终点组语义——单终点挂载；多终点不挂（披露，按单资产 /analyze 看）
    endpoint_bundles = [b for b in bundles
                        if b["dir"] in {g["dir"] for g in endpoints}]
    schedule_info = dq_rules = asset_info = None
    dq_declared_absent = False
    if len(endpoint_bundles) == 1:
        ep = endpoint_bundles[0]
        schedule_info = ep.get("schedule_info")
        dq_rules = ep.get("dq_rules")
        dq_declared_absent = dq_rules is None
        asset_info = ep.get("asset_info")
    elif endpoint_bundles:
        print("  [LTS/DQ] 多终点：调度/DQ/I 视图不挂载（各组按单资产 /analyze 查看）")

    chain_groups = [{"name": g["rule_group_en"], "target_table": g["target_table"],
                     "all_target_tables": g.get("all_target_tables", []),
                     "depth": g["depth"],
                     "is_endpoint": bool(g.get("is_endpoint"))}
                    for g in result["groups"]]

    _chain_finish(
        merged_rules=merged_rules, merged_target_fields=merged_tf,
        merged_group_variables=merged_gv, ddl_dir=ddl_dir,
        schedule_info=schedule_info, dq_rules=dq_rules,
        dq_declared_absent=dq_declared_absent, asset_info=asset_info,
        chain_groups=chain_groups, output_base=Path(args.output),
        safe_name="chain_" + input_path.name,
        t0=t0, trace_id=trace_id, input_type="artifact_collection",
        asset_label=input_path.name, group_count=len(result["groups"]),
        timings={"ddl_discovery": 0, "lts_discovery": 0},
    )


def main_chain():
    """多规则组链路分析 CLI 入口（/analyze-chain）。

    输入路由（命令区分：本入口拿到目录即按链路语义处理）：
    - 制品形态（推荐）：资产集合目录（子目录=各资产制品目录，闭环建图）或
      单资产制品目录（1 组链）
    - legacy：最终 F 表 yml 规则组目录 / 表名 + --repo-root 代码仓追溯
    """
    parser = argparse.ArgumentParser(
        description="多规则组链路分析（/analyze-chain）",
    )
    parser.add_argument("--input", required=True,
                        help="资产集合目录（推荐，子目录=各资产制品目录，自动识别依赖建全图）"
                             "｜单资产制品目录｜最终F表yml规则组目录/表名（legacy）")
    parser.add_argument("--output", required=True,
                        help="输出基础目录")
    parser.add_argument("--repo-root", default="",
                        help="代码仓根目录（legacy yml 输入用，含 BFT/；不指定则自动从 input 向上找）")
    parser.add_argument("--ddl-dir", default="",
                        help="DDL 文件目录（可选；制品形态叠加在各组自带 DDL 之上）")
    args = parser.parse_args()

    # ── 运营埋点：记录开始时刻 ──
    import time as _t_usage
    import uuid as _uuid_usage
    _t0_usage = _t_usage.time()
    _trace_id = f"trace_{int(_t0_usage)}_{_uuid_usage.uuid4().hex[:8]}"

    input_path = Path(args.input)

    # 单 xlsx 文件不是本命令的输入形态（/analyze 的形态 2）
    if input_path.is_file():
        print("[ERROR] analyze-chain 的输入是目录（资产集合/单资产目录）或表名；"
              "单个术加制品包请用 /analyze", file=sys.stderr)
        sys.exit(2)

    # ── 制品形态：目录且非 yml 规则组目录 → collection（含单资产目录）──
    if input_path.is_dir() and not (any(input_path.glob("*.yml"))
                                    or any(input_path.glob("*.yaml"))):
        _run_chain_collection(args, input_path, _t0_usage, _trace_id)
        return

    # ╀─ legacy：yml 规则组目录 / 表名 + 代码仓追溯 ──
    # 定位代码仓根
    if args.repo_root:
        repo_root = Path(args.repo_root)
    else:
        repo_root = _find_repo_root(input_path if input_path.is_dir() else input_path.parent)
        if not repo_root:
            print("[ERROR] 无法定位代码仓根（需含 BFT/ + DDL/），请用 --repo-root 指定", file=sys.stderr)
            sys.exit(1)

    # 定位最终 F 表规则组目录
    if input_path.is_dir():
        final_group_dir = input_path
    else:
        # 按表名定位（复用现有 locate_asset_dirs 逻辑）
        from impact_analyzer import locate_asset_dirs
        located = locate_asset_dirs([args.input], str(repo_root))
        info = located.get(args.input, {})
        if not info.get("found"):
            print(f"[ERROR] 未定位到规则组目录: {args.input}", file=sys.stderr)
            sys.exit(1)
        final_group_dir = Path(info["dir"])

    print(f"=== 多规则组链路分析（legacy 代码仓追溯） ===")
    print(f"最终规则组: {final_group_dir.name}")
    print(f"代码仓根: {repo_root}")

    # 追溯上游
    print(f"\n[Step 1] 追溯上游规则组...")
    result = trace_upstream_rule_groups(final_group_dir, repo_root)
    print(f"  找到 {len(result['groups'])} 个规则组:")
    for g in sorted(result["groups"], key=lambda x: x["depth"]):
        indent = "    " * g["depth"]
        print(f"  {indent}{g['rule_group_en']} → 写 {g['target_table']}")
    if result["not_found"]:
        print(f"  源表（不追溯）: {', '.join(result['not_found'])}")

    # 合并
    print(f"\n[Step 2] 合并规则组...")
    merged_rules, merged_target_fields, merged_group_variables = merge_rule_groups(result, repo_root)
    print(f"  合并后 {len(merged_rules)} 条规则（exec_sequence 已重编号）")

    # DDL 发现
    ddl_dir = args.ddl_dir
    _t_ddl = 0
    if not ddl_dir and repo_root:
        try:
            _t_ddl0 = _t_usage.time()
            ddl_dir = _auto_discover_ddl_from_repo(final_group_dir, merged_rules) or ""
            _t_ddl = round(_t_usage.time() - _t_ddl0, 2)
        except Exception:
            ddl_dir = ""

    # LTS 调度任务发现（用最终 F 组的 rule_group_code 匹配）
    schedule_info = None
    _t_lts = 0
    if repo_root:
        # 最终 F 组的 rule_group_code（chain_groups 里 depth 最大的）
        final_group_code = ""
        for g in sorted(result["groups"], key=lambda x: -x["depth"]):
            if g.get("rule_group_code"):
                final_group_code = g["rule_group_code"]
                break
        if final_group_code:
            try:
                _t_lts0 = _t_usage.time()
                schedule_info = _discover_lts_from_repo(final_group_dir, final_group_code)
                _t_lts = round(_t_usage.time() - _t_lts0, 2)
                if schedule_info:
                    f_names = [t["task_name"] for t in schedule_info.get("f_tasks", [])]
                    i_names = [t["task_name"] for t in schedule_info.get("i_tasks", [])]
                    print(f"  [LTS] 调度任务: F={f_names} I={i_names} ({_t_lts}s)")
            except Exception:
                pass

    chain_groups = [
        {"name": g["rule_group_en"], "target_table": g["target_table"],
         "all_target_tables": g.get("all_target_tables", [g["target_table"].lower()] if g["target_table"] else []),
         "depth": g["depth"]}
        for g in sorted(result["groups"], key=lambda x: x["depth"])
    ]

    _chain_finish(
        merged_rules=merged_rules, merged_target_fields=merged_target_fields,
        merged_group_variables=merged_group_variables, ddl_dir=ddl_dir,
        schedule_info=schedule_info, dq_rules=None,
        dq_declared_absent=False, asset_info=None,
        chain_groups=chain_groups, output_base=Path(args.output),
        safe_name="chain_" + final_group_dir.name,
        t0=_t0_usage, trace_id=_trace_id,
        input_type="yml_dir" if input_path.is_dir() else "table_name",
        asset_label=final_group_dir.name, group_count=len(result["groups"]),
        timings={"ddl_discovery": _t_ddl, "lts_discovery": _t_lts},
        dq_discover=lambda tgt: _discover_dq_from_repo(final_group_dir, tgt),
    )


# ═══════════════════════════════════════════════════════════════
# 兼容层：re-export engine 的符号
# 引擎代码已物理搬入 engine.py。以下 re-export 保证现有代码
# `from analyzer import xxx` 继续可用（过渡期，新代码请直接 from engine import）。
# ═══════════════════════════════════════════════════════════════
from engine import (  # noqa: E402,F401
    ParsedSQL, ParsedJoin, RawRule, RawTargetField, RawGroupVariable,
    ParsedColumn, ParsedCTE, TableRef, ColumnRef, QueryUnit,
    analyze_pipeline,
    detect_dialect, parse_single_sql, classify_transform,
    build_topology, build_data_flow, build_field_mappings, analyze_quality,
    build_join_key_lineage, enrich_join_key_lineage, enrich_field_physical_sources,
    build_data_blocks, build_structured_step_summary, generate_step_description,
    detect_patterns, build_source, parse_ddl_for_metadata,
    _append_block_summary, _append_block_template,
    _is_intermediate_table, _strip_dws_clauses, _replace_placeholders,
    _normalize_table_name, _norm_table, _table_match, _infer_layer, _clean_name,
    SELECT_RULE_TYPES, RULE_TYPE_MAP, DELETE_MODE_MAP,
)


if __name__ == "__main__":
    _is_chain = len(sys.argv) > 1 and sys.argv[1] == "--chain"
    if _is_chain:
        sys.argv.pop(1)
    try:
        main_chain() if _is_chain else main()
    except SystemExit:
        raise  # sys.exit 不当异常处理
    except Exception as _main_exc:
        # 崩溃时也记一条埋点（细节可能不全，但有 command + error_type）
        try:
            from usage import record as _record_usage, _classify_error as _cls_err, _error_message as _msg_err
            _record_usage({
                "command": "analyze-chain" if _is_chain else "analyze",
                "status": "error",
                "error_type": _cls_err(_main_exc),
                "error_message": _msg_err(_main_exc),
            })
        except Exception:
            pass
        raise
