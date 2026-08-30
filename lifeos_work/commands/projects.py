"""Project-reference command handlers."""

import json

from lifeos_projects.catalog import discover_projects
from lifeos_projects.registry import hydrate_project_record

from ..errors import fail
from ..model import (
    find_item,
    generate_id,
    iso_now,
    make_event,
)
from ..runtime import (
    read_current_data,
    transaction,
)
from ..views import render_projects


def command_projects(args):
    (
        projects_data,
        _work_items,
        _tasks,
        _glossary,
        _ideas,
        _achievements,
    ) = read_current_data()
    projects = projects_data["projects"]
    if args.tracking_state:
        projects = [
            item
            for item in projects
            if item.get("tracking_state") == args.tracking_state
        ]
    if args.json:
        print(json.dumps(projects, ensure_ascii=False, indent=2))
    else:
        print(
            render_projects(
                {"updated_at": projects_data.get("updated_at"), "projects": projects}
            ),
            end="",
        )


def command_project_track(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        catalog = discover_projects()
        project_manifest = catalog.by_key.get(args.project_key)
        if project_manifest is None:
            related = [
                item.message for item in catalog.findings
                if item.project_key == args.project_key
            ]
            detail = f"：{'；'.join(related)}" if related else ""
            fail(f"Project Catalog 中没有唯一有效项目：{args.project_key}{detail}")
        projects_data = tx.data("projects")
        if any(
            item.get("project_key") == args.project_key
            for item in projects_data["projects"]
        ):
            fail(f"项目已在 Work 中跟踪：{args.project_key}")
        if args.tracking_state in {"paused", "archived"} and not args.reason:
            fail("暂停或归档项目引用必须提供 --reason")
        timestamp = iso_now()
        project_id = generate_id(
            "PRJ", [item.get("id", "") for item in projects_data["projects"]]
        )
        projects_data["projects"].append(
            hydrate_project_record({
                "id": project_id,
                "project_key": args.project_key,
                "tracking_state": args.tracking_state,
                "status_reason": args.reason,
                "created_at": timestamp,
                "updated_at": timestamp,
            }, catalog)
        )
        projects_data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "project_registered",
            f"跟踪项目 {project_id}：{project_manifest.name}",
            project_id=project_id,
            sources=args.source,
        )
        tx.commit("projects", event)


def command_project_update(args):
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects_data = tx.data("projects")
        project = find_item(projects_data["projects"], args.id, "项目引用")
        project_id = project["id"]
        changes = []
        for field, value in {"tracking_state": args.tracking_state}.items():
            if value is not None and value != project.get(field):
                project[field] = value
                changes.append(field)
        if args.reason is not None and args.reason != project.get("status_reason"):
            project["status_reason"] = args.reason
            changes.append("status_reason")
        if project.get("tracking_state") in {"paused", "archived"} and not project.get("status_reason"):
            fail("暂停或归档项目引用必须提供 --reason")
        if not changes:
            fail("没有提供任何实际更新")
        timestamp = iso_now()
        project["updated_at"] = timestamp
        projects_data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "project_updated",
            f"更新项目引用 {project_id}：{', '.join(changes)}",
            project_id=project_id,
            sources=args.source,
        )
        tx.commit("projects", event)
