"""Configuration and schema constants for the LifeOS Work runtime.

This module has no dependency on other ``lifeos_work`` modules.  Paths are
resolved at import time so the ``LIFEOS_HOME`` test/runtime override is shared
by every Work module.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
DATA_DIR = Path(
    os.environ.get("LIFEOS_HOME", Path.home() / ".local" / "share" / "lifeos")
).expanduser()
PROJECTS_PATH = DATA_DIR / "projects.json"
WORK_ITEMS_PATH = DATA_DIR / "work-items.json"
TASKS_PATH = DATA_DIR / "tasks.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"
NOW_PATH = DATA_DIR / "now.md"
PROJECTS_VIEW_PATH = DATA_DIR / "projects.md"
GLOSSARY_PATH = DATA_DIR / "glossary.json"
GLOSSARY_VIEW_PATH = DATA_DIR / "glossary.md"
IDEAS_PATH = DATA_DIR / "ideas.json"
IDEAS_VIEW_PATH = DATA_DIR / "ideas.md"
ACHIEVEMENTS_PATH = DATA_DIR / "achievements.json"
ACHIEVEMENTS_VIEW_PATH = DATA_DIR / "achievements.md"
WORK_ITEMS_VIEW_PATH = DATA_DIR / "work-items.md"
LOCK_PATH = DATA_DIR / ".lifeos.lock"

TASK_STATUSES = {"active", "waiting", "paused", "completed", "cancelled"}
SCHEDULE_REASON_CODES = {
    "external_change",
    "priority_changed",
    "dependency_blocked",
    "capacity_overload",
    "estimate_error",
    "self_delay",
    "date_correction",
}
WORK_ITEM_STATES = {"active", "waiting", "needs_confirmation", "paused", "closed"}
MILESTONE_STATUSES = {
    "planned",
    "current",
    "completed",
    "cancelled",
}
MILESTONE_DECISIONS = {"continue", "adjust", "pause", "close"}
MILESTONE_TERMINAL_STATUSES = {"completed", "cancelled"}
MILESTONE_TRANSITIONS = {
    "planned": {"current", "cancelled"},
    "current": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
PROJECT_TRACKING_STATES = {"active", "paused", "archived"}
IDEA_STATUSES = {"inbox", "incubating", "promoted", "archived"}
ACHIEVEMENT_LIFECYCLES = {"current", "superseded", "archived"}
ACHIEVEMENT_RELATIONS = {"origin", "contribution", "validation", "revision"}
ENTITY_KINDS = {"self", "person", "organization", "project", "system", "concept"}
ENTITY_KIND_LABELS = {
    "self": "本人",
    "person": "人员",
    "organization": "组织",
    "project": "项目",
    "system": "系统",
    "concept": "概念",
}
VALUE_TYPES = {
    "business": "业务价值",
    "capability": "能力提升",
    "relationship": "人脉积累",
    "reportable": "可汇报成果",
    "efficiency": "效率提升",
    "risk_reduction": "风险降低",
    "other": "其他价值",
}

CURRENT_SCHEMA_VERSION = 1
CURRENT_TOP_LEVEL_FIELDS = {
    "projects.json": {"schema_version", "updated_at", "projects"},
    "work-items.json": {"schema_version", "updated_at", "work_items"},
    "tasks.json": {"schema_version", "updated_at", "tasks"},
    "glossary.json": {"schema_version", "updated_at", "terms"},
    "ideas.json": {"schema_version", "updated_at", "ideas"},
    "achievements.json": {"schema_version", "updated_at", "achievements"},
}
CURRENT_OBJECT_FIELDS = {
    "项目引用": {
        "id", "project_key", "manifest_path", "tracking_state",
        "status_reason", "created_at", "updated_at",
    },
    "事项": {
        "id", "title", "project_id", "state", "status_reason", "stage",
        "context", "next_gate", "milestones", "sources", "created_at", "updated_at",
    },
    "待办": {
        "id", "outcome", "work_item_id", "project_id", "milestone_id",
        "status", "status_reason", "responsible_party", "next_action", "due_at",
        "why", "completion_criteria", "context", "completion", "sources",
        "created_at", "updated_at", "closed_at",
    },
    "实体名词": {
        "id", "name", "kind", "aliases", "description", "related_items",
        "sources", "confirmed_at",
    },
    "闪念": {
        "id", "text", "status", "context", "status_reason", "sources",
        "promoted_to", "created_at", "updated_at",
    },
    "成果胶囊": {
        "id", "title", "task_links", "context", "outcome",
        "key_learnings", "reuse", "lifecycle", "status_reason",
        "superseded_by", "sources", "created_at", "updated_at",
    },
}
CURRENT_COMPLETION_FIELDS = {"summary", "sources", "values", "reflections"}
CURRENT_VALUE_FIELDS = {"type", "statement"}
CURRENT_ACTOR_FIELDS = {"kind", "name"}
CURRENT_SOURCE_FIELDS = {"kind", "location", "label", "section", "observed_at"}
CURRENT_NEXT_ACTION_FIELDS = {"text"}
CURRENT_RESPONSIBLE_PARTY_FIELDS = {"kind", "name", "entity_id"}
SELF_ENTITY_ID = "ENT-SELF"
CURRENT_ACHIEVEMENT_LINK_FIELDS = {"task_id", "relation", "contribution"}
CURRENT_MILESTONE_FIELDS = {
    "id", "title", "status", "outcome", "completion_criteria", "target_at",
    "completion", "decision", "created_at", "updated_at", "completed_at",
}
MILESTONE_STATUS_LABELS = {
    "planned": "计划中",
    "current": "当前阶段",
    "completed": "已完成",
    "cancelled": "已取消",
}

BRIEF_WINDOW_DAYS = 5
__all__ = [name for name in globals() if name.isupper()]
