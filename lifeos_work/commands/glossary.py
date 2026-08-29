"""Glossary query and mutation handlers for the LifeOS Work domain."""

import json

from ..config import ENTITY_KIND_LABELS
from ..errors import fail
from ..model import (
    all_target_ids,
    find_item,
    generate_id,
    glossary_matches,
    iso_now,
    make_event,
    now,
    source_objects,
)
from ..runtime import (
    read_current_data,
    transaction,
)


def command_glossary(args):
    _projects, _work_items, _tasks, data, _ideas, _achievements = read_current_data()
    terms = data["terms"]
    if args.kind:
        terms = [term for term in terms if term.get("kind") == args.kind]
    terms = [term for term in terms if glossary_matches(term, args.query)]
    if args.json:
        print(json.dumps(terms, ensure_ascii=False, indent=2))
        return
    if not terms:
        print("没有找到匹配的名词。")
        return
    for index, term in enumerate(terms):
        if index:
            print("---")
        aliases = "、".join(term.get("aliases", [])) or "无"
        related = "、".join(term.get("related_items", [])) or "无"
        sources = "；".join(
            value.get("location", "未记录")
            for value in term.get("sources", [])
        ) or "未记录"
        print(
            f"{term['id']} · {term['name']} · "
            f"{ENTITY_KIND_LABELS.get(term.get('kind'), term.get('kind'))}\n"
            f"  {term.get('description', '未记录')}\n"
            f"  别名：{aliases}\n"
            f"  关联事项：{related}\n"
            f"  来源：{sources}"
        )

def command_term_add(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects = tx.data("projects")
        work_items = tx.data("work_items")
        tasks = tx.data("tasks")
        data = tx.data("glossary")
        events = tx.events
        owners = {
            value.casefold()
            for term in data["terms"]
            for value in [term.get("name", ""), *term.get("aliases", [])]
            if value
        }
        collisions = [
            value for value in [args.name, *args.alias] if value.casefold() in owners
        ]
        if collisions:
            fail(f"名称或别名已存在：{', '.join(collisions)}")
        known = all_target_ids(projects, work_items, tasks)
        for related in args.related_item:
            if related not in known:
                fail(f"找不到关联事项：{related}")
        item_id = generate_id(
            "ENT", [item.get("id", "") for item in data["terms"]]
        )
        data["terms"].append(
            {
                "id": item_id,
                "name": args.name,
                "kind": args.kind,
                "aliases": list(dict.fromkeys(args.alias)),
                "description": args.description,
                "related_items": list(dict.fromkeys(args.related_item)),
                "sources": source_objects(args.source),
                "confirmed_at": args.confirmed_at or now().date().isoformat(),
            }
        )
        data["updated_at"] = iso_now()
        event = make_event(
            events,
            args,
            "glossary_term_created",
            f"创建实体名词 {item_id}：{args.name}",
            sources=args.source,
        )
        tx.commit("glossary", event)
def command_term_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects = tx.data("projects")
        work_items = tx.data("work_items")
        tasks = tx.data("tasks")
        data = tx.data("glossary")
        events = tx.events
        term = find_item(data["terms"], args.id, "实体名词")
        known = all_target_ids(projects, work_items, tasks)
        for related in args.related_item:
            if related not in known:
                fail(f"找不到关联事项：{related}")
        owners = {
            value.casefold(): item.get("id")
            for item in data["terms"]
            for value in [item.get("name", ""), *item.get("aliases", [])]
            if value
        }
        collisions = [
            value
            for value in [candidate for candidate in [args.name, *args.alias] if candidate]
            if owners.get(value.casefold()) not in {None, args.id}
        ]
        if collisions:
            fail(f"名称或别名已存在：{', '.join(collisions)}")
        changes = []
        for field, value in {
            "name": args.name,
            "kind": args.kind,
            "description": args.description,
            "confirmed_at": args.confirmed_at,
        }.items():
            if value is not None and value != term.get(field):
                term[field] = value
                changes.append(field)
        for field, values in {
            "aliases": args.alias,
            "related_items": args.related_item,
        }.items():
            for value in values:
                if value not in term.setdefault(field, []):
                    term[field].append(value)
                    changes.append(field)
        for value in source_objects(args.source):
            if value not in term.setdefault("sources", []):
                term["sources"].append(value)
                changes.append("sources")
        if not changes:
            fail("没有提供任何实际更新")
        timestamp = iso_now()
        data["updated_at"] = timestamp
        updated_task_refs = 0
        if "name" in changes:
            for task in tasks["tasks"]:
                party = task.get("responsible_party") or {}
                if party.get("entity_id") != args.id:
                    continue
                if party.get("name") != term["name"]:
                    party["name"] = term["name"]
                    task["updated_at"] = timestamp
                    updated_task_refs += 1
            if updated_task_refs:
                tasks["updated_at"] = timestamp
        event = make_event(
            events,
            args,
            "glossary_term_updated",
            f"更新实体名词 {args.id}：{', '.join(dict.fromkeys(changes))}；"
            f"同步 {updated_task_refs} 条责任方引用",
            sources=args.source,
        )
        tx.commit(("tasks", "glossary") if updated_task_refs else "glossary", event)
