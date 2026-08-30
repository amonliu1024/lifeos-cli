"""Pure presentation helpers for the LifeOS Work domain.

The functions in this module render a supplied current snapshot.  They do not
read or write Runtime files; ``current_view_contents`` only assembles the
deterministic path-to-content mapping consumed by the Runtime layer.
"""

from datetime import date, timedelta

from .config import (
    ACHIEVEMENTS_VIEW_PATH,
    ENTITY_KIND_LABELS,
    GLOSSARY_VIEW_PATH,
    IDEAS_VIEW_PATH,
    MILESTONE_STATUS_LABELS,
    NOW_PATH,
    PROJECTS_VIEW_PATH,
    WORK_ITEMS_VIEW_PATH,
)
from .model import (
    brief_calendar_label,
    brief_needs_reminder,
    brief_sort_key,
    brief_task_label_parts,
    brief_task_labels,
    brief_work_item_action,
    brief_work_item_labels,
    current_milestone,
    display_iso_time,
    item_sort_key,
    latest_task_started_dates,
    milestone_list,
    now,
    parsed_date,
    responsible_display_name,
    responsibility_bucket,
    task_milestone,
    work_item_state_label,
)


CURRENT_WINDOW_DAYS = 15


def generated_view(source_name, content):
    """给派生视图加自述头。

    这些 .md 是从 JSON 事实源渲染出来的，直接编辑会在下次 refresh 被覆盖。
    渲染函数本身还用于终端输出，所以自述头只加在写文件这一层。
    """
    return (
        f"<!-- 本文件由 lifeos.py 从 {source_name} 生成，不要直接编辑。\n"
        f"     改事实源后运行 lifeos work refresh 重新生成。 -->\n"
        f"{content}"
    )


def append_horizontal_rule(lines):
    """在 Markdown 展示的相邻区块之间加入统一分割线。"""
    if lines and lines[-1] != "":
        lines.append("")
    lines += ["---", ""]


def append_brief_group(lines, title, entries):
    if not entries:
        return
    append_horizontal_rule(lines)
    lines += [f"**{title}**", ""]
    for index, (entry_title, labels, action) in enumerate(entries, 1):
        lines += [f"【{index}】{entry_title}", ""]
        lines += ["　".join(f"`{label}`" for label in labels), ""]
        lines += [action, ""]


def current_brief_task_suffix(task, reference_date, started_at=None):
    """Render compact task metadata for the item-first current brief."""
    party, is_self, state_labels, time_labels = brief_task_label_parts(
        task, reference_date, started_at
    )
    parts = []
    if party:
        parts.append(f"👥 {party}")
    if task.get("status") == "waiting":
        parts.append("待当前节点完成")
    else:
        parts.extend(label for label in state_labels if label != party)
    parts.extend(f"`{label}`" for label in time_labels)
    return f" | {' · '.join(parts)}" if parts else ""


def current_brief_milestone_line(item):
    milestone = current_milestone(item)
    if not milestone:
        return f"下一门槛：{brief_work_item_action(item)}"
    line = f"当前里程碑：{milestone['title']}"
    target_at = milestone.get("target_at")
    if target_at:
        try:
            target_label = (
                f"{brief_calendar_label(date.fromisoformat(target_at))}前完成"
            )
        except ValueError:
            target_label = f"{target_at}前完成"
        line += f" ｜ `{target_label}`"
    return line


def current_brief_item_sort_key(
    item, tasks_by_work_item, reference_date, started_dates
):
    linked_tasks = tasks_by_work_item.get(item.get("id"), [])
    if linked_tasks:
        return min(
            brief_sort_key(
                task,
                reference_date,
                "task",
                started_dates.get(task.get("id")),
            )
            for task in linked_tasks
        )
    return brief_sort_key(item, reference_date, "work_item")


def current_task_is_visible(task, reference_date):
    """Keep unfinished work in the current view; dates no longer gate visibility."""
    return task.get("status") in {"active", "waiting"}


def current_brief_scope(work_items, tasks, reference_date, started_dates=None):
    """Select the current work horizon without guessing task dependencies."""
    started_dates = started_dates or {}
    task_sort_key = lambda task: brief_sort_key(
        task,
        reference_date,
        "task",
        started_dates.get(task.get("id")),
    )
    active_items = [item for item in work_items if item.get("state") == "active"]
    active_item_ids = {item.get("id") for item in active_items}
    candidates_by_work_item = {}
    standalone_tasks = []
    for task in tasks:
        if not current_task_is_visible(task, reference_date):
            continue
        work_item_id = task.get("work_item_id")
        if work_item_id in active_item_ids:
            candidates_by_work_item.setdefault(work_item_id, []).append(task)
        elif work_item_id is None and task.get("status") == "active":
            standalone_tasks.append(task)

    active_items = [
        item for item in active_items if candidates_by_work_item.get(item.get("id"))
    ]

    selected_tasks = []
    for item in active_items:
        linked = candidates_by_work_item.get(item.get("id"), [])
        current_tasks = [
            task for task in linked if task.get("status") == "active"
        ]
        selected_for_item = list(current_tasks)
        waiting_tasks = [task for task in linked if task.get("status") == "waiting"]
        if current_tasks and waiting_tasks:
            selected_for_item.append(
                min(
                    waiting_tasks,
                    key=task_sort_key,
                )
            )
        selected_tasks.extend(
            sorted(
                selected_for_item,
                key=task_sort_key,
            )
        )
    selected_tasks.extend(
        sorted(
            standalone_tasks,
            key=task_sort_key,
        )
    )
    return active_items, selected_tasks


def render_current_brief(work_items, tasks, ideas, reference_date, started_dates=None):
    started_dates = started_dates or {}
    work_items, tasks = current_brief_scope(
        work_items, tasks, reference_date, started_dates
    )
    tasks_by_work_item = {}
    active_work_item_ids = {item.get("id") for item in work_items}
    standalone_tasks = []
    for task in tasks:
        work_item_id = task.get("work_item_id")
        if work_item_id and work_item_id in active_work_item_ids:
            tasks_by_work_item.setdefault(work_item_id, []).append(task)
        else:
            standalone_tasks.append(task)

    buckets = [responsibility_bucket(task) for task in tasks]
    my_task_count = buckets.count("self")
    other_task_count = buckets.count("external")
    responsibility_summary = (
        f" · 我**{my_task_count}** / 他人**{other_task_count}**"
        if all(bucket is not None for bucket in buckets)
        else ""
    )
    lines = [
        f"📌 当前简报｜{brief_calendar_label(reference_date)}",
        "",
        f"总览：**{len(work_items)}** 个事项 · **{len(tasks)}** 条当前待办"
        f"{responsibility_summary}",
        "",
    ]
    append_horizontal_rule(lines)
    lines += ["📍 当前事项", ""]

    sorted_work_items = sorted(
        work_items,
        key=lambda item: current_brief_item_sort_key(
            item, tasks_by_work_item, reference_date, started_dates
        ),
    )
    if not sorted_work_items:
        lines += ["当前没有推进中事项。", ""]
    for item in sorted_work_items:
        state_label = work_item_state_label(item.get("state"))
        lines.append(f"- 【{item['title']}】 · {state_label}")
        lines.append(f"  - {current_brief_milestone_line(item)}")
        linked_tasks = tasks_by_work_item.get(item.get("id"), [])
        for task in linked_tasks:
            lines.append(
                f"    - {task['outcome']}"
                f"{current_brief_task_suffix(task, reference_date, started_dates.get(task.get('id')))}"
            )
        lines.append("")

    if standalone_tasks:
        append_horizontal_rule(lines)
        lines += ["🧩 独立待办", ""]
        for task in sorted(
            standalone_tasks,
            key=lambda value: brief_sort_key(
                value,
                reference_date,
                "task",
                started_dates.get(value.get("id")),
            ),
        ):
            lines.append(
                f"- {task['outcome']}"
                f"{current_brief_task_suffix(task, reference_date, started_dates.get(task.get('id')))}"
            )
        lines.append("")

    append_horizontal_rule(lines)
    lines += ["💡 闪念", ""]
    if not ideas:
        lines += ["*暂无*", ""]
    else:
        for idea in sorted(
            ideas,
            key=lambda value: value.get("created_at", ""),
            reverse=True,
        ):
            label = "刚记下" if idea.get("status") == "inbox" else "酝酿中"
            lines.append(f"- {idea['text']} ｜ {label}")
            if idea.get("context"):
                lines.append(f"  {idea['context']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def closeout_status_label(status):
    return {
        "active": "推进中",
        "waiting": "待前置完成",
        "paused": "已暂停",
    }.get(status, status)


def render_closeout_entry(task, reference_date):
    """Return (headline, meta_labels) for one result-overdue task."""
    time_labels = []
    meta_labels = []
    party = responsible_display_name(task)
    if party:
        meta_labels.append(f"负责人：{party}")
    meta_labels.append(f"状态：{closeout_status_label(task.get('status'))}")
    due_at = parsed_date(task.get("due_at"))
    if due_at:
        meta_labels.append(f"截止：`{due_at.isoformat()}`")
        time_labels.append(f"`已逾期 {(reference_date - due_at).days} 天`")
    headline = f"- {task['outcome']}"
    if time_labels:
        headline += " | " + " · ".join(time_labels)
    meta_line = f"  - {' ｜ '.join(meta_labels)}" if meta_labels else ""
    return headline, meta_line


def render_closeout_brief(tasks, reference_date):
    """Render only result deadlines that have actually been missed."""
    overdue = [
        task
        for task in tasks
        if (parsed_date(task.get("due_at")) or date.max) < reference_date
    ]
    overdue.sort(
        key=lambda task: -(
            reference_date - parsed_date(task.get("due_at"))
        ).days
    )
    if not overdue:
        return "今天没有仍待收口的结果逾期待办。\n"

    lines = [
        f"📌 18:00 晚间收口提醒｜{brief_calendar_label(reference_date)}",
        "",
        f"总览：**{len(overdue)}** 条结果逾期",
        "",
    ]
    append_horizontal_rule(lines)
    lines += ["🔴 结果逾期", ""]
    for task in overdue:
        headline, meta_line = render_closeout_entry(task, reference_date)
        lines.append(headline)
        if meta_line:
            lines.append(meta_line)
        action_text = (task.get("next_action") or {}).get("text")
        if action_text:
            lines.append(f"  - 当前行动：{action_text}")

    append_horizontal_rule(lines)
    lines += [
        "请直接告诉我真实状态：",
        "",
        "- 已完成；",
        "- 继续推进；",
        "- 不再做，取消或关闭。",
        "",
        "---",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_brief(
    work_items_data,
    tasks_data,
    ideas_data,
    mode,
    reference_date=None,
    events=None,
):
    reference_date = reference_date or now().date()
    started_dates = latest_task_started_dates(events)
    active_tasks = [
        item
        for item in tasks_data.get("tasks", [])
        if item.get("status") in {"active", "waiting", "paused"}
    ]
    active_work_items = [
        item
        for item in work_items_data.get("work_items", [])
        if item.get("state") != "closed"
    ]
    active_ideas = [
        item
        for item in ideas_data.get("ideas", [])
        if item.get("status") in {"inbox", "incubating"}
    ]
    if mode == "closeout":
        return render_closeout_brief(active_tasks, reference_date)
    if mode == "reminder":
        active_tasks = [
            item
            for item in active_tasks
            if brief_needs_reminder(item, reference_date, "task")
        ]
        active_work_items = [
            item
            for item in active_work_items
            if brief_needs_reminder(item, reference_date, "work_item")
        ]
        active_ideas = []

    if mode == "current":
        return render_current_brief(
            work_items_data.get("work_items", []),
            active_tasks,
            active_ideas,
            reference_date,
            started_dates,
        )

    task_sort = lambda item: brief_sort_key(
        item,
        reference_date,
        "task",
        started_dates.get(item.get("id")),
    )
    work_item_sort = lambda item: brief_sort_key(
        item, reference_date, "work_item"
    )
    my_tasks = [
        item
        for item in sorted(active_tasks, key=task_sort)
        if responsibility_bucket(item) == "self"
    ]
    other_tasks = [
        item
        for item in sorted(active_tasks, key=task_sort)
        if responsibility_bucket(item) == "external"
    ]
    unassigned_tasks = [
        item
        for item in sorted(active_tasks, key=task_sort)
        if responsibility_bucket(item) is None
    ]
    title = "当前简报" if mode == "current" else "提醒"
    lines = [
        f"**📌 {title}｜{brief_calendar_label(reference_date)}**",
        "",
    ]
    append_brief_group(
        lines,
        "🙋 我的待办",
        [
            (
                item["outcome"],
                brief_task_labels(item, reference_date, started_dates.get(item.get("id"))),
                (item.get("next_action") or {}).get("text") or "尚未记录下一步",
            )
            for item in my_tasks
        ],
    )
    append_brief_group(
        lines,
        "👥 他人待办",
        [
            (
                item["outcome"],
                brief_task_labels(item, reference_date, started_dates.get(item.get("id"))),
                (item.get("next_action") or {}).get("text") or "尚未记录下一步",
            )
            for item in other_tasks
        ],
    )
    append_brief_group(
        lines,
        "📋 待办",
        [
            (
                item["outcome"],
                brief_task_labels(item, reference_date, started_dates.get(item.get("id"))),
                (item.get("next_action") or {}).get("text") or "尚未记录下一步",
            )
            for item in unassigned_tasks
        ],
    )
    append_brief_group(
        lines,
        "📍 事项",
        [
            (
                item["title"],
                brief_work_item_labels(item, reference_date),
                brief_work_item_action(item),
            )
            for item in sorted(active_work_items, key=work_item_sort)
        ],
    )
    append_brief_group(
        lines,
        "💡 闪念",
        [
            (
                item["text"],
                ["刚记下" if item.get("status") == "inbox" else "酝酿中"],
                item.get("context") or "尚未形成下一步",
            )
            for item in sorted(
                active_ideas,
                key=lambda value: value.get("created_at", ""),
                reverse=True,
            )
        ],
    )
    if len(lines) == 2:
        lines += ["当前没有需要关注的事项。", ""]
    return "\n".join(lines).rstrip() + "\n"


def render_now(projects_data, work_items_data, tasks_data):
    projects_by_id = {
        item.get("id"): item for item in projects_data.get("projects", [])
    }
    work_items = work_items_data.get("work_items", [])
    work_items_by_id = {item.get("id"): item for item in work_items}
    tasks = tasks_data.get("tasks", [])
    active_tasks = [
        item
        for item in tasks
        if item.get("status") in {"active", "waiting", "paused"}
    ]
    active_work_items = [
        item for item in work_items if item.get("state") != "closed"
    ]
    lines = [
        "# 当前工作",
        "",
        f"> 更新：{display_iso_time(tasks_data.get('updated_at'))}",
        "> 项目、事项关联均可选；项目事实仍以对应 Project Workspace 为准。",
        "",
    ]
    append_horizontal_rule(lines)
    lines += [f"## 待办（{len(active_tasks)}）", ""]
    if not active_tasks:
        lines += ["当前没有活跃待办。", ""]
    for index, task in enumerate(sorted(active_tasks, key=item_sort_key)):
        if index:
            append_horizontal_rule(lines)
        work_item = work_items_by_id.get(task.get("work_item_id"))
        project_id = (
            work_item.get("project_id") if work_item else task.get("project_id")
        )
        project = projects_by_id.get(project_id) if project_id else None
        project_label = (project or {}).get("name") or "无"
        lines += [
            f"- **{task['id']}** · {task.get('outcome')}",
            f"  - 状态：{task.get('status')}；项目：{project_label}；事项：{task.get('work_item_id') or '无'}",
        ]
        milestone = task_milestone(task, work_items_by_id)
        if milestone:
            lines.append(
                f"  - 里程碑：{milestone['title']}（{MILESTONE_STATUS_LABELS.get(milestone.get('status'), milestone.get('status'))}）"
            )

        if task.get("due_at"):
            lines.append(f"  - 截止时间：{task['due_at']}")
        if task.get("next_action"):
            action = task["next_action"]
            lines.append(f"  - 下一步：{action['text']}")
    append_horizontal_rule(lines)
    lines += [f"## 事项（{len(active_work_items)}）", ""]
    if not active_work_items:
        lines += ["当前没有活跃事项。", ""]
    for index, item in enumerate(
        sorted(active_work_items, key=lambda value: value.get("id", ""))
    ):
        if index:
            append_horizontal_rule(lines)
        project = (
            projects_by_id.get(item.get("project_id"))
            if item.get("project_id")
            else None
        )
        project_label = (project or {}).get("name") or "无"
        lines += [
            f"- **{item['id']}** · {item.get('title')}",
            f"  - 状态：{item.get('state')}；项目：{project_label}",
            f"  - 下一门槛：{brief_work_item_action(item)}",
        ]
        milestone = current_milestone(item)
        if milestone:
            lines.append(
                f"  - 当前里程碑：{milestone['title']}（{MILESTONE_STATUS_LABELS.get(milestone.get('status'), milestone.get('status'))}）"
            )
    append_horizontal_rule(lines)
    lines += [
        "## 操作",
        "",
        "- 查看待办：`lifeos work tasks`",
        "- 查看事项：`lifeos work work-items`",
        "- 查看项目引用：`lifeos work projects`",
        "",
    ]
    return "\n".join(lines)


def render_projects(projects_data):
    projects = projects_data.get("projects", [])
    lines = [
        "# 项目引用",
        "",
        f"> 更新：{display_iso_time(projects_data.get('updated_at'))}",
        "> Work 只保存个人跟踪关系；名称与当前目录由 Project Catalog 动态补全。",
        "",
    ]
    append_horizontal_rule(lines)
    if not projects:
        lines += ["当前没有个人跟踪项目。", ""]
    for index, project in enumerate(
        sorted(projects, key=lambda value: value.get("id", ""))
    ):
        if index:
            append_horizontal_rule(lines)
        source = project.get("fact_source") or {}
        aliases = "、".join(project.get("aliases", [])) or "无"
        lines += [
            f"## {project['id']} · {project['name']}",
            "",
            f"- **跟踪状态**：{project.get('tracking_state')}",
            f"- **引用类型**：{project.get('reference_type', 'project')}",
            f"- **别名**：{aliases}",
            f"- **事实源**：{source.get('kind', 'unknown')} · {source.get('location', '未记录')}",
            "",
        ]
    return "\n".join(lines)


def render_tasks(tasks_data):
    tasks = tasks_data.get("tasks", [])
    lines = [
        "# 待办",
        "",
        f"> 更新：{display_iso_time(tasks_data.get('updated_at'))}",
        "> 待办是具体、可关闭的结果或动作；项目与事项关联均可选。",
        "",
    ]
    active = [
        item
        for item in tasks
        if item.get("status") in {"active", "waiting", "paused"}
    ]
    append_horizontal_rule(lines)
    if not active:
        lines += ["当前没有活跃待办。", ""]
    for index, item in enumerate(sorted(active, key=item_sort_key)):
        if index:
            append_horizontal_rule(lines)
        lines += [
            f"## {item['id']} · {item['outcome']}",
            "",
            f"- **状态**：{item.get('status')}",
        ]
        party = responsible_display_name(item)
        if party:
            lines.append(f"- **责任方**：{party}")
        lines += [
            f"- **事项**：{item.get('work_item_id') or '无'}",
            f"- **项目**：{item.get('_effective_project_id') or item.get('project_id') or '无'}",
        ]
        if item.get("_milestone_title"):
            lines.append(
                f"- **里程碑**：{item['_milestone_title']}（{MILESTONE_STATUS_LABELS.get(item.get('_milestone_status'), item.get('_milestone_status'))}）"
            )
        if item.get("due_at"):
            lines.append(f"- **截止时间**：{item['due_at']}")
        if item.get("next_action"):
            action = item["next_action"]
            lines.append(f"- **下一步**：{action['text']}")
        lines.append("")
    return "\n".join(lines)


def render_work_items(work_items_data):
    items = work_items_data.get("work_items", [])
    lines = [
        "# 事项",
        "",
        f"> 更新：{display_iso_time(work_items_data.get('updated_at'))}",
        "> 事项是需要持续跟踪的工作脉络；可以没有项目，也可以暂时没有待办。",
        "",
    ]
    active_items = [item for item in items if item.get("state") != "closed"]
    append_horizontal_rule(lines)
    if not active_items:
        lines += ["当前没有活跃事项。", ""]
    for index, item in enumerate(
        sorted(active_items, key=lambda value: value.get("id", ""))
    ):
        if index:
            append_horizontal_rule(lines)
        lines += [
            f"## {item['id']} · {item['title']}",
            "",
            f"- **状态**：{work_item_state_label(item.get('state'))}",
            f"- **项目**：{item.get('project_id') or '无'}",
            f"- **类型**：{'路线事项' if milestone_list(item) else '轻量事项'}",
        ]
        if item.get("stage"):
            lines.append(f"- **业务阶段**：{item['stage']}")
        milestone = current_milestone(item)
        if milestone:
            lines.append(
                f"- **当前里程碑**：{milestone['title']}（{MILESTONE_STATUS_LABELS.get(milestone.get('status'), milestone.get('status'))}）"
            )
            lines.append(f"- **下一门槛**：{milestone['outcome']}")
        elif item.get("next_gate"):
            lines.append(f"- **下一门槛**：{item['next_gate']}")
        lines.append("")
    return "\n".join(lines)


def render_work_item_milestones(work_item):
    milestones = milestone_list(work_item)
    lines = [
        f"# {work_item['id']} · {work_item['title']} · 里程碑",
        "",
    ]
    append_horizontal_rule(lines)
    if not milestones:
        lines += ["当前事项没有里程碑。", ""]
        return "\n".join(lines)
    for index, milestone in enumerate(milestones, 1):
        if index > 1:
            append_horizontal_rule(lines)
        status = MILESTONE_STATUS_LABELS.get(
            milestone.get("status"), milestone.get("status")
        )
        lines += [
            f"## {index}. {milestone['title']}",
            "",
            f"- **ID**：{milestone['id']}",
            f"- **状态**：{status}",
            f"- **阶段结果**：{milestone['outcome']}",
            f"- **完成标准**：{milestone['completion_criteria']}",
        ]
        if milestone.get("target_at"):
            lines.append(f"- **目标日期**：{milestone['target_at']}")
        if milestone.get("completion"):
            lines.append(f"- **完成摘要**：{milestone['completion']['summary']}")
        if milestone.get("decision"):
            lines.append(f"- **完成决定**：{milestone['decision']}")
        lines.append("")
    return "\n".join(lines)


def render_glossary(glossary_data):
    lines = [
        "# 实体名词表",
        "",
        f"> 更新：{display_iso_time(glossary_data.get('updated_at'))}",
        "> 作用：帮助不同 Agent 和会话识别人员、组织、项目、系统及专有概念。",
        "",
        "---",
        "",
        "| ID | 名称 | 类型 | 已确认关系或含义 | 关联事项 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for term in sorted(
        glossary_data.get("terms", []), key=lambda item: item["id"]
    ):
        related = "、".join(term.get("related_items", [])) or "—"
        description = term.get("description", "未记录").replace("|", "\\|")
        lines.append(
            f"| {term['id']} | {term['name']} | "
            f"{ENTITY_KIND_LABELS.get(term.get('kind'), term.get('kind'))} | "
            f"{description} | {related} |"
        )
    append_horizontal_rule(lines)
    lines += [
        "## 使用规则",
        "",
        "- 只记录已确认的身份、当前关系和来源；不猜测职位、组织归属或敏感履历。",
        "- 内部使用 kind=self，面向用户显示为“我”。",
        "- 用 `lifeos work glossary <名称或别名>` 查询；用 `term-add`、`term-update` 维护。",
        "",
    ]
    return "\n".join(lines)


def render_ideas(ideas_data, include_archived=False):
    ideas = ideas_data.get("ideas", [])
    groups = [
        ("刚记下，尚未判断", "inbox"),
        ("值得继续酝酿", "incubating"),
        ("已提升为正式事项", "promoted"),
    ]
    if include_archived:
        groups.append(("已归档", "archived"))
    lines = [
        "# 闪念",
        "",
        f"> 更新：{display_iso_time(ideas_data.get('updated_at'))}",
        "> 这里承载随口想法与未成形念头；记录本身不产生责任、期限或待办。",
        "",
    ]
    for title, status in groups:
        items = [item for item in ideas if item.get("status") == status]
        if not items:
            continue
        append_horizontal_rule(lines)
        lines += [f"## {title}（{len(items)}）", ""]
        for item in sorted(
            items, key=lambda value: value.get("created_at", ""), reverse=True
        ):
            lines.append(f"- **{item['id']}**：{item['text']}")
            if item.get("promoted_to"):
                lines.append(f"  - 已提升到：{'、'.join(item['promoted_to'])}")
            if item.get("status_reason"):
                lines.append(f"  - 状态原因：{item['status_reason']}")
        lines.append("")
    if not any(item.get("status") != "archived" for item in ideas):
        append_horizontal_rule(lines)
        lines += ["当前没有活跃闪念。", ""]
    append_horizontal_rule(lines)
    lines += [
        "## 使用规则",
        "",
        "- 随手记只需要内容；项目、责任方、价值、截止时间和关闭证据都不是准入条件。",
        "- 想法成熟后，先创建对应事项或待办，再用 `idea-update <ID> --promote-to <WI/TASK-ID>` 建立关联。",
        "- 不再需要关注的想法改为 `archived`，保留记录与内部审计。",
        "",
    ]
    return "\n".join(lines)


def render_achievements(achievements_data, include_non_current=False):
    achievements = achievements_data.get("achievements", [])
    lines = [
        "# 成果胶囊",
        "",
        f"> 更新：{display_iso_time(achievements_data.get('updated_at'))}",
        "> 成果胶囊是从高价值完成事项中提炼的可复用资产，不承担待办、提醒或项目正文。",
        "",
    ]
    visible = (
        achievements
        if include_non_current
        else [item for item in achievements if item.get("lifecycle") == "current"]
    )
    append_horizontal_rule(lines)
    if not visible:
        lines += ["当前没有可复用的成果胶囊。", ""]
    for index, item in enumerate(
        sorted(visible, key=lambda value: value.get("id", ""))
    ):
        if index:
            append_horizontal_rule(lines)
        lines += [
            f"## {item['id']} · {item['title']}",
            "",
            f"- **生命周期**：{item['lifecycle']}",
            f"- **成果**：{item['outcome']}",
            f"- **来源待办**：" + "、".join(link["task_id"] for link in item["task_links"]),
            f"- **复用**：{item['reuse']}",
            "",
            "### 核心经验",
            "",
        ]
        lines.extend(f"- {learning}" for learning in item["key_learnings"])
        lines += [
            "",
        ]
    return "\n".join(lines)


def current_view_contents(
    projects_data,
    work_items_data,
    tasks_data,
    glossary_data,
    ideas_data,
    achievements_data,
):
    return {
        NOW_PATH: generated_view(
            "projects.json、work-items.json、tasks.json",
            render_now(projects_data, work_items_data, tasks_data),
        ),
        PROJECTS_VIEW_PATH: generated_view(
            "projects.json + Project Catalog", render_projects(projects_data)
        ),
        WORK_ITEMS_VIEW_PATH: generated_view(
            "work-items.json", render_work_items(work_items_data)
        ),
        IDEAS_VIEW_PATH: generated_view("ideas.json", render_ideas(ideas_data)),
        GLOSSARY_VIEW_PATH: generated_view(
            "glossary.json", render_glossary(glossary_data)
        ),
        ACHIEVEMENTS_VIEW_PATH: generated_view(
            "achievements.json", render_achievements(achievements_data)
        ),
    }
