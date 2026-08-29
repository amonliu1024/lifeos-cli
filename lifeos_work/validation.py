"""Pure current-schema ledger and audit-event validation.

This module deliberately has no Runtime or command dependencies.  Callers
provide the complete current snapshot and event history; validation returns
the same ordered list of errors used by the monolithic CLI implementation.
"""

import re
from datetime import date

from .config import (
    ACHIEVEMENT_LIFECYCLES,
    ACHIEVEMENT_RELATIONS,
    CURRENT_ACHIEVEMENT_LINK_FIELDS,
    CURRENT_COMPLETION_FIELDS,
    CURRENT_MILESTONE_FIELDS,
    CURRENT_NEXT_ACTION_FIELDS,
    CURRENT_OBJECT_FIELDS,
    CURRENT_RESPONSIBLE_PARTY_FIELDS,
    CURRENT_SCHEMA_VERSION,
    CURRENT_SOURCE_FIELDS,
    CURRENT_TOP_LEVEL_FIELDS,
    CURRENT_VALUE_FIELDS,
    ENTITY_KINDS,
    IDEA_STATUSES,
    MILESTONE_STATUSES,
    PROJECT_TRACKING_STATES,
    SCHEDULE_REASON_CODES,
    SELF_ENTITY_ID,
    TASK_STATUSES,
    VALUE_TYPES,
    WORK_ITEM_STATES,
)
from .model import milestone_list, schedule_change


def valid_date_field(item_id, field, value, errors):
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        errors.append(f"{item_id} {field} 日期非法：{value}")


def current_data_errors(
    projects_data,
    work_items_data,
    tasks_data,
    glossary_data,
    ideas_data,
    achievements_data,
    events,
    expected_schema_version=CURRENT_SCHEMA_VERSION,
):
    errors = []
    collections = [
        ("projects.json", projects_data, "projects"),
        ("work-items.json", work_items_data, "work_items"),
        ("tasks.json", tasks_data, "tasks"),
        ("glossary.json", glossary_data, "terms"),
        ("ideas.json", ideas_data, "ideas"),
        ("achievements.json", achievements_data, "achievements"),
    ]
    for filename, data, array_name in collections:
        if not isinstance(data, dict):
            errors.append(f"{filename} 必须为对象")
            continue
        if data.get("schema_version") != expected_schema_version:
            errors.append(f"{filename} schema_version 必须为 {expected_schema_version}")
        unknown = sorted(set(data) - CURRENT_TOP_LEVEL_FIELDS[filename])
        if unknown:
            errors.append(f"{filename} 包含非当前字段：{', '.join(unknown)}")
        if not isinstance(data.get(array_name), list):
            errors.append(f"{filename} {array_name} 缺少数组")
    if errors:
        return errors

    projects = projects_data["projects"]
    work_items = work_items_data["work_items"]
    tasks = tasks_data["tasks"]
    terms = glossary_data["terms"]
    ideas = ideas_data["ideas"]
    achievements = achievements_data["achievements"]
    groups = [
        ("项目引用", projects), ("事项", work_items), ("待办", tasks),
        ("实体名词", terms), ("闪念", ideas), ("成果胶囊", achievements),
    ]

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

    ids = []
    for label, values in groups:
        allowed = CURRENT_OBJECT_FIELDS[label]
        local_ids = []
        for item in values:
            item_id = item.get("id") if isinstance(item, dict) else None
            local_ids.append(item_id)
            if not isinstance(item, dict):
                errors.append(f"{label}必须为对象数组")
                continue
            unknown = sorted(set(item) - allowed)
            if unknown:
                errors.append(f"{item_id} {label}包含非当前字段：{', '.join(unknown)}")
        if any(value is None for value in local_ids):
            errors.append(f"{label}存在空 ID")
        if len(local_ids) != len(set(local_ids)):
            errors.append(f"{label} ID 重复")
        ids.extend(local_ids)
    if len(ids) != len(set(ids)):
        errors.append("记录 ID 跨类型重复")

    project_ids = {item.get("id") for item in projects}
    work_item_ids = {item.get("id") for item in work_items}
    task_ids = {item.get("id") for item in tasks}
    term_ids = {item.get("id") for item in terms}
    terms_by_id = {item.get("id"): item for item in terms}
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
    achievement_ids = {item.get("id") for item in achievements}
    work_items_by_id = {item.get("id"): item for item in work_items}
    tasks_by_id = {item.get("id"): item for item in tasks}
    milestones_by_id = {}

    for project in projects:
        item_id = project.get("id")
        if not item_id or not re.fullmatch(r"PRJ-\d{8}-\d{3}", item_id):
            errors.append(f"项目引用 ID 格式非法：{item_id}")
        if not project.get("project_key") or not project.get("manifest_path"):
            errors.append(f"{item_id} 缺少 project_key 或 manifest_path")
        if project.get("tracking_state") not in PROJECT_TRACKING_STATES:
            errors.append(f"{item_id} 跟踪状态非法")
        if project.get("tracking_state") in {"paused", "archived"} and not project.get("status_reason"):
            errors.append(f"{item_id} 暂停或归档必须有 status_reason")

    for item in work_items:
        item_id = item.get("id")
        if not item_id or not re.fullmatch(r"WI-\d{8}-\d{3}", item_id):
            errors.append(f"事项 ID 格式非法：{item_id}")
        if not item.get("title") or item.get("state") not in WORK_ITEM_STATES:
            errors.append(f"{item_id} 标题或状态非法")
        if item.get("project_id") and item.get("project_id") not in project_ids:
            errors.append(f"{item_id} 关联不存在的项目引用")
        if item.get("state") in {"waiting", "needs_confirmation", "paused", "closed"} and not item.get("status_reason"):
            errors.append(f"{item_id} 当前状态必须有 status_reason")
        validate_sources(item_id, item.get("sources"))
        milestones = item.get("milestones")
        if not isinstance(milestones, list):
            errors.append(f"{item_id} milestones 必须为数组")
            continue
        if milestones and item.get("next_gate") is not None:
            errors.append(f"{item_id} 路线事项不得保存根 next_gate")
        current_count = 0
        for milestone in milestones:
            milestone_id = milestone.get("id")
            if set(milestone) - CURRENT_MILESTONE_FIELDS:
                errors.append(f"{milestone_id} 里程碑包含非当前字段")
            if not milestone_id or not re.fullmatch(r"MS-\d{8}-\d{3}", milestone_id):
                errors.append(f"里程碑 ID 格式非法：{milestone_id}")
            if milestone_id in milestones_by_id:
                errors.append(f"里程碑 ID 重复：{milestone_id}")
            milestones_by_id[milestone_id] = (item_id, milestone)
            if milestone.get("status") not in MILESTONE_STATUSES:
                errors.append(f"{milestone_id} 状态非法")
            if milestone.get("status") == "current":
                current_count += 1
            for field in ("title", "outcome", "completion_criteria"):
                if not milestone.get(field):
                    errors.append(f"{milestone_id} 缺少 {field}")
            valid_date_field(milestone_id, "target_at", milestone.get("target_at"), errors)
            completion = milestone.get("completion")
            if milestone.get("status") == "completed":
                if not milestone.get("completed_at") or not milestone.get("decision"):
                    errors.append(f"{milestone_id} 完成时缺少 completed_at 或 decision")
                if not isinstance(completion, dict) or not completion.get("summary"):
                    errors.append(f"{milestone_id} 完成时缺少 completion.summary")
                elif set(completion) != {"summary", "sources"}:
                    errors.append(f"{milestone_id} completion 结构非法")
                else:
                    validate_sources(milestone_id, completion.get("sources"))
            elif completion is not None or milestone.get("completed_at") is not None:
                errors.append(f"{milestone_id} 非完成状态不得保存 completion 或 completed_at")
        if current_count > 1:
            errors.append(f"{item_id} 最多只能有一个 current 里程碑")
        if milestones and item.get("state") in {"active", "waiting", "needs_confirmation"} and current_count != 1:
            errors.append(f"{item_id} 活跃路线事项必须有且仅有一个 current 里程碑")

    open_tasks_by_work_item = set()
    for item in tasks:
        item_id = item.get("id")
        if not item_id or not re.fullmatch(r"TASK-\d{8}-\d{3}", item_id):
            errors.append(f"待办 ID 格式非法：{item_id}")
        status = item.get("status")
        if status not in TASK_STATUSES or not item.get("outcome"):
            errors.append(f"{item_id} 结果或状态非法")
        work_item_id = item.get("work_item_id")
        project_id = item.get("project_id")
        if work_item_id and work_item_id not in work_item_ids:
            errors.append(f"{item_id} 关联不存在的事项")
        if work_item_id and project_id is not None:
            errors.append(f"{item_id} 关联事项时 project_id 必须为空")
        if project_id and project_id not in project_ids:
            errors.append(f"{item_id} 关联不存在的项目引用")
        if status in {"active", "waiting", "paused"} and work_item_id:
            open_tasks_by_work_item.add(work_item_id)
        if status in {"waiting", "paused", "cancelled"} and not item.get("status_reason"):
            errors.append(f"{item_id} 当前状态必须有 status_reason")
        party = item.get("responsible_party")
        if not isinstance(party, dict) or not party.get("kind") or not party.get("name") or set(party) - CURRENT_RESPONSIBLE_PARTY_FIELDS:
            errors.append(f"{item_id} responsible_party 结构非法")
        elif party.get("entity_id") and party.get("entity_id") not in term_ids:
            errors.append(f"{item_id} 责任实体不存在")
        elif party.get("kind") != "self" and party.get("entity_id") == SELF_ENTITY_ID:
            errors.append(f"{item_id} {SELF_ENTITY_ID} 只能用于 kind=self")
        elif party.get("entity_id") and party.get("name") != terms_by_id[
            party["entity_id"]
        ].get("name"):
            errors.append(f"{item_id} 责任方名称必须与 entity_id 的规范名称一致")
        elif party.get("kind") == "self" and self_term and (
            party.get("entity_id") != SELF_ENTITY_ID
        ):
            errors.append(
                f"{item_id} self 责任方必须使用 {SELF_ENTITY_ID} 的规范名称与实体 ID"
            )
        action = item.get("next_action")
        if action is not None:
            if not isinstance(action, dict) or set(action) - CURRENT_NEXT_ACTION_FIELDS or not action.get("text"):
                errors.append(f"{item_id} next_action 结构非法")
        valid_date_field(item_id, "due_at", item.get("due_at"), errors)
        validate_sources(item_id, item.get("sources"))
        milestone_id = item.get("milestone_id")
        if milestone_id:
            owner = milestones_by_id.get(milestone_id)
            if not owner or owner[0] != work_item_id:
                errors.append(f"{item_id} 关联不存在或不属于事项的里程碑")
            elif status in {"active", "waiting", "paused"} and owner[1].get("status") != "current":
                errors.append(f"{item_id} 未完成时必须关联 current 里程碑")
        elif work_item_id and milestone_list(work_items_by_id[work_item_id]) and status in {"active", "waiting", "paused"}:
            errors.append(f"{item_id} 路线事项的未完成待办缺少 milestone_id")
        completion = item.get("completion")
        if status == "completed":
            if not item.get("closed_at") or not isinstance(completion, dict):
                errors.append(f"{item_id} 完成时缺少 closed_at 或 completion")
            elif set(completion) - CURRENT_COMPLETION_FIELDS or not completion.get("summary"):
                errors.append(f"{item_id} completion 结构非法")
            else:
                validate_sources(item_id, completion.get("sources"))
                for value in completion.get("values", []):
                    if not isinstance(value, dict) or set(value) - CURRENT_VALUE_FIELDS or value.get("type") not in VALUE_TYPES or not value.get("statement"):
                        errors.append(f"{item_id} completion.values 结构非法")
                if not isinstance(completion.get("reflections", []), list) or any(not isinstance(v, str) or not v for v in completion.get("reflections", [])):
                    errors.append(f"{item_id} completion.reflections 结构非法")
        elif completion is not None or item.get("closed_at") is not None:
            errors.append(f"{item_id} 未完成时不得保存 completion 或 closed_at")

    for item in work_items:
        if not milestone_list(item) and item.get("state") != "closed" and not item.get("next_gate") and item.get("id") not in open_tasks_by_work_item:
            errors.append(f"{item.get('id')} 轻量事项必须有 next_gate 或未完成待办")

    for term in terms:
        item_id = term.get("id")
        if item_id != "ENT-SELF" and (not item_id or not re.fullmatch(r"ENT-\d{8}-\d{3}", item_id)):
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
        for related_id in term.get("related_items", []):
            if related_id not in project_ids | work_item_ids | task_ids:
                errors.append(f"{item_id} 关联不存在的工作对象")

    for idea in ideas:
        item_id = idea.get("id")
        if not item_id or not re.fullmatch(r"IDEA-\d{8}-\d{3}", item_id) or idea.get("status") not in IDEA_STATUSES or not idea.get("text"):
            errors.append(f"{item_id} 闪念字段非法")
        validate_sources(item_id, idea.get("sources"), required=False)
        if idea.get("status") == "archived" and not idea.get("status_reason"):
            errors.append(f"{item_id} 归档必须有 status_reason")
        if idea.get("status") == "promoted" and not idea.get("promoted_to"):
            errors.append(f"{item_id} 已提升但没有目标")
        for target in idea.get("promoted_to", []):
            if target not in work_item_ids | task_ids:
                errors.append(f"{item_id} 提升目标不存在")

    achievements_by_id = {item.get("id"): item for item in achievements}
    for item in achievements:
        item_id = item.get("id")
        if not item_id or not re.fullmatch(r"ACH-\d{8}-\d{3}", item_id):
            errors.append(f"成果胶囊 ID 格式非法：{item_id}")
        for field in ("title", "context", "outcome", "reuse"):
            if not item.get(field):
                errors.append(f"{item_id} 缺少 {field}")
        if not isinstance(item.get("key_learnings"), list) or not item.get("key_learnings"):
            errors.append(f"{item_id} key_learnings 必须为非空数组")
        validate_sources(item_id, item.get("sources"))
        links = item.get("task_links")
        if not isinstance(links, list) or not links:
            errors.append(f"{item_id} task_links 必须为非空数组")
            links = []
        if not any(link.get("relation") == "origin" for link in links if isinstance(link, dict)):
            errors.append(f"{item_id} 至少需要一个 origin 待办")
        for link in links:
            if not isinstance(link, dict) or set(link) - CURRENT_ACHIEVEMENT_LINK_FIELDS or link.get("relation") not in ACHIEVEMENT_RELATIONS or not link.get("contribution"):
                errors.append(f"{item_id} task_link 结构非法")
            elif link.get("task_id") not in task_ids or tasks_by_id[link["task_id"]].get("status") != "completed":
                errors.append(f"{item_id} task_link 必须关联已完成待办")
        lifecycle = item.get("lifecycle")
        if lifecycle not in ACHIEVEMENT_LIFECYCLES:
            errors.append(f"{item_id} 生命周期非法")
        if lifecycle in {"archived", "superseded"} and not item.get("status_reason"):
            errors.append(f"{item_id} 当前生命周期必须有 status_reason")
        replacement = item.get("superseded_by")
        if lifecycle == "superseded":
            if replacement == item_id or replacement not in achievement_ids or achievements_by_id[replacement].get("lifecycle") != "current":
                errors.append(f"{item_id} superseded_by 非法")
        elif replacement is not None:
            errors.append(f"{item_id} 非 superseded 不得保存 superseded_by")

    event_ids = [event.get("event_id") for event in events]
    if len(event_ids) != len(set(event_ids)):
        errors.append("内部审计 ID 重复")
    for event in events:
        if event.get("achievement_id") and event.get("achievement_id") not in achievement_ids:
            errors.append("内部审计关联不存在的成果胶囊")
        if event.get("kind") == "task_created" and event.get("schedule") is not None:
            schedule = event["schedule"]
            if not isinstance(schedule, dict) or set(schedule) - {"due_at"}:
                errors.append("task_created schedule 结构非法")
            else:
                for field, value in schedule.items():
                    valid_date_field(event.get("task_id", "内部审计"), field, value, errors)
        if event.get("kind") == "task_schedule_changed":
            if event.get("task_id") not in task_ids:
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
                    or not isinstance(change.get("field"), str)
                    or change.get("field") != "due_at"
                    or not isinstance(change.get("direction"), str)
                    or change.get("direction")
                    not in {"set", "advanced", "postponed", "cleared"}
                ):
                    errors.append("计划日期变更事件 change 结构非法")
                    continue
                valid_date_field(
                    event.get("task_id", "内部审计"),
                    f"{change['field']}.from",
                    change.get("from"),
                    errors,
                )
                valid_date_field(
                    event.get("task_id", "内部审计"),
                    f"{change['field']}.to",
                    change.get("to"),
                    errors,
                )
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
        if event.get("kind") == "task_started":
            task_id = event.get("task_id")
            if task_id not in task_ids:
                errors.append("开始推进事件关联不存在的待办")
                continue
            valid_date_field(
                task_id,
                "task_started.started_at",
                event.get("started_at"),
                errors,
            )

    return errors
