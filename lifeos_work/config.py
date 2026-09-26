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
ENTRIES_PATH = DATA_DIR / "entries.json"
GLOSSARY_PATH = DATA_DIR / "glossary.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"
NOW_PATH = DATA_DIR / "now.md"
PROJECTS_VIEW_PATH = DATA_DIR / "projects.md"
GLOSSARY_VIEW_PATH = DATA_DIR / "glossary.md"
INSIGHTS_VIEW_PATH = DATA_DIR / "insights.md"
LOCK_PATH = DATA_DIR / ".lifeos.lock"

# 子弹笔记的四种记法，每记一条算「一笔」：• 待办、– 随记、? 疑问、! 洞见。
ENTRY_KINDS = ("task", "note", "question", "insight")
ENTRY_SYMBOLS = {"task": "•", "note": "–", "question": "?", "insight": "!"}
ENTRY_KIND_LABELS = {
    "task": "待办",
    "note": "随记",
    "question": "疑问",
    "insight": "洞见",
}
ENTRY_ID_PREFIXES = {"task": "TASK", "note": "NOTE", "question": "ASK", "insight": "INS"}

# 底层只有一套状态值，每种记法用其中几个，界面上各用自己的叫法。
ENTRY_STATUS_VALUES = ("open", "scheduled", "done", "converted", "dropped")
ENTRY_STATUSES = {
    "task": {"open", "scheduled", "done", "dropped"},
    "note": {"open", "converted", "dropped"},
    "question": {"open", "done", "converted", "dropped"},
    "insight": {"open", "dropped"},
}
ENTRY_STATUS_LABELS = {
    "task": {"open": "待做", "scheduled": "排到以后", "done": "完成", "dropped": "划掉"},
    "note": {"open": "记着", "converted": "转成别的", "dropped": "划掉"},
    "question": {"open": "没想通", "done": "想通了", "converted": "转成别的", "dropped": "划掉"},
    "insight": {"open": "有效", "dropped": "退役"},
}
# 进入这些状态时必须写 note（一句批注）或 ref（指向另一笔）。
NOTE_REQUIRED = {
    "task": {"done", "dropped"},
    "note": {"dropped"},
    "question": {"done", "dropped"},
    "insight": {"dropped"},
}
REF_ALLOWED = {
    "task": set(),
    "note": {"converted"},
    "question": {"converted", "done"},
    "insight": {"dropped"},
}
REF_REQUIRED = {"converted"}
# 月初盘点只看这几种记法里还没了结的。
REVIEWED_KINDS = {"task", "note", "question"}
# 转化 `>` 只从还没落地的记录出发。
CONVERTIBLE_KINDS = {"note", "question"}
STARRABLE_KINDS = {"task", "question"}

# 字段顺序即写入 entries.json 的顺序；集合形式供校验使用。
ENTRY_COMMON_FIELD_ORDER = ("id", "kind", "text", "project", "status", "note", "ref")
ENTRY_TRAILING_FIELD_ORDER = ("context", "created_at", "updated_at")
ENTRY_KIND_FIELD_ORDER = {
    "task": ("starred", "owner", "due", "month"),
    "note": (),
    "question": ("starred",),
    "insight": (),
}
ENTRY_COMMON_FIELDS = set(ENTRY_COMMON_FIELD_ORDER) | set(ENTRY_TRAILING_FIELD_ORDER)
ENTRY_KIND_FIELDS = {kind: set(fields) for kind, fields in ENTRY_KIND_FIELD_ORDER.items()}


def entry_field_order(kind):
    return (
        *ENTRY_COMMON_FIELD_ORDER,
        *ENTRY_KIND_FIELD_ORDER[kind],
        *ENTRY_TRAILING_FIELD_ORDER,
    )


def status_label(kind, status):
    return ENTRY_STATUS_LABELS.get(kind, {}).get(status, status)


SCHEDULE_REASON_CODES = {
    "external_change",
    "priority_changed",
    "dependency_blocked",
    "capacity_overload",
    "estimate_error",
    "self_delay",
    "date_correction",
}
PROJECT_TRACKING_STATES = {"active", "paused", "archived"}
ENTITY_KINDS = {"self", "person", "organization", "project", "system", "concept"}
ENTITY_KIND_LABELS = {
    "self": "本人",
    "person": "人员",
    "organization": "组织",
    "project": "项目",
    "system": "系统",
    "concept": "概念",
}

# Work 事实文件不带 schema 版本：是否需要迁移看 v1 文件在不在，结构对不对靠逐字段校验。
CURRENT_TOP_LEVEL_FIELDS = {
    "projects.json": {"updated_at", "projects"},
    "entries.json": {"updated_at", "entries"},
    "glossary.json": {"updated_at", "terms"},
}
PROJECT_FIELDS = {"project_key", "tracking_state", "status_reason", "updated_at"}
TERM_FIELDS = {
    "id", "name", "kind", "aliases", "description", "sources", "confirmed_at",
}
CURRENT_SOURCE_FIELDS = {"kind", "location", "label", "section", "observed_at"}
SELF_ENTITY_ID = "ENT-SELF"

# 紧急：已过截止或 7 天内到期；这个窗口同时决定简报里相对日期的写法。
URGENT_WINDOW_DAYS = 7
# 月初盘点：进行中的一笔超过这么多天没有变化就要给去向。
STALE_DAYS = 30
__all__ = [name for name in globals() if name.isupper()] + [
    "entry_field_order",
    "status_label",
]
