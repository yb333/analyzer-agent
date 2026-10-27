#!/usr/bin/env python3
"""调度设计还原（schedule_recovery）—— LTS 事实 → 调度声明槽位（ts 草稿 §11.5）。

定位：从 LTS 解析事实（schedule_info：f/i/other 任务 + jobs + params）与术加
GroupVariables **确定性还原**"平台配置的设计结果"（恢复声明进本体）：
    cron            → schedule_type（频率推断=确定性文法，不是语义猜测）
    tskdep 型 job   → 上游任务依赖（任务名↔job 名对应：Pjob_X ↔ task_X）
    taskParams      → lts_params（LTS 变量 → ETL 参数映射）
    GroupVariables  → exec_params（SQL 占位符及默认值）
    F/I/DQ 任务分类 → tasks.f/view/dq（task_name/job_name/cron/upstream/project/group）

纪律（12 章 §11.2 推断不冒充声明）：
    - dep_type：制品只有依赖指向，无依赖语义类型 → 留消费方默认"宽依赖"，不推断
    - "依赖上一周期"是任务**自依赖开关**（等自己上一周期），不是对上游的依赖类型——
      作任务属性随行，不冒充 dep_type
    - tskdep job 在制品内匹配不到上游任务 → resolved=False 显式标注，不编造任务名
    - 全部原文进 evidence[]（证据随行，供人审与 _reverse.schedule_evidence）

纯函数、只读；多场景消费：ts 草稿 meta.schedule / tech_design 调度章节 / 影响分析。
单一权威生产点：调度槽位只在此还原，渲染层只摆位。

Author: 院博
"""

from __future__ import annotations

import re

TSKDEP = "tskdep"          # LTS 任务依赖型 job 的 job类型 值
DEFAULT_DEP_TYPE = "宽依赖"  # 消费方默认（制品无依赖语义类型，不推断）

_TASK_PREFIX_RE = re.compile(r"^task_", re.IGNORECASE)
_JOB_PREFIX_RE = re.compile(r"^pjob_", re.IGNORECASE)
_ETL_PARAM_RE = re.compile(r"\$\{?\s*(P_\w+)\s*\}?")


# ═══════════════════════════════════════════════════════════════
# cron → schedule_type（确定性频率推断）
# ═══════════════════════════════════════════════════════════════

def infer_schedule_type(cron: str) -> str:
    """cron 表达式 → 频率标签（daily / hourly / weekly / monthly / realtime / ""）。

    支持 Quartz 6-7 段（秒 分 时 日 月 周 [年]）与 5 段（分 时 日 月 周）。
    判定是对 cron 字段形态的文法分析（确定性），判不出 → ""（不猜）。
    """
    parts = (cron or "").strip().split()
    if len(parts) not in (5, 6, 7):
        return ""
    if len(parts) == 5:
        minute, hour, dom, month, dow = parts
    else:
        _sec, minute, hour, dom, month, dow = parts[:6]

    def _any(f: str) -> bool:
        return f in ("*", "?")

    def _stepped(f: str) -> bool:
        return "/" in f

    # 周位指定（非 */?）→ 按周
    if not _any(dow) and dow.upper() not in ("*", "?"):
        return "weekly"
    # 日位指定数字/L → 按月
    if not _any(dom) and not _stepped(dom):
        return "monthly"
    # 时位：步进/多值/通配 → 按小时及更细
    if _any(hour) or _stepped(hour) or "," in hour or "-" in hour:
        if _stepped(minute) or _any(minute):
            return "realtime" if _any(minute) and _any(hour) else "hourly"
        return "hourly"
    # 时位单值 → 每日
    if hour.isdigit():
        return "daily"
    return ""


# ═══════════════════════════════════════════════════════════════
# 任务名 / job 名 ↔ 表名对应（LTS 平台命名约定：task_X / Pjob_X）
# ═══════════════════════════════════════════════════════════════

def _task_table(task_name: str) -> str:
    return _TASK_PREFIX_RE.sub("", task_name or "").strip().lower()


def _job_table(job_name: str) -> str:
    return _JOB_PREFIX_RE.sub("", job_name or "").strip().lower()


def _main_job_name(task: dict) -> str:
    """任务的主 job（第一个非 tskdep 型 job）名；无则空串。"""
    for j in task.get("jobs") or []:
        if str(j.get("type") or "").lower() != TSKDEP:
            return str(j.get("name") or "")
    return ""


# ═══════════════════════════════════════════════════════════════
# 上游依赖：tskdep 型 job → 上游任务
# ═══════════════════════════════════════════════════════════════

def extract_upstream(task: dict, tasks_index: dict) -> list:
    """任务的上游依赖清单（来自其 tskdep 型 job）。

    tskdep job 的 job 名 = 上游任务的主 job 名（Pjob_X），按表名 X 在制品内任务
    索引（{表名小写: task}）里匹配上游任务。匹配到 → 恢复 task/project/group；
    匹配不到 → resolved=False、task 留空（不编造），表名仍给出供人定位。

    Returns: [{"task", "table", "dep_type", "project", "group", "job", "resolved"}]
    """
    out = []
    for j in task.get("jobs") or []:
        if str(j.get("type") or "").lower() != TSKDEP:
            continue
        job_name = str(j.get("name") or "")
        table = _job_table(job_name)
        up = tasks_index.get(table)
        out.append({
            "task": str(up.get("task_name") or "") if up else "",
            "table": table,
            "dep_type": DEFAULT_DEP_TYPE,       # 制品无依赖语义类型——不推断
            "project": str(up.get("project") or "") if up else "",
            "group": str(up.get("task_group") or "") if up else "",
            "job": job_name,
            "resolved": up is not None,
        })
    return out


# ═══════════════════════════════════════════════════════════════
# 参数：taskParams → lts_params；GroupVariables → exec_params
# ═══════════════════════════════════════════════════════════════

def build_lts_params(task: dict) -> list:
    """taskParams → [{lts_var, etl_param, desc}]。

    etl_param = 参数值中 ${P_xxx} 形态的 ETL 占位符（确定性提取）；无则空串。
    """
    out = []
    for p in task.get("params") or []:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        m = _ETL_PARAM_RE.search(str(p.get("value") or ""))
        out.append({"lts_var": name, "etl_param": m.group(1) if m else "", "desc": ""})
    return out


def build_exec_params(group_variables: dict | None) -> dict:
    """术加 GroupVariables（{变量名: 默认值}）→ exec_params 中性结构。

    value_type/desc 制品未显式记载 → 留空（不推断）；默认值原文随行。
    """
    out = {}
    for name, default in (group_variables or {}).items():
        n = str(name or "").strip()
        if not n:
            continue
        out[n] = {"value_type": "", "desc": "", "default_value": str(default or "")}
    return out


# ═══════════════════════════════════════════════════════════════
# 总装：schedule_info → 调度声明
# ═══════════════════════════════════════════════════════════════

def _task_slot(task: dict, tasks_index: dict) -> dict:
    """单任务 → tasks.f/view/dq 槽位形态（恢复：平台配置的设计结果）。"""
    return {
        "task_name": str(task.get("task_name") or ""),
        "job_name": _main_job_name(task),
        "cron": str(task.get("schedule_cron") or ""),
        "upstream": extract_upstream(task, tasks_index),
        "project_name": str(task.get("project") or ""),
        "task_group": str(task.get("task_group") or ""),
        "depends_prev_cycle": str(task.get("depends_prev_cycle") or ""),  # 自依赖开关（非 dep_type）
    }


def recover_schedule(schedule_info: dict | None, group_variables: dict | None = None) -> dict:
    """LTS 事实 → 调度声明槽位（中性结构，渲染层按 ts 形态摆位）。

    Returns: {
      "schedule_type": str, "cron": str,          # 以 F 任务为准（资产主调度）
      "upstream_added": [...],                    # F 任务的上游（对本资产而言的依赖）
      "tasks": {"f": {...}, "view": {...}, "dq": {...}},   # 有则填，无则缺键
      "lts_params": [...], "exec_params": {...},
      "evidence": [{...原文...}],                 # 证据随行（_reverse.schedule_evidence 源）
    }
    schedule_info 为 None → 全空结构（显式空，不编造）。
    """
    si = schedule_info or {}
    all_tasks = [t for t in (si.get("all_tasks") or []) if isinstance(t, dict)]
    if not all_tasks:
        for key in ("f_tasks", "i_tasks", "other_tasks"):
            all_tasks.extend(t for t in (si.get(key) or []) if isinstance(t, dict))
    # 上游匹配索引：tskdep job 名 = 上游任务的主 job 名（Pjob_X）——同时按任务名表名
    # 与主 job 名表名索引（平台约定 task_X ↔ Pjob_X，但存量命名不严格时主 job 名更可靠）
    tasks_index: dict = {}
    for t in all_tasks:
        for key in (_task_table(t.get("task_name") or ""), _job_table(_main_job_name(t))):
            if key:
                tasks_index.setdefault(key, t)

    f_task = next(iter(si.get("f_tasks") or []), None)
    i_task = next(iter(si.get("i_tasks") or []), None)
    dq_task = next((t for t in (si.get("other_tasks") or [])
                    if str(t.get("task_name") or "").upper().endswith("_DQ")), None)

    tasks = {}
    if f_task:
        tasks["f"] = _task_slot(f_task, tasks_index)
    if i_task:
        tasks["view"] = _task_slot(i_task, tasks_index)
    if dq_task:
        tasks["dq"] = _task_slot(dq_task, tasks_index)

    cron = str((f_task or {}).get("schedule_cron") or "") if f_task else ""
    lts_params = build_lts_params(f_task) if f_task else []

    evidence = []
    for t in all_tasks:
        evidence.append({
            "task_name": str(t.get("task_name") or ""),
            "task_type": str(t.get("task_type") or ""),
            "project": str(t.get("project") or ""),
            "task_group": str(t.get("task_group") or ""),
            "cron": str(t.get("schedule_cron") or ""),
            "depends_prev_cycle": str(t.get("depends_prev_cycle") or ""),
            "tskdep_jobs": [{"job": str(j.get("name") or ""),
                             "parent": str(j.get("parent") or ""),
                             "exec_path": str(j.get("exec_path") or ""),
                             "params": str(j.get("params") or "")}
                            for j in (t.get("jobs") or [])
                            if str(j.get("type") or "").lower() == TSKDEP],
            "params": [{"name": str(p.get("name") or ""), "value": str(p.get("value") or "")}
                       for p in (t.get("params") or [])],
            "lts_file": str(t.get("lts_file") or ""),
        })

    return {
        "schedule_type": infer_schedule_type(cron),
        "cron": cron,
        "upstream_added": tasks.get("f", {}).get("upstream", []),
        "tasks": tasks,
        "lts_params": lts_params,
        "exec_params": build_exec_params(group_variables),
        "evidence": evidence,
    }
