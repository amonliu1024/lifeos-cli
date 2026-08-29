"""Read-only Work reporting and history handlers."""

import json
import sys
from datetime import datetime

from ..config import TIMEZONE, VALUE_TYPES
from ..errors import fail
from ..model import (
    effective_project_id,
    effective_project_label,
    find_item,
    matches_created_period,
    matches_period,
    normalized_values,
    parse_moment,
    review_period_label,
)
from ..runtime import read_current_data, read_events
from ..views import append_horizontal_rule, render_brief, render_now


def command_now(_args):
    projects, work_items, tasks, _glossary, _ideas, _achievements = read_current_data()
    print(render_now(projects, work_items, tasks), end="")

def command_brief(args):
    (
        _projects,
        work_items,
        tasks,
        _glossary,
        ideas,
        _achievements,
    ) = read_current_data()
    print(render_brief(work_items, tasks, ideas, args.mode, events=read_events()), end="")

def command_show(args):
    projects, work_items, tasks, glossary, ideas, achievements = read_current_data()
    all_items = [
        *projects["projects"],
        *work_items["work_items"],
        *tasks["tasks"],
        *glossary["terms"],
        *ideas["ideas"],
        *achievements["achievements"],
    ]
    print(
        json.dumps(
            find_item(all_items, args.id, "记录"), ensure_ascii=False, indent=2
        )
    )

def completed_tasks(args):
    (
        projects,
        work_items,
        tasks,
        _glossary,
        _ideas,
        achievements,
    ) = read_current_data()
    projects_by_id = {item.get("id"): item for item in projects["projects"]}
    work_items_by_id = {
        item.get("id"): item for item in work_items["work_items"]
    }
    achievements_by_task = {}
    for achievement in achievements["achievements"]:
        summary = {
            "id": achievement["id"],
            "title": achievement["title"],
            "lifecycle": achievement["lifecycle"],
        }
        for link in achievement["task_links"]:
            achievements_by_task.setdefault(link["task_id"], []).append(summary)
    result = []
    for task in tasks["tasks"]:
        if task.get("status") != "completed" or not matches_period(task, args):
            continue
        display = dict(task)
        display["_project_label"] = effective_project_label(
            task, projects_by_id, work_items_by_id
        )
        if args.project and args.project.casefold() not in (
            f"{display['_project_label']} "
            f"{effective_project_id(task, work_items_by_id) or ''}"
        ).casefold():
            continue
        if args.value_type and not any(
            value.get("type") == args.value_type
            for value in normalized_values(task.get("completion", {}))
        ):
            continue
        display["_achievements"] = sorted(
            achievements_by_task.get(task["id"], []),
            key=lambda value: value["id"],
        )
        result.append(display)
    return sorted(result, key=lambda item: item.get("closed_at", ""), reverse=True)

def history_lines(item):
    completion = item.get("completion", {})
    lines = [
        f"### {item['id']} · {item.get('_project_label', '未归属')}",
        "",
        f"- **完成时间**：{item.get('closed_at', '未记录')}",
        f"- **完成摘要**：{completion.get('summary', item.get('outcome', '未记录'))}",
        "- **来源**：",
    ]
    for source in completion.get("sources", []):
        lines.append(f"  - {source.get('location', '未记录')}")
    lines.append("- **实际价值**：")
    values = normalized_values(completion)
    if values:
        for value in values:
            lines.append(
                f"  - {VALUE_TYPES.get(value.get('type'), value.get('type', '其他'))}："
                f"{value.get('statement', '未记录')}"
            )
    else:
        lines.append("  - 本次关闭明确记录为尚未观察到价值。")
    lines.append("- **复盘**：")
    reflections = completion.get("reflections", [])
    if reflections:
        for reflection in reflections:
            lines.append(f"  - {reflection}")
    else:
        lines.append("  - 尚未补充。")
    achievements = item.get("_achievements", [])
    lines.append("- **成果胶囊**：")
    if achievements:
        for achievement in achievements:
            lines.append(
                f"  - {achievement['id']} · {achievement['title']}"
                f"（{achievement['lifecycle']}）"
            )
    else:
        lines.append("  - 无；普通完成事项不要求形成成果胶囊。")
    lines.append("")
    return lines

def command_history(args):
    items = completed_tasks(args)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    lines = ["# 历史成果", "", f"共 {len(items)} 项。", ""]
    if not items:
        lines += ["当前筛选范围内没有已完成待办。", ""]
    else:
        append_horizontal_rule(lines)
    for index, item in enumerate(items):
        if index:
            append_horizontal_rule(lines)
        lines += history_lines(item)
    print("\n".join(lines), end="")

def command_review(args):
    items = completed_tasks(args)
    (
        _projects,
        _work_items,
        _tasks,
        _glossary,
        _ideas,
        achievements_data,
    ) = read_current_data()
    achievements = [
        item
        for item in achievements_data["achievements"]
        if matches_created_period(item, args)
    ]
    if args.json:
        counts = {}
        for item in items:
            for value in normalized_values(item.get("completion", {})):
                value_type = value.get("type", "other")
                counts[value_type] = counts.get(value_type, 0) + 1
        print(
            json.dumps(
                {
                    "period": review_period_label(args),
                    "completed_count": len(items),
                    "projects": sorted(
                        {item.get("_project_label", "未归属") for item in items}
                    ),
                    "value_counts": counts,
                    "items": items,
                    "achievements": achievements,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    groups = {}
    for item in items:
        groups.setdefault(item["_project_label"], []).append(item)
    lines = [
        f"# {review_period_label(args)} 成果复盘",
        "",
        f"- 完成待办：{len(items)} 项",
        f"- 覆盖项目引用：{len(groups)} 个",
        f"- 形成成果胶囊：{len(achievements)} 个",
        "",
    ]
    for project, project_items in sorted(groups.items()):
        append_horizontal_rule(lines)
        lines += [f"## {project}（{len(project_items)}）", ""]
        for item in project_items:
            lines.append(
                f"- {item['id']}：{item.get('completion', {}).get('summary', item['outcome'])}"
            )
        lines.append("")
    append_horizontal_rule(lines)
    lines += ["## 成果胶囊", ""]
    if achievements:
        for achievement in sorted(
            achievements, key=lambda value: value["created_at"], reverse=True
        ):
            lines.append(
                f"- {achievement['id']}：{achievement['title']}"
                f"（{achievement['lifecycle']}）"
            )
    else:
        lines.append("- 本周期未形成成果胶囊；普通完成事项不构成沉淀债务。")
    lines.append("")
    print("\n".join(lines), end="")

DEFAULT_CHANGES_LIMIT = 20


def _within_window(event, window_from, window_to):
    """Half-open ``[from, to)`` membership for one audit event.

    Events whose ``occurred_at`` cannot be read are reported rather than
    silently dropped: a windowed query that quietly loses events is worse than
    one that says it did.
    """

    raw = event.get("occurred_at")
    if not raw:
        return None
    try:
        occurred_at = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=TIMEZONE)
    if window_from is not None and occurred_at < window_from:
        return False
    if window_to is not None and occurred_at >= window_to:
        return False
    return True


def command_changes(args):
    events = read_events()
    window_from = parse_moment(args.from_value) if args.from_value else None
    window_to = parse_moment(args.to_value) if args.to_value else None
    if window_from and window_to and window_to <= window_from:
        fail("--to 必须晚于 --from")
    limit = args.limit
    if limit is not None and limit < 1:
        fail("--limit 必须是正整数")

    if window_from or window_to:
        unreadable = 0
        selected = []
        for event in events:
            membership = _within_window(event, window_from, window_to)
            if membership is None:
                unreadable += 1
            elif membership:
                selected.append(event)
        if unreadable:
            print(
                f"! {unreadable} 条事件缺少可解析的 occurred_at，未纳入时间窗",
                file=sys.stderr,
            )
    else:
        selected = events
        limit = limit or DEFAULT_CHANGES_LIMIT
    if limit:
        selected = selected[-limit:]

    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return
    blocks = []
    for event in selected:
        target_id = next(
            (
                event.get(field)
                for field in (
                    "task_id",
                    "work_item_id",
                    "milestone_id",
                    "project_id",
                    "idea_id",
                    "achievement_id",
                    "commitment_id",
                )
                if event.get(field)
            ),
            None,
        )
        target = f" · {target_id}" if target_id else ""
        blocks.append(
            f"{event.get('occurred_at', '?')} · {event.get('event_id', '?')}"
            f" · {event.get('kind', '?')}{target}\n  {event.get('summary', '')}"
        )
    if blocks:
        print("\n---\n".join(blocks))
