"""Entry query and mutation handlers: • 待办、– 随记、? 疑问、! 洞见.

Every mutation goes through one ``transaction`` on ``entries.json``.  Status
changes are recorded in the audit event with the previous status and the note
given at that moment, because the entry itself only keeps the latest note.
"""

import json

from ..config import (
    CONVERTIBLE_KINDS,
    ENTRY_KIND_LABELS,
    ENTRY_SYMBOLS,
    STARRABLE_KINDS,
    entry_field_order,
    status_label,
)
from ..errors import fail
from ..model import (
    entry_matches_query,
    event_entry_ids,
    find_entry,
    generate_entry_id,
    is_kept,
    is_live,
    iso_now,
    last_activity_date,
    logged_on,
    make_event,
    now,
    project_label,
    schedule_change,
)
from ..runtime import read_current_data, read_events, transaction
from ..views import render_entries


CREATE_VERBS = {
    "task": "记下待办",
    "note": "记下随记",
    "question": "记下疑问",
    "insight": "留下洞见",
}


def new_entry(kind, entries, args, *, timestamp):
    entry = {field: None for field in entry_field_order(kind)}
    entry.update(
        {
            "id": generate_entry_id(kind, entries),
            "kind": kind,
            "text": args.text,
            "project": getattr(args, "project", None),
            "status": "open",
            "context": getattr(args, "context", None) or None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    if kind in STARRABLE_KINDS:
        entry["starred"] = bool(getattr(args, "star", False))
    return entry


def ensure_project(projects_data, project_key):
    if project_key and not any(
        item.get("project_key") == project_key for item in projects_data["projects"]
    ):
        fail(f"没有跟踪这个项目：{project_key}；先用 project-track 跟踪")


def take_source(entries, source_id, new_kind):
    """A note or question about to be converted (`>`) into a new entry."""
    source = find_entry(entries, source_id, CONVERTIBLE_KINDS)
    if source.get("status") != "open":
        fail(f"{source_id} 已经了结，不能再转化")
    if source["kind"] == new_kind:
        fail(f"{source_id} 已经是{ENTRY_KIND_LABELS[new_kind]}，不需要转化")
    return source


def add_entry(args, kind, prepare=None):
    """Shared create path; ``prepare(entry, entries)`` may fill kind fields."""
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("entries")
        entries = data["entries"]
        ensure_project(tx.data("projects"), getattr(args, "project", None))
        source = None
        if getattr(args, "from_id", None):
            source = take_source(entries, args.from_id, kind)
        timestamp = iso_now()
        entry = new_entry(kind, entries, args, timestamp=timestamp)
        if source is not None and entry["project"] is None:
            entry["project"] = source.get("project")
        summary = f"{CREATE_VERBS[kind]} {entry['id']}：{entry['text']}"
        if prepare:
            summary += prepare(entry, entries) or ""
        entries.append(entry)
        if source is not None:
            source["status"] = "converted"
            source["ref"] = entry["id"]
            source["updated_at"] = timestamp
            summary += f"；由 {source['id']} 转化"
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "entry_created",
            summary,
            entry_id=entry["id"],
            related_entry_id=source["id"] if source else None,
            sources=args.source,
            status_change=(None, "open", None),
        )
        if kind == "task" and entry.get("due"):
            event["schedule"] = {"due": entry["due"]}
        tx.commit("entries", event)


def command_task_add(args):
    def prepare(entry, _entries):
        entry["due"] = args.due
        entry["owner"] = args.owner
        return f"；负责人 {args.owner}" if args.owner else ""

    add_entry(args, "task", prepare)


def command_note_add(args):
    add_entry(args, "note")


def command_question_add(args):
    add_entry(args, "question")


def command_insight_add(args):
    if args.answers and args.from_id:
        fail("--answers 与 --from 不能同时使用")

    def prepare(entry, entries):
        if not args.answers:
            return ""
        question = find_entry(entries, args.answers, {"question"})
        if question.get("status") != "open":
            fail(f"{args.answers} 已经了结，不能再回答")
        question["status"] = "done"
        question["note"] = entry["text"]
        question["ref"] = entry["id"]
        question["updated_at"] = entry["created_at"]
        return f"；回答了 {args.answers}"

    add_entry(args, "insight", prepare)


def update_entry(args, kinds, summary, event_kind, change):
    """Apply ``change(tx, entry, entries)`` to one entry in a single transaction.

    ``change`` returns an optional detail for the audit summary.  A change of
    status is written into the event together with the note given for it.
    """
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("entries")
        entry = find_entry(data["entries"], args.id, kinds)
        before = entry.get("status")
        detail = change(tx, entry, data["entries"])
        timestamp = iso_now()
        entry["updated_at"] = timestamp
        data["updated_at"] = timestamp
        after = entry.get("status")
        changed = before != after
        event = make_event(
            tx.events,
            args,
            event_kind,
            f"{summary} {args.id}" + (f"：{detail}" if detail else ""),
            entry_id=args.id,
            related_entry_id=entry.get("ref") if changed else None,
            sources=args.source,
            status_change=(before, after, entry.get("note")) if changed else None,
        )
        tx.commit("entries", event)


def require_editable(entry):
    if not is_kept(entry):
        label = status_label(entry.get("kind"), entry.get("status"))
        fail(f"{entry['id']} 已经{label}，不能再修改")


def command_entry_update(args):
    def change(tx, entry, _entries):
        require_editable(entry)
        kind = entry["kind"]
        changes = []

        def set_field(field, value):
            if entry.get(field) != value:
                entry[field] = value
                changes.append(field)

        if args.text is not None:
            set_field("text", args.text)
        if args.context is not None:
            set_field("context", args.context)
        elif args.clear_context:
            if kind == "insight":
                fail("洞见的来由不能清空")
            set_field("context", None)
        if args.note is not None:
            set_field("note", args.note)
        elif args.clear_note:
            set_field("note", None)
        if args.project is not None:
            ensure_project(tx.data("projects"), args.project)
            set_field("project", args.project)
        elif args.clear_project:
            set_field("project", None)
        if args.star or args.unstar:
            if kind not in STARRABLE_KINDS:
                fail("只有待办和疑问可以标星")
            set_field("starred", bool(args.star))
        if args.owner is not None or args.mine:
            if kind != "task":
                fail("只有待办有负责人")
            set_field("owner", None if args.mine else args.owner)
        if args.reopen:
            if kind != "task" or entry.get("status") != "scheduled":
                fail("--reopen 只用于排到以后的待办")
            set_field("status", "open")
            set_field("month", None)
            if args.note is None:
                set_field("note", None)
        if not changes:
            fail("没有提供任何实际更新")
        return ", ".join(dict.fromkeys(changes))

    update_entry(args, None, "修改", "entry_updated", change)


def command_task_done(args):
    def change(_tx, entry, _entries):
        if not is_live(entry):
            fail(f"{entry['id']} 已经{status_label('task', entry.get('status'))}")
        entry["status"] = "done"
        entry["note"] = args.note
        entry["month"] = None
        return args.note

    update_entry(args, {"task"}, "完成待办", "task_done", change)


def command_task_schedule(args):
    def change(_tx, entry, _entries):
        if not is_live(entry):
            fail(f"{entry['id']} 已经{status_label('task', entry.get('status'))}")
        if args.month <= now().strftime("%Y-%m"):
            fail("--month 必须是以后的月份；这个月还要做就保持待做")
        entry["status"] = "scheduled"
        entry["month"] = args.month
        entry["note"] = args.note
        return f"排到 {args.month}"

    update_entry(args, {"task"}, "排到以后", "task_scheduled", change)


def command_task_reschedule(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("entries")
        entry = find_entry(data["entries"], args.id, {"task"})
        if not is_live(entry):
            fail("已经了结的待办不能调整截止时间")
        if args.due is None and not args.clear_due:
            fail("没有提供任何实际日期变化")
        change = schedule_change("due_at", entry.get("due"), None if args.clear_due else args.due)
        if not change:
            fail("没有提供任何实际日期变化")
        if change["direction"] in {"postponed", "cleared"} and not args.reason_code:
            fail("延后或清除截止时间必须提供 --reason-code")
        if args.note and not args.reason_code:
            fail("--note 必须配合 --reason-code")
        entry["due"] = change["to"]
        timestamp = iso_now()
        entry["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "task_schedule_changed",
            f"调整待办 {args.id} 的截止时间：{change['from'] or '未设置'} → {change['to'] or '未设置'}",
            entry_id=args.id,
            sources=args.source,
        )
        event["schedule_changes"] = [change]
        if args.reason_code:
            event["reason_code"] = args.reason_code
        if args.note:
            event["reason_note"] = args.note
        tx.commit("entries", event)


def command_task_schedule_history(args):
    _projects, entries_data, _glossary = read_current_data()
    find_entry(entries_data["entries"], args.id, {"task"})
    selected = []
    for event in read_events():
        if args.id not in event_entry_ids(event):
            continue
        schedule = event.get("schedule") or {}
        baseline = schedule.get("due") or schedule.get("due_at")
        if event.get("kind") in {"task_created", "entry_created"} and baseline:
            selected.append(
                {
                    "event_id": event.get("event_id"),
                    "occurred_at": event.get("occurred_at"),
                    "kind": "schedule_baseline",
                    "due": baseline,
                    "sources": event.get("sources", []),
                }
            )
        elif event.get("kind") == "task_schedule_changed":
            changes = [
                change for change in event.get("schedule_changes", [])
                if change.get("field") == "due_at"
            ]
            if not changes:
                continue
            record = {
                "event_id": event.get("event_id"),
                "occurred_at": event.get("occurred_at"),
                "kind": event["kind"],
                "schedule_changes": changes,
                "sources": event.get("sources", []),
            }
            for field in ("reason_code", "reason_note"):
                if event.get(field):
                    record[field] = event[field]
            selected.append(record)
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return
    if not selected:
        print("这条待办没有截止时间历史")
        return
    blocks = []
    for record in selected:
        if record["kind"] == "schedule_baseline":
            blocks.append(f"{record['occurred_at']} · 初始截止 · {record['due']}")
            continue
        lines = [f"{record['occurred_at']} · 日期调整 · {record.get('reason_code') or '未提供原因'}"]
        for change in record["schedule_changes"]:
            lines.append(
                f"  截止：{change['from'] or '未设置'} -> {change['to'] or '未设置'} ({change['direction']})"
            )
        blocks.append("\n".join(lines))
    print("\n---\n".join(blocks))


def command_question_answer(args):
    def change(_tx, entry, entries):
        if entry.get("status") != "open":
            fail(f"{entry['id']} 已经了结")
        if args.by:
            find_entry(entries, args.by, {"insight"})
        entry["status"] = "done"
        entry["note"] = args.note
        entry["ref"] = args.by
        return args.note

    update_entry(args, {"question"}, "想通了", "question_answered", change)


def command_entry_drop(args):
    def change(_tx, entry, entries):
        if not is_kept(entry):
            fail(f"{entry['id']} 已经了结")
        if args.by:
            if entry["kind"] != "insight":
                fail("--by 只用于被新洞见取代的洞见")
            replacement = find_entry(entries, args.by, {"insight"})
            if replacement["id"] == entry["id"] or replacement.get("status") != "open":
                fail("--by 必须指向另一条仍有效的洞见")
        entry["status"] = "dropped"
        entry["note"] = args.note
        entry["ref"] = args.by
        if entry["kind"] == "task":
            entry["month"] = None
        return args.note

    update_entry(args, None, "划掉", "entry_dropped", change)


def command_entry_keep(args):
    def change(_tx, entry, _entries):
        if not is_live(entry):
            fail(f"{entry['id']} 已经了结，不在盘点范围内")
        if last_activity_date(entry) == now().date():
            fail(f"{entry['id']} 今天已经动过，不需要再复核")
        return None

    update_entry(args, None, "月初盘点：继续", "entry_kept", change)


def command_entries(args):
    projects_data, entries_data, _glossary = read_current_data()
    projects_by_key = {item.get("project_key"): item for item in projects_data["projects"]}
    entries = entries_data["entries"]
    if args.kind:
        entries = [item for item in entries if item.get("kind") == args.kind]
    if args.status:
        entries = [item for item in entries if item.get("status") == args.status]
    if args.live:
        entries = [item for item in entries if is_kept(item)]
    if args.starred:
        entries = [item for item in entries if item.get("starred")]
    if args.project:
        needle = args.project.casefold()
        entries = [
            item for item in entries
            if item.get("project")
            and needle in f"{item['project']} {project_label(item['project'], projects_by_key)}".casefold()
        ]
    if args.on:
        entries = [item for item in entries if logged_on(item) == args.on]
    entries = [item for item in entries if entry_matches_query(item, args.query)]
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return
    title = "记下的每一笔"
    if args.kind:
        title = f"{ENTRY_SYMBOLS[args.kind]} {ENTRY_KIND_LABELS[args.kind]}"
    print(render_entries(projects_data, entries, title), end="")
