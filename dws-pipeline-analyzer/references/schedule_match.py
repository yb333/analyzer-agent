#!/usr/bin/env python3
"""schedule_match —— LTS 任务 ↔ 源表 三档模糊匹配（SPEC 通道适配层）。

口径（docs/17-SPEC列级映射矩阵.md §六，2026-10-30 用户拍板）：
    默认模糊匹配（精确匹配在真实制品里基本命不中）。三档：
        1 等值      归一化后相等                → 备注标「精确匹配」
        2 包含      一侧含另一侧子串            → 「模糊匹配(包含)，请人工核对」
        3 相似度    difflib 比值 ≥ SIM_THRESHOLD → 「模糊匹配(相似度0.xx)，请人工核对」
    歧义保护：最佳与次佳（不同任务）得分差 < AMBIGUITY_MARGIN → 不自动填，
    任务进「未匹配列举」待人工（宁可少填，不错配调度信息）。
    全档未中 → 尾部列举行（源表列留空，备注标明不对应源表）。

    匹配键 = 任务的主 Pjob 名（jobs[0] 的 job名称，无 jobs 时用任务名兜底）；
    任务范围 = read_lts_excel 全量任务（源表的生产任务可能不在本资产规则组，
    docs/17 §六）。

分层：本模块是 SPEC 适配层构件，不进 schedule_recovery（那是 ts_draft 的
调度还原，语义不同：恢复本资产调度 vs 匹配源表调度）。

Author: 院博
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# 阈值常量（docs/17 §六缺口 2：初始值，真实 LTS 制品校准后调整）
SIM_THRESHOLD = 0.6
AMBIGUITY_MARGIN = 0.1

_STRIP_PREFIXES = ("pjob_", "task_")


def _norm(s: str) -> str:
    """匹配归一化：小写 → 去 Pjob_/task_ 前缀 → 去 schema 前缀 → 去分隔符。"""
    s = (s or "").strip().lower()
    for p in _STRIP_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    if "." in s:
        s = s.split(".")[-1]
    return re.sub(r"[_\-]", "", s)


def _task_row(task: dict) -> dict:
    """LTS 任务 → Sheet2 调度七列（docs/17 §二；调度应用列 LTS 无对应字段，留空）。"""
    jobs = task.get("jobs") or []
    # Pjob 选取（2026-11-02 实测修订）：任务可挂多个 job（start→Pjob→…链），
    # 首个 job 未必是主 Pjob（曾取到父节点链上的 job）→ 优先 Pjob_ 命名惯例，
    # 无则回退首个非空 job 名
    pjob = ""
    for j in jobs:
        n = (j.get("name") or "").strip()
        if n and n.lower().startswith("pjob"):
            pjob = n
            break
    if not pjob:
        for j in jobs:
            n = (j.get("name") or "").strip()
            if n:
                pjob = n
                break
    return {
        "task_name": task.get("task_name", ""),
        "app": "",  # 缺口 1：LTS 制品无「调度应用」对应字段
        "project": task.get("project", ""),
        "group": task.get("task_group", ""),
        "pjob": pjob,
        "schedule_type": task.get("task_type", ""),
        "cron": task.get("schedule_cron", ""),
    }


def _match_keys(task: dict) -> list:
    """任务的匹配键清单：全部 Pjob 名 + 任务名（去重）。"""
    keys = [j.get("name", "") for j in (task.get("jobs") or []) if j.get("name")]
    if task.get("task_name"):
        keys.append(task["task_name"])
    out, seen = [], set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _score(key: str, table: str):
    """单个键 × 单个源表的 (tier, score, label)。tier: 1 等值 / 2 包含 / 3 相似度 / 0 未中。

    返回 score: 1.0（等值）/ 0.9（包含）/ difflib 比值（相似度）。
    """
    nk, nt = _norm(key), _norm(table)
    if not nk or not nt:
        return 0, 0.0, ""
    if nk == nt:
        return 1, 1.0, "调度信息按Pjob名精确匹配"
    if nk in nt or nt in nk:
        return 2, 0.9, "模糊匹配(包含)，请人工核对"
    ratio = SequenceMatcher(None, nk, nt).ratio()
    if ratio >= SIM_THRESHOLD:
        return 3, ratio, f"模糊匹配(相似度{ratio:.2f})，请人工核对"
    return 0, 0.0, ""


def match_schedules(schedule_info, source_tables: list) -> tuple:
    """LTS 全量任务 × 源表清单 → (matched, unmatched_tasks)。

    Args:
        schedule_info: read_lts_excel 输出（用 all_tasks 全量，docs/17 §六）
        source_tables: [{schema, table}]（ViewContext.entity_source_tables 口径）

    Returns:
        matched: {查找键: 调度列 dict（含 tier/remark）}
            查找键 = "schema.table".lower() 与 table.lower() 双写
            （与 spec_export.build_spec_model 的 lookup 口径一致）
        unmatched_tasks: [调度列 dict + remark]（源表列留空，尾部列举，docs/17 §二）
    """
    tasks = (schedule_info or {}).get("all_tasks", []) or []
    tables = [(t.get("schema", ""), t.get("table", "")) for t in (source_tables or [])]

    # 每任务 × 每表打分 → 每表选最佳；记录歧义
    best_for_table: dict = {}      # (schema,table) -> (task, tier, score, label)
    second_score: dict = {}        # (schema,table) -> float
    task_hit: dict = {}            # id(task) -> (schema,table) 该任务被哪张表选中

    for schema, table in tables:
        scores = []                # [(score, tier, label, task)]
        for t in tasks:
            best_key = (0, 0.0, "")
            for k in _match_keys(t):
                tier, sc, label = _score(k, table)
                if sc > best_key[1]:
                    best_key = (tier, sc, label)
            if best_key[1] > 0:
                scores.append((best_key[1], best_key[0], best_key[2], t))
        scores.sort(key=lambda x: (-x[0], x[1]))
        if not scores:
            continue
        top = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        # 歧义保护：并列/近并列（含两个任务都精确命中）→ 不自动填
        if top[0] - second < AMBIGUITY_MARGIN:
            continue
        best_for_table[(schema, table)] = (top[3], top[1], top[0], top[2])

    matched = {}
    for (schema, table), (task, tier, sc, label) in best_for_table.items():
        row = _task_row(task)
        row["tier"] = tier
        row["remark"] = label
        task_hit[id(task)] = (schema, table)
        if schema:
            matched[f"{schema}.{table}".lower()] = row
            matched[table.lower()] = row
        else:
            matched[table.lower()] = row

    unmatched = []
    for t in tasks:
        if id(t) in task_hit:
            continue
        row = _task_row(t)
        row["remark"] = "LTS任务，未匹配到本资产源表（Pjob名归一化后无对应表），仅列举供人工核对"
        unmatched.append(row)

    return matched, unmatched
