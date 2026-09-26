"""Pure Work-domain helpers.

The functions in this module operate on caller-provided snapshots and command
arguments. They do not read or write the LifeOS runtime; runtime ownership
belongs to the higher-level ``runtime`` module. User-facing validation failures
use the shared ``fail`` boundary so every command reports domain errors the same
way.
"""

import argparse
import os
import re
from datetime import date, datetime, time, timedelta

from .config import (
    ENTRY_ID_PREFIXES,
    ENTRY_KIND_LABELS,
    ENTRY_SYMBOLS,
    REVIEWED_KINDS,
    STALE_DAYS,
    TIMEZONE,
    URGENT_WINDOW_DAYS,
)
from .errors import fail


def now():
    return datetime.now(TIMEZONE)


def iso_now():
    return now().isoformat(timespec="seconds")


def display_iso_time(value):
    if not value:
        return "未知"
    try:
        return datetime.fromisoformat(value).astimezone(TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return value


def validate_date(value):
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD")
    return value


def parse_moment(value):
    """Resolve one edge of a time window into an offset-aware datetime.

    A bare ``YYYY-MM-DD`` means local midnight, so a natural day is written as
    ``--from 2026-08-09 --to 2026-08-10``.  A full ISO timestamp must carry an
    offset: a naive one would silently mean different instants depending on
    where the runtime happens to sit.
    """

    text = value.strip()
    try:
        return datetime.combine(date.fromisoformat(text), time.min, tzinfo=TIMEZONE)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("时间必须是 YYYY-MM-DD 或带时区偏移的 ISO 时间戳")
    if parsed.tzinfo is None:
        raise ValueError("ISO 时间戳必须带时区偏移，例如 2026-08-09T09:00:00+08:00")
    return parsed


def validate_moment(value):
    if value is None:
        return None
    try:
        parse_moment(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    return value


def validate_nonempty_text(value):
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("内容不能为空")
    return normalized


def validate_period(value, pattern, example):
    if not re.fullmatch(pattern, value):
        raise argparse.ArgumentTypeError(f"时间范围必须使用 {example}")
    return value


def validate_month(value):
    validated = validate_period(value, r"\d{4}-(0[1-9]|1[0-2])", "YYYY-MM")
    date.fromisoformat(f"{validated}-01")
    return validated


def validate_quarter(value):
    return validate_period(value, r"\d{4}-Q[1-4]", "YYYY-Q1")


def validate_half(value):
    return validate_period(value, r"\d{4}-H[12]", "YYYY-H1")


def timestamp_date(item, field):
    value = item.get(field)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def date_in_period(value, args):
    if value is None:
        return False
    if getattr(args, "month", None):
        return value.strftime("%Y-%m") == args.month
    if getattr(args, "quarter", None):
        year, quarter = args.quarter.split("-Q")
        start_month = (int(quarter) - 1) * 3 + 1
        return value.year == int(year) and start_month <= value.month <= start_month + 2
    if getattr(args, "half", None):
        year, half = args.half.split("-H")
        months = range(1, 7) if half == "1" else range(7, 13)
        return value.year == int(year) and value.month in months
    return True


def matches_period(item, args):
    """A finished entry is never edited again, so updated_at is when it ended."""
    return date_in_period(timestamp_date(item, "updated_at"), args)


def matches_created_period(item, args):
    return date_in_period(timestamp_date(item, "created_at"), args)


def review_period_label(args):
    return args.month or args.quarter or args.half


def generate_id(prefix, existing_ids):
    day = now().strftime("%Y%m%d")
    base = f"{prefix}-{day}-"
    suffixes = []
    for item_id in existing_ids:
        if item_id.startswith(base):
            try:
                suffixes.append(int(item_id.rsplit("-", 1)[1]))
            except ValueError:
                continue
    return f"{base}{max(suffixes, default=0) + 1:03d}"


def generate_entry_id(kind, entries):
    return generate_id(
        ENTRY_ID_PREFIXES[kind], [item.get("id", "") for item in entries]
    )


def actor_from(args):
    return {
        "kind": getattr(args, "actor_kind", "agent"),
        "name": getattr(args, "actor_name", None)
        or os.environ.get("LIFEOS_ACTOR", "Agent"),
    }


def source_objects(values):
    observed_at = now().date().isoformat()
    return [
        {
            "kind": "agent_input",
            "location": value,
            "section": "lifeos input",
            "observed_at": observed_at,
        }
        for value in values
    ]


def idempotent_event(events, key):
    if not key:
        return None
    return next((event for event in events if event.get("idempotency_key") == key), None)


def make_event(
    events,
    args,
    kind,
    summary,
    entry_id=None,
    related_entry_id=None,
    project=None,
    sources=None,
    status_change=None,
):
    event = {
        "event_id": generate_id("EVT", [item.get("event_id", "") for item in events]),
        "occurred_at": iso_now(),
        "actor": actor_from(args),
        "kind": kind,
        "summary": summary,
        "sources": sources or [],
    }
    if entry_id:
        event["entry_id"] = entry_id
    if related_entry_id:
        event["related_entry_id"] = related_entry_id
    if project:
        event["project"] = project
    if status_change:
        # 当前记录只留最新一句批注；状态怎么变过来的、当时说了什么留在这里。
        before, after, note = status_change
        event["status_from"] = before
        event["status_to"] = after
        if note:
            event["note"] = note
    if getattr(args, "idempotency_key", None):
        event["idempotency_key"] = args.idempotency_key
    return event


def event_entry_ids(event):
    """Entry IDs an audit event talks about; pre-v2 task events used task_id."""

    return {
        value
        for value in (
            event.get("entry_id"),
            event.get("related_entry_id"),
            event.get("task_id"),
        )
        if value
    }


def schedule_change(field, previous, current):
    if previous == current:
        return None
    if previous is None:
        direction = "set"
    elif current is None:
        direction = "cleared"
    elif date.fromisoformat(current) > date.fromisoformat(previous):
        direction = "postponed"
    else:
        direction = "advanced"
    return {
        "field": field,
        "from": previous,
        "to": current,
        "direction": direction,
    }


def owner_name(entry):
    """None means the entry is mine; otherwise the named person or team."""
    owner = entry.get("owner")
    return owner.strip() if isinstance(owner, str) and owner.strip() else None


def find_item(items, item_id, label):
    item = next((candidate for candidate in items if candidate.get("id") == item_id), None)
    if item is None:
        fail(f"找不到{label}：{item_id}")
    return item


def find_entry(entries, entry_id, kinds=None):
    entry = find_item(entries, entry_id, "这一笔")
    if kinds and entry.get("kind") not in kinds:
        expected = "、".join(ENTRY_KIND_LABELS[kind] for kind in kinds)
        fail(f"{entry_id} 是{ENTRY_KIND_LABELS.get(entry.get('kind'), '未知类型')}，这里只接受{expected}")
    return entry


def glossary_matches(term, query):
    if not query:
        return True
    normalized = query.casefold()
    searchable = [
        term.get("id", ""),
        term.get("name", ""),
        term.get("description", ""),
        *term.get("aliases", []),
    ]
    return any(normalized in str(value).casefold() for value in searchable)


def entry_matches_query(entry, query):
    if not query:
        return True
    normalized = query.casefold()
    searchable = [
        entry.get("id", ""),
        entry.get("text", ""),
        entry.get("context") or "",
        entry.get("note") or "",
    ]
    return any(normalized in str(value).casefold() for value in searchable)


def project_label(project_key, projects_by_key):
    project = projects_by_key.get(project_key)
    if project:
        return project.get("name") or project_key
    return project_key or "未归属"


def entry_symbol(entry):
    return ENTRY_SYMBOLS.get(entry.get("kind"), "·")


def is_live(entry):
    """Still on the books for briefs and the monthly check (insights excluded)."""
    if entry.get("kind") not in REVIEWED_KINDS:
        return False
    return entry.get("status") == "open" or (
        entry.get("kind") == "task" and entry.get("status") == "scheduled"
    )


def is_kept(entry):
    """Live entries plus insights that are still valid."""
    return is_live(entry) or (
        entry.get("kind") == "insight" and entry.get("status") == "open"
    )


def parsed_date(value):
    return date.fromisoformat(value) if value else None


def brief_calendar_label(value):
    return f"{value.month}月{value.day}日"


def month_label(value):
    year, month = value.split("-")
    return f"{int(month)}月" if year == str(now().year) else f"{year}年{int(month)}月"


def brief_date_label(value, reference_date):
    target = parsed_date(value)
    if target is None:
        return None
    days = (target - reference_date).days
    if days < 0:
        return f"已逾期 {-days} 天"
    if days == 0:
        return "今天到期"
    if days == 1:
        return "明天到期"
    if days == 2:
        return "后天到期"
    if days <= URGENT_WINDOW_DAYS:
        return f"{days} 天后到期"
    return f"{brief_calendar_label(target)}到期"


def is_urgent(task, reference_date):
    due = parsed_date(task.get("due"))
    return due is not None and due <= reference_date + timedelta(days=URGENT_WINDOW_DAYS)


def is_overdue(task, reference_date):
    due = parsed_date(task.get("due"))
    return due is not None and due < reference_date


def scheduled_month_arrived(task, reference_date):
    month = task.get("month")
    return bool(month) and month <= reference_date.strftime("%Y-%m")


def task_is_current(task, reference_date):
    """Open tasks, plus scheduled ones whose month has come round."""
    if task.get("kind") != "task":
        return False
    if task.get("status") == "open":
        return True
    return task.get("status") == "scheduled" and scheduled_month_arrived(
        task, reference_date
    )


def is_mine(task):
    return owner_name(task) is None


def task_quadrant(task, reference_date):
    """0 重要且紧急 · 1 重要不紧急 · 2 紧急 · 3 其余。"""
    starred = bool(task.get("starred"))
    urgent = is_urgent(task, reference_date)
    if starred and urgent:
        return 0
    if starred:
        return 1
    if urgent:
        return 2
    return 3


def task_sort_key(task, reference_date):
    due = parsed_date(task.get("due"))
    return (
        task_quadrant(task, reference_date),
        0 if due else 1,
        due or date.max,
        task.get("created_at") or "",
        task.get("id", ""),
    )


def last_activity_date(entry):
    return timestamp_date(entry, "updated_at")


def is_stale(entry, reference_date):
    """Live entries untouched for STALE_DAYS need a decision in migration."""
    if not is_live(entry) or entry.get("status") == "scheduled":
        return False
    last = last_activity_date(entry)
    return last is not None and (reference_date - last).days >= STALE_DAYS


def star_needs_review(entry, reference_date):
    if not entry.get("starred") or not is_live(entry):
        return False
    touched = last_activity_date(entry)
    return touched is None or touched.strftime("%Y-%m") != reference_date.strftime(
        "%Y-%m"
    )


def logged_on(entry):
    day = timestamp_date(entry, "created_at")
    return day.isoformat() if day else None


__all__ = [
    "actor_from",
    "brief_calendar_label",
    "brief_date_label",
    "date_in_period",
    "display_iso_time",
    "entry_matches_query",
    "entry_symbol",
    "event_entry_ids",
    "find_entry",
    "find_item",
    "generate_entry_id",
    "generate_id",
    "glossary_matches",
    "idempotent_event",
    "is_kept",
    "is_live",
    "is_mine",
    "is_overdue",
    "is_stale",
    "is_urgent",
    "iso_now",
    "last_activity_date",
    "logged_on",
    "make_event",
    "matches_created_period",
    "matches_period",
    "month_label",
    "now",
    "parse_moment",
    "parsed_date",
    "project_label",
    "owner_name",
    "review_period_label",
    "schedule_change",
    "scheduled_month_arrived",
    "source_objects",
    "star_needs_review",
    "task_is_current",
    "task_quadrant",
    "task_sort_key",
    "timestamp_date",
    "validate_date",
    "validate_half",
    "validate_moment",
    "validate_month",
    "validate_nonempty_text",
    "validate_period",
    "validate_quarter",
]
