"""Resolve lightweight Runtime registrations through project manifests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from .manifest import ProjectManifestError, load_manifest, resolve_manifest_path


STORED_PROJECT_FIELDS = {
    "id",
    "project_key",
    "manifest_path",
    "tracking_state",
    "status_reason",
    "created_at",
    "updated_at",
}


def hydrate_project_record(record: Dict[str, Any]) -> Dict[str, Any]:
    manifest_path = resolve_manifest_path(record.get("manifest_path") or "")
    manifest = load_manifest(manifest_path)
    if record.get("project_key") != manifest["project_key"]:
        raise ProjectManifestError(
            f"{record.get('id')} project_key 与项目清单不一致："
            f"{record.get('project_key')} != {manifest['project_key']}"
        )
    return {
        **{key: record.get(key) for key in STORED_PROJECT_FIELDS},
        "name": manifest["name"],
        "aliases": manifest["aliases"],
        "scope": manifest["scope"],
        "sources": manifest["sources"],
        "fact_source": {
            "kind": "local_workspace",
            "location": str(manifest_path.parent),
        },
    }


def hydrate_projects_data(projects_data: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(projects_data)
    result["projects"] = [
        hydrate_project_record(item) for item in projects_data.get("projects", [])
    ]
    return result


def compact_projects_data(projects_data: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "schema_version": projects_data.get("schema_version"),
        "updated_at": projects_data.get("updated_at"),
        "projects": [],
    }
    for item in projects_data.get("projects", []):
        result["projects"].append(
            {key: item.get(key) for key in STORED_PROJECT_FIELDS}
        )
    return result


def project_registry_errors(projects_data: Dict[str, Any]) -> list[str]:
    errors: list[str] = []
    manifests: list[Dict[str, Any]] = []
    paths: list[str] = []
    for item in projects_data.get("projects", []):
        try:
            hydrated = hydrate_project_record(item)
        except ProjectManifestError as exc:
            errors.append(str(exc))
            continue
        manifests.append(hydrated)
        paths.append(str(resolve_manifest_path(item.get("manifest_path") or "")))
    keys = [item.get("project_key") for item in manifests]
    if len(keys) != len(set(keys)):
        errors.append("项目清单 project_key 在当前 LifeOS 注册中重复")
    if len(paths) != len(set(paths)):
        errors.append("同一 lifeos-project.json 不得注册为多个项目引用")
    names: Dict[str, str] = {}
    for item in manifests:
        for value in [item.get("name"), *(item.get("aliases") or [])]:
            folded = str(value).casefold()
            owner = names.get(folded)
            if owner and owner != item.get("id"):
                errors.append(f"项目名称或别名重复：{value}")
            names[folded] = str(item.get("id"))
    return errors


def _read_registered_projects(data_dir: Path) -> Dict[str, Any]:
    path = Path(data_dir) / "projects.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectManifestError(f"LifeOS 项目注册不可读：{path}（{exc}）") from exc
    if payload.get("schema_version") != 1:
        raise ProjectManifestError("LifeOS 项目注册 schema_version 必须为 1")
    errors = project_registry_errors(payload)
    if errors:
        raise ProjectManifestError("；".join(errors))
    return hydrate_projects_data(payload)


def project_map_payload(data_dir: Path) -> Dict[str, Any]:
    projects = _read_registered_projects(Path(data_dir)).get("projects", [])
    return {
        "schema_version": 1,
        "projects": [
            {
                "key": item["project_key"],
                "title": item["name"],
                "roots": [item["fact_source"]["location"]],
            }
            for item in projects
        ],
    }


def dchat_project_rows(data_dir: Path) -> list[Dict[str, Any]]:
    projects = _read_registered_projects(Path(data_dir)).get("projects", [])
    by_vid: Dict[str, list[str]] = {}
    for project in projects:
        for chat in project.get("sources", {}).get("dchat", {}).get("groups", []):
            by_vid.setdefault(chat["vid"], []).append(project["project_key"])
    return [
        {"conversation_id": vid, "projects": sorted(set(keys))}
        for vid, keys in sorted(by_vid.items())
    ]


__all__ = [
    "STORED_PROJECT_FIELDS",
    "compact_projects_data",
    "dchat_project_rows",
    "hydrate_project_record",
    "hydrate_projects_data",
    "project_map_payload",
    "project_registry_errors",
]
