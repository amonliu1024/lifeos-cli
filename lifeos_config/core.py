"""Load and validate the Git-external LifeOS configuration."""

from __future__ import annotations

import json
import os
import tempfile
import contextlib
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lifeos_sessions.adapters import SESSION_SOURCE_NAMES


CONFIG_SCHEMA_VERSION = 1
SUPPORTED_SESSION_SOURCES = SESSION_SOURCE_NAMES
SUPPORTED_PROJECT_SOURCES = ("dchat", "cooper")
DEFAULT_PROJECT_EXCLUDES = (".git", ".venv", "archive", "node_modules")


class ConfigError(ValueError):
    """Raised when the private LifeOS configuration is invalid."""


@dataclass(frozen=True)
class DChatConfig:
    enabled: bool
    dws_wrapper: Optional[str]


@dataclass(frozen=True)
class LifeOSConfig:
    path: Path
    exists: bool
    timezone: str
    dchat: DChatConfig
    session_sources: tuple[str, ...]
    project_sources: tuple[str, ...]
    project_roots: tuple[str, ...]
    project_excludes: tuple[str, ...]


def resolve_config_path(value: Optional[str | os.PathLike[str]] = None) -> Path:
    if value is not None:
        return Path(value).expanduser().absolute()
    override = os.environ.get("LIFEOS_CONFIG")
    if override:
        return Path(override).expanduser().absolute()
    return (Path.home() / ".config" / "lifeos" / "config.json").absolute()


def default_payload() -> dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "timezone": "Asia/Shanghai",
        "modules": {
            "dchat": {
                "enabled": False,
                "dws_wrapper": None,
            },
            "sessions": {"sources": list(SUPPORTED_SESSION_SOURCES)},
            "project_sources": {"enabled": ["dchat", "cooper"]},
            "projects": {
                "roots": [],
                "exclude": list(DEFAULT_PROJECT_EXCLUDES),
            },
        },
    }


def _object(value: Any, label: str, allowed: set[str], required: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} 必须是对象")
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ConfigError(f"{label} 包含未知字段：{', '.join(unknown)}")
    if missing:
        raise ConfigError(f"{label} 缺少字段：{', '.join(missing)}")
    return value


def _optional_text(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} 必须是非空字符串或 null")
    return value.strip()


def _names(value: Any, label: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{label} 必须是字符串数组")
    if any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{label} 必须是字符串数组")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ConfigError(f"{label} 包含未知值：{', '.join(unknown)}")
    if len(value) != len(set(value)):
        raise ConfigError(f"{label} 不得包含重复值")
    return tuple(value)


def _project_roots(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError("modules.projects.roots 必须是字符串数组")
    roots = []
    for item in value:
        path = Path(item).expanduser()
        if not path.is_absolute():
            raise ConfigError("modules.projects.roots 必须全部使用绝对路径")
        normalized = str(path.absolute())
        if normalized not in roots:
            roots.append(normalized)
    if len(roots) != len(value):
        raise ConfigError("modules.projects.roots 不得包含重复值")
    return tuple(roots)


def _project_excludes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError("modules.projects.exclude 必须是字符串数组")
    result = []
    for item in value:
        name = item.strip()
        if not name or Path(name).is_absolute() or "/" in name or name in {".", ".."}:
            raise ConfigError("modules.projects.exclude 只能包含相对目录名")
        if name in result:
            raise ConfigError("modules.projects.exclude 不得包含重复值")
        result.append(name)
    return tuple(result)


def normalize_config(payload: Any, path: Path, *, exists: bool) -> LifeOSConfig:
    root = _object(
        payload,
        "config",
        {"schema_version", "timezone", "modules"},
        {"schema_version", "timezone", "modules"},
    )
    if root.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError(f"schema_version 必须为 {CONFIG_SCHEMA_VERSION}")
    timezone = root.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ConfigError("timezone 必须是非空字符串")
    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone 不可用：{timezone}") from exc

    modules = _object(
        root.get("modules"),
        "modules",
        {"dchat", "sessions", "project_sources", "projects"},
        {"dchat", "sessions", "project_sources"},
    )
    dchat = _object(
        modules.get("dchat"),
        "modules.dchat",
        {"enabled", "dws_wrapper"},
        {"enabled", "dws_wrapper"},
    )
    enabled = dchat.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError("modules.dchat.enabled 必须是布尔值")
    wrapper = _optional_text(dchat.get("dws_wrapper"), "modules.dchat.dws_wrapper")
    if enabled and wrapper is None:
        raise ConfigError("启用 dchat 时必须配置 dws_wrapper")

    sessions = _object(
        modules.get("sessions"),
        "modules.sessions",
        {"sources"},
        {"sources"},
    )
    project_sources = _object(
        modules.get("project_sources"),
        "modules.project_sources",
        {"enabled"},
        {"enabled"},
    )
    projects_value = modules.get("projects") or {
        "roots": [],
        "exclude": list(DEFAULT_PROJECT_EXCLUDES),
    }
    projects = _object(
        projects_value,
        "modules.projects",
        {"roots", "exclude"},
        {"roots", "exclude"},
    )
    return LifeOSConfig(
        path=path,
        exists=exists,
        timezone=timezone,
        dchat=DChatConfig(enabled, wrapper),
        session_sources=_names(
            sessions.get("sources"),
            "modules.sessions.sources",
            SUPPORTED_SESSION_SOURCES,
        ),
        project_sources=_names(
            project_sources.get("enabled"),
            "modules.project_sources.enabled",
            SUPPORTED_PROJECT_SOURCES,
        ),
        project_roots=_project_roots(projects.get("roots")),
        project_excludes=_project_excludes(projects.get("exclude")),
    )


def load_config(
    value: Optional[str | os.PathLike[str]] = None,
    *,
    allow_missing: bool = True,
) -> LifeOSConfig:
    path = resolve_config_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not allow_missing:
            raise ConfigError(f"配置文件不存在：{path}")
        return normalize_config(default_payload(), path, exists=False)
    except OSError as exc:
        raise ConfigError(f"配置文件不可读：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置 JSON 无法解析：{path}（{exc}）") from exc
    return normalize_config(payload, path, exists=True)


def initialize_config(value: Optional[str | os.PathLike[str]] = None) -> Path:
    path = resolve_config_path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ConfigError(f"配置文件已存在：{path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(default_payload(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def _atomic_write_config(path: Path, payload: Mapping[str, Any]) -> None:
    normalized = normalize_config(payload, path, exists=True)
    del normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _payload_for_update(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_payload()
    except OSError as exc:
        raise ConfigError(f"配置文件不可读：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置 JSON 无法解析：{path}（{exc}）") from exc
    normalize_config(payload, path, exists=True)
    return dict(payload)


@contextlib.contextmanager
def _locked_config(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.parent / ".lifeos-config.lock"
    lock_path.touch(exist_ok=True, mode=0o600)
    os.chmod(lock_path, 0o600)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def configure_dchat(
    dws_wrapper: str,
    value: Optional[str | os.PathLike[str]] = None,
) -> dict[str, Any]:
    wrapper = Path(dws_wrapper).expanduser()
    if not wrapper.is_absolute() or not wrapper.is_file():
        raise ConfigError("dws_wrapper 必须是存在的绝对文件")
    path = resolve_config_path(value)
    with _locked_config(path):
        payload = _payload_for_update(path)
        modules = dict(payload["modules"])
        desired: dict[str, Any] = {
            "enabled": True,
            "dws_wrapper": str(wrapper),
        }
        changed = modules.get("dchat") != desired or not path.exists()
        modules["dchat"] = desired
        payload["modules"] = modules
        if changed:
            _atomic_write_config(path, payload)
    return {"changed": changed, "configured": True, "path": str(path)}


def configure_project_root(
    action: str,
    root: str | os.PathLike[str],
    value: Optional[str | os.PathLike[str]] = None,
) -> dict[str, Any]:
    candidate = Path(root).expanduser().absolute()
    if action == "add" and not candidate.is_dir():
        raise ConfigError(f"项目发现根必须是存在的目录：{candidate}")
    if action not in {"add", "remove"}:
        raise ConfigError(f"未知项目发现根操作：{action}")
    path = resolve_config_path(value)
    with _locked_config(path):
        payload = _payload_for_update(path)
        modules = dict(payload["modules"])
        current = dict(modules.get("projects") or {
            "roots": [],
            "exclude": list(DEFAULT_PROJECT_EXCLUDES),
        })
        roots = list(current.get("roots") or [])
        normalized = str(candidate)
        if action == "add":
            changed = normalized not in roots
            if changed:
                roots.append(normalized)
        else:
            changed = normalized in roots
            if changed:
                roots.remove(normalized)
        current["roots"] = roots
        current.setdefault("exclude", list(DEFAULT_PROJECT_EXCLUDES))
        modules["projects"] = current
        payload["modules"] = modules
        if changed or not path.exists():
            _atomic_write_config(path, payload)
    return {
        "changed": changed,
        "action": action,
        "root": normalized,
        "roots": roots,
        "path": str(path),
    }


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "ConfigError",
    "DEFAULT_PROJECT_EXCLUDES",
    "DChatConfig",
    "LifeOSConfig",
    "SUPPORTED_PROJECT_SOURCES",
    "SUPPORTED_SESSION_SOURCES",
    "default_payload",
    "configure_dchat",
    "configure_project_root",
    "initialize_config",
    "load_config",
    "normalize_config",
    "resolve_config_path",
]
