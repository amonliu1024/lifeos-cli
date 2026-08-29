"""Side-effect-free capability probes for built-in LifeOS modules."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .core import LifeOSConfig, SUPPORTED_PROJECT_SOURCES, SUPPORTED_SESSION_SOURCES
from lifeos_sessions.adapters import source_root


def _status(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def capability_report(config: LifeOSConfig) -> dict[str, Any]:
    sessions: dict[str, dict[str, str]] = {}
    for source in SUPPORTED_SESSION_SOURCES:
        if source not in config.session_sources:
            sessions[source] = _status("disabled", "not_enabled")
        elif source_root(source).is_dir():
            sessions[source] = _status("ready", "source_directory_available")
        else:
            sessions[source] = _status("unavailable", "source_directory_missing")

    project_sources = {
        source: _status(
            "ready" if source in config.project_sources else "disabled",
            "enabled" if source in config.project_sources else "not_enabled",
        )
        for source in SUPPORTED_PROJECT_SOURCES
    }

    if not config.dchat.enabled:
        dchat = _status("disabled", "not_enabled")
    elif config.dchat.dws_wrapper and Path(config.dchat.dws_wrapper).is_file():
        dchat = _status("ready", "configured")
    else:
        dchat = _status("unavailable", "dws_wrapper_missing")

    return {
        "config": {
            "path": str(config.path),
            "status": "ready" if config.exists else "unconfigured",
        },
        "modules": {
            "work": _status("ready", "built_in"),
            "project": _status("ready", "built_in"),
            "reports": _status("ready", "built_in"),
            "git": _status(
                "ready" if shutil.which("git") else "unavailable",
                "git_available" if shutil.which("git") else "git_missing",
            ),
            "sessions": {
                "status": "ready" if any(
                    item["status"] == "ready" for item in sessions.values()
                ) else "unavailable",
                "sources": sessions,
            },
            "dchat": dchat,
            "project_sources": project_sources,
        },
    }


__all__ = ["capability_report"]
