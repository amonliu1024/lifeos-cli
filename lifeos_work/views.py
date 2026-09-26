"""Pure presentation helpers for the LifeOS Work domain.

The functions in this module render a supplied current snapshot.  They do not
read or write Runtime files; ``current_view_contents`` only assembles the
deterministic path-to-content mapping consumed by the Runtime layer.  Views
written to disk never depend on today's date, so ``validate`` can compare them
with the facts on any day; only ``render_brief`` takes a reference date.
"""

from datetime import date

from .config import (
    ENTITY_KIND_LABELS,
    GLOSSARY_VIEW_PATH,
    INSIGHTS_VIEW_PATH,
    NOW_PATH,
    PROJECTS_VIEW_PATH,
    STALE_DAYS,
    URGENT_WINDOW_DAYS,
    status_label,
)
from .model import (
    brief_calendar_label,
    brief_date_label,
    display_iso_time,
    entry_symbol,
    is_mine,
    is_overdue,
    is_stale,
    is_urgent,
    last_activity_date,
    logged_on,
    month_label,
    now,
    owner_name,
    parsed_date,
    project_label,
    scheduled_month_arrived,
    star_needs_review,
    task_is_current,
    task_quadrant,
    task_sort_key,
)


QUADRANT_TITLES = ("★ 重要且紧急", "★ 重要不紧急", "⏰ 紧急", "· 其余")


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


def _projects_by_key(projects_data):
    return {item.get("project_key"): item for item in projects_data.get("projects", [])}


def _project_part(entry, projects_by_key):
    if not entry.get("project"):
        return None
    return project_label(entry["project"], projects_by_key)


def _owner_part(task):
    owner = owner_name(task)
    return f"👤 {owner}" if owner else None


def brief_task_line(task, projects_by_key, reference_date, *, star=False):
    parts = [task["text"]]
    for part in (_owner_part(task), _project_part(task, projects_by_key)):
        if part:
            parts.append(part)
    if task.get("status") == "scheduled":
        parts.append(f"`原排 {month_label(task['month'])}`")
    label = brief_date_label(task.get("due"), reference_date)
    if label:
        parts.append(f"`{label}`")
    prefix = "★ " if star and task.get("starred") else ""
    return f"- {prefix}{' · '.join(parts)}"


def brief_entry_line(entry, projects_by_key, *, star=True):
    parts = [entry["text"]]
    project = _project_part(entry, projects_by_key)
    if project:
        parts.append(project)
    prefix = "★ " if star and entry.get("starred") else ""
    return f"- {prefix}{' · '.join(parts)}"


def _live(entries, kind, status):
    return [
        item for item in entries
        if item.get("kind") == kind and item.get("status") == status
    ]


def _newest_first(entries):
    return sorted(
        entries,
        key=lambda item: (item.get("created_at") or "", item.get("id", "")),
        reverse=True,
    )


def _questions_in_order(entries):
    questions = _live(entries, "question", "open")
    return sorted(
        questions,
        key=lambda item: (not item.get("starred"), item.get("created_at") or "", item.get("id", "")),
    )


def render_current_brief(projects_data, entries, reference_date):
    """Four flat groups by importance × urgency, each line tagged with its project."""
    projects_by_key = _projects_by_key(projects_data)
    current_tasks = sorted(
        (
            task for task in entries
            if task_is_current(task, reference_date) and is_mine(task)
        ),
        key=lambda task: task_sort_key(task, reference_date),
    )
    waiting = sorted(
        (task for task in _live(entries, "task", "open") if not is_mine(task)),
        key=lambda task: task_sort_key(task, reference_date),
    )
    questions = _questions_in_order(entries)
    notes = _newest_first(_live(entries, "note", "open"))
    lines = [
        f"📌 当前简报｜{brief_calendar_label(reference_date)}",
        "",
        f"待办 **{len(current_tasks)}** 条 · 等别人 **{len(waiting)}** · "
        f"没想通 **{len(questions)}** · 随记 **{len(notes)}**",
        "",
    ]
    append_horizontal_rule(lines)
    if not current_tasks:
        lines += ["当前没有进行中的待办。", ""]
    else:
        for quadrant, title in enumerate(QUADRANT_TITLES):
            members = [
                task for task in current_tasks
                if task_quadrant(task, reference_date) == quadrant
            ]
            lines += [f"**{title}**", ""]
            if members:
                lines += [
                    brief_task_line(task, projects_by_key, reference_date)
                    for task in members
                ]
            else:
                lines.append("（无）")
            lines.append("")
    if waiting:
        append_horizontal_rule(lines)
        lines += ["**⏳ 等别人**", ""]
        lines += [
            brief_task_line(task, projects_by_key, reference_date, star=True)
            for task in waiting
        ]
        lines.append("")
    append_horizontal_rule(lines)
    lines += ["**? 没想通**", ""]
    if questions:
        lines += [brief_entry_line(item, projects_by_key) for item in questions]
    else:
        lines.append("*暂无*")
    lines.append("")
    append_horizontal_rule(lines)
    lines += ["**– 随记**", ""]
    if notes:
        lines += [brief_entry_line(item, projects_by_key, star=False) for item in notes]
    else:
        lines.append("*暂无*")
    return "\n".join(lines).rstrip() + "\n"


def render_reminder_brief(projects_data, entries, reference_date):
    projects_by_key = _projects_by_key(projects_data)
    live_tasks = [
        task for task in entries
        if task_is_current(task, reference_date) and is_mine(task)
    ]
    overdue = [task for task in live_tasks if is_overdue(task, reference_date)]
    soon = [
        task for task in live_tasks
        if is_urgent(task, reference_date) and not is_overdue(task, reference_date)
    ]
    waiting = [task for task in _live(entries, "task", "open") if not is_mine(task)]
    groups = [
        ("🔴 已逾期", overdue),
        (f"⏰ {URGENT_WINDOW_DAYS} 天内到期", soon),
        ("⏳ 等别人", waiting),
    ]
    if not any(members for _title, members in groups):
        return "当前没有需要提醒的待办。\n"
    lines = [f"📌 提醒｜{brief_calendar_label(reference_date)}", ""]
    for title, members in groups:
        if not members:
            continue
        append_horizontal_rule(lines)
        lines += [f"**{title}**", ""]
        lines += [
            brief_task_line(task, projects_by_key, reference_date, star=True)
            for task in sorted(members, key=lambda task: task_sort_key(task, reference_date))
        ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_closeout_brief(projects_data, entries, reference_date):
    """Render only result deadlines that have actually been missed."""
    projects_by_key = _projects_by_key(projects_data)
    overdue = [
        task for task in entries
        if task.get("kind") == "task"
        and task.get("status") in {"open", "scheduled"}
        and is_overdue(task, reference_date)
    ]
    overdue.sort(key=lambda task: task["due"])
    if not overdue:
        return "今天没有仍待收口的结果逾期待办。\n"
    lines = [
        f"📌 18:00 晚间收口提醒｜{brief_calendar_label(reference_date)}",
        "",
        f"总览：**{len(overdue)}** 条结果逾期",
        "",
    ]
    append_horizontal_rule(lines)
    lines += ["**🔴 结果逾期**", ""]
    for task in overdue:
        lines.append(brief_task_line(task, projects_by_key, reference_date, star=True))
        meta = [f"状态：{status_label('task', task.get('status'))}"]
        meta.append(f"截止：`{task['due']}`")
        lines.append(f"  - {' ｜ '.join(meta)}")
    append_horizontal_rule(lines)
    lines += [
        "请直接告诉我真实状态：",
        "",
        "- 已完成；",
        "- 继续推进；",
        "- 不再做，放弃。",
        "",
        "---",
    ]
    return "\n".join(lines).rstrip() + "\n"


def monthly_entry_line(entry, projects_by_key, *, extra=None):
    parts = [f"`{entry['id']}` {entry_symbol(entry)} {entry['text']}"]
    project = _project_part(entry, projects_by_key)
    if project:
        parts.append(project)
    if extra:
        parts.append(extra)
    prefix = "★ " if entry.get("starred") else ""
    return f"- {prefix}{' · '.join(parts)}"


def render_monthly_brief(projects_data, entries, reference_date):
    """月初盘点 (BuJo migration): every listed entry needs an explicit decision."""
    projects_by_key = _projects_by_key(projects_data)
    arrived = sorted(
        (
            task for task in entries
            if task.get("kind") == "task"
            and task.get("status") == "scheduled"
            and scheduled_month_arrived(task, reference_date)
        ),
        key=lambda task: (task["month"], task["id"]),
    )
    stale = sorted(
        (entry for entry in entries if is_stale(entry, reference_date)),
        key=lambda entry: (last_activity_date(entry), entry["id"]),
    )
    stars = sorted(
        (entry for entry in entries if star_needs_review(entry, reference_date)),
        key=lambda entry: entry["id"],
    )
    if not (arrived or stale or stars):
        return "本月盘点没有要处理的。\n"
    lines = [
        f"🗂 月初盘点｜{brief_calendar_label(reference_date)}",
        "",
        f"月份已到 **{len(arrived)}** · 久未变化 **{len(stale)}** · 星标复核 **{len(stars)}**",
        "",
    ]
    if arrived:
        append_horizontal_rule(lines)
        lines += ["**< 排到的月份已到**", ""]
        lines += [
            monthly_entry_line(
                task, projects_by_key, extra=f"原排 {month_label(task['month'])}"
            )
            for task in arrived
        ]
        lines.append("")
    if stale:
        append_horizontal_rule(lines)
        lines += [f"**💤 超过 {STALE_DAYS} 天没有变化**", ""]
        lines += [
            monthly_entry_line(
                entry,
                projects_by_key,
                extra=f"上次变化 {brief_calendar_label(last_activity_date(entry))}",
            )
            for entry in stale
        ]
        lines.append("")
    if stars:
        append_horizontal_rule(lines)
        lines += ["**★ 星标还成立吗**", ""]
        lines += [monthly_entry_line(entry, projects_by_key) for entry in stars]
        lines.append("")
    append_horizontal_rule(lines)
    lines += [
        "每一笔给个去向：继续（entry-keep）、排到以后（task-schedule）、"
        "划掉（entry-drop），或转成别的一笔；星标不再成立就 entry-update --unstar。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_brief(projects_data, entries_data, mode, reference_date=None):
    reference_date = reference_date or now().date()
    entries = entries_data.get("entries", [])
    if mode == "closeout":
        return render_closeout_brief(projects_data, entries, reference_date)
    if mode == "reminder":
        return render_reminder_brief(projects_data, entries, reference_date)
    if mode == "monthly":
        return render_monthly_brief(projects_data, entries, reference_date)
    return render_current_brief(projects_data, entries, reference_date)


def entry_detail_line(entry, projects_by_key):
    """One stable, date-independent line for list views."""
    parts = [f"**{entry['id']}** {entry_symbol(entry)} {entry['text']}"]
    meta = [status_label(entry.get("kind"), entry.get("status"))]
    project = _project_part(entry, projects_by_key)
    if project:
        meta.append(project)
    owner = _owner_part(entry) if entry.get("kind") == "task" else None
    if owner:
        meta.append(owner)
    if entry.get("due"):
        meta.append(f"截止 {entry['due']}")
    if entry.get("month"):
        meta.append(f"排到 {entry['month']}")
    if entry.get("note"):
        meta.append(entry["note"])
    if entry.get("ref"):
        meta.append(f"→ {entry['ref']}")
    prefix = "★ " if entry.get("starred") else ""
    return f"- {prefix}{' '.join(parts)}（{' · '.join(meta)}）"


def render_entries(projects_data, entries, title="记下的每一笔"):
    projects_by_key = _projects_by_key(projects_data)
    lines = [f"# {title}", ""]
    if not entries:
        lines += ["没有符合条件的记录。", ""]
        return "\n".join(lines)
    by_day = {}
    for entry in entries:
        by_day.setdefault(logged_on(entry) or "未知日期", []).append(entry)
    for day in sorted(by_day, reverse=True):
        append_horizontal_rule(lines)
        lines += [f"## {day}", ""]
        for entry in sorted(by_day[day], key=lambda item: item.get("id", "")):
            lines.append(entry_detail_line(entry, projects_by_key))
        lines.append("")
    return "\n".join(lines)


def render_now(projects_data, entries_data):
    projects_by_key = _projects_by_key(projects_data)
    entries = entries_data.get("entries", [])

    def task_order(task):
        due = parsed_date(task.get("due"))
        return (not task.get("starred"), due or date.max, task.get("id", ""))

    open_tasks = _live(entries, "task", "open")
    groups = [
        ("待做", sorted((task for task in open_tasks if is_mine(task)), key=task_order)),
        ("等别人", sorted((task for task in open_tasks if not is_mine(task)), key=task_order)),
        (
            "排到以后",
            sorted(
                _live(entries, "task", "scheduled"),
                key=lambda task: (task.get("month") or "", task.get("id", "")),
            ),
        ),
        ("没想通", _questions_in_order(entries)),
        ("随记", _newest_first(_live(entries, "note", "open"))),
    ]
    lines = [
        "# 当前工作",
        "",
        f"> 更新：{display_iso_time(entries_data.get('updated_at'))}",
        "> 相对日期、重要紧急分组和月初盘点清单见 `lifeos work brief`。",
        "",
    ]
    for title, members in groups:
        append_horizontal_rule(lines)
        lines += [f"## {title}（{len(members)}）", ""]
        if not members:
            lines += ["暂无。", ""]
            continue
        lines += [entry_detail_line(entry, projects_by_key) for entry in members]
        lines.append("")
    return "\n".join(lines)


def render_insights(projects_data, entries_data):
    projects_by_key = _projects_by_key(projects_data)
    insights = [
        item for item in entries_data.get("entries", [])
        if item.get("kind") == "insight" and item.get("status") == "open"
    ]
    lines = [
        "# 洞见",
        "",
        f"> 更新：{display_iso_time(entries_data.get('updated_at'))}",
        "> 只收本人认过有用的判断；反复出现时写成规则，并在批注里写明写进了哪里。",
        "",
    ]
    if not insights:
        append_horizontal_rule(lines)
        lines += ["还没有留下的洞见。", ""]
        return "\n".join(lines)
    by_project = {}
    for item in insights:
        by_project.setdefault(_project_part(item, projects_by_key) or "不归项目", []).append(item)
    for group in sorted(by_project, key=lambda name: (name == "不归项目", name)):
        append_horizontal_rule(lines)
        lines += [f"## {group}", ""]
        for item in sorted(by_project[group], key=lambda value: value.get("id", "")):
            lines.append(f"### {item['id']} · {item['text']}")
            lines.append("")
            lines.append(f"- **来由**：{item['context']}")
            if item.get("note"):
                lines.append(f"- **批注**：{item['note']}")
            lines.append("")
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
        sorted(projects, key=lambda value: value.get("project_key", ""))
    ):
        if index:
            append_horizontal_rule(lines)
        source = project.get("fact_source") or {}
        aliases = "、".join(project.get("aliases", [])) or "无"
        lines += [
            f"## {project['project_key']} · {project['name']}",
            "",
            f"- **跟踪状态**：{project.get('tracking_state')}",
            f"- **引用类型**：{project.get('reference_type', 'project')}",
            f"- **别名**：{aliases}",
            f"- **事实源**：{source.get('kind', 'unknown')} · {source.get('location', '未记录')}",
            "",
        ]
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
        "| ID | 名称 | 类型 | 已确认关系或含义 |",
        "| --- | --- | --- | --- |",
    ]
    for term in sorted(
        glossary_data.get("terms", []), key=lambda item: item["id"]
    ):
        description = term.get("description", "未记录").replace("|", "\\|")
        lines.append(
            f"| {term['id']} | {term['name']} | "
            f"{ENTITY_KIND_LABELS.get(term.get('kind'), term.get('kind'))} | "
            f"{description} |"
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


def current_view_contents(projects_data, entries_data, glossary_data):
    return {
        NOW_PATH: generated_view(
            "entries.json + projects.json", render_now(projects_data, entries_data)
        ),
        PROJECTS_VIEW_PATH: generated_view(
            "projects.json + Project Catalog", render_projects(projects_data)
        ),
        INSIGHTS_VIEW_PATH: generated_view(
            "entries.json", render_insights(projects_data, entries_data)
        ),
        GLOSSARY_VIEW_PATH: generated_view(
            "glossary.json", render_glossary(glossary_data)
        ),
    }
