"""Achievement-capsule command handlers."""

import json

from ..errors import fail
from ..model import (
    achievement_matches_query,
    achievement_project_ids,
    find_item,
    generate_id,
    iso_now,
    make_event,
    parse_achievement_evidence_sources,
    parse_achievement_task_links,
    source_objects,
)
from ..runtime import (
    read_current_data,
    transaction,
)
from ..views import render_achievements


def command_achievements(args):
    (
        projects_data,
        work_items_data,
        tasks_data,
        _glossary,
        _ideas,
        achievements_data,
    ) = read_current_data()
    achievements = achievements_data["achievements"]
    if args.lifecycle:
        achievements = [
            item
            for item in achievements
            if item.get("lifecycle") == args.lifecycle
        ]
    if args.task:
        achievements = [
            item
            for item in achievements
            if any(link.get("task_id") == args.task for link in item["task_links"])
        ]
    if args.query:
        achievements = [
            item
            for item in achievements
            if achievement_matches_query(item, args.query)
        ]
    if args.project:
        projects_by_id = {
            item.get("id"): item for item in projects_data["projects"]
        }
        tasks_by_id = {item.get("id"): item for item in tasks_data["tasks"]}
        work_items_by_id = {
            item.get("id"): item for item in work_items_data["work_items"]
        }
        needle = args.project.casefold()
        achievements = [
            item
            for item in achievements
            if any(
                needle
                in f"{project_id} {(projects_by_id.get(project_id) or {}).get('name', '')}".casefold()
                for project_id in achievement_project_ids(
                    item, tasks_by_id, work_items_by_id
                )
            )
        ]
    selected = sorted(
        achievements, key=lambda item: item.get("created_at", ""), reverse=True
    )
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        print(
            render_achievements(
                {
                    "updated_at": achievements_data.get("updated_at"),
                    "achievements": selected,
                },
                include_non_current=True,
            ),
            end="",
        )


def command_achievement_add(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        tasks_data = tx.data("tasks")
        data = tx.data("achievements")
        timestamp = iso_now()
        links = parse_achievement_task_links(
            args.task_link, tasks_data["tasks"], timestamp
        )
        if not links or not any(link["relation"] == "origin" for link in links):
            fail("成果胶囊至少需要一个 relation=origin 的已完成待办")
        item_id = generate_id(
            "ACH", [item.get("id", "") for item in data["achievements"]]
        )
        item = {
            "id": item_id,
            "title": args.title,
            "task_links": links,
            "context": args.context,
            "outcome": args.outcome,
            "key_learnings": list(dict.fromkeys(args.learning)),
            "reuse": args.reuse,
            "lifecycle": "current",
            "status_reason": None,
            "superseded_by": None,
            "sources": [
                *source_objects(args.source),
                *parse_achievement_evidence_sources(args.source_ref),
            ],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        data["achievements"].append(item)
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "achievement_created",
            f"创建成果胶囊 {item_id}：{args.title}",
            achievement_id=item_id,
            sources=args.source,
        )
        tx.commit("achievements", event)


def command_achievement_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        tasks_data = tx.data("tasks")
        data = tx.data("achievements")
        item = find_item(data["achievements"], args.id, "成果胶囊")
        if item.get("lifecycle") != "current":
            fail("只有 current 成果胶囊可以更新正文或贡献")
        changes = []
        for field, value in {
            "title": args.title,
            "context": args.context,
            "outcome": args.outcome,
            "reuse": args.reuse,
        }.items():
            if value is not None and value != item.get(field):
                item[field] = value
                changes.append(field)
        for learning in args.learning:
            if learning not in item.setdefault("key_learnings", []):
                item["key_learnings"].append(learning)
                changes.append("key_learnings")
        for source in parse_achievement_evidence_sources(args.source_ref):
            if source not in item.setdefault("sources", []):
                item["sources"].append(source)
                changes.append("sources")
        timestamp = iso_now()
        new_links = parse_achievement_task_links(
            args.task_link, tasks_data["tasks"], timestamp
        )
        links_by_task = {
            link.get("task_id"): link for link in item.setdefault("task_links", [])
        }
        for link in new_links:
            existing = links_by_task.get(link["task_id"])
            if existing:
                if (
                    existing.get("relation") != link["relation"]
                    or existing.get("contribution") != link["contribution"]
                ):
                    existing.update(link)
                    changes.append("task_links")
            else:
                item["task_links"].append(link)
                links_by_task[link["task_id"]] = link
                changes.append("task_links")
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
            changes.append("sources")
        if not changes:
            fail("没有提供任何实际更新")
        if not any(
            link.get("relation") == "origin" for link in item["task_links"]
        ):
            fail("成果胶囊至少需要一个 relation=origin 的已完成待办")
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "achievement_updated",
            f"更新成果胶囊 {args.id}：{', '.join(dict.fromkeys(changes))}",
            achievement_id=args.id,
            sources=args.source,
        )
        tx.commit("achievements", event)


def command_achievement_archive(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("achievements")
        item = find_item(data["achievements"], args.id, "成果胶囊")
        if item.get("lifecycle") != "current":
            fail("只有 current 成果胶囊可以归档")
        timestamp = iso_now()
        item["lifecycle"] = "archived"
        item["status_reason"] = args.reason
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
        event = make_event(
            tx.events,
            args,
            "achievement_archived",
            f"归档成果胶囊 {args.id}：{item['title']}",
            achievement_id=args.id,
            sources=args.source,
        )
        tx.commit("achievements", event)


def command_achievement_supersede(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        data = tx.data("achievements")
        item = find_item(data["achievements"], args.id, "成果胶囊")
        replacement = find_item(data["achievements"], args.by, "替代成果胶囊")
        if item["id"] == replacement["id"]:
            fail("成果胶囊不能替代自身")
        if item.get("lifecycle") != "current":
            fail("只有 current 成果胶囊可以被替代")
        if replacement.get("lifecycle") != "current":
            fail("替代目标必须是 current 成果胶囊")
        timestamp = iso_now()
        item["lifecycle"] = "superseded"
        item["status_reason"] = args.reason
        item["superseded_by"] = replacement["id"]
        item["updated_at"] = timestamp
        data["updated_at"] = timestamp
        if args.source:
            item.setdefault("sources", []).extend(source_objects(args.source))
        event = make_event(
            tx.events,
            args,
            "achievement_superseded",
            f"替代成果胶囊 {args.id} -> {replacement['id']}",
            achievement_id=args.id,
            sources=args.source,
        )
        tx.commit("achievements", event)
