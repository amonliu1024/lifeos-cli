"""Task query and mutation handlers for the LifeOS Work domain.

Handlers retain the stage-one function interface while their shared rules,
Runtime I/O, and presentation are owned by dedicated modules.
"""

import json

from ..errors import fail
from ..model import (
    canonical_responsible_party,
    ensure_entity_ids,
    ensure_task_milestone,
    find_item,
    generate_id,
    iso_now,
    latest_task_started_dates,
    make_event,
    normalized_values,
    now,
    parse_value_entries,
    schedule_change,
    source_objects,
    tasks_for_display,
)
from ..runtime import (
    read_current_data,
    read_events,
    transaction,
)
from ..views import render_tasks


def command_tasks(args):
    (
        _projects,
        work_items_data,
        tasks_data,
        _glossary,
        _ideas,
        _achievements,
    ) = read_current_data()
    tasks = tasks_data["tasks"]
    if args.status:
        tasks = [item for item in tasks if item.get("status") == args.status]
    tasks = tasks_for_display(tasks, work_items_data["work_items"])
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
    else:
        print(
            render_tasks(
                {
                    "updated_at": tasks_data.get("updated_at"),
                    "tasks": tasks,
                }
            ),
            end="",
        )

def command_task_add(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects_data = tx.data("projects")
        work_items_data = tx.data("work_items")
        data = tx.data("tasks")
        events = tx.events
        if args.work_item_id and args.project_id:
            fail("待办已关联事项时不能再声明项目；有效项目由事项继承")
        if args.work_item_id:
            find_item(work_items_data["work_items"], args.work_item_id, "事项")
        if args.project_id:
            find_item(projects_data["projects"], args.project_id, "项目引用")
        if args.status in {"waiting", "paused"} and not args.reason:
            fail("waiting 或 paused 待办必须提供 --reason")
        if args.responsible_kind != "self" and not args.responsible_party:
            fail("非 self 待办必须提供 --responsible-party 的真实名称")
        ensure_task_milestone(
            work_items_data["work_items"],
            args.work_item_id,
            args.milestone_id,
            args.status,
        )
        timestamp = iso_now()
        item_id = generate_id(
            "TASK", [item.get("id", "") for item in data["tasks"]]
        )
        glossary_data = tx.data("glossary")
        party = canonical_responsible_party(
            glossary_data,
            args.responsible_kind,
            args.responsible_party,
            args.responsible_entity,
        )
        if args.responsible_entity and args.responsible_kind != "self":
            ensure_entity_ids(glossary_data, [args.responsible_entity])
        data["tasks"].append(
            {
                "id": item_id,
                "outcome": args.outcome,
                "work_item_id": args.work_item_id,
                "project_id": args.project_id if not args.work_item_id else None,
                "status": args.status,
                "status_reason": args.reason,
                "responsible_party": party,
                "next_action": (
                    {"text": args.next_action}
                    if args.next_action
                    else None
                ),
                "due_at": args.due,
                "why": args.why,
                "completion_criteria": args.completion_criteria,
                "context": args.context,
                "completion": None,
                "sources": source_objects(args.source),
                "created_at": timestamp,
                "updated_at": timestamp,
                "milestone_id": args.milestone_id,
            }
        )
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "task_created",
            f"创建待办 {item_id}：{args.outcome}",
            task_id=item_id,
            milestone_id=args.milestone_id,
            sources=args.source,
        )
        initial_schedule = {
            "due_at": args.due,
        }
        initial_schedule = {
            field: value
            for field, value in initial_schedule.items()
            if value is not None
        }
        if initial_schedule:
            event["schedule"] = initial_schedule
        tx.commit("tasks", event)


def command_task_start(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("tasks")
        events = tx.events
        item = find_item(data["tasks"], args.id, "待办")
        if item.get("status") in {"completed", "cancelled"}:
            fail("已完成或已取消待办不能记录开始推进日期")
        if args.started_at > now().date().isoformat():
            fail("开始推进日期不能晚于今天")
        previous_started_at = latest_task_started_dates(events).get(args.id)
        if previous_started_at == args.started_at:
            fail("待办已经有相同的开始推进日期")
        item.setdefault("sources", []).extend(source_objects(args.source))
        timestamp = iso_now()
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "task_started",
            f"记录待办 {args.id} 开始推进日期：{args.started_at}",
            task_id=args.id,
            sources=args.source,
        )
        event["started_at"] = args.started_at
        if previous_started_at is not None:
            event["previous_started_at"] = previous_started_at
        tx.commit("tasks", event)


def command_task_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects_data = tx.data("projects")
        work_items_data = tx.data("work_items")
        glossary_data = tx.data("glossary")
        data = tx.data("tasks")
        events = tx.events
        item = find_item(data["tasks"], args.id, "待办")
        if item.get("status") == "completed":
            fail("已完成待办只能使用 task-reflect 补充成果")
        if args.work_item_id and args.project_id:
            fail("待办已关联事项时不能再声明项目；有效项目由事项继承")
        if args.work_item_id:
            find_item(work_items_data["work_items"], args.work_item_id, "事项")
        if args.project_id:
            find_item(projects_data["projects"], args.project_id, "项目引用")
        changes = []
        for field, value in {
            "outcome": args.outcome,
            "status": args.status,
            "status_reason": args.reason,
            "why": args.why,
            "completion_criteria": args.completion_criteria,
            "context": args.context,
        }.items():
            if value is not None and value != item.get(field):
                item[field] = value
                changes.append(field)
        if args.work_item_id is not None:
            if (
                item.get("work_item_id") != args.work_item_id
                or item.get("project_id") is not None
            ):
                item["work_item_id"] = args.work_item_id
                item["project_id"] = None
                changes.append("work_item_id")
        elif args.clear_work_item and item.get("work_item_id") is not None:
            item["work_item_id"] = None
            changes.append("work_item_id")
        if args.project_id is not None:
            if item.get("work_item_id") and not args.clear_work_item:
                fail("设置直接项目引用前必须同时使用 --clear-work-item")
            if item.get("project_id") != args.project_id:
                item["work_item_id"] = None
                item["project_id"] = args.project_id
                changes.append("project_id")
        elif args.clear_project and item.get("project_id") is not None:
            item["project_id"] = None
            changes.append("project_id")
        if item.get("work_item_id"):
            item["project_id"] = None
        if args.milestone_id is not None:
            if item.get("milestone_id") != args.milestone_id:
                item["milestone_id"] = args.milestone_id
                changes.append("milestone_id")
        elif args.clear_milestone and item.get("milestone_id") is not None:
            item.pop("milestone_id")
            changes.append("milestone_id")
        if (
            args.responsible_party is not None
            or args.responsible_kind is not None
            or args.responsible_entity is not None
        ):
            current_party = item.get("responsible_party") or {}
            kind = args.responsible_kind or current_party.get("kind")
            if (
                args.responsible_kind is not None
                and args.responsible_kind != current_party.get("kind")
                and args.responsible_kind != "self"
                and not args.responsible_party
            ):
                fail("切换为个人、组织或 unknown 责任方时必须提供真实名称")
            name = (
                args.responsible_party
                if args.responsible_party is not None
                else current_party.get("name")
            )
            entity_id = (
                args.responsible_entity
                if args.responsible_entity is not None
                else current_party.get("entity_id")
            )
            if args.responsible_party is not None and args.responsible_entity is None:
                entity_id = None
            if args.responsible_kind is not None and args.responsible_kind != current_party.get("kind") and args.responsible_entity is None:
                entity_id = None
            if args.responsible_entity is not None and kind != "self":
                ensure_entity_ids(glossary_data, [args.responsible_entity])
            party = canonical_responsible_party(
                glossary_data, kind, name, entity_id
            )
            if party != current_party:
                item["responsible_party"] = party
                changes.append("responsible_party")
        if args.next_action is not None or args.clear_next_action:
            value = None if args.clear_next_action else dict(item.get("next_action") or {})
            if value is not None:
                if args.next_action is not None:
                    value["text"] = args.next_action
            if value != item.get("next_action"):
                item["next_action"] = value
                changes.append("next_action")
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
            changes.append("sources")
        ensure_task_milestone(
            work_items_data["work_items"],
            item.get("work_item_id"),
            item.get("milestone_id"),
            item.get("status"),
        )
        if item.get("status") in {"waiting", "paused", "cancelled"} and not item.get("status_reason"):
            fail("waiting、paused 或 cancelled 待办必须提供 --reason")
        if not changes:
            fail("没有提供任何实际更新")
        timestamp = iso_now()
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "task_updated",
            f"更新待办 {args.id}：{', '.join(changes)}",
            task_id=args.id,
            milestone_id=item.get("milestone_id"),
            sources=args.source,
        )
        tx.commit("tasks", event)
def command_task_reschedule(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("tasks")
        events = tx.events
        item = find_item(data["tasks"], args.id, "待办")
        if item.get("status") == "completed":
            fail("已完成待办不能调整计划日期")

        changes = []
        due_value = None if args.clear_due else args.due
        if args.due is not None or args.clear_due:
            change = schedule_change("due_at", item.get("due_at"), due_value)
            if change:
                changes.append(change)

        if not changes:
            fail("没有提供任何实际日期变化")
        if any(
            value["direction"] in {"postponed", "cleared"}
            for value in changes
        ) and not args.reason_code:
            fail("延后或清除计划日期必须提供 --reason-code")
        if args.note and not args.reason_code:
            fail("--note 必须配合 --reason-code")

        for change in changes:
            item["due_at"] = change["to"]

        timestamp = iso_now()
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        fields = ", ".join(value["field"] for value in changes)
        event = make_event(
            events,
            args,
            "task_schedule_changed",
            f"调整待办 {args.id} 的计划日期：{fields}",
            task_id=args.id,
            milestone_id=item.get("milestone_id"),
            sources=args.source,
        )
        event["schedule_changes"] = changes
        if args.reason_code:
            event["reason_code"] = args.reason_code
        if args.note:
            event["reason_note"] = args.note
        tx.commit("tasks", event)

def command_task_schedule_history(args):
    _projects, _work_items, tasks, _glossary, _ideas, _achievements = (
        read_current_data()
    )
    find_item(tasks["tasks"], args.id, "待办")
    selected = []
    for event in read_events():
        if event.get("task_id") != args.id:
            continue
        if event.get("kind") == "task_created" and event.get("schedule"):
            schedule = {
                field: value
                for field, value in event["schedule"].items()
                if field == "due_at"
            }
            if not schedule:
                continue
            selected.append(
                {
                    "event_id": event.get("event_id"),
                    "occurred_at": event.get("occurred_at"),
                    "kind": "schedule_baseline",
                    "schedule": schedule,
                    "sources": event.get("sources", []),
                }
            )
        elif event.get("kind") == "task_schedule_changed":
            schedule_changes = [
                change
                for change in event.get("schedule_changes", [])
                if change.get("field") == "due_at"
            ]
            if not schedule_changes:
                continue
            selected.append(
                {
                    "event_id": event.get("event_id"),
                    "occurred_at": event.get("occurred_at"),
                    "kind": event["kind"],
                    "schedule_changes": schedule_changes,
                    **(
                        {"reason_code": event["reason_code"]}
                        if event.get("reason_code")
                        else {}
                    ),
                    **(
                        {"reason_note": event["reason_note"]}
                        if event.get("reason_note")
                        else {}
                    ),
                    "sources": event.get("sources", []),
                }
            )
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return
    if not selected:
        print("没有可查询的结构化硬截止历史；历史行动日期不会作为当前字段展示")
        return
    blocks = []
    for entry in selected:
        if entry["kind"] == "schedule_baseline":
            values = ", ".join(
                f"{field}={value}" for field, value in entry["schedule"].items()
            )
            blocks.append(f"{entry['occurred_at']} · 初始计划 · {values}")
            continue
        reason = entry.get("reason_code") or "未提供原因"
        lines = [f"{entry['occurred_at']} · 日期调整 · {reason}"]
        for change in entry["schedule_changes"]:
            lines.append(
                f"  {change['field']}: {change['from'] or '未设置'} -> "
                f"{change['to'] or '未设置'} ({change['direction']})"
            )
        blocks.append("\n".join(lines))
    print("\n---\n".join(blocks))

def command_task_close(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("tasks")
        events = tx.events
        item = find_item(data["tasks"], args.id, "待办")
        if item.get("status") == "completed":
            fail(f"待办已经关闭：{args.id}")
        values = parse_value_entries(args.value)
        if values and args.no_realized_value:
            fail("--value 与 --no-realized-value 不能同时使用")
        timestamp = iso_now()
        item["status"] = "completed"
        item["status_reason"] = None
        item["closed_at"] = timestamp
        item["next_action"] = None
        item["completion"] = {
            "summary": args.summary,
            "sources": source_objects(args.completion_source),
            "values": values,
            "reflections": [args.reflection] if args.reflection else [],
        }
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "task_closed",
            f"关闭待办 {args.id}：{args.summary}",
            task_id=args.id,
            sources=args.source,
        )
        tx.commit("tasks", event)

def command_task_reflect(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("tasks")
        events = tx.events
        item = find_item(data["tasks"], args.id, "待办")
        if item.get("status") != "completed":
            fail("只有已完成待办可以补充成果和复盘")
        completion = item.setdefault(
            "completion",
            {"summary": "", "sources": [], "values": [], "reflections": []},
        )
        changes = []
        if args.summary and args.summary != completion.get("summary"):
            completion["summary"] = args.summary
            changes.append("summary")
        for source in source_objects(args.completion_source):
            if source not in completion.setdefault("sources", []):
                completion["sources"].append(source)
                changes.append("completion_sources")
        values = parse_value_entries(args.value)
        if values and args.no_realized_value:
            fail("--value 与 --no-realized-value 不能同时使用")
        if values:
            completion.setdefault("values", []).extend(values)
            changes.append("values")
        if args.no_realized_value:
            if normalized_values(completion):
                fail("已有实际价值记录，不能标记为未观察到价值")
        timestamp = iso_now()
        if args.reflection:
            completion.setdefault("reflections", []).append(args.reflection)
            changes.append("reflection")
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
            changes.append("sources")
        if not changes:
            fail("没有提供任何成果或复盘补充")
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "task_reflected",
            f"补充待办 {args.id} 的历史成果：{', '.join(changes)}",
            task_id=args.id,
            sources=args.source,
        )
        tx.commit("tasks", event)
