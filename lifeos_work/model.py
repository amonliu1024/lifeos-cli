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
    ACHIEVEMENT_RELATIONS,
    BRIEF_WINDOW_DAYS,
    MILESTONE_TRANSITIONS,
    SELF_ENTITY_ID,
    TIMEZONE,
    VALUE_TYPES,
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


def parse_value_entries(entries):
    values = []
    for value_type, statement in entries or []:
        if value_type not in VALUE_TYPES:
            fail(
                f"价值类型非法：{value_type}；可用类型："
                + ", ".join(VALUE_TYPES)
            )
        values.append(
            {
                "type": value_type,
                "statement": statement,
            }
        )
    return values


def normalized_values(completion):
    values = completion.get("values")
    return values if isinstance(values, list) else []


def timestamp_date(item, field):
    value = item.get(field)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def matches_period(item, args):
    completed_at = timestamp_date(item, "closed_at")
    if completed_at is None:
        return False
    if getattr(args, "month", None):
        return completed_at.strftime("%Y-%m") == args.month
    if getattr(args, "quarter", None):
        year, quarter = args.quarter.split("-Q")
        start_month = (int(quarter) - 1) * 3 + 1
        return completed_at.year == int(year) and start_month <= completed_at.month <= start_month + 2
    if getattr(args, "half", None):
        year, half = args.half.split("-H")
        months = range(1, 7) if half == "1" else range(7, 13)
        return completed_at.year == int(year) and completed_at.month in months
    return True


def matches_created_period(item, args):
    created_at = timestamp_date(item, "created_at")
    if created_at is None:
        return False
    if getattr(args, "month", None):
        return created_at.strftime("%Y-%m") == args.month
    if getattr(args, "quarter", None):
        year, quarter = args.quarter.split("-Q")
        start_month = (int(quarter) - 1) * 3 + 1
        return (
            created_at.year == int(year)
            and start_month <= created_at.month <= start_month + 2
        )
    if getattr(args, "half", None):
        year, half = args.half.split("-H")
        months = range(1, 7) if half == "1" else range(7, 13)
        return created_at.year == int(year) and created_at.month in months
    return True


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
    work_item_id=None,
    task_id=None,
    project_id=None,
    idea_id=None,
    achievement_id=None,
    milestone_id=None,
    sources=None,
):
    event = {
        "event_id": generate_id("EVT", [item.get("event_id", "") for item in events]),
        "occurred_at": iso_now(),
        "actor": actor_from(args),
        "kind": kind,
        "summary": summary,
        "sources": sources or [],
    }
    if work_item_id:
        event["work_item_id"] = work_item_id
    if task_id:
        event["task_id"] = task_id
    if project_id:
        event["project_id"] = project_id
    if idea_id:
        event["idea_id"] = idea_id
    if achievement_id:
        event["achievement_id"] = achievement_id
    if milestone_id:
        event["milestone_id"] = milestone_id
    if getattr(args, "idempotency_key", None):
        event["idempotency_key"] = args.idempotency_key
    return event


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


def responsible_name(item):
    party = item.get("responsible_party") or {}
    return party.get("name") or "未知"


def responsible_display_name(item):
    """Return only an explicit external owner name for human-facing views."""
    party = item.get("responsible_party") or {}
    if party.get("kind") not in {"person", "organization"}:
        return None
    name = party.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def responsibility_bucket(item):
    """Classify only explicit self or named external responsibility."""
    party = item.get("responsible_party") or {}
    if party.get("kind") == "self":
        return "self"
    if responsible_display_name(item):
        return "external"
    return None


def canonical_responsible_party(glossary_data, kind, name, entity_id=None):
    """Build a responsibility snapshot, with ENT-SELF owning self identity."""
    if kind == "self":
        self_term = next(
            (
                term
                for term in glossary_data.get("terms", [])
                if term.get("id") == SELF_ENTITY_ID
            ),
            None,
        )
        if not self_term or self_term.get("kind") != "self" or not self_term.get("name"):
            fail(f"缺少合法的本人实体：{SELF_ENTITY_ID}")
        return {
            "kind": "self",
            "name": self_term["name"],
            "entity_id": SELF_ENTITY_ID,
        }
    if entity_id == SELF_ENTITY_ID:
        fail(f"{SELF_ENTITY_ID} 只能用于 kind=self")
    party = {"kind": kind, "name": name}
    if entity_id:
        party["entity_id"] = entity_id
    return party


def item_sort_key(item):
    return (
        item.get("due_at") or "9999-12-31",
        item.get("id", ""),
    )


def work_item_state_label(value):
    return {
        "active": "推进中",
        "waiting": "待前置完成",
        "needs_confirmation": "待确认",
        "paused": "暂停",
    }.get(value, value)


def parsed_date(value):
    return date.fromisoformat(value) if value else None


def brief_calendar_label(value):
    return f"{value.month}月{value.day}日"


def brief_date_label(value, reference_date, kind="due"):
    target = parsed_date(value)
    if target is None or kind != "due":
        return None
    days = (target - reference_date).days
    calendar = brief_calendar_label(target)
    if days < 0:
        return f"已逾期 {-days} 天"
    if days == 0:
        return "今天到期"
    if days == 1:
        return "明天到期"
    if days == 2:
        return "后天到期"
    if days <= BRIEF_WINDOW_DAYS:
        return f"{days} 天后到期"
    return f"{calendar}到期"


def brief_sort_key(item, reference_date, item_kind, started_at=None):
    """Sort tasks by hard deadline, then actual start date when undated."""
    item_id = item.get("id", "")
    if item_kind == "task":
        due = parsed_date(item.get("due_at"))
        if due:
            return (0, due, item_id)
        started = parsed_date(started_at)
        if started:
            return (1, started, item_id)
        return (2, date.max, item_id)

    state = item.get("state")
    if state in {"waiting", "needs_confirmation"}:
        return (3, date.max, item_id)
    return (5, date.max, item_id)


def brief_started_label(value, reference_date):
    started = parsed_date(value)
    if started is None or started > reference_date:
        return None
    days = (reference_date - started).days
    if days == 0:
        return "今天开始推进"
    return f"已推进 {days} 天"


def brief_needs_reminder(item, reference_date, item_kind):
    window_end = reference_date + timedelta(days=BRIEF_WINDOW_DAYS)
    if item_kind == "task":
        due_at = parsed_date(item.get("due_at"))
        if due_at and due_at <= window_end:
            return True
    state = item.get("status") if item_kind == "task" else item.get("state")
    return state in {"waiting", "needs_confirmation"}


def latest_task_started_dates(events):
    result = {}
    for event in events or []:
        if event.get("kind") == "task_started":
            task_id = event.get("task_id")
            started_at = event.get("started_at")
            if task_id and started_at:
                result[task_id] = started_at
    return result


def brief_task_label_parts(task, reference_date, started_at=None):
    party = responsible_display_name(task)
    is_self = (task.get("responsible_party") or {}).get("kind") == "self"
    state_labels = []
    if task.get("status") == "waiting":
        state_labels.append("待前置完成")
    elif party:
        state_labels.append(party)
    if task.get("status") == "paused":
        state_labels.append("已暂停")
    time_labels = []
    started_label = brief_started_label(started_at, reference_date)
    if started_label:
        time_labels.append(started_label)
    due_label = brief_date_label(task.get("due_at"), reference_date)
    if due_label:
        time_labels.append(due_label)
    return party, is_self, state_labels, time_labels


def brief_task_labels(task, reference_date, started_at=None):
    party, is_self, state_labels, time_labels = brief_task_label_parts(
        task, reference_date, started_at
    )
    if task.get("status") == "waiting" and party:
        state_labels = [f"等待 {party}", *state_labels[1:]]
    labels = [*state_labels, *time_labels]
    return labels or ["进行中"]


def brief_work_item_labels(item, reference_date):
    labels = []
    if item.get("stage"):
        labels.append(item["stage"])
    milestone = current_milestone(item)
    if milestone:
        labels.append(f"里程碑：{milestone['title']}")
    if item.get("state") != "active":
        labels.append(work_item_state_label(item.get("state")))
    return labels or ["推进中"]


def brief_work_item_action(item):
    milestone = current_milestone(item)
    if milestone:
        return milestone["outcome"]
    return item.get("next_gate") or "由关联待办承接下一步"


def find_item(items, item_id, label):
    item = next((candidate for candidate in items if candidate.get("id") == item_id), None)
    if item is None:
        fail(f"找不到{label}：{item_id}")
    return item


def milestone_list(work_item):
    return work_item.get("milestones") or []


def find_milestone(work_item, milestone_id):
    milestone = next(
        (
            candidate
            for candidate in milestone_list(work_item)
            if candidate.get("id") == milestone_id
        ),
        None,
    )
    if milestone is None:
        fail(f"事项 {work_item.get('id')} 中找不到里程碑：{milestone_id}")
    return milestone


def current_milestone(work_item):
    return next(
        (item for item in milestone_list(work_item) if item.get("status") == "current"),
        None,
    )


def task_milestone(task, work_items_by_id):
    work_item = work_items_by_id.get(task.get("work_item_id"))
    if not work_item or not task.get("milestone_id"):
        return None
    return next(
        (
            milestone
            for milestone in milestone_list(work_item)
            if milestone.get("id") == task.get("milestone_id")
        ),
        None,
    )


def all_milestone_ids(work_items):
    return [
        milestone.get("id")
        for work_item in work_items
        for milestone in milestone_list(work_item)
        if milestone.get("id")
    ]


def ensure_task_milestone(work_items, work_item_id, milestone_id, status):
    if not work_item_id:
        if milestone_id:
            fail("待办提供 milestone_id 时必须同时提供 work_item_id")
        return
    work_item = find_item(work_items, work_item_id, "事项")
    milestones = milestone_list(work_item)
    if not milestones:
        if milestone_id:
            fail("轻量事项的待办不得关联里程碑")
        return
    if status in {"active", "waiting", "paused"} and not milestone_id:
        fail("路线事项的未完成待办必须关联当前里程碑")
    if not milestone_id:
        return
    milestone = find_milestone(work_item, milestone_id)
    milestone_status = milestone.get("status")
    if status in {"active", "waiting", "paused"} and milestone_status != "current":
        fail("路线事项的未完成待办必须关联 current 里程碑")


def ensure_milestone_transition(current_status, next_status):
    if next_status == current_status:
        return
    if next_status not in MILESTONE_TRANSITIONS.get(current_status, set()):
        fail(f"里程碑状态不能从 {current_status} 变为 {next_status}")


def ensure_entity_ids(glossary_data, entity_ids):
    known_ids = {term.get("id") for term in glossary_data.get("terms", [])}
    for entity_id in entity_ids:
        if entity_id not in known_ids:
            fail(f"找不到实体名词：{entity_id}")


def glossary_matches(term, query):
    if not query:
        return True
    normalized = query.casefold()
    searchable = [
        term.get("id", ""),
        term.get("name", ""),
        term.get("description", ""),
        *term.get("aliases", []),
        *term.get("related_items", []),
    ]
    return any(normalized in str(value).casefold() for value in searchable)


def review_period_label(args):
    return args.month or args.quarter or args.half


def effective_project_id(task, work_items_by_id):
    if task.get("work_item_id"):
        work_item = work_items_by_id.get(task["work_item_id"])
        return work_item.get("project_id") if work_item else None
    return task.get("project_id")


def effective_project_label(task, projects_by_id, work_items_by_id):
    work_item = work_items_by_id.get(task.get("work_item_id"))
    project_id = effective_project_id(task, work_items_by_id)
    project = projects_by_id.get(project_id)
    if project:
        return project.get("name") or project_id
    return project_id or "未归属"


def project_name_owners(projects, excluded_id=None):
    return {
        value.casefold(): project.get("id")
        for project in projects
        if project.get("id") != excluded_id
        for value in [project.get("name", ""), *project.get("aliases", [])]
        if value
    }


def tasks_for_display(tasks, work_items):
    work_items_by_id = {item.get("id"): item for item in work_items}
    result = []
    for task in tasks:
        display = dict(task)
        display["_effective_project_id"] = effective_project_id(
            task, work_items_by_id
        )
        milestone = task_milestone(task, work_items_by_id)
        if milestone:
            display["_milestone_title"] = milestone.get("title")
            display["_milestone_status"] = milestone.get("status")
        result.append(display)
    return result


def parse_achievement_evidence_sources(entries):
    return [
        {"kind": kind, "location": location, "label": label}
        for kind, location, label in entries or []
    ]


def parse_achievement_task_links(entries, tasks, _timestamp=None):
    tasks_by_id = {item.get("id"): item for item in tasks}
    links = []
    seen = set()
    for task_id, relation, contribution in entries or []:
        if relation not in ACHIEVEMENT_RELATIONS:
            fail(
                f"成果胶囊关系非法：{relation}；可用关系："
                + ", ".join(sorted(ACHIEVEMENT_RELATIONS))
            )
        if task_id in seen:
            fail(f"成果胶囊不能重复关联同一待办：{task_id}")
        task = tasks_by_id.get(task_id)
        if task is None:
            fail(f"找不到待办：{task_id}")
        if task.get("status") != "completed":
            fail(f"成果胶囊只能关联已完成待办：{task_id}")
        seen.add(task_id)
        links.append(
            {
                "task_id": task_id,
                "relation": relation,
                "contribution": contribution,
            }
        )
    return links


def achievement_project_ids(achievement, tasks_by_id, work_items_by_id):
    return {
        project_id
        for link in achievement.get("task_links", [])
        for task in [tasks_by_id.get(link.get("task_id"))]
        if task
        for project_id in [effective_project_id(task, work_items_by_id)]
        if project_id
    }


def achievement_matches_query(achievement, query):
    if not query:
        return True
    normalized = query.casefold()
    searchable = [
        achievement.get("id", ""),
        achievement.get("title", ""),
        achievement.get("context", ""),
        achievement.get("outcome", ""),
        achievement.get("reuse", ""),
        *achievement.get("key_learnings", []),
        *[
            value
            for source in achievement.get("sources", [])
            for value in (
                source.get("kind", ""),
                source.get("location", ""),
                source.get("label", ""),
            )
        ],
    ]
    return any(normalized in str(value).casefold() for value in searchable)


def all_target_ids(projects, work_items, tasks):
    return {
        item.get("id")
        for item in [
            *projects.get("projects", []),
            *work_items.get("work_items", []),
            *tasks.get("tasks", []),
        ]
    }


def idea_promotion_target_ids(work_items, tasks):
    return {
        item.get("id")
        for item in [
            *work_items.get("work_items", []),
            *tasks.get("tasks", []),
        ]
    }


__all__ = [
    "achievement_matches_query",
    "achievement_project_ids",
    "actor_from",
    "all_milestone_ids",
    "all_target_ids",
    "brief_calendar_label",
    "brief_date_label",
    "brief_needs_reminder",
    "brief_sort_key",
    "brief_task_label_parts",
    "brief_task_labels",
    "brief_work_item_action",
    "brief_work_item_labels",
    "canonical_responsible_party",
    "current_milestone",
    "display_iso_time",
    "effective_project_id",
    "effective_project_label",
    "ensure_entity_ids",
    "ensure_milestone_transition",
    "ensure_task_milestone",
    "find_item",
    "find_milestone",
    "generate_id",
    "glossary_matches",
    "idempotent_event",
    "idea_promotion_target_ids",
    "iso_now",
    "item_sort_key",
    "make_event",
    "matches_created_period",
    "matches_period",
    "milestone_list",
    "normalized_values",
    "now",
    "parse_achievement_evidence_sources",
    "parse_achievement_task_links",
    "parse_value_entries",
    "parsed_date",
    "project_name_owners",
    "review_period_label",
    "responsible_name",
    "responsible_display_name",
    "responsibility_bucket",
    "schedule_change",
    "source_objects",
    "task_milestone",
    "tasks_for_display",
    "timestamp_date",
    "validate_date",
    "validate_half",
    "validate_month",
    "validate_nonempty_text",
    "validate_period",
    "validate_quarter",
    "work_item_state_label",
]
