"""Read-only projections shared by the Web transport and its tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from lifeos_reports import store as report_store
from lifeos_work.model import brief_sort_key, latest_task_started_dates
from lifeos_work.views import current_brief_item_sort_key


TERMINAL_TASK_STATUSES = {"completed", "cancelled"}


def _project_summary(project: dict[str, Any] | None) -> dict[str, Any] | None:
    if not project:
        return None
    return {
        "id": project.get("id"),
        "key": project.get("project_key"),
        "name": project.get("name") or project.get("project_key"),
        "availability": project.get("availability"),
    }


def _next_action_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"text": value.get("text")}


def _completion_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"summary": value.get("summary")}


def _task_projection(
    task: dict[str, Any],
    projects: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "work_item_id": task.get("work_item_id"),
        "outcome": task.get("outcome"),
        "status": task.get("status"),
        "due_at": task.get("due_at"),
        "closed_at": task.get("closed_at"),
        "created_at": task.get("created_at"),
        "next_action": _next_action_summary(task.get("next_action")),
        "completion_criteria": task.get("completion_criteria"),
        "why": task.get("why"),
        "completion": _completion_summary(task.get("completion")),
        "project": _project_summary(projects.get(task.get("project_id"))),
        "terminal": task.get("status") in TERMINAL_TASK_STATUSES,
    }


def _idea_projection(idea: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": idea.get("id"),
        "text": idea.get("text"),
        "context": idea.get("context"),
        "status": idea.get("status"),
        "status_reason": idea.get("status_reason"),
        "promoted_to": list(idea.get("promoted_to") or []),
        "updated_at": idea.get("updated_at"),
    }


def _achievement_projection(achievement: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": achievement.get("id"),
        "title": achievement.get("title"),
        "outcome": achievement.get("outcome"),
        "context": achievement.get("context"),
        "key_learnings": list(achievement.get("key_learnings") or []),
        "reuse": achievement.get("reuse"),
        "task_links": [
            {
                "task_id": link.get("task_id"),
                "contribution": link.get("contribution"),
            }
            for link in achievement.get("task_links") or []
            if isinstance(link, dict)
        ],
        "lifecycle": achievement.get("lifecycle"),
        "updated_at": achievement.get("updated_at"),
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
    events: list[dict[str, Any]] | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Build the single browser projection without retaining or writing state."""

    (
        projects_data,
        work_items_data,
        tasks_data,
        _glossary_data,
        ideas_data,
        achievements_data,
    ) = current_data
    reference_date = reference_date or date.today()
    started_dates = latest_task_started_dates(events or [])
    projects = {
        item.get("id"): item for item in projects_data.get("projects", [])
    }
    tasks = [
        _task_projection(item, projects)
        for item in tasks_data.get("tasks", [])
    ]
    task_sort_key = lambda task: brief_sort_key(
        task,
        reference_date,
        "task",
        started_dates.get(task.get("id")),
    )
    tasks_by_work_item: dict[str, list[dict[str, Any]]] = {}
    standalone_tasks: list[dict[str, Any]] = []
    for task in tasks:
        work_item_id = task.get("work_item_id")
        if work_item_id:
            tasks_by_work_item.setdefault(work_item_id, []).append(task)
        else:
            standalone_tasks.append(task)

    work_items = []
    for item in work_items_data.get("work_items", []):
        milestones = item.get("milestones") or []
        current_milestone = next(
            (milestone for milestone in milestones if milestone.get("status") == "current"),
            None,
        )
        work_items.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "state": item.get("state"),
                "next_gate": item.get("next_gate"),
                "context": item.get("context"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "project": _project_summary(projects.get(item.get("project_id"))),
                "current_milestone": (
                    {"outcome": current_milestone.get("outcome")}
                    if current_milestone
                    else None
                ),
                "tasks": sorted(
                    tasks_by_work_item.get(item.get("id"), []),
                    key=task_sort_key,
                ),
                "terminal": item.get("state") == "closed",
            }
        )
    current_tasks_by_work_item = {
        work_item_id: [
            task for task in linked_tasks
            if task.get("status") in {"active", "waiting"}
        ]
        for work_item_id, linked_tasks in tasks_by_work_item.items()
    }
    work_items.sort(
        key=lambda item: current_brief_item_sort_key(
            item,
            current_tasks_by_work_item,
            reference_date,
            started_dates,
        )
    )

    return {
        "updated_at": max(
            value
            for value in (
                work_items_data.get("updated_at"),
                tasks_data.get("updated_at"),
                ideas_data.get("updated_at"),
                achievements_data.get("updated_at"),
            )
            if value
        ),
        "work": {
            "items": work_items,
            "standalone_tasks": sorted(standalone_tasks, key=task_sort_key),
        },
        "reports": _report_index(reports_root),
        "ideas": sorted(
            (_idea_projection(item) for item in ideas_data.get("ideas", [])),
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        ),
        "achievements": sorted(
            (
                _achievement_projection(item)
                for item in achievements_data.get("achievements", [])
            ),
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        ),
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
