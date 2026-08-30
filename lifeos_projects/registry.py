"""Merge Work tracking overlays with the dynamic Project Catalog."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from lifeos_config.core import ConfigError

from .catalog import CatalogFinding, ProjectCatalog, discover_projects


STORED_PROJECT_FIELDS = {
    "id",
    "project_key",
    "tracking_state",
    "status_reason",
    "created_at",
    "updated_at",
}
LEGACY_STORED_PROJECT_FIELDS = {*STORED_PROJECT_FIELDS, "manifest_path"}


def _catalog_for_work() -> ProjectCatalog:
    """Keep core Work readable when project discovery configuration is broken."""

    try:
        return discover_projects()
    except ConfigError as exc:
        return ProjectCatalog(
            roots=(),
            projects=(),
            findings=(CatalogFinding("config_error", str(exc)),),
            complete=False,
        )


def hydrate_project_record(
    record: Dict[str, Any],
    catalog: ProjectCatalog | None = None,
) -> Dict[str, Any]:
    resolved_catalog = catalog or _catalog_for_work()
    project = resolved_catalog.by_key.get(str(record.get("project_key") or ""))
    stored = {key: record.get(key) for key in STORED_PROJECT_FIELDS}
    if project is None:
        return {
            **stored,
            "name": record.get("project_key"),
            "aliases": [],
            "scope": None,
            "sources": {},
            "fact_source": None,
            "availability": "missing",
        }
    return {
        **stored,
        "name": project.name,
        "aliases": list(project.aliases),
        "scope": project.scope,
        "sources": project.sources,
        "fact_source": {
            "kind": "local_workspace",
            "location": project.root,
        },
        "availability": "available",
    }


def hydrate_projects_data(
    projects_data: Dict[str, Any],
    catalog: ProjectCatalog | None = None,
) -> Dict[str, Any]:
    resolved_catalog = catalog or _catalog_for_work()
    result = deepcopy(projects_data)
    result["projects"] = [
        hydrate_project_record(item, resolved_catalog)
        for item in projects_data.get("projects", [])
    ]
    return result


def compact_projects_data(projects_data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": projects_data.get("schema_version"),
        "updated_at": projects_data.get("updated_at"),
        "projects": [
            {key: item.get(key) for key in STORED_PROJECT_FIELDS}
            for item in projects_data.get("projects", [])
        ],
    }


def project_registry_errors(projects_data: Dict[str, Any]) -> list[str]:
    errors: list[str] = []
    projects = projects_data.get("projects", [])
    keys = [item.get("project_key") for item in projects if isinstance(item, dict)]
    if any(not key for key in keys):
        errors.append("项目引用存在空 project_key")
    if len(keys) != len(set(keys)):
        errors.append("project_key 在当前 Work 跟踪中重复")
    return errors


def project_linkage_findings(
    projects_data: Dict[str, Any],
    catalog: ProjectCatalog | None = None,
) -> list[str]:
    resolved_catalog = catalog or _catalog_for_work()
    known = set(resolved_catalog.by_key)
    return [
        f"{item.get('id')} 跟踪的项目当前不可用：{item.get('project_key')}"
        for item in projects_data.get("projects", [])
        if item.get("project_key") not in known
    ]


def project_map_payload(_data_dir: Path | None = None) -> Dict[str, Any]:
    catalog = discover_projects()
    return {
        "schema_version": 1,
        "projects": [
            {
                "key": item.project_key,
                "title": item.name,
                "roots": [item.root],
            }
            for item in catalog.projects
        ],
        "catalog": {
            "complete": catalog.complete,
            "findings": [item.to_dict() for item in catalog.findings],
        },
    }


def dchat_project_rows(_data_dir: Path | None = None) -> list[Dict[str, Any]]:
    by_vid: Dict[str, list[str]] = {}
    for project in discover_projects().projects:
        for chat in project.sources.get("dchat", {}).get("groups", []):
            by_vid.setdefault(chat["vid"], []).append(project.project_key)
    return [
        {"conversation_id": vid, "projects": sorted(set(keys))}
        for vid, keys in sorted(by_vid.items())
    ]


__all__ = [
    "LEGACY_STORED_PROJECT_FIELDS",
    "STORED_PROJECT_FIELDS",
    "compact_projects_data",
    "dchat_project_rows",
    "hydrate_project_record",
    "hydrate_projects_data",
    "project_linkage_findings",
    "project_map_payload",
    "project_registry_errors",
]
