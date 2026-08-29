"""Shared project-manifest contract for LifeOS domains."""

from .manifest import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    ProjectManifestError,
    load_manifest,
    resolve_manifest_path,
)
from .registry import (
    compact_projects_data,
    dchat_project_rows,
    hydrate_projects_data,
    project_map_payload,
    project_registry_errors,
)

__all__ = [
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "ProjectManifestError",
    "compact_projects_data",
    "dchat_project_rows",
    "hydrate_projects_data",
    "load_manifest",
    "project_map_payload",
    "project_registry_errors",
    "resolve_manifest_path",
]
