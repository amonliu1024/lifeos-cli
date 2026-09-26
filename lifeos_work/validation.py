"""Pure current-schema ledger and audit-event validation.

This module deliberately has no Runtime or command dependencies.  Callers
provide the complete current snapshot and event history; validation returns an
ordered list of errors.
"""

import re
from datetime import date

from .config import (
    CURRENT_SOURCE_FIELDS,
    CURRENT_TOP_LEVEL_FIELDS,
    ENTITY_KINDS,
    ENTRY_ID_PREFIXES,
    ENTRY_KINDS,
    ENTRY_STATUSES,
    NOTE_REQUIRED,
    PROJECT_FIELDS,
    PROJECT_TRACKING_STATES,
    REF_ALLOWED,
    REF_REQUIRED,
    SCHEDULE_REASON_CODES,
    SELF_ENTITY_ID,
    TERM_FIELDS,
    entry_field_order,
)
from .model import schedule_change


def valid_date_field(item_id, field, value, errors):
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        errors.append(f"{item_id} {field} 日期非法：{value}")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def current_data_errors(projects_data, entries_data, glossary_data, events):
    errors = []
    collections = [
        ("projects.json", projects_data, "projects"),
        ("entries.json", entries_data, "entries"),
        ("glossary.json", glossary_data, "terms"),
    ]
    for filename, data, array_name in collections:
        if not isinstance(data, dict):
            errors.append(f"{filename} 必须为对象")
            continue
        unknown = sorted(set(data) - CURRENT_TOP_LEVEL_FIELDS[filename])
        if unknown:
            errors.append(f"{filename} 包含非当前字段：{', '.join(unknown)}")
        if not isinstance(data.get(array_name), list):
            errors.append(f"{filename} {array_name} 缺少数组")
    if errors:
        return errors

    projects = projects_data["projects"]
    entries = entries_data["entries"]
    terms = glossary_data["terms"]

    def validate_sources(item_id, values, required=True):
        if not isinstance(values, list) or (required and not values):
            errors.append(f"{item_id} sources 必须为来源对象数组")
            return
        for value in values:
            if (
                not isinstance(value, dict)
                or set(value) - CURRENT_SOURCE_FIELDS
                or not isinstance(value.get("kind"), str)
                or not value.get("kind")
                or not isinstance(value.get("location"), str)
                or not value.get("location")
            ):
                errors.append(f"{item_id} sources 存在非法来源对象")
                return

    term_ids = []
    for term in terms:
        if not isinstance(term, dict):
            errors.append("实体名词必须为对象数组")
            continue
        term_ids.append(term.get("id"))
        unknown = sorted(set(term) - TERM_FIELDS)
        if unknown:
            errors.append(f"{term.get('id')} 实体名词包含非当前字段：{', '.join(unknown)}")
    if any(value is None for value in term_ids) or len(term_ids) != len(set(term_ids)):
        errors.append("实体名词 ID 为空或重复")

    project_keys = []
    for project in projects:
        if not isinstance(project, dict):
            errors.append("项目引用必须为对象数组")
            continue
        key = project.get("project_key")
        project_keys.append(key)
        if set(project) != PROJECT_FIELDS:
            errors.append(f"{key} 项目引用字段不符合当前合同")
        if not isinstance(key, str) or not key:
            errors.append("项目引用缺少 project_key")
        if project.get("tracking_state") not in PROJECT_TRACKING_STATES:
            errors.append(f"{key} 跟踪状态非法")
        if project.get("tracking_state") in {"paused", "archived"} and not project.get("status_reason"):
            errors.append(f"{key} 暂停或归档必须有 status_reason")
    if len(project_keys) != len(set(project_keys)):
        errors.append("project_key 重复")

    entry_ids = []
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("entries 必须为对象数组")
            continue
        entry_ids.append(entry.get("id"))
    if any(value is None for value in entry_ids):
        errors.append("entries 存在空 ID")
    if len(entry_ids) != len(set(entry_ids)):
        errors.append("entries ID 重复")
    if set(entry_ids) & set(term_ids):
        errors.append("记录 ID 跨类型重复")
    if errors:
        return errors

    known_projects = set(project_keys)
    entries_by_id = {item.get("id"): item for item in entries}
    self_term = next(
        (item for item in terms if item.get("id") == SELF_ENTITY_ID), None
    )
    if (
        not self_term
        or self_term.get("kind") != "self"
        or not isinstance(self_term.get("name"), str)
        or not self_term["name"].strip()
    ):
        errors.append(f"{SELF_ENTITY_ID} 必须是具备规范名称的本人实体")

    for entry in entries:
        item_id = entry.get("id")
        kind = entry.get("kind")
        if kind not in ENTRY_KINDS:
            errors.append(f"{item_id} 类型非法：{kind}")
            continue
        expected_fields = set(entry_field_order(kind))
        if set(entry) != expected_fields:
            missing = sorted(expected_fields - set(entry))
            unknown = sorted(set(entry) - expected_fields)
            detail = "；".join(
                part
                for part in (
                    f"缺少 {', '.join(missing)}" if missing else "",
                    f"多出 {', '.join(unknown)}" if unknown else "",
                )
                if part
            )
            errors.append(f"{item_id} 字段不符合 {kind} 的合同：{detail}")
            continue
        prefix = ENTRY_ID_PREFIXES[kind]
        if not item_id or not re.fullmatch(rf"{prefix}-\d{{8}}-\d{{3}}", item_id):
            errors.append(f"ID 格式非法：{item_id}")
        status = entry.get("status")
        if status not in ENTRY_STATUSES[kind]:
            errors.append(f"{item_id} 状态非法：{status}")
            continue
        if not _text(entry.get("text")):
            errors.append(f"{item_id} 正文不能为空")
        if entry.get("project") is not None and entry.get("project") not in known_projects:
            errors.append(f"{item_id} 挂在没有跟踪的项目上：{entry.get('project')}")
        for field in ("note", "context"):
            if entry.get(field) is not None and not _text(entry.get(field)):
                errors.append(f"{item_id} {field} 不能是空文本")
        if status in NOTE_REQUIRED[kind] and not _text(entry.get("note")):
            errors.append(f"{item_id} 当前状态必须写 note")
        if kind == "insight" and not _text(entry.get("context")):
            errors.append(f"{item_id} 洞见必须在 context 写来由")
        if not entry.get("created_at") or not entry.get("updated_at"):
            errors.append(f"{item_id} 缺少 created_at 或 updated_at")

        ref = entry.get("ref")
        if status in REF_REQUIRED and not ref:
            errors.append(f"{item_id} 转成别的必须用 ref 指向那一笔")
        if ref is not None:
            target = entries_by_id.get(ref)
            if status not in REF_ALLOWED[kind]:
                errors.append(f"{item_id} 当前状态不得保存 ref")
            elif ref == item_id or target is None:
                errors.append(f"{item_id} ref 指向不存在的记录：{ref}")
            elif status != "converted" and target.get("kind") != "insight":
                errors.append(f"{item_id} ref 只能指向洞见：{ref}")
            elif kind == "insight" and target.get("status") != "open":
                errors.append(f"{item_id} 被取代时 ref 必须指向仍有效的洞见")

        if kind in {"task", "question"} and not isinstance(entry.get("starred"), bool):
            errors.append(f"{item_id} starred 必须为布尔值")
        if kind == "task":
            owner = entry.get("owner")
            if owner is not None and not _text(owner):
                errors.append(f"{item_id} owner 只能为空（本人）或负责人名字")
            valid_date_field(item_id, "due", entry.get("due"), errors)
            month = entry.get("month")
            if status == "scheduled":
                if not isinstance(month, str) or not re.fullmatch(
                    r"\d{4}-(0[1-9]|1[0-2])", month
                ):
                    errors.append(f"{item_id} 排到以后必须写 month（YYYY-MM）")
            elif month is not None:
                errors.append(f"{item_id} 非 scheduled 状态不得保存 month")

    for term in terms:
        item_id = term.get("id")
        if item_id != SELF_ENTITY_ID and (not item_id or not re.fullmatch(r"ENT-\d{8}-\d{3}", item_id)):
            errors.append(f"实体名词 ID 格式非法：{item_id}")
        name = term.get("name")
        description = term.get("description")
        aliases = term.get("aliases")
        if (
            term.get("kind") not in ENTITY_KINDS
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(description, str)
            or not description.strip()
        ):
            errors.append(f"{item_id} 名词字段非法")
        if (
            not isinstance(aliases, list)
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
            or len(aliases) != len(set(aliases))
            or name in aliases
        ):
            errors.append(f"{item_id} aliases 结构非法")
        validate_sources(item_id, term.get("sources"))

    event_ids = [event.get("event_id") for event in events]
    if len(event_ids) != len(set(event_ids)):
        errors.append("内部审计 ID 重复")
    for event in events:
        if event.get("kind") != "task_schedule_changed":
            continue
        task_id = event.get("entry_id") or event.get("task_id")
        if task_id not in entries_by_id:
            errors.append("计划日期变更事件关联不存在的待办")
        changes = event.get("schedule_changes")
        if not isinstance(changes, list) or not changes:
            errors.append("计划日期变更事件缺少 schedule_changes")
            continue
        reason_code = event.get("reason_code")
        if reason_code is not None and (
            not isinstance(reason_code, str)
            or reason_code not in SCHEDULE_REASON_CODES
        ):
            errors.append("计划日期变更事件 reason_code 非法")
        if event.get("reason_note") and not reason_code:
            errors.append("计划日期变更事件 reason_note 缺少 reason_code")
        for change in changes:
            if (
                not isinstance(change, dict)
                or set(change) != {"field", "from", "to", "direction"}
                or change.get("field") != "due_at"
                or change.get("direction")
                not in {"set", "advanced", "postponed", "cleared"}
            ):
                errors.append("计划日期变更事件 change 结构非法")
                continue
            valid_date_field(task_id, "due_at.from", change.get("from"), errors)
            valid_date_field(task_id, "due_at.to", change.get("to"), errors)
            try:
                expected = schedule_change(
                    change["field"], change.get("from"), change.get("to")
                )
            except (TypeError, ValueError):
                expected = None
            if expected is None or expected["direction"] != change["direction"]:
                errors.append("计划日期变更事件 direction 与 from/to 不一致")
            if change["direction"] in {"postponed", "cleared"} and not reason_code:
                errors.append("延后或清除计划日期的事件缺少 reason_code")

    return errors
