"""Work-item and milestone command handlers."""

import json

from ..errors import fail
from ..model import (
    all_milestone_ids,
    current_milestone,
    ensure_milestone_transition,
    find_item,
    find_milestone,
    generate_id,
    iso_now,
    make_event,
    milestone_list,
    source_objects,
)
from ..runtime import (
    read_current_data,
    transaction,
)
from ..views import render_work_item_milestones, render_work_items


def command_work_items(args):
    _projects, data, _tasks, _glossary, _ideas, _achievements = read_current_data()
    items = data["work_items"]
    if args.state:
        items = [item for item in items if item.get("state") == args.state]
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print(
            render_work_items(
                {"updated_at": data.get("updated_at"), "work_items": items}
            ),
            end="",
        )


def command_work_item_milestones(args):
    _projects, data, _tasks, _glossary, _ideas, _achievements = read_current_data()
    item = find_item(data["work_items"], args.id, "事项")
    if args.json:
        print(json.dumps(milestone_list(item), ensure_ascii=False, indent=2))
    else:
        print(render_work_item_milestones(item), end="")


def command_work_item_milestone_add(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("work_items")
        item = find_item(data["work_items"], args.id, "事项")
        if args.status == "current" and current_milestone(item):
            fail("一个事项最多只能有一个当前里程碑")
        timestamp = iso_now()
        milestone_id = generate_id("MS", all_milestone_ids(data["work_items"]))
        milestone = {
            "id": milestone_id,
            "title": args.title,
            "status": args.status,
            "outcome": args.outcome,
            "completion_criteria": args.completion_criteria,
            "target_at": args.target,
            "completion": None,
            "decision": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "completed_at": None,
        }
        item.setdefault("milestones", []).append(milestone)
        item["next_gate"] = None
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "work_item_milestone_created",
            f"创建事项里程碑 {milestone_id}：{args.title}",
            work_item_id=args.id,
            milestone_id=milestone_id,
            sources=args.source,
        )
        tx.commit("work_items", event)


def command_work_item_milestone_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("work_items")
        item = find_item(data["work_items"], args.id, "事项")
        milestone = find_milestone(item, args.milestone_id)
        changes = []
        for field, value in {
            "title": args.title,
            "outcome": args.outcome,
            "completion_criteria": args.completion_criteria,
        }.items():
            if value is not None and value != milestone.get(field):
                milestone[field] = value
                changes.append(field)
        if args.status is not None:
            ensure_milestone_transition(milestone.get("status"), args.status)
            existing_current = current_milestone(item)
            if args.status == "current" and existing_current is not None and existing_current is not milestone:
                fail("一个事项最多只能有一个 current 里程碑")
            if args.status != milestone.get("status"):
                milestone["status"] = args.status
                changes.append("status")
        if args.target is not None or args.clear_target:
            value = None if args.clear_target else args.target
            if value != milestone.get("target_at"):
                milestone["target_at"] = value
                changes.append("target_at")
        if args.summary is not None or args.completion_source:
            if args.status != "completed" and milestone.get("status") != "completed":
                fail("只有 completed 里程碑才能写入完成信息")
            completion = milestone.get("completion") or {"summary": "", "sources": []}
            if args.summary is not None:
                completion["summary"] = args.summary
            completion["sources"].extend(source_objects(args.completion_source))
            milestone["completion"] = completion
            changes.append("completion")
        if args.decision is not None:
            if args.status != "completed" and milestone.get("status") != "completed":
                fail("只有 completed 里程碑才能写入完成决定")
            if args.decision != milestone.get("decision"):
                milestone["decision"] = args.decision
                changes.append("decision")
        if milestone.get("status") == "completed":
            completion = milestone.get("completion") or {}
            if not completion.get("summary") or not completion.get("sources"):
                fail("completed 里程碑必须提供 --summary 和 --completion-source")
            if not milestone.get("decision"):
                fail("completed 里程碑必须提供 --decision")
            if milestone["decision"] in {"continue", "adjust"}:
                if not args.activate_next:
                    fail("继续或调整路线事项时必须使用 --activate-next 指定下一个里程碑")
                next_milestone = find_milestone(item, args.activate_next)
                if next_milestone is milestone or next_milestone.get("status") != "planned":
                    fail("--activate-next 必须指向同一事项的 planned 里程碑")
                next_milestone["status"] = "current"
                next_milestone["updated_at"] = iso_now()
                changes.append("activate_next")
            elif milestone["decision"] == "pause":
                item["state"] = "paused"
                item["status_reason"] = milestone["completion"]["summary"]
                changes.append("work_item_paused")
            elif milestone["decision"] == "close":
                item["state"] = "closed"
                item["status_reason"] = milestone["completion"]["summary"]
                changes.append("work_item_closed")
        if not changes:
            fail("没有提供任何实际更新")
        timestamp = iso_now()
        if milestone.get("status") == "completed" and not milestone.get("completed_at"):
            milestone["completed_at"] = timestamp
            changes.append("completed_at")
        milestone["updated_at"] = timestamp
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "work_item_milestone_updated",
            f"更新事项里程碑 {args.milestone_id}：{', '.join(changes)}",
            work_item_id=args.id,
            milestone_id=args.milestone_id,
            sources=args.source,
        )
        tx.commit("work_items", event)


def command_work_item_add(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects_data = tx.data("projects")
        data = tx.data("work_items")
        if args.project_id:
            find_item(projects_data["projects"], args.project_id, "项目引用")
        if args.state in {"waiting", "needs_confirmation", "paused"} and not args.reason:
            fail("等待、待确认或暂停事项必须提供 --reason")
        timestamp = iso_now()
        item_id = generate_id(
            "WI", [item.get("id", "") for item in data["work_items"]]
        )
        item = {
            "id": item_id,
            "title": args.title,
            "project_id": args.project_id,
            "state": args.state,
            "status_reason": args.reason,
            "context": args.context,
            "next_gate": args.next_gate,
            "milestones": [],
            "sources": source_objects(args.source),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if args.stage:
            item["stage"] = args.stage
        data["work_items"].append(item)
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "work_item_created",
            f"创建事项 {item_id}：{args.title}",
            work_item_id=item_id,
            sources=args.source,
        )
        tx.commit("work_items", event)


def command_work_item_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects_data = tx.data("projects")
        data = tx.data("work_items")
        item = find_item(data["work_items"], args.id, "事项")
        if args.project_id:
            find_item(projects_data["projects"], args.project_id, "项目引用")
        changes = []
        for field, value in {
            "title": args.title,
            "state": args.state,
            "context": args.context,
            "next_gate": args.next_gate,
            "status_reason": args.reason,
        }.items():
            if value is not None and value != item.get(field):
                item[field] = value
                changes.append(field)
        if args.project_id is not None or args.clear_project:
            value = None if args.clear_project else args.project_id
            if value != item.get("project_id"):
                item["project_id"] = value
                changes.append("project_id")
        if args.stage is not None:
            if args.stage != item.get("stage"):
                item["stage"] = args.stage
                changes.append("stage")
        elif args.clear_stage and "stage" in item:
            item.pop("stage")
            changes.append("stage")
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
            changes.append("sources")
        if not changes:
            fail("没有提供任何实际更新")
        if item.get("state") in {"waiting", "needs_confirmation", "paused", "closed"} and not item.get("status_reason"):
            fail("等待、待确认、暂停或关闭事项必须提供 --reason")
        if milestone_list(item) and item.get("next_gate") is not None:
            fail("路线事项的 next_gate 由 current 里程碑派生，根字段必须为空")
        timestamp = iso_now()
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "work_item_updated",
            f"更新事项 {args.id}：{', '.join(changes)}",
            work_item_id=args.id,
            sources=args.source,
        )
        tx.commit("work_items", event)
