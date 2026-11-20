"""Read-only daily context aggregator for ``lifeos reports prepare``."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from lifeos_calendar.store import CalendarStore
from lifeos_config.core import ConfigError, LifeOSConfig, load_config
from lifeos_dchat.store import DChatStore
from lifeos_git.store import GitStore, GitStoreError
from lifeos_projects.catalog import discover_projects
from lifeos_sessions.core import TimeWindow
from lifeos_sessions.store import SessionsStore
from lifeos_work.commands.reporting import _within_window

from . import store


def _safe_load_config() -> tuple[Optional[LifeOSConfig], Optional[str]]:
    try:
        return load_config(allow_missing=True), None
    except ConfigError as exc:
        return None, str(exc)


def _projects_summary(config: Optional[LifeOSConfig]) -> Dict[str, Any]:
    if config is None:
        return {"roots": [], "valid_count": 0, "complete": False, "findings": [], "projects": []}
    catalog = discover_projects(config)
    return {
        "roots": list(catalog.roots),
        "valid_count": len(catalog.projects),
        "complete": catalog.complete,
        "findings": [item.to_dict() for item in catalog.findings],
        "projects": [
            {"project_key": item.project_key, "name": item.name, "scope": item.scope}
            for item in catalog.projects
        ],
    }


def _sessions_readiness(
    data_dir: Path,
    from_iso: str,
    to_iso: str,
    config: Optional[LifeOSConfig],
) -> Dict[str, Any]:
    enabled_sources = list(config.session_sources) if config else []
    sessions_store = SessionsStore(data_dir / "sessions")
    window = TimeWindow.from_values(from_iso, to_iso)
    scans = sessions_store.list_scans(window=window)
    latest = scans[0] if scans else None
    ready = False
    if latest is not None:
        sources = latest.get("sources") or []
        present = {item.get("source") for item in sources}
        no_failed = not any(item.get("status") == "failed" for item in sources)
        covers_enabled = not enabled_sources or set(enabled_sources).issubset(present)
        ready = (
            bool(latest.get("manifest_valid"))
            and latest.get("status") in {"complete", "partial"}
            and no_failed
            and covers_enabled
        )
    return {
        "enabled_sources": enabled_sources,
        "ready": ready,
        "latest_scan": (
            {
                "scan_id": latest["scan_id"],
                "status": latest.get("status"),
                "manifest_valid": latest.get("manifest_valid"),
                "created_at": latest.get("created_at"),
                "sources": latest.get("sources") or [],
            }
            if latest is not None
            else None
        ),
    }


def _git_readiness(data_dir: Path, from_iso: str, to_iso: str) -> Dict[str, Any]:
    git_store = GitStore(data_dir / "git")
    error: Optional[str] = None
    repos_total = 0
    repos_enabled = 0
    try:
        repos = git_store.load_registry()
        repos_total = len(repos)
        repos_enabled = sum(1 for item in repos if item.enabled)
    except GitStoreError as exc:
        error = str(exc)

    expected_window = {"from": from_iso, "to": to_iso}
    matching = [
        item
        for item in git_store.list_scans()
        if not item.get("error") and item.get("window") == expected_window
    ]
    matching.sort(key=lambda item: (item.get("captured_at") or "", item.get("scan_id") or ""), reverse=True)
    latest = matching[0] if matching else None
    ready = bool(latest and latest.get("status") in {"complete", "partial"})
    return {
        "repos_total": repos_total,
        "repos_enabled": repos_enabled,
        "ready": ready,
        "error": error,
        "latest_scan": (
            {
                "scan_id": latest["scan_id"],
                "status": latest.get("status"),
                "captured_at": latest.get("captured_at"),
                "summary": latest.get("summary"),
            }
            if latest is not None
            else None
        ),
    }


def _dchat_readiness(
    data_dir: Path,
    from_iso: str,
    to_iso: str,
    config: Optional[LifeOSConfig],
) -> Dict[str, Any]:
    enabled = bool(config and config.dchat.enabled)
    configured = bool(enabled and config and config.dchat.dws_wrapper)
    latest = DChatStore(data_dir / "dchat").latest_scan(from_iso, to_iso) if enabled else None
    ready = bool(latest and latest.get("status") in {"complete", "partial"})
    return {
        "enabled": enabled,
        "configured": configured,
        "ready": ready,
        "latest_scan": (
            {
                "scan_id": latest["scan_id"],
                "status": latest.get("status"),
                "captured_at": latest.get("captured_at"),
                "summary": latest.get("summary"),
            }
            if latest is not None
            else None
        ),
    }


def _calendar_readiness(
    data_dir: Path,
    from_iso: str,
    to_iso: str,
    config: Optional[LifeOSConfig],
) -> Dict[str, Any]:
    enabled = bool(config and config.calendar.enabled)
    latest = CalendarStore(data_dir / "calendar").latest_scan(from_iso, to_iso) if enabled else None
    ready = bool(latest and latest.get("status") in {"complete", "partial"})
    return {
        "enabled": enabled,
        "ready": ready,
        "latest_scan": (
            {
                "scan_id": latest["scan_id"],
                "status": latest.get("status"),
                "captured_at": latest.get("captured_at"),
                "summary": latest.get("summary"),
            }
            if latest is not None
            else None
        ),
    }


def _work_summary(data_dir: Path, window_from: Any, window_to: Any) -> Dict[str, Any]:
    entries_path = data_dir / "entries.json"
    events_path = data_dir / "events.jsonl"
    if not entries_path.is_file():
        return {
            "initialized": False,
            "window_events": [],
            "same_day_entries": [],
            "live_tasks_count": 0,
        }

    entries_data = json.loads(entries_path.read_text(encoding="utf-8"))
    entries = entries_data.get("entries") or []
    same_day_entries: List[Dict[str, Any]] = []
    live_tasks_count = 0
    for item in entries:
        if item.get("kind") == "task" and item.get("status") == "open":
            live_tasks_count += 1
        if _within_window({"occurred_at": item.get("created_at")}, window_from, window_to):
            same_day_entries.append({
                "id": item.get("id"),
                "kind": item.get("kind"),
                "text": item.get("text"),
                "project": item.get("project"),
                "status": item.get("status"),
                "note": item.get("note"),
                "context": item.get("context"),
                "created_at": item.get("created_at"),
            })

    window_events: List[Dict[str, Any]] = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if _within_window(event, window_from, window_to):
                window_events.append({
                    "event_id": event.get("event_id"),
                    "kind": event.get("kind"),
                    "entry_id": event.get("entry_id"),
                    "project": event.get("project"),
                    "summary": event.get("summary"),
                    "occurred_at": event.get("occurred_at"),
                })

    return {
        "initialized": True,
        "window_events": window_events,
        "same_day_entries": same_day_entries,
        "live_tasks_count": live_tasks_count,
    }


def prepare_daily_context(data_dir: Path, day: date) -> Dict[str, Any]:
    """Assemble read-only report status, source readiness, previous report and Work summary."""

    reports_root = data_dir / "reports"
    window_from, window_to = store.day_window(day)
    from_iso = window_from.isoformat()
    to_iso = window_to.isoformat()

    report_file = store.report_path(reports_root, day)
    report_info: Dict[str, Any] = {
        "path": str(report_file),
        "exists": report_file.exists(),
        "status": None,
        "generated_at": None,
        "confirmed_at": None,
        "superseded": [str(item) for item in store.superseded_paths(reports_root, day)],
    }
    if report_file.exists():
        try:
            meta, _body = store.read_report(report_file)
            report_info["status"] = meta.get("status")
            report_info["generated_at"] = meta.get("generated_at")
            report_info["confirmed_at"] = meta.get("confirmed_at")
        except store.ReportError as exc:
            report_info["error"] = str(exc)

    config, config_error = _safe_load_config()
    previous = store.previous_confirmed_report(reports_root, day)

    return {
        "day": day.isoformat(),
        "window": {"from": from_iso, "to": to_iso},
        "config_error": config_error,
        "report": report_info,
        "previous_confirmed": previous,
        "projects": _projects_summary(config),
        "sources": {
            "sessions": _sessions_readiness(data_dir, from_iso, to_iso, config),
            "git": _git_readiness(data_dir, from_iso, to_iso),
            "dchat": _dchat_readiness(data_dir, from_iso, to_iso, config),
            "calendar": _calendar_readiness(data_dir, from_iso, to_iso, config),
        },
        "work": _work_summary(data_dir, window_from, window_to),
    }


__all__ = ["prepare_daily_context"]
