"""Built-in project source adapters for ``lifeos-project.json``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .validation import ProjectManifestError, object_value, text


@dataclass(frozen=True)
class ProjectSourceAdapter:
    name: str
    normalize: Callable[[Any], dict[str, Any]]


def _normalize_dchat(value: Any) -> dict[str, Any]:
    payload = object_value(
        value,
        "sources.dchat",
        allowed={"groups"},
        required={"groups"},
    )
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise ProjectManifestError("sources.dchat.groups 必须为数组")
    result = []
    for index, raw in enumerate(groups):
        item = object_value(
            raw,
            f"sources.dchat.groups[{index}]",
            allowed={"vid", "name", "description"},
            required={"vid", "name", "description"},
        )
        vid = text(item.get("vid"), "sources.dchat.groups[].vid")
        if not vid.isdigit():
            raise ProjectManifestError("sources.dchat.groups[].vid 必须是数字字符串")
        result.append(
            {
                "vid": vid,
                "name": text(item.get("name"), "sources.dchat.groups[].name"),
                "description": text(
                    item.get("description"), "sources.dchat.groups[].description"
                ),
            }
        )
    vids = [item["vid"] for item in result]
    if len(vids) != len(set(vids)):
        raise ProjectManifestError("sources.dchat.groups[].vid 不得重复")
    return {"groups": result}


def _normalize_cooper(value: Any) -> dict[str, Any]:
    payload = object_value(
        value,
        "sources.cooper",
        allowed={"resources"},
        required={"resources"},
    )
    resources = payload.get("resources")
    if not isinstance(resources, list):
        raise ProjectManifestError("sources.cooper.resources 必须为数组")
    result = []
    for index, raw in enumerate(resources):
        item = object_value(
            raw,
            f"sources.cooper.resources[{index}]",
            allowed={"link", "name", "description"},
            required={"link", "name", "description"},
        )
        link = text(item.get("link"), "sources.cooper.resources[].link")
        parsed = urlsplit(link)
        host = (parsed.hostname or "").casefold()
        is_internal_host = (
            host == "didichuxing.com" or host.endswith(".didichuxing.com")
        )
        if (
            parsed.scheme not in {"http", "https"}
            or not is_internal_host
            or "cooper" not in host
        ):
            raise ProjectManifestError(
                "sources.cooper.resources[].link 必须是 Cooper HTTP(S) 链接"
            )
        result.append(
            {
                "link": link,
                "name": text(item.get("name"), "sources.cooper.resources[].name"),
                "description": text(
                    item.get("description"),
                    "sources.cooper.resources[].description",
                ),
            }
        )
    links = [item["link"] for item in result]
    if len(links) != len(set(links)):
        raise ProjectManifestError("sources.cooper.resources[].link 不得重复")
    return {"resources": result}


PROJECT_SOURCE_ADAPTERS = (
    ProjectSourceAdapter("dchat", _normalize_dchat),
    ProjectSourceAdapter("cooper", _normalize_cooper),
)
PROJECT_SOURCE_NAMES = tuple(adapter.name for adapter in PROJECT_SOURCE_ADAPTERS)
_BY_NAME = {adapter.name: adapter for adapter in PROJECT_SOURCE_ADAPTERS}


def normalize_sources(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectManifestError("sources 必须为对象")
    unknown = sorted(set(value) - set(PROJECT_SOURCE_NAMES))
    if unknown:
        raise ProjectManifestError(f"sources 包含未知 Adapter：{', '.join(unknown)}")
    return {
        name: _BY_NAME[name].normalize(payload)
        for name, payload in value.items()
    }


__all__ = [
    "PROJECT_SOURCE_ADAPTERS",
    "PROJECT_SOURCE_NAMES",
    "ProjectSourceAdapter",
    "normalize_sources",
]
