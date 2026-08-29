"""Small structural validators shared by project manifest owners."""

from __future__ import annotations

from typing import Any, Mapping


class ProjectManifestError(ValueError):
    """Raised when a project manifest cannot be consumed safely."""


def text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectManifestError(f"{label} 必须是非空字符串")
    return value.strip()


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ProjectManifestError(f"{label} 必须是字符串数组")
    result = [text(item, label) for item in value]
    if len(result) != len(set(result)):
        raise ProjectManifestError(f"{label} 不得包含重复值")
    return result


def object_value(
    value: Any,
    label: str,
    *,
    allowed: set[str],
    required: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectManifestError(f"{label} 必须为对象")
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ProjectManifestError(f"{label} 包含未知字段：{', '.join(unknown)}")
    if missing:
        raise ProjectManifestError(f"{label} 缺少字段：{', '.join(missing)}")
    return value


__all__ = ["ProjectManifestError", "object_value", "string_list", "text"]
