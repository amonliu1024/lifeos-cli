"""Read-only projections shared by the Web transport and its tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from lifeos_reports import store as report_store
from lifeos_work.config import ENTRY_SYMBOLS, status_label
from lifeos_work.model import (
    is_kept,
    is_live,
    is_mine,
    logged_on,
    owner_name,
    task_is_current,
    task_quadrant,
    task_sort_key,
)


def _project_summary(project: dict[str, Any] | None) -> dict[str, Any] | None:
    if not project:
        return None
    return {
        "key": project.get("project_key"),
        "name": project.get("name") or project.get("project_key"),
        "availability": project.get("availability"),
    }


def _entry_projection(
    entry: dict[str, Any],
    projects: dict[str, dict[str, Any]],
    reference_date: date,
) -> dict[str, Any]:
    kind = entry.get("kind")
    current = task_is_current(entry, reference_date)
    mine = kind == "task" and is_mine(entry)
    return {
        "id": entry.get("id"),
        "kind": kind,
        "symbol": ENTRY_SYMBOLS.get(kind),
        "text": entry.get("text"),
        "status": entry.get("status"),
        "status_label": status_label(kind, entry.get("status")),
        "note": entry.get("note"),
        "ref": entry.get("ref"),
        "starred": bool(entry.get("starred")),
        "owner": owner_name(entry) if kind == "task" else None,
        "due": entry.get("due"),
        "month": entry.get("month"),
        "context": entry.get("context"),
        "project": _project_summary(projects.get(entry.get("project"))),
        "logged_on": logged_on(entry),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "live": is_live(entry),
        "kept": is_kept(entry),
        "current": current and mine,
        "quadrant": task_quadrant(entry, reference_date) if current and mine else None,
    }


def _report_index(reports_root: Path) -> list[dict[str, Any]]:
    directory = report_store.daily_dir(reports_root)
    if not directory.is_dir():
        return []
    reports: list[dict[str, Any]] = []
    for path in directory.iterdir():
        matched = report_store.DAY_NAME.fullmatch(path.name)
        if not matched or not path.is_file():
            continue
        day_text = matched.group(1)
        try:
            meta, _body = report_store.read_report(path)
            reports.append(
                {
                    "day": day_text,
                    "status": meta.get("status"),
                    "generated_at": meta.get("generated_at"),
                    "confirmed_at": meta.get("confirmed_at"),
                    "counts": {
                        "activities": meta.get("sessions_activities", "0"),
                        "work_events": meta.get("work_events", "0"),
                        "git_commits": meta.get("git_commits", "0"),
                        "unresolved": meta.get("unresolved", "0"),
                    },
                    "readable": True,
                }
            )
        except report_store.ReportError:
            reports.append(
                {
                    "day": day_text,
                    "status": "invalid",
                    "readable": False,
                    "error": "日报无法读取",
                }
            )
    return sorted(reports, key=lambda item: item["day"], reverse=True)


def build_snapshot(
    current_data: tuple[dict[str, Any], ...],
    reports_root: Path,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Build the single browser projection without retaining or writing state."""

    projects_data, entries_data, _glossary_data = current_data
    reference_date = reference_date or date.today()
    projects = {
        item.get("project_key"): item for item in projects_data.get("projects", [])
    }
    raw_entries = entries_data.get("entries", [])
    ranked = sorted(
        (
            entry for entry in raw_entries
            if entry.get("kind") == "task" and (task_is_current(entry, reference_date) or is_live(entry))
        ),
        key=lambda entry: task_sort_key(entry, reference_date),
    )
    rank = {entry["id"]: index for index, entry in enumerate(ranked)}
    entries = []
    for entry in raw_entries:
        projected = _entry_projection(entry, projects, reference_date)
        projected["rank"] = rank.get(entry.get("id"))
        entries.append(projected)
    entries.sort(
        key=lambda item: (item.get("created_at") or "", item.get("id") or ""),
        reverse=True,
    )
    return {
        "updated_at": entries_data.get("updated_at"),
        "reference_date": reference_date.isoformat(),
        "entries": entries,
        "reports": _report_index(reports_root),
    }


def parse_day(value: str) -> date:
    """Accept only the canonical date spelling used by report filenames."""

    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("日期必须使用 YYYY-MM-DD")
    return parsed


def _read_report(
    reports_root: Path,
    day_text: str,
) -> tuple[Path, dict[str, str], str]:
    day = parse_day(day_text)
    path = report_store.report_path(reports_root, day)
    meta, body = report_store.read_report(path)
    return path, meta, body


def report_detail(reports_root: Path, day_text: str) -> dict[str, Any]:
    _path, meta, body = _read_report(reports_root, day_text)
    return {
        "day": day_text,
        "status": meta.get("status"),
        "generated_at": meta.get("generated_at"),
        "confirmed_at": meta.get("confirmed_at"),
        "window": meta.get("window"),
        "counts": {
            "activities": meta.get("sessions_activities", "0"),
            "work_events": meta.get("work_events", "0"),
            "git_commits": meta.get("git_commits", "0"),
            "unresolved": meta.get("unresolved", "0"),
        },
        "body": body,
    }


def resolve_openable_report(reports_root: Path, day_text: str) -> Path:
    """Resolve one existing canonical report; no caller-supplied path is accepted."""

    path, _meta, _body = _read_report(reports_root, day_text)
    return path
