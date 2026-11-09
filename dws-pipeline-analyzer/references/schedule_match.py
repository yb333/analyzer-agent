#!/usr/bin/env python3
"""schedule_match —— LTS 依赖 job ↔ 源表 调度匹配（SPEC 通道适配层）。

口径（docs/17 §六，2026-11-02 三轮实测定稿）：
    候选集 = 资产 F 任务的 **tskdep 依赖 job** 指向的上游任务——不是全量任务。
    LTS 任务图（用户口述）：资产 job 的父 job 是 start/getdata 类根节点；
    依赖 job（tskdep 型）的父 job = 资产 job，其 job 名 = 上游任务主 job 名
    （Pjob_X，X 即上游表名）。资产自身任务/主 job 一律不进候选。

    匹配规则（用户口径，一句话）：**job 名（归一化后）包含表名 → 命中；
    唯一命中就填；多个命中（歧义）或零命中 → 拿出来列举**。
    - 归一化：小写、去 Pjob_/task_ 前缀、去 schema、去 _/- 分隔符——所以
      `pjob_x_ods_acct`（工程前缀）、`pjob_ods_acct_di`（后缀）天然命中；
    - 匹配键 = tskdep job 名 + 上游任务名，任一包含即算该候选命中；
    - 唯一性是硬约束：宁可列举待人工，不错配调度信息。

分层：本模块是 SPEC 适配层构件，复用 schedule_recovery 的 tskdep 解析口径
（TSKDEP / _job_table / _main_job_name / _task_table），不重复造轮子。

Author: 院博
"""

from __future__ import annotations

import re

from schedule_recovery import TSKDEP, _job_table, _main_job_name, _task_table

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
            ep = j.get("exec_parsed") or {}
            up = tasks_index.get(key)
            # 二次索引：tskdep 执行路径里的上游任务名（集群|appid|项目|任务组|任务）
            if up is None and ep.get("task"):
                up = tasks_index.get(_task_table(ep["task"]))
            if up is None and ep.get("task"):
                # 跨制品依赖（上游任务不在本 excel）：路径坐标合成上游行——
                # 任务名/项目/任务组可用，可参与匹配；_via=path 标注出处
                up = {"task_name": ep.get("task", ""),
                      "project": ep.get("project", ""),
                      "task_group": ep.get("task_group", ""),
                      "task_type": "", "schedule_cron": "",
                      "_via": "path"}
            candidates.append({
                "job": job_name, "table_key": key,
                "up_task": up, "exec_parsed": ep,
            })
    if not candidates:
        return [], "no_tskdep"
    return candidates, "ok"


def _hit(key: str, table: str) -> bool:
    """命中判定（用户口径）：归一化后 job 名包含表名（含相等）。"""
    nk, nt = _norm(key), _norm(table)
    return bool(nk and nt and nt in nk)


def _hit_label(key: str, table: str) -> str:
    """命中方式的备注标注：等值=精确，包含=含表名。"""
    if _norm(key) == _norm(table):
        return "按job名精确匹配（含表名）"
    return "匹配（job名含表名），请人工核对"


def _upstream_row(cand: dict) -> dict:
    """依赖候选 → Sheet2 调度七列（上游任务行）。

    调度应用 = tskdep 执行路径的 appid 段（com.huawei.xxx，2026-11-09 闭合
    缺口①：此前误判"LTS 无对应字段"——路径坐标里有）；项目/任务组制品内
    任务行权威、路径坐标兜底。pjob = tskdep job 名（平台约定：= 上游任务的
    主 Pjob 名）。via = 解析出处（in_artifact / path）。
    """
    ep = cand.get("exec_parsed") or {}
    up = cand.get("up_task")
    if up is None:
        return {"task_name": "", "app": "", "project": "", "group": "",
                "pjob": cand.get("job", ""), "schedule_type": "", "cron": "",
                "unresolved": True}
    return {
        "task_name": up.get("task_name", ""),
        "app": ep.get("appid", ""),          # 调度应用 = appid（执行路径坐标）
        "project": up.get("project", "") or ep.get("project", ""),
        "group": up.get("task_group", "") or ep.get("task_group", ""),
        "pjob": cand.get("job", "") or _main_job_name(up),
        "schedule_type": up.get("task_type", ""),
        "cron": up.get("schedule_cron", ""),
        "via": up.get("_via", "in_artifact"),
    }


def match_schedules(schedule_info, source_tables: list) -> tuple:
    """资产 F 任务的 tskdep 依赖 × 源表清单 → (matched, unmatched, status)。

    匹配规则（用户口径定稿）：归一化后 job 名/上游任务名**包含**表名 → 该候选
    命中；**唯一命中自动填**；多个命中（歧义）或零命中 → 候选进尾部列举。

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

    # 只参与匹配的候选：已解析到上游任务的（未解析的只列举，防半空行）
    usable = [c for c in candidates if c["up_task"] is not None]

    def _cand_hits(c, table):
        """候选命中 + 命中键（job 名或上游任务名，含表名即中）。"""
        keys = [c["job"], (c["up_task"].get("task_name") or "")]
        hit_keys = [k for k in keys if _hit(k, table)]
        if not hit_keys:
            return None
        # 等值优先出标注（job 名先于任务名）
        for k in hit_keys:
            if _norm(k) == _norm(table):
                return k
        return hit_keys[0]

    matched = {}
    used = set()
    for schema, table in tables:
        hits = []
        for c in usable:
            k = _cand_hits(c, table)
            if k is not None:
                hits.append((c, k))
        if len(hits) != 1:
            # 零命中（源表侧无事发生）或多命中（歧义，候选留待列举）
            continue
        cand, key = hits[0]
        row = _upstream_row(cand)
        row["remark"] = _hit_label(key, table)
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
                             f"未唯一匹配到本资产源表（零命中或歧义），仅列举供人工核对")
        unmatched.append(row)

    return matched, unmatched, "ok"
