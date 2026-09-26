"""Read-only Work reporting and history handlers."""

import json
import sys
from datetime import datetime

from ..config import TIMEZONE
from ..errors import fail
from ..model import (
    event_entry_ids,
    find_item,
    matches_created_period,
    matches_period,
    parse_moment,
    project_label,
    review_period_label,
)
from ..runtime import read_current_data, read_events
from ..views import append_horizontal_rule, render_brief, render_now


def command_now(_args):
    projects, entries, _glossary = read_current_data()
    print(render_now(projects, entries), end="")


def command_brief(args):
    projects, entries, _glossary = read_current_data()
    print(render_brief(projects, entries, args.mode), end="")


def command_show(args):
    """One record plus, for an entry, the audit events that shaped it."""
    projects, entries, glossary = read_current_data()
    project = next(
        (item for item in projects["projects"] if item.get("project_key") == args.id),
        None,
    )
    record = project or find_item([*entries["entries"], *glossary["terms"]], args.id, "记录")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if project or args.no_history:
        return
    history = [event for event in read_events() if args.id in event_entry_ids(event)]
    if not history:
        return
    print("\n历史：")
    for event in history:
        line = f"- {event.get('occurred_at', '?')} · {event.get('kind', '?')} · {event.get('summary', '')}"
        if event.get("status_from") or event.get("status_to"):
            line += f"（{event.get('status_from') or '—'} → {event.get('status_to')}）"
        print(line)


def _project_filter(args, projects_by_key):
    if not args.project:
        return lambda item: True
    needle = args.project.casefold()
    return lambda item: needle in (
        f"{project_label(item.get('project'), projects_by_key)} "
        f"{item.get('project') or ''}"
    ).casefold()


def period_records(args):
    projects, entries_data, _glossary = read_current_data()
    projects_by_key = {item.get("project_key"): item for item in projects["projects"]}
    in_project = _project_filter(args, projects_by_key)
    done = []
    insights = []
    for entry in entries_data["entries"]:
        if not in_project(entry):
            continue
        display = dict(entry)
        display["_project_label"] = project_label(entry.get("project"), projects_by_key)
        if entry.get("kind") == "task" and entry.get("status") == "done" and matches_period(entry, args):
            done.append(display)
        elif entry.get("kind") == "insight" and matches_created_period(entry, args):
            insights.append(display)
    done.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    insights.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return done, insights


def history_lines(item):
    return [
        f"### {item['id']} · {item.get('_project_label', '未归属')}",
        "",
        f"- **待办**：{item['text']}",
        f"- **完成时间**：{item.get('updated_at', '未记录')}",
        f"- **完成了什么**：{item.get('note') or '未记录'}",
        "",
    ]


def command_history(args):
    items, _insights = period_records(args)
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
    items, insights = period_records(args)
    if args.json:
        print(
            json.dumps(
                {
                    "period": review_period_label(args),
                    "completed_count": len(items),
                    "projects": sorted({item["_project_label"] for item in items}),
                    "items": items,
                    "insights": insights,
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
        f"- 覆盖项目：{len(groups)} 个",
        f"- 新留下的洞见：{len(insights)} 条",
        "",
    ]
    for project, project_items in sorted(groups.items()):
        append_horizontal_rule(lines)
        lines += [f"## {project}（{len(project_items)}）", ""]
        for item in project_items:
            lines.append(
                f"- {item['id']}：{item.get('note') or item['text']}"
            )
        lines.append("")
    append_horizontal_rule(lines)
    lines += ["## 洞见", ""]
    if insights:
        for insight in insights:
            lines.append(f"- {insight['id']}：{insight['text']}")
    else:
        lines.append("- 本周期没有新留下的洞见。")
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
                    "entry_id",
                    "project",
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
