#!/usr/bin/env python3
"""输入装配层（inputs）——「源 → AssetInputs」的唯一归属。

职责（docs/13 §四，波 1a 落地）：
    - 源发现：DDL / LTS / DQ / I 视图的代码仓发现（自 analyzer.py 平移，行为零变）
    - 装配函数：I 视图步骤追加、knowledge.meta 注入（注入有主）、baseline 投影上下文组装
    - 波 1b/3 将进入：DQ/LTS excel loader、manifest 装配（三态语义）、连库 DDL 导出

边界纪律：
    - 本模块不知道任何契约版本/消费场景（baseline_v1 的校验与投影在 baseline_export）
    - 引擎接口只描述「要什么」不描述「谁在用」（context_out 由本层持有、投影按需消费）
    - analyzer.py 保留 re-export（过渡期），新代码请直接 from inputs import

Author: 院博
"""

from __future__ import annotations

from pathlib import Path

from engine import RawRule, _is_intermediate_table
from excel_utils import safe_str as _safe_str, find_col as _find_col, get_val as _get_val


# ═══════════════════════════════════════════════════════════════
# 代码仓定位与 DDL 发现（自 analyzer.py 平移）
# ═══════════════════════════════════════════════════════════════

def _find_repo_root(start_dir: Path) -> Path | None:
    """从 start_dir 逐级向上，找到代码仓根（含 BFT/ + DDL/ 的目录）。

    代码仓根目录下有 BFT/DDL/DQ/LTS/ADMS/Release 等目录，且这些目录名在
    根目录下一层唯一（无同名），向上探测可靠零误判。
    """
    current = start_dir.resolve()
    for parent in [current] + list(current.parents):
        if (parent / "BFT").is_dir() and (parent / "DDL").is_dir():
            return parent
    return None


def _auto_discover_ddl_from_repo(yml_dir: Path, rules: list) -> str:
    """从代码仓结构自动发现目标表的 DDL 文件路径。

    代码仓 DDL 目录结构：DDL/{DWS_EDW|DWS_RT_EDW}/{schema}/table/{target_table}.sql
    从 yml 目录向上找仓根，再按 target_schema + target_table 定位 DDL。
    两层（DWS_EDW / DWS_RT_EDW）都试，找到第一个就返回其父目录（parse_ddl 接收目录）。

    Returns: DDL 文件所在目录路径（供 parse_ddl_for_metadata 扫描）；找不到返回 ""。
    """
    repo_root = _find_repo_root(yml_dir)
    if not repo_root:
        return ""

    # 取目标表信息，优先级：
    # 1. 交换分区的 exchange_source_table（交换分区时真正目标表）
    # 2. 最后一个非中间表、非视图步骤的 target_table
    # 3. 兜底：最后一个规则的 target_table
    target_schema = ""
    target_table = ""
    # 先找交换分区的 exchange_source_table
    for rule in reversed(rules):
        if rule.rule_type == 9 and rule.exchange_source_table:
            target_schema = rule.target_schema
            target_table = rule.exchange_source_table
            break
    # 没有交换分区，找最后一个非中间表
    if not target_table:
        for rule in reversed(rules):
            if getattr(rule, "is_view_step", False):
                continue
            if not _is_intermediate_table(rule.target_table):
                target_schema = rule.target_schema
                target_table = rule.target_table
                break
    # 兜底
    if not target_table and rules:
        target_schema = rules[-1].target_schema
        target_table = rules[-1].target_table
    if not target_table:
        return ""

    table_lower = target_table.lower()
    schema_lower = target_schema.lower() if target_schema else ""

    # 两层都试：DWS_EDW（离线）/ DWS_RT_EDW（实时）
    for layer in ("DWS_EDW", "DWS_RT_EDW"):
        ddl_root = repo_root / "DDL" / layer
        if not ddl_root.is_dir():
            continue
        # schema 目录可能是 dws / DWS 等，大小写容错
        schema_dir = None
        if schema_lower:
            for sd in ddl_root.iterdir():
                if sd.is_dir() and sd.name.lower() == schema_lower:
                    schema_dir = sd
                    break
        if not schema_dir:
            continue
        # table 目录下找目标表 DDL（包含匹配 + 多扩展名，不靠严格相等）
        # 匹配规则：文件名（去扩展名）等于表名，或文件名以"表名."开头（schema前缀），
        # 或表名是文件名的一部分（容错 create_table_xxx / xxx_v2 等前缀后缀）。
        table_dir = schema_dir / "table"
        if table_dir.is_dir():
            if _find_ddl_file(table_dir, table_lower):
                # 返回 schema 目录（而非 table 目录），parse_ddl 用 rglob 递归扫描，
                # 能同时覆盖 table/（F表DDL）和 view/（I视图DDL）
                return str(schema_dir)
    return ""


# ═══════════════════════════════════════════════════════════════
# LTS 调度任务发现（自 analyzer.py 平移）
# ═══════════════════════════════════════════════════════════════

def _parse_lts_task(yml_path: Path) -> dict | None:
    """解析一个 LTS 任务 yml，提取关键调度信息。

    两级容错：
    1. 先尝试标准 yaml.safe_load（理想情况，格式规范的 yml）
    2. 失败则用文本解析（真实 yml 从 Excel 转来，格式常不规范：
       冒号无空格、block scalar 吞掉子表等）
    """
    import re
    raw_text = yml_path.read_text(encoding="utf-8")

    # 先尝试标准 YAML 解析
    import yaml
    try:
        data = yaml.safe_load(raw_text)
        if isinstance(data, dict) and (data.get("*任务名称") or data.get("任务名称")):
            result = _parse_lts_from_dict(data)
            if result:
                result["lts_file"] = str(yml_path)
            return result
    except Exception:
        pass

    # 标准 YAML 失败，用文本解析兜底
    result = _parse_lts_from_text(raw_text)
    if result:
        result["lts_file"] = str(yml_path)
    return result


def _extract_jobs_from_text(text):
    """从 block scalar 字符串（额外信息）里提取 Jobs 列表。

    真实 yml 里 Jobs 被「额外信息: |」的 block scalar 吞掉，
    成了一段字符串文本。这里按 *job名称 分块提取。
    """
    import re
    jobs = []
    job_starts = [(m.start(), m.group(1).strip())
                  for m in re.finditer(r"\*?job名称['\"]?\s*[:：]\s*(\S+)", text)]
    for i, (start, name) in enumerate(job_starts):
        end = job_starts[i + 1][0] if i + 1 < len(job_starts) else len(text)
        block = text[start:end]
        jobs.append({
            "name": name,
            "type": _find_block(block, r"\*?job类型['\"]?\s*[:：]\s*(\S+)"),
            "parent": _find_block(block, r"\*?job的父节点名称['\"]?\s*[:：]\s*(\S+)"),
            "retry_count": _find_block(block, r"job重试次数\s*[:：]\s*'?(\d+)"),
            "retry_interval": _find_block(block, r"job重试间隔\s*[:：]\s*'?(\d+)"),
            "timeout": _find_block(block, r"job超时时间\s*[:：]\s*'?(\d+)"),
            "error_handler": _find_block(block, r"job异常处理方式\s*[:：]\s*(\S+)"),
        })
    return jobs


def _extract_params_from_text(text):
    """从 block scalar 字符串提取任务参数（参数名称→参数值 成对）。

    返回 (params_list, group_code)。
    """
    import re
    params = []
    group_code = ""
    cur_name = ""
    for line in text.split("\n"):
        m_name = re.search(r"\*?参数名称['\"]?\s*[:：]\s*(\S+)", line)
        if m_name:
            cur_name = m_name.group(1).strip().rstrip("'")
            continue
        m_val = re.search(r"参数值\s*[:：]\s*(.*)", line)
        if m_val and cur_name:
            pval = m_val.group(1).strip().rstrip("'\"")
            params.append({"name": cur_name, "value": pval})
            if cur_name == "V_GROUP_CODE":
                group_code = pval
            cur_name = ""
    return params, group_code


def _parse_lts_from_dict(data: dict) -> dict | None:
    """从标准 YAML dict 解析 LTS 任务信息。

    真实 yml 里 Jobs 和 taskParams 可能在「额外信息」的 block scalar 里
    （整个被吞成一段字符串），需要从字符串二次提取。
    """
    # 任务名（key 带星号前缀 '*任务名称'）
    task_name = (data.get("*任务名称") or data.get("任务名称") or "").strip()

    # 找 block scalar 字符串（Jobs 和 taskParams 可能都在里面）
    block_scalar_text = ""
    for k in data:
        v = data[k]
        if isinstance(v, str) and ("job名称" in v or "参数名称" in v):
            block_scalar_text = v
            break

    # 任务参数：优先从顶层 taskParams 取，取不到则从 block scalar 提取
    group_code = ""
    params = []
    raw_params = data.get("taskParams") or data.get("任务参数") or []
    if isinstance(raw_params, list) and raw_params:
        # 顶层 taskParams 是结构化 list
        for p in raw_params:
            if not isinstance(p, dict):
                continue
            pname = (p.get("*参数名称") or p.get("参数名称") or "").strip()
            pval = p.get("参数值") or ""
            params.append({"name": pname, "value": str(pval)})
            if pname == "V_GROUP_CODE":
                group_code = str(pval).strip()
    elif block_scalar_text:
        # 从 block scalar 字符串提取参数
        params, gc = _extract_params_from_text(block_scalar_text)
        if params:
            if gc:
                group_code = gc

    # Jobs：优先从顶层 Jobs 取，取不到则从 block scalar 提取
    jobs = []
    raw_jobs = data.get("Jobs") or []
    if not raw_jobs and block_scalar_text:
        raw_jobs = _extract_jobs_from_text(block_scalar_text)
    if isinstance(raw_jobs, list):
        for j in raw_jobs:
            if not isinstance(j, dict):
                continue
            # 如果已经有 name key（来自 _extract_jobs_from_text），直接用
            if "name" in j:
                jobs.append(j)
            else:
                # 来自标准 YAML dict，用中文 key 提取
                jobs.append({
                    "name": (j.get("*job名称") or j.get("job名称") or "").strip(),
                    "type": (j.get("*job类型") or j.get("job类型") or "").strip(),
                    "parent": (j.get("*job的父节点名称") or j.get("job的父节点名称") or "").strip(),
                    "retry_count": str(j.get("job重试次数") or ""),
                    "retry_interval": str(j.get("job重试间隔") or ""),
                    "timeout": str(j.get("job超时时间") or ""),
                    "error_handler": str(j.get("job异常处理方式") or ""),
                })

    return _build_lts_result(data, task_name, group_code, params, jobs)


def _parse_lts_from_text(text: str) -> dict | None:
    """从非标准 yml 文本解析 LTS 任务信息（容错）。

    真实 yml 从 Excel 转来，格式常不规范（冒号无空格、block scalar 吞掉子表）。
    用正则按字段名提取关键信息。
    """
    import re

    def _find(pattern, default=""):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    # 顶层任务字段
    task_name = _find(r"\*?任务名称['\"]?\s*[:：]\s*(\S+)")
    if not task_name:
        return None
    task_type = _find(r"\*?任务类型['\"]?\s*[:：]\s*(.+)")
    schedule_cron = _find(r"调度周期\s*[:：]\s*(.+)")
    owner = _find(r"\*?责任人['\"]?\s*[:：]\s*(\S+)")
    start_time = _find(r"开始时间\s*[:：]\s*(.+)")
    depends_prev = _find(r"依赖上一周期\s*[:：]\s*(\S+)")
    multi_day = _find(r"是否一天多调\s*[:：]\s*(\S+)")
    task_group = _find(r"\*?任务组名称['\"]?\s*[:：]\s*(\S+)")
    project = _find(r"\*?项目名称['\"]?\s*[:：]\s*(\S+)")

    # 任务参数：按行扫描成对提取（参数名称→参数值）
    group_code = ""
    params = []
    _cur_param_name = ""
    for line in text.split("\n"):
        # 匹配 参数名称:XXX 或 *参数名称:XXX
        m_name = re.search(r"\*?参数名称['\"]?\s*[:：]\s*(\S+)", line)
        if m_name:
            _cur_param_name = m_name.group(1).strip()
            continue
        # 匹配 参数值:XXX
        m_val = re.search(r"参数值\s*[:：]\s*(.*)", line)
        if m_val and _cur_param_name:
            pval = m_val.group(1).strip().rstrip("'\"")
            params.append({"name": _cur_param_name, "value": pval})
            if _cur_param_name == "V_GROUP_CODE":
                group_code = pval
            _cur_param_name = ""

    # Jobs：按 *job名称 分块提取
    jobs = []
    # 找所有 job 名称位置
    job_starts = [(m.start(), m.group(1).strip())
                  for m in re.finditer(r"\*?job名称['\"]?\s*[:：]\s*(\S+)", text)]
    for i, (start, name) in enumerate(job_starts):
        # job 块范围：从当前 job 到下一个 job（或文件末尾）
        end = job_starts[i + 1][0] if i + 1 < len(job_starts) else len(text)
        block = text[start:end]
        jobs.append({
            "name": name,
            "type": _find_block(block, r"\*?job类型['\"]?\s*[:：]\s*(\S+)"),
            "parent": _find_block(block, r"\*?job的父节点名称['\"]?\s*[:：]\s*(\S+)"),
            "retry_count": _find_block(block, r"job重试次数\s*[:：]\s*'?(\d+)"),
            "retry_interval": _find_block(block, r"job重试间隔\s*[:：]\s*'?(\d+)"),
            "timeout": _find_block(block, r"job超时时间\s*[:：]\s*'?(\d+)"),
            "error_handler": _find_block(block, r"job异常处理方式\s*[:：]\s*(\S+)"),
        })

    return {
        "task_name": task_name,
        "task_type": task_type,
        "schedule_cron": schedule_cron,
        "owner": owner,
        "start_time": start_time,
        "depends_prev_cycle": depends_prev,
        "multi_day": multi_day,
        "task_group": task_group,
        "project": project,
        "group_code": group_code,
        "jobs": jobs,
        "params": params,
        "lts_file": "",  # 调用方会填充
    }


def _find_block(text, pattern, default=""):
    """在文本块里找第一个匹配。"""
    import re
    m = re.search(pattern, text)
    return m.group(1).strip() if m else default


def _build_lts_result(data, task_name, group_code, params, jobs):
    """组装标准 YAML 解析的 LTS 结果。"""
    return {
        "task_name": task_name,
        "task_type": str(data.get("*任务类型") or data.get("任务类型") or ""),
        "schedule_cron": str(data.get("调度周期") or ""),
        "owner": str(data.get("*责任人") or data.get("责任人") or ""),
        "start_time": str(data.get("开始时间") or ""),
        "depends_prev_cycle": str(data.get("依赖上一周期") or ""),
        "multi_day": str(data.get("是否一天多调") or ""),
        "task_group": str(data.get("*任务组名称") or data.get("任务组名称") or ""),
        "project": str(data.get("*项目名称") or data.get("项目名称") or ""),
        "group_code": group_code,
        "jobs": jobs,
        "params": params,
        "lts_file": "",
    }


def _classify_lts_tasks(matched: list, lookup: dict | None = None) -> dict:
    """LTS 任务分类（_F/_I/其他）+ F→I 命名关联。excel/yml 两源共用的唯一权威定义。

    I 任务通常不带 V_GROUP_CODE（不会被规则组匹配命中），按 _F→_I 命名从
    lookup（同批候选任务索引，key=任务名）关联补入 i_tasks。

    Returns: {"f_tasks": [...], "i_tasks": [...], "other_tasks": [...], "all_tasks": [...]}
    """
    f_tasks = [t for t in matched if t["task_name"].endswith("_F")]
    i_tasks = [t for t in matched if t["task_name"].endswith("_I")]
    other_tasks = [t for t in matched
                   if not t["task_name"].endswith("_F") and not t["task_name"].endswith("_I")]
    pool = dict(lookup or {})
    for t in matched:
        pool.setdefault(t["task_name"], t)
    for f_task in f_tasks:
        i_task = pool.get(f_task["task_name"][:-2] + "_I")
        if i_task and i_task not in i_tasks:
            i_tasks.append(i_task)
    all_tasks = list(matched)
    for t in i_tasks:
        if t not in all_tasks:
            all_tasks.append(t)
    return {"f_tasks": f_tasks, "i_tasks": i_tasks, "other_tasks": other_tasks,
            "all_tasks": all_tasks}


def _discover_lts_from_repo(yml_dir: Path, rule_group_code: str) -> dict | None:
    """从代码仓 LTS/ 目录发现规则组对应的调度任务（支持多调度匹配同一规则组）。

    匹配逻辑：
    1. 扫所有 LTS/**/*.yml，找 taskParams 里 V_GROUP_CODE == rule_group_code 的所有任务
    2. 按任务名分类：_F 结尾的是 F 任务，_I 结尾的是 I 任务，其他归为其他
    3. 返回 {f_tasks: [...], i_tasks: [...], other_tasks: [...]} 或 None

    一个规则组可能被多个调度任务调用（临时/特殊场景），全部收集。

    Returns: {"f_tasks": [...], "i_tasks": [...]} 或 None
    """
    if not rule_group_code:
        return None

    repo_root = _find_repo_root(yml_dir)
    if not repo_root:
        return None

    lts_root = repo_root / "LTS"
    if not lts_root.is_dir():
        return None

    # 两阶段定位：先快速 grep 找含 rule_group_code 的文件，再只解析这几个
    # 避免全量解析所有 LTS yml（可能几百个），从十几秒降到毫秒级
    candidate_files = []
    for yml_path in lts_root.rglob("*.yml"):
        # 快速文本搜索（不做 YAML 解析），找含 rule_group_code 的文件
        try:
            content = yml_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if rule_group_code in content:
            candidate_files.append(yml_path)

    if not candidate_files:
        return None

    # 只解析命中的文件（通常 1-5 个）
    matched = []
    by_task_name = {}
    for yml_path in candidate_files:
        task = _parse_lts_task(yml_path)
        if not task or not task["task_name"]:
            continue
        by_task_name[task["task_name"]] = task
        if task["group_code"] == rule_group_code:
            matched.append(task)

    # 分类 + F→I 命名关联（_classify_lts_tasks：excel/yml 两源共用的唯一权威定义）
    result = _classify_lts_tasks(matched, by_task_name)

    # yml 源补充：候选文件里没有的 I 任务，按文件名在同目录找
    # （文件名通常是 TASK_XXX_I.yml；excel 源无此步骤——所有任务行都在表里）
    i_tasks = result["i_tasks"]
    for f_task in result["f_tasks"]:
        f_name = f_task["task_name"]
        if not f_name.endswith("_F"):
            continue
        i_name = f_name[:-2] + "_I"
        if any(t["task_name"] == i_name for t in i_tasks):
            continue
        f_file = Path(f_task["lts_file"])
        i_file = f_file.parent / (i_name + ".yml")
        if i_file.exists():
            i_task = _parse_lts_task(i_file)
            if i_task and i_task not in i_tasks:
                i_tasks.append(i_task)
                result["all_tasks"].append(i_task)

    return result


# ═══════════════════════════════════════════════════════════════
# LTS excel 读取器（平台导出三 sheet，yml 的原始形态）
# ═══════════════════════════════════════════════════════════════

def _read_lts_excel_jobs(wb) -> dict:
    """jobs sheet → {任务名称: [job dict]}。sheet 缺失/空 → {}（显式空，不猜）。"""
    if "jobs" not in wb.sheetnames:
        return {}
    rows = list(wb["jobs"].iter_rows(values_only=True))
    if not rows:
        return {}
    headers = [_safe_str(h) for h in rows[0]]
    col = {h: i for i, h in enumerate(headers) if h}
    idx = {k: _find_col(col, k) for k in
           ("任务名称", "job名称", "job类型", "job的父节点名称",
            "job重试次数", "job重试间隔", "job超时时间", "job异常处理方式")}
    out: dict = {}
    for row in rows[1:]:
        if not row:
            continue
        task_name = _get_val(row, idx["任务名称"])
        if not task_name:
            continue
        out.setdefault(task_name, []).append({
            "name": _get_val(row, idx["job名称"]),
            "type": _get_val(row, idx["job类型"]),
            "parent": _get_val(row, idx["job的父节点名称"]),
            "retry_count": _get_val(row, idx["job重试次数"]),
            "retry_interval": _get_val(row, idx["job重试间隔"]),
            "timeout": _get_val(row, idx["job超时时间"]),
            "error_handler": _get_val(row, idx["job异常处理方式"]),
        })
    return out


def _read_lts_excel_params(wb) -> dict:
    """taskParams sheet → {任务名称: [{name, value}]}。sheet 缺失/空 → {}。"""
    if "taskParams" not in wb.sheetnames:
        return {}
    rows = list(wb["taskParams"].iter_rows(values_only=True))
    if not rows:
        return {}
    headers = [_safe_str(h) for h in rows[0]]
    col = {h: i for i, h in enumerate(headers) if h}
    idx = {k: _find_col(col, k) for k in ("任务名称", "参数名称", "参数值")}
    out: dict = {}
    for row in rows[1:]:
        if not row:
            continue
        task_name = _get_val(row, idx["任务名称"])
        if not task_name:
            continue
        out.setdefault(task_name, []).append({
            "name": _get_val(row, idx["参数名称"]),
            "value": _get_val(row, idx["参数值"]),
        })
    return out


def read_lts_excel(excel_path, rule_group_code: str = "") -> dict | None:
    """读取 LTS 调度任务 excel（平台导出）→ 与 yml 发现同构的 schedule dict。

    LTS yml 就是从本 excel 格式转出的（tasks/jobs/taskParams 三 sheet），
    本函数是 excel 源 loader，与 _discover_lts_from_repo（yml 源）产出同构
    （task 字段/分类结构/lts_file 记路径），投影层（baseline_export / view）
    无感源差异（docs/13：loader 接口契约）。

    三 sheet 列与 yml 字段的对应：
        tasks      一行一任务：项目名称/任务组名称/任务名称/任务类型/开始时间/
                   调度周期/依赖上一周期/责任人/是否一天多调
        jobs       一行一 job（按任务名称关联）：job名称/job类型/job的父节点名称/
                   job重试次数/job重试间隔/job超时时间/job异常处理方式
        taskParams 一行一参数（按任务名称关联）：参数名称/参数值
                   （V_GROUP_CODE → group_code，与 yml 同口径）
    jobs/taskParams sheet 缺失或为空 → jobs/params 为空列表（显式空）。

    Args:
        excel_path: LTS excel 文件路径
        rule_group_code: 规则组编码。给定且有任务 V_GROUP_CODE 匹配 → 只收匹配
            任务（与 yml 发现同口径）；给空或全不匹配 → 全收（excel 是按资产
            导出的显式输入，如 V_GROUP_CODE 列值为空时信任其范围）。

    Returns: {"f_tasks": [...], "i_tasks": [...], "other_tasks": [...], "all_tasks": [...]}
        或 None（文件不存在 / 无 tasks sheet / 读不到任务行）。
    """
    import openpyxl

    path = Path(excel_path)
    if not path.exists():
        return None
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        if "tasks" not in wb.sheetnames:
            return None
        rows = list(wb["tasks"].iter_rows(values_only=True))
        if not rows:
            return None
        headers = [_safe_str(h) for h in rows[0]]
        col = {h: i for i, h in enumerate(headers) if h}
        idx = {k: _find_col(col, k) for k in
               ("项目名称", "任务组名称", "任务名称", "任务类型", "开始时间",
                "调度周期", "依赖上一周期", "责任人", "是否一天多调")}
        jobs_by_task = _read_lts_excel_jobs(wb)
        params_by_task = _read_lts_excel_params(wb)
        # v1.2 provenance：实际读到的 sheet 清单（私有 key，随 dict 流动到
        # meta.schedule，baseline 投影层读出进 provenance[].sheets 并裁掉）
        source_sheets = [s for s in ("tasks", "jobs", "taskParams")
                         if s in wb.sheetnames]

        tasks = []
        for row in rows[1:]:
            if not row:
                continue
            task_name = _get_val(row, idx["任务名称"])
            if not task_name:
                continue
            params = params_by_task.get(task_name, [])
            group_code = ""
            for p in params:
                if p.get("name") == "V_GROUP_CODE":
                    group_code = str(p.get("value") or "").strip()
                    break
            tasks.append({
                "task_name": task_name,
                "task_type": _get_val(row, idx["任务类型"]),
                "schedule_cron": _get_val(row, idx["调度周期"]),
                "owner": _get_val(row, idx["责任人"]),
                "start_time": _get_val(row, idx["开始时间"]),
                "depends_prev_cycle": _get_val(row, idx["依赖上一周期"]),
                "multi_day": _get_val(row, idx["是否一天多调"]),
                "task_group": _get_val(row, idx["任务组名称"]),
                "project": _get_val(row, idx["项目名称"]),
                "group_code": group_code,
                "jobs": jobs_by_task.get(task_name, []),
                "params": params,
                "lts_file": str(path),  # 与 yml 源同构（provenance 采集用）
            })
    finally:
        wb.close()

    if not tasks:
        return None
    matched = [t for t in tasks
               if rule_group_code and t["group_code"] == rule_group_code]
    if not matched:
        matched = tasks
    # lookup=全表：匹配模式下 I 任务（无 V_GROUP_CODE）仍可按 _F→_I 命名关联，
    # 与 yml 发现的候选集语义一致
    result = _classify_lts_tasks(matched, {t["task_name"]: t for t in tasks})
    result["_source_sheets"] = source_sheets
    return result


# ═══════════════════════════════════════════════════════════════
# DQ 质量规则发现（自 analyzer.py 平移；excel loader 等模板到位后进入）
# ═══════════════════════════════════════════════════════════════

def _parse_dq_yml(yml_path: Path) -> dict | None:
    """解析一个 DQ 规则 yml，提取关键字段。

    DQ yml 是规范 YAML（无 *前缀问题），但容错处理。
    """
    import yaml
    try:
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
    except Exception:
        # YAML 解析失败，尝试文本解析
        return _parse_dq_from_text(yml_path.read_text(encoding="utf-8"))

    return {
        "rule_number": str(data.get("规则Number") or data.get("规则编号") or ""),
        "rule_name": str(data.get("规则中文名") or data.get("规则名称") or ""),
        "rule_desc": str(data.get("规则中文描述") or data.get("规则描述") or ""),
        "check_type": str(data.get("检查类型") or ""),
        "alert_level": str(data.get("告警级别") or ""),
        "sql": str(data.get("SQL") or data.get("sql") or ""),
        "target_table": str(data.get("目标表") or data.get("目标表编码") or ""),
        "owner": str(data.get("规则负责人") or ""),
        "dq_file": str(yml_path),
    }


def _parse_dq_from_text(text: str) -> dict | None:
    """DQ yml YAML 解析失败时的文本兜底解析。"""
    import re

    def _find(pattern, default=""):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    rule_number = _find(r"规则Number\s*[:：]\s*(\S+)")
    if not rule_number:
        rule_number = _find(r"规则编号\s*[:：]\s*(\S+)")
    if not rule_number:
        return None

    # SQL 是 block scalar，取 |- 后到下一个顶层 key 的内容
    sql = ""
    sql_match = re.search(r"SQL\s*[:：]\s*\|-?\s*\n((?:\s+.*\n)*)", text)
    if sql_match:
        sql = sql_match.group(1).strip()

    return {
        "rule_number": rule_number,
        "rule_name": _find(r"规则中文名\s*[:：]\s*(.+)", ),
        "rule_desc": _find(r"规则中文描述\s*[:：]\s*(.+)"),
        "check_type": _find(r"检查类型\s*[:：]\s*(\S+)"),
        "alert_level": _find(r"告警级别\s*[:：]\s*'?(\S+)"),
        "sql": sql,
        "target_table": _find(r"目标表\s*[:：]\s*(\S+)"),
        "owner": _find(r"规则负责人\s*[:：]\s*(\S+)"),
        "dq_file": "",
    }


def read_dq_excel(excel_path) -> list | None:
    """读取 DQ 质量规则 excel（平台导出）→ 与 yml 解析同构的 dq_rules 列表。

    excel 是 LTS/DQ yml 的原始形态（平台从该 excel 转出 yml），本函数是 DQ 源
    loader，与 _parse_dq_yml（yml 源）产出同构，投影层（baseline_export / view）
    无感源差异（docs/13：loader 接口契约）。

    只消费主 sheet「规则基本信息」——需求已明确的关心点（规则 Number/中文名/
    描述/检查类型/告警级别/SQL/目标表/schema/负责人）。附属 sheet（参数配置/
    输出列配置/目录路径）需求未清晰，暂不消费（provenance 亦不带 sheets——
    "实际消费的源"只有主 sheet，路径即够定位；需求清晰后按对账表扩充）。

    列映射（find_col 模糊匹配，全角/半角/空格容错）：
        规则Number→rule_number  规则中文名→rule_name  规则中文描述→rule_desc
        检查类型→check_type  告警级别→alert_level  SQL→sql
        目标表→target_table  目标表schema→target_schema（excel 特有，yml 源空串）
        规则负责人→owner
        dq_file=excel 路径（provenance 采集用，同 yml 源形态）

    Args:
        excel_path: DQ excel 文件路径

    Returns: [dq_rule_dict, ...]（每行一条，同 yml 解析产物结构）；
        None（文件不存在 / 无主 sheet / 无有效规则行——"给了但读不到规则"
        由调用方决定语义：manifest provided 场景应 fail loud）。
    """
    import openpyxl

    path = Path(excel_path)
    if not path.exists():
        return None
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        if "规则基本信息" not in wb.sheetnames:
            return None
        rows = list(wb["规则基本信息"].iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return None

    headers = [_safe_str(h) for h in rows[0]]
    col = {h: i for i, h in enumerate(headers) if h}
    idx = {k: _find_col(col, k) for k in
           ("规则Number", "规则中文名", "规则中文描述", "检查类型", "告警级别",
            "SQL", "目标表", "目标表schema", "规则负责人")}

    rules = []
    for row in rows[1:]:
        if not row:
            continue
        rule_number = _get_val(row, idx["规则Number"])
        if not rule_number:
            continue  # 无编号的行不是规则（模板尾部统计行等）
        rules.append({
            "rule_number": rule_number,
            "rule_name": _get_val(row, idx["规则中文名"]),
            "rule_desc": _get_val(row, idx["规则中文描述"]),
            "check_type": _get_val(row, idx["检查类型"]),
            "alert_level": _get_val(row, idx["告警级别"]),
            "sql": _get_val(row, idx["SQL"]),
            "target_table": _get_val(row, idx["目标表"]),
            "target_schema": _get_val(row, idx["目标表schema"]),
            "owner": _get_val(row, idx["规则负责人"]),
            "dq_file": str(path),  # 与 yml 源同构（provenance 采集用）
        })
    return rules if rules else None

def _discover_dq_from_repo(yml_dir: Path, target_table: str) -> list | None:
    """从代码仓 DQ/ 目录发现资产对应的 DQ 质量检查规则。

    定位逻辑：目录+目标表字段双确认
    1. 快速文本搜索找含 target_table 的 DQ yml 文件
    2. 解析确认「目标表」字段匹配
    3. 返回所有匹配的 DQ 规则列表

    Args:
        yml_dir: 规则组目录（用于向上找代码仓根）
        target_table: 资产目标表名（I 视图名或 F 表名）

    Returns: [dq_rule_dict, ...] 或 None（找不到不阻塞）
    """
    if not target_table:
        return None

    repo_root = _find_repo_root(yml_dir)
    if not repo_root:
        return None

    dq_root = repo_root / "DQ"
    if not dq_root.is_dir():
        return None

    target_lower = target_table.lower()

    # 两阶段定位：先快速文本搜索，再解析确认
    candidate_files = []
    for yml_path in dq_root.rglob("*.yml"):
        try:
            content = yml_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if target_lower in content.lower():
            candidate_files.append(yml_path)

    if not candidate_files:
        return None

    # 解析候选文件，确认目标表字段匹配
    rules = []
    for yml_path in candidate_files:
        rule = _parse_dq_yml(yml_path)
        if not rule or not rule["rule_number"]:
            continue
        # 双确认：目标表字段匹配（容错大小写）
        if rule["target_table"].lower() == target_lower:
            rules.append(rule)

    return rules if rules else None


# ═══════════════════════════════════════════════════════════════
# DDL 文件定位（自 analyzer.py 平移）
# ═══════════════════════════════════════════════════════════════

def _find_ddl_file(table_dir: Path, table_lower: str) -> Path | None:
    """在 DDL 目录里找目标表的 DDL 文件（包含匹配 + 多扩展名）。

    匹配优先级（从严到宽，避免误匹配）：
    1. 文件名(去扩展名) == 表名（最精确，如 dwb_trade.sql 匹配 dwb_trade）
    2. 文件名以"表名."开头（schema 前缀，如 ods.dwb_trade.sql 匹配 dwb_trade）
    3. 文件名(去扩展名) 包含表名（容错 create_table_dwb_trade / dwb_trade_v2）

    支持的扩展名：.sql / .ddl / .txt（不区分大小写）
    """
    candidates = []
    for ext in ("*.sql", "*.ddl", "*.txt", "*.SQL", "*.DDL", "*.TXT"):
        candidates.extend(table_dir.glob(ext))
    # 去重（大小写不同的 glob 可能重复）
    seen = set()
    files = []
    for f in candidates:
        real = str(f.resolve())
        if real not in seen:
            seen.add(real)
            files.append(f)

    for f in files:  # 优先级1：精确匹配
        if f.stem.lower() == table_lower:
            return f
    for f in files:  # 优先级2：schema 前缀（文件名以"表名."开头）
        if f.name.lower().startswith(table_lower + "."):
            return f
    for f in files:  # 优先级3：包含（容错前缀后缀，但要求表名足够长避免误匹配）
        if len(table_lower) >= 4 and table_lower in f.stem.lower():
            return f
    return None


# ═══════════════════════════════════════════════════════════════
# I 视图发现（资产是 I 视图，F 表是底表，分析链路需补 F→I 这一段）
# ═══════════════════════════════════════════════════════════════

def _extract_view_sql(view_content: str) -> str:
    """从 CREATE VIEW 文件内容里提取 SELECT 语句。

    CREATE VIEW xxx AS SELECT ... → 返回 SELECT ...
    CREATE OR REPLACE VIEW xxx AS SELECT ... → 同上

    只提取 AS 后的 SELECT 语句，截断后续的 COMMENT/CREATE 等语句
    （视图 DDL 文件常含 COMMENT ON COLUMN，不能混入 query_sql）。
    """
    import re
    # 匹配 CREATE [OR REPLACE] VIEW xxx AS 后面的内容
    m = re.search(
        r'CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+\S+\s+AS\s+(.*)',
        view_content, re.IGNORECASE | re.DOTALL
    )
    if not m:
        return ""
    sql = m.group(1).strip()
    # 先剔除注释（行注释 -- 和块注释 /* */），避免注释里的分号
    # 导致 find(";") 提前截断，丢失后续 JOIN 的表名
    sql = re.sub(r"--[^\n]*", "", sql)            # 行注释
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)  # 块注释
    # 截断到第一个独立语句结束（SELECT 的分号），去掉后续的 COMMENT/CREATE 等
    first_semicolon = sql.find(";")
    if first_semicolon >= 0:
        sql = sql[:first_semicolon]
    return sql.strip()


def _find_view_by_name(view_dir: Path, view_name_lower: str) -> Path | None:
    """在 view/ 目录里按视图名找 DDL 文件（复用 _find_ddl_file 的匹配逻辑）。"""
    return _find_ddl_file(view_dir, view_name_lower)


def _search_view_by_source(view_dirs: list, f_table_full: str) -> tuple | None:
    """全局搜索 view/ 目录，找 CREATE VIEW ... FROM ... f_table 的视图。

    遍历所有 view_dir 下的 .sql/.ddl 文件，匹配 FROM 子句引用了 f_table 的。
    用于命名不规律的 I 视图（Step 2 兜底）。

    Returns: (view_file_path, view_name) 或 None
    """
    import re
    f_table_lower = f_table_full.lower()
    # 也匹配不带 schema 的表名（FROM dwb_xxx_f / FROM dws.dwb_xxx_f）
    f_table_short = f_table_lower.split(".")[-1]

    for view_dir in view_dirs:
        if not view_dir.is_dir():
            continue
        for ext in ("*.sql", "*.ddl", "*.SQL", "*.DDL"):
            for vf in view_dir.glob(ext):
                try:
                    content = vf.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                content_lower = content.lower()
                if "create view" not in content_lower and "create or replace view" not in content_lower:
                    continue
                # 检查 FROM 子句是否引用了 F 表
                if f_table_lower in content_lower or f_table_short in content_lower:
                    # 提取视图名（CREATE VIEW 视图名 AS）
                    nm = re.search(
                        r'CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\S+)',
                        content, re.IGNORECASE
                    )
                    view_name = nm.group(1) if nm else vf.stem
                    return (vf, view_name)
    return None


def discover_i_view(yml_dir: Path, f_schema: str, f_table: str) -> dict | None:
    """发现 F 表对应的 I 视图（两步走策略）。

    资产是 I 视图（dwb_xxx_i），F 表是底表。此函数找到 I 视图的 CREATE VIEW
    定义，返回视图信息供分析链路追加 F→I 步骤。

    两步走发现：
      Step 1: 按名字快速找（_f → _i，覆盖95%直封，快路径）
      Step 2: 名字没找到 → 全局搜索 view/ 里 FROM 引用 F 表的（兜底）

    Args:
        yml_dir: 规则组目录（用于向上找代码仓根）
        f_schema: F 表的 schema（如 dws）
        f_table: F 表名（如 dwb_trade_order_f）

    Returns: {
        "view_name": 视图名（如 dwb_trade_order_i）,
        "view_sql": SELECT 语句（从 CREATE VIEW 提取）,
        "view_schema": schema,
        "ddl_path": DDL 文件路径,
    } 或 None（找不到 I 视图，以 F 表为终点）
    """
    repo_root = _find_repo_root(yml_dir)
    if not repo_root:
        return None

    # 推导 I 视图名：_f → _i（_F → _I 大小写容错）
    # 只有 _f 结尾的表才找 I 视图（资产 = I 视图，F 表是底表）
    # 非 _f 表（如 dwb_xxx_d）不做 I 视图发现
    f_table_lower = f_table.lower()
    if f_table_lower.endswith("_f"):
        i_table = f_table[:-2] + "_i"
    else:
        return None  # 非 _f 表不找 I 视图

    i_table_lower = (i_table or "").lower()
    schema_lower = f_schema.lower() if f_schema else ""

    # 收集所有层的 view 目录
    view_dirs = []
    for layer in ("DWS_EDW", "DWS_RT_EDW"):
        ddl_root = repo_root / "DDL" / layer
        if not ddl_root.is_dir():
            continue
        # schema 目录大小写容错
        if schema_lower:
            for sd in ddl_root.iterdir():
                if sd.is_dir() and sd.name.lower() == schema_lower:
                    vd = sd / "view"
                    if vd.is_dir():
                        view_dirs.append(vd)
                    break

    # Step 1: 按名字快速找
    if i_table_lower:
        for vd in view_dirs:
            vf = _find_view_by_name(vd, i_table_lower)
            if vf:
                content = vf.read_text(encoding="utf-8", errors="ignore")
                view_sql = _extract_view_sql(content)
                if view_sql:
                    return {
                        "view_name": i_table,
                        "view_sql": view_sql,
                        "view_schema": f_schema,
                        "ddl_path": str(vf),
                    }

    # Step 2: 全局搜索来源表（兜底）
    f_full = f"{f_schema}.{f_table}" if f_schema else f_table
    result = _search_view_by_source(view_dirs, f_full)
    if result:
        vf, view_name = result
        content = vf.read_text(encoding="utf-8", errors="ignore")
        view_sql = _extract_view_sql(content)
        if view_sql:
            return {
                "view_name": view_name,
                "view_sql": view_sql,
                "view_schema": f_schema,
                "ddl_path": str(vf),
            }

    return None


# ═══════════════════════════════════════════════════════════════
# 装配函数（原 analyzer.main 内联逻辑收敛于此；行为零变）
# ═══════════════════════════════════════════════════════════════

def append_i_view_step(raw: dict, input_path: Path) -> dict | None:
    """yml 场景发现 I 视图并追加为链路最后一步（F→I）。

    F 表是加工底表，I 视图是对外资产。发现 I 视图后往 raw["rules"] 追加
    视图步骤，使完整链路为 ODS→F表→I视图。找不到 I 视图 → 以 F 表为终点
    （容错，不阻塞）。

    Args:
        raw: read_yml 产物（其 rules 会被追加视图步骤）
        input_path: 规则组目录（用于向上找代码仓根）

    Returns: i_view_info（供 apply_meta_enrichments 注入 meta.asset_info）或 None。
    """
    rules = raw["rules"]
    # 取最终目标表（max exec_sequence 的 F 表）
    # 注意：交换分区（rule_type=9）的 target_table 是临时表，
    # exchange_source_table 才是真正的目标表（F 表），不能拿临时表去找 I 视图
    f_schema = ""
    f_table = ""
    f_rule = None
    for rule in reversed(rules):
        if rule.rule_type == 9 and rule.exchange_source_table:
            # 交换分区：真正目标表是 exchange_source_table
            f_schema = rule.target_schema
            f_table = rule.exchange_source_table
            f_rule = rule
            break
        if not _is_intermediate_table(rule.target_table):
            f_schema = rule.target_schema
            f_table = rule.target_table
            f_rule = rule
            break
    if not (f_rule and f_table):
        return None

    i_view = discover_i_view(input_path, f_schema, f_table)
    if i_view:
        # 追加 I 视图作为最后一步
        max_seq = max((r.exec_sequence for r in rules), default=0)
        i_rule = RawRule(
            rule_code=f"{f_rule.rule_code}_VIEW",
            rule_name=f"{f_rule.rule_name}（I视图）",
            rule_type=1,
            exec_sequence=max_seq + 1,
            target_schema=i_view["view_schema"],
            target_table=i_view["view_name"],
            delete_mode="",
            query_sql=i_view["view_sql"],
            rule_group_code=f_rule.rule_group_code,
            rule_group_en=f_rule.rule_group_en,
            is_view_step=True,  # 标记为视图步骤，下游统一读这个字段
        )
        raw["rules"].append(i_rule)
        # 记录 I 视图信息，供 knowledge.meta.asset_info
        # 注意：view_step 不在这里算（exec_sequence≠step_id），等 topology 生成后取
        i_view_info = {
            "is_view": True,
            "view_table": i_view["view_name"],
            "view_schema": i_view["view_schema"],
            "base_table": f_table,
            "base_schema": f_schema,
            "view_rule_code": f"{f_rule.rule_code}_VIEW",  # 用于从 topology 查 step_id
        }
        print(f"Step 2b: 发现 I 视图 {i_view['view_schema']}.{i_view['view_name']}")
        print(f"  链路扩展: F表 {f_table} → I视图 {i_view['view_name']}")
        print()
        return i_view_info

    print(f"Step 2b: 未找到 I 视图（以 F 表 {f_rule.target_table} 为终点）")
    print()
    return None


def apply_meta_enrichments(knowledge: dict, i_view_info: dict | None,
                           schedule_info: dict | None, dq_rules: list | None,
                           dq_declared_absent: bool = False) -> None:
    """把装配层发现的信息注入 knowledge.meta（注入有主：唯此一处写这些 key）。

    - i_view_info：先从 topology 查 view_step（不自己算，避免 exec_sequence≠step_id
      的错位），再写 meta.asset_info（view_rule_code 已消费，pop 掉）
    - schedule_info → meta.schedule（excel 输入无调度 → 调用方传 None，key 不写）
    - dq_rules → meta.dq_rules
    - dq_declared_absent（v2 三态）→ meta.dq_declared_absent：用户声明资产无 DQ，
      投影层据此输出 dq_rules=[]（而非 null）——"明确没有"与"未提供"分离

    各注入互不依赖，均为条件注入（缺省不写 key，保持既有 draft 形态）。
    """
    if i_view_info:
        view_rule_code = i_view_info.pop("view_rule_code", "")
        view_step = ""
        for s in knowledge["topology"]["steps"]:
            if s.get("rule_code") == view_rule_code:
                view_step = s["step_id"]
                break
        i_view_info["view_step"] = view_step
        knowledge["meta"]["asset_info"] = i_view_info
    if schedule_info:
        knowledge["meta"]["schedule"] = schedule_info
    if dq_rules:
        knowledge["meta"]["dq_rules"] = dq_rules
    if dq_declared_absent:
        knowledge["meta"]["dq_declared_absent"] = True


def build_baseline_export_ctx(knowledge: dict, engine_ctx: dict, *,
                              is_yml_mode: bool,
                              consumed_yml_files: list,
                              input_path: Path,
                              platform_generation: str = "1.0",
                              rule_group_en: str = "",
                              group_en_fallback: str = "",
                              input_sheets: list | None = None) -> dict:
    """组装 baseline_v1 投影上下文（provenance 采集 + ctx 拼装）。

    装配层职责：provenance 是「实际消费的源文件清单」，随装配在此采集；
    baseline_export 只消费 ctx、不自己组装。契约语义（版本/校验/投影）仍在投影层。

    Args:
        knowledge: 引擎产物（读 meta.schedule / meta.dq_rules 采集 LTS/DQ 源清单）
        engine_ctx: analyze_pipeline 的 context_out（table_catalog / final_target / ddl_files）
        is_yml_mode / consumed_yml_files / input_path: 输入形态与实际消费的 yml 清单
        platform_generation: 平台代次（透传投影层，决定 write_plan 映射版本）
        rule_group_en / group_en_fallback: 规则组英文名（yml 原值 → CLI 兜底）
        input_sheets: xlsx 主输入实际读到的 sheet 清单（v1.2 provenance[].sheets，
            read_excel 的 _source_sheets；yml 模式不携带）
    """
    provenance = []
    if is_yml_mode:
        for f in consumed_yml_files:
            provenance.append({"type": "rule_yml", "path": f})
    else:
        entry = {"type": "execution_tasks", "path": str(input_path.resolve())}
        if input_sheets:
            entry["sheets"] = list(input_sheets)
        provenance.append(entry)
    for f in engine_ctx.get("ddl_files", []):
        provenance.append({"type": "ddl", "path": f})
    _sched_meta = knowledge.get("meta", {}).get("schedule") or {}
    _lts_sheets = _sched_meta.get("_source_sheets") or []
    _seen_lts = set()
    for key in ("f_tasks", "i_tasks", "other_tasks", "all_tasks"):
        for t in (_sched_meta.get(key) or []):
            if isinstance(t, dict) and t.get("lts_file") and t["lts_file"] not in _seen_lts:
                _seen_lts.add(t["lts_file"])
                entry = {"type": "lts", "path": t["lts_file"]}
                if _lts_sheets:
                    entry["sheets"] = list(_lts_sheets)
                provenance.append(entry)
    _seen_dq = set()
    for d in sorted((knowledge.get("meta", {}).get("dq_rules") or []),
                    key=lambda d: str(d.get("rule_number") or "")):
        p = d.get("dq_file") if isinstance(d, dict) else None
        if p and p not in _seen_dq:
            _seen_dq.add(p)
            provenance.append({"type": "dq", "path": p})
    return {
        "rule_group_en": engine_ctx.get("rule_group_en") or rule_group_en or group_en_fallback,
        "platform_generation": platform_generation,
        "source_type": "repo_yml" if is_yml_mode else "execution_tasks.xlsx",
        "provenance": provenance,
        "final_target_schema": engine_ctx.get("final_target_schema", ""),
        "final_target_table": engine_ctx.get("final_target_table", ""),
        "table_catalog": engine_ctx.get("table_catalog") or {},
    }


# ═══════════════════════════════════════════════════════════════
# manifest 装配（v2 编排合入，波 3）
# ═══════════════════════════════════════════════════════════════

class ManifestError(Exception):
    """manifest 结构/内容不合法（编排模式 fail loud，错误清单原样退回消费方）。"""


# ── 制品类型自动分拣（docs/15：sheet 结构指纹判型，判型知识单源在解析侧）──
# 指纹与各 loader 的 sheet 认知同源：read_excel 认 RULE/TargetFields、
# read_lts_excel 认 tasks（jobs/taskParams 为可选附属）、read_dq_excel 认规则基本信息。
# 三类指纹集合不相交（消费方实测，docs/15 §一）；平台结构演进时改这里并知会消费方。
_ARTIFACT_SHEET_FINGERPRINTS = {
    "execution_tasks": ("RULE", "TargetFields"),   # 全部命中判术加
    "lts": ("tasks",),
    "dq": ("规则基本信息",),
}

# 目录扫描时忽略的文件（非制品干扰）：Excel 打开时的锁文件 / 隐藏文件 / 子目录
_DIR_SKIP_PREFIXES = ("~$", ".")


def classify_artifact_excel(path) -> tuple[str | None, str]:
    """按 sheet 结构指纹判制品类型（自动分拣，docs/15）。

    Returns: (kind, reason)。kind ∈ {"execution_tasks", "lts", "dq"}；
    无法判型时 kind=None、reason 说明原因（供 fail loud 报
    "无法识别的文件 X（原因）"）。确定性判定：不猜、不靠文件名。
    """
    p = Path(path)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        return None, f"非 excel 文件（扩展名 {p.suffix or '无'}；分拣只认 xlsx/xlsm）"
    import openpyxl
    try:
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    except Exception as e:
        return None, f"excel 无法读取（{e}）"
    try:
        sheets = {s.strip() for s in wb.sheetnames}
    finally:
        wb.close()
    hits = [kind for kind, req in _ARTIFACT_SHEET_FINGERPRINTS.items()
            if all(s in sheets for s in req)]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, f"缺已知指纹 sheet（实际 sheets: {sorted(sheets)[:6]}）"
    return None, f"多指纹命中 {hits}（指纹不应重叠，语料异常请反馈）"


def assemble_artifact_dir(dir_path) -> dict:
    """制品目录 → 通道装配（消费方 docs/specs/opt/12 v3：识别职责全在逆向侧）。

    目录就是用户所给的全部（文件命名/组织/混放非制品文件零要求）：
      - 术加制品包必需——缺失/判不出是唯一整体 fail 闸（没有它无法产出）
      - 同型多个（多义）fail loud，不静默取一
      - `.sql` / `.ddl` 后缀即型归 DDL（无歧义）
      - 无法识别的文件（非 excel / 缺指纹 sheet，如 mapping、说明文档）**不阻断**：
        收集进 unrecognized，由调用方进 warnings 随产物交付（消费方闸口人扫兜底）
      - DQ 未检出 → dq=None，调用方按两态落 dq_rules=[] + dq_not_detected warning
        （不要求调用方显式声明"无"——发现不了"无"，闸口向人披露纠错）

    Returns: {"execution_tasks": path, "lts": path|None, "dq": path|None,
              "ddl": [paths], "unrecognized": [(文件名, 原因)]}
    Raises: ManifestError（目录不存在 / 术加缺失 / 同型多义——错误一次报齐）。
    """
    d = Path(dir_path)
    if not d.is_dir():
        raise ManifestError(f"制品目录不存在: {dir_path}")

    found: dict[str, list] = {"execution_tasks": [], "lts": [], "dq": []}
    ddl: list[str] = []
    unrecognized: list = []
    for f in sorted(d.iterdir()):
        if f.is_dir() or f.name.startswith(_DIR_SKIP_PREFIXES):
            continue
        if f.suffix.lower() in (".sql", ".ddl"):
            ddl.append(str(f))  # 后缀即型，直接收录
            continue
        kind, reason = classify_artifact_excel(f)
        if kind is None:
            unrecognized.append((f.name, reason))
        else:
            found[kind].append(str(f))

    errors: list[str] = []
    if not found["execution_tasks"]:
        hint = (f"（目录另有 {len(unrecognized)} 个无法识别文件，清单随错误给出）"
                if unrecognized else "")
        errors.append("缺必需料：目录中未识别出术加制品包 excel"
                      f"（需含 RULE + TargetFields sheet）{hint}")
    for kind in ("execution_tasks", "lts", "dq"):
        if len(found[kind]) > 1:
            errors.append(f"{kind} 多义：{[Path(x).name for x in found[kind]]}"
                          "——同型制品请只保留一个")
    if unrecognized and errors:
        errors.append("无法识别的文件（不阻断，供排查）：" +
                      "；".join(f"{n}（{r}）" for n, r in unrecognized[:5]))
    if errors:
        raise ManifestError("\n".join(f"- {e}" for e in errors))

    return {
        "execution_tasks": found["execution_tasks"][0] if found["execution_tasks"] else None,
        "lts": found["lts"][0] if found["lts"] else None,
        "dq": found["dq"][0] if found["dq"] else None,
        "ddl": ddl,
        "unrecognized": unrecognized,
    }


def stage_ddl_files(ddl_paths: list, stage_dir) -> dict:
    """DDL 文件/目录混合清单 → 统一 stage 目录（engine 的 ddl_dir 只收目录扫描）。

    文件拷入 stage（保文件名，同名加序号前缀）；目录 rglob 拷入其中 DDL 文件。

    Returns: {stage 文件绝对路径: manifest 原路径}——engine 消费 stage 路径，
    provenance 记原路径（manifest 显式清单即"实际消费"证据，比目录扫描推断更强）。
    Raises: ManifestError（路径不存在）。
    """
    import shutil

    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)
    mapping: dict = {}
    used_names: dict = {}

    def _stage(src: Path):
        key = src.name.lower()
        n = used_names.get(key, 0)
        used_names[key] = n + 1
        dst = stage / (f"{n}_{src.name}" if n else src.name)
        shutil.copyfile(src, dst)
        mapping[str(dst.resolve())] = str(src.resolve())

    for p in ddl_paths or []:
        src = Path(p)
        if src.is_file():
            _stage(src)
        elif src.is_dir():
            for ext in ("*.sql", "*.ddl", "*.txt", "*.SQL", "*.DDL", "*.TXT"):
                for f in src.rglob(ext):
                    _stage(f)
        else:
            raise ManifestError(f"DDL 路径不存在: {src}")
    return mapping
