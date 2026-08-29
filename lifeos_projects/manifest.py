"""The project-owned ``lifeos-project.json`` core contract."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .sources import normalize_sources
from .validation import ProjectManifestError, object_value, string_list, text

MANIFEST_NAME = "lifeos-project.json"
SCHEMA_VERSION = 1
SCOPES = {"project", "project-group"}
TOP_LEVEL_FIELDS = {
    "schema_version",
    "project_key",
    "name",
    "aliases",
    "scope",
    "sources",
}
PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def resolve_manifest_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.name != MANIFEST_NAME or path.is_dir():
        path = path / MANIFEST_NAME
    return Path(os.path.abspath(path))


def normalize_manifest(payload: Any) -> dict[str, Any]:
    root = object_value(
        payload,
        MANIFEST_NAME,
        allowed=TOP_LEVEL_FIELDS,
        required=TOP_LEVEL_FIELDS,
    )
    schema_version = root.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ProjectManifestError(f"schema_version 必须为 {SCHEMA_VERSION}")
    project_key = text(root.get("project_key"), "project_key")
    if not PROJECT_KEY_PATTERN.fullmatch(project_key):
        raise ProjectManifestError("project_key 必须使用小写 kebab-case")
    name = text(root.get("name"), "name")
    aliases = string_list(root.get("aliases"), "aliases")
    if name in aliases:
        raise ProjectManifestError("aliases 不得重复项目 name")
    scope = text(root.get("scope"), "scope")
    if scope not in SCOPES:
        raise ProjectManifestError(
            f"scope 必须为以下值之一：{', '.join(sorted(SCOPES))}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "project_key": project_key,
        "name": name,
        "aliases": aliases,
        "scope": scope,
        "sources": normalize_sources(root.get("sources")),
    }


def load_manifest(value: str | Path) -> dict[str, Any]:
    path = resolve_manifest_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectManifestError(f"缺少项目清单：{path}") from exc
    except OSError as exc:
        raise ProjectManifestError(f"项目清单不可读：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ProjectManifestError(f"项目清单 JSON 无法解析：{path}（{exc}）") from exc
    return normalize_manifest(payload)


__all__ = [
    "MANIFEST_NAME",
    "SCHEMA_VERSION",
    "ProjectManifestError",
    "load_manifest",
    "normalize_manifest",
    "resolve_manifest_path",
]
