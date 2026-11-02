#!/usr/bin/env python3
"""schedule_match —— LTS 依赖 job ↔ 源表 调度匹配（SPEC 通道适配层）。

口径（docs/17-SPEC列级映射矩阵.md §六，2026-11-02 实测修订⑧）：
    候选集 = 资产 F 任务的 **tskdep 依赖 job** 指向的上游任务——不是全量任务。
    LTS 任务图（用户口述）：资产 job 的父 job 是 start/getdata 类根节点；
    依赖 job（tskdep 型）的父 job = 资产 job，其 job 名 = 上游任务主 job 名
    （Pjob_X，X 即上游表名）。资产自身任务/主 job 一律不进候选。

    匹配两层：
        精确层  tskdep job 名解出的表名 == 源表裸名（确定性）
        模糊层  三档：等值 / 包含 / 相似度 ≥ SIM_THRESHOLD（真实制品 Pjob 命
                名常与表名有出入，模糊为主力）
    歧义保护：最佳与次佳得分差 < AMBIGUITY_MARGIN → 不自动填，列举待人工
    （宁可少填，不错配调度信息）。全档未中 → 尾部列举行。

分层：本模块是 SPEC 适配层构件，复用 schedule_recovery 的 tskdep 解析口径
（TSKDEP / _job_table / _main_job_name / _task_table），不重复造轮子。

Author: 院博
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from schedule_recovery import TSKDEP, _job_table, _main_job_name, _task_table

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


# ════════════════════════════════════════════════════════════
# 依赖候选提取（2026-11-02 实测修订⑧：只放「依赖的」job，不放资产自身 job）
# ════════════════════════════════════════════════════════════

def dependency_candidates(schedule_info) -> tuple:
    """资产 F 任务的 tskdep 依赖 job → 候选清单。

    LTS 任务图（用户口述，2026-11-02）：
        资产 job 的父 job 一般是 start/getdata 类根节点；
        依赖 job（tskdep 型）的父 job = 资产 job，其 job 名 = 上游任务的
        主 job 名（Pjob_X，X 即上游表名）。
    所以「数据源调度」要填的是这些 tskdep 指向的**上游任务**——资产自身的
    任务/主 job 一律不进候选（曾因此把资产 job 填进了源表行）。

    Returns: (candidates, status)
        candidates: [{job, table_key, up_task(resolved 任务 dict 或 None)}]
        status: "ok" | "no_f_task"（无资产 F 任务）| "no_tskdep"（F 任务无 tskdep job）
    """
    si = schedule_info or {}
    f_tasks = [t for t in (si.get("f_tasks") or []) if isinstance(t, dict)]
    if not f_tasks:
        return [], "no_f_task"
    # 任务索引与 recover_schedule 同构（任务名表名 + 主 job 名表名）。
    # 索引底池用全量任务（_all_raw_tasks 私有键）——上游任务通常不带本规则组的
    # V_GROUP_CODE，不随 all_tasks 流动，但 tskdep 解析必须找得到它们
    pool = si.get("_all_raw_tasks")
    if pool is None:
        pool = si.get("all_tasks") or []
    all_tasks = [t for t in pool if isinstance(t, dict)]
    tasks_index: dict = {}
    for t in all_tasks:
        for key in (_task_table(t.get("task_name") or ""),
                    _job_table(_main_job_name(t))):
            if key:
                tasks_index.setdefault(key, t)
    candidates = []
    for ft in f_tasks:
        for j in ft.get("jobs") or []:
            if str(j.get("type") or "").lower() != TSKDEP:
                continue
            job_name = str(j.get("name") or "")
            key = _job_table(job_name)
            candidates.append({
                "job": job_name, "table_key": key,
                "up_task": tasks_index.get(key),
            })
    if not candidates:
        return [], "no_tskdep"
    return candidates, "ok"


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


def _upstream_row(cand: dict) -> dict:
    """依赖候选 → Sheet2 调度七列（上游任务行；调度应用列 LTS 无对应字段，留空）。

    pjob = tskdep job 名（平台约定：= 上游任务的主 Pjob 名）。
    """
    up = cand.get("up_task")
    if up is None:
        return {"task_name": "", "app": "", "project": "", "group": "",
                "pjob": cand.get("job", ""), "schedule_type": "", "cron": "",
                "unresolved": True}
    return {
        "task_name": up.get("task_name", ""),
        "app": "",  # 缺口 1：LTS 制品无「调度应用」对应字段
        "project": up.get("project", ""),
        "group": up.get("task_group", ""),
        "pjob": cand.get("job", "") or _main_job_name(up),
        "schedule_type": up.get("task_type", ""),
        "cron": up.get("schedule_cron", ""),
    }


def match_schedules(schedule_info, source_tables: list) -> tuple:
    """资产 F 任务的 tskdep 依赖 × 源表清单 → (matched, unmatched, status)。

    匹配两层（docs/17 §六实测修订⑧）：
    1. 精确层：tskdep job 名解出的表名（_job_table）== 源表裸名（确定性，
       多候选并列 → 歧义保护不自动填）；
    2. 模糊层：候选 job 名/上游任务名 × 源表，三档（等值/包含/相似度）。

    Returns:
        matched: {查找键: 调度列 dict（含 remark）}，键 = "schema.table".lower()
            与裸表名小写双写（与 spec_export lookup 口径一致）
        unmatched: [调度列 dict + remark]（依赖 job 未命中源表的，尾部列举）
        status: ok | no_f_task | no_tskdep（后两者调度列留空 + 两态披露）
    """
    candidates, status = dependency_candidates(schedule_info)
    if status != "ok":
        return {}, [], status
    tables = [(t.get("schema", ""), t.get("table", ""))
              for t in (source_tables or [])]

    best_for_table: dict = {}   # (schema, table) -> (cand, score, label)
    for schema, table in tables:
        bare = (table or "").lower()
        scores = []
        for c in candidates:
            if c["up_task"] is None:
                continue    # 未解析到上游任务的候选不参与匹配（防半空行），只列举
            if c["table_key"] and c["table_key"] == bare:
                scores.append((2.0, 0, "按依赖job（tskdep）表名精确匹配", c))
                continue
            best_key = (0, 0.0, "")
            for k in (c["job"],
                      (c["up_task"].get("task_name") or "")):
                tier, sc, label = _score(k, table)
                if sc > best_key[1]:
                    best_key = (tier, sc, label)
            if best_key[1] > 0:
                scores.append((best_key[1], best_key[0], best_key[2], c))
        scores.sort(key=lambda x: (-x[0], x[1]))
        if not scores:
            continue
        top = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        if top[0] - second < AMBIGUITY_MARGIN:
            continue            # 歧义保护：近并列不自动填
        best_for_table[(schema, table)] = (top[3], top[0], top[2])

    matched = {}
    used = set()
    for (schema, table), (cand, _sc, label) in best_for_table.items():
        row = _upstream_row(cand)
        row["remark"] = label
        used.add(id(cand))
        if schema:
            matched[f"{schema}.{table}".lower()] = row
            matched[table.lower()] = row
        else:
            matched[table.lower()] = row

    unmatched = []
    for c in candidates:
        if id(c) in used:
            continue
        row = _upstream_row(c)
        if c["up_task"] is None:
            row["remark"] = (f"依赖job（tskdep）指向 {c['job']}，制品内未找到"
                             f"对应上游任务，请人工核对")
        else:
            row["remark"] = (f"依赖job（tskdep）指向表 {c['table_key']}，"
                             f"未匹配到本资产源表，仅列举供人工核对")
        unmatched.append(row)

    return matched, unmatched, "ok"
