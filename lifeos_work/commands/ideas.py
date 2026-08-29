"""Idea query and mutation handlers for the LifeOS Work domain."""

import json

from ..errors import fail
from ..model import (
    find_item,
    generate_id,
    idea_promotion_target_ids,
    iso_now,
    make_event,
    source_objects,
)
from ..runtime import (
    read_current_data,
    transaction,
)
from ..views import render_ideas


def command_idea_add(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("ideas")
        events = tx.events
        item_id = generate_id(
            "IDEA", [item.get("id", "") for item in data["ideas"]]
        )
        timestamp = iso_now()
        data["ideas"].append(
            {
                "id": item_id,
                "text": args.text,
                "status": "inbox",
                "context": args.context,
                "status_reason": None,
                "sources": source_objects(args.source),
                "promoted_to": [],
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "idea_created",
            f"记录闪念 {item_id}：{args.text}",
            idea_id=item_id,
            sources=args.source,
        )
        tx.commit("ideas", event)

def command_idea_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects = tx.data("projects")
        work_items = tx.data("work_items")
        tasks = tx.data("tasks")
        data = tx.data("ideas")
        events = tx.events
        item = find_item(data["ideas"], args.id, "闪念")
        known = idea_promotion_target_ids(work_items, tasks)
        project_ids = {item.get("id") for item in projects.get("projects", [])}
        for target in args.promote_to:
            if target in project_ids:
                fail(f"闪念只允许提升到事项或待办：{target}")
            if target not in known:
                fail(f"找不到提升目标：{target}")
        changes = []
        for field, value in {"text": args.text, "context": args.context}.items():
            if value is not None and value != item.get(field):
                item[field] = value
                changes.append(field)
        for target in args.promote_to:

            if target not in item.setdefault("promoted_to", []):
                item["promoted_to"].append(target)
                changes.append("promote")
        status = "promoted" if args.promote_to else args.status
        if status == "promoted" and not item.get("promoted_to"):
            fail("提升闪念时必须提供 --promote-to <WI/TASK-ID>")
        if status is not None and status != item.get("status"):
            item["status"] = status
            changes.append("status")
        if args.reason is not None and args.reason != item.get("status_reason"):
            item["status_reason"] = args.reason
            changes.append("status_reason")
        if status == "archived" and not item.get("status_reason"):
            fail("归档闪念必须提供 --reason")
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
            changes.append("sources")
        if not changes:
            fail("没有提供任何实际更新")
        timestamp = iso_now()
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "idea_updated",
            f"更新闪念 {args.id}：{', '.join(changes)}",
            idea_id=args.id,
            sources=args.source,
        )
        tx.commit("ideas", event)

def command_ideas(args):
    _projects, _work_items, _tasks, _glossary, data, _achievements = read_current_data()
    ideas = data["ideas"]
    if args.status:
        ideas = [item for item in ideas if item.get("status") == args.status]
    if args.json:
        print(json.dumps(ideas, ensure_ascii=False, indent=2))
    else:
        print(
            render_ideas(
                {"updated_at": data.get("updated_at"), "ideas": ideas},
                include_archived=args.status in {None, "archived"},
            ),
            end="",
        )
