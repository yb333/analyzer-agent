#!/usr/bin/env python3
"""excel 读取公共底座 —— 列定位/取值三件套（唯一定义处）。

收拢背景（docs/13 §三 证据 4）：_safe_str/_find_col/_get_val 曾在 analyzer.py 与
impact_analyzer.py 各有一份（行为一致），DQ/LTS excel loader（波 1b）将是第三个
消费者——第 3 份复制发生前收口。各模块以 `from excel_utils import find_col as _find_col`
方式保留旧名，既有调用点零改动。

无第三方依赖（openpyxl 由调用方自己 import）。
"""

from __future__ import annotations


def safe_str(val) -> str:
    """安全转字符串"""
    if val is None:
        return ""
    return str(val).strip()


def find_col(col_idx: dict, name: str) -> int | None:
    """查找列索引。先精确匹配，再模糊匹配（去空格+全角半角归一化）。"""
    # 精确匹配
    if name in col_idx:
        return col_idx[name]
    # 模糊匹配：去空格、全角括号转半角
    def normalize(s):
        # 全角括号 → 半角
        s = s.replace("（", "(").replace("）", ")")
        # 去所有空格
        s = s.replace(" ", "").replace("\u3000", "")
        return s
    norm_name = normalize(name)
    for actual, idx in col_idx.items():
        if normalize(actual) == norm_name:
            return idx
    # 包含匹配（期望列名是实际列名的子串，或反过来）
    for actual, idx in col_idx.items():
        na = normalize(actual)
        if norm_name in na or na in norm_name:
            return idx
    return None


def get_val(row: tuple, idx: int | None) -> str:
    """安全获取行值"""
    if idx is None or idx >= len(row):
        return ""
    return safe_str(row[idx])
