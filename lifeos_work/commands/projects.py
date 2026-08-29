"""Project-reference command handlers."""

import json

from lifeos_projects.manifest import ProjectManifestError, load_manifest, resolve_manifest_path
from lifeos_projects.registry import hydrate_project_record

from ..errors import fail
from ..model import (
    find_item,
    generate_id,
    iso_now,
    make_event,
    project_name_owners,
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


def command_project_add(args):
    manifest_path = resolve_manifest_path(args.manifest)
    try:
        manifest = load_manifest(manifest_path)
    except ProjectManifestError as exc:
        fail(str(exc))
    with transaction(args) as tx:
        if tx.idempotent_result():
            return
        projects_data = tx.data("projects")
        owners = project_name_owners(projects_data["projects"])
        collisions = [
            value
            for value in [manifest["name"], *manifest["aliases"]]
            if value.casefold() in owners
        ]
        if collisions:
            fail(f"项目名称或别名已存在：{', '.join(collisions)}")
        if args.tracking_state in {"paused", "archived"} and not args.reason:
            fail("暂停或归档项目引用必须提供 --reason")
        timestamp = iso_now()
        project_id = generate_id(
            "PRJ", [item.get("id", "") for item in projects_data["projects"]]
        )
        projects_data["projects"].append(
            hydrate_project_record({
                "id": project_id,
                "project_key": manifest["project_key"],
                "manifest_path": str(manifest_path),
                "tracking_state": args.tracking_state,
                "status_reason": args.reason,
                "created_at": timestamp,
                "updated_at": timestamp,
            })
        )
        projects_data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "project_registered",
            f"注册项目引用 {project_id}：{manifest['name']}",
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
        changes = []
        if args.manifest is not None:
            manifest_path = resolve_manifest_path(args.manifest)
            try:
                manifest = load_manifest(manifest_path)
            except ProjectManifestError as exc:
                fail(str(exc))
            owners = project_name_owners(projects_data["projects"], args.id)
            collisions = [
                value
                for value in [manifest["name"], *manifest["aliases"]]
                if value.casefold() in owners
            ]
            if collisions:
                fail(f"项目名称或别名已存在：{', '.join(collisions)}")
            if manifest["project_key"] != project.get("project_key"):
                fail("更新 manifest 路径时 project_key 必须保持不变")
            if str(manifest_path) != project.get("manifest_path"):
                project["manifest_path"] = str(manifest_path)
                changes.append("manifest_path")
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
        refreshed = hydrate_project_record(project)
        project.clear()
        project.update(refreshed)
        projects_data["updated_at"] = timestamp
        event = make_event(
            tx.events,
            args,
            "project_updated",
            f"更新项目引用 {args.id}：{', '.join(changes)}",
            project_id=args.id,
            sources=args.source,
        )
        tx.commit("projects", event)
