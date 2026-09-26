"""One-shot migration from the v1 Work model to the v2 entry log.

v1 kept projects, work items (with milestones), tasks, ideas and achievement
capsules in separate files.  v2 keeps one ``entries.json``.  The migration is
two steps: ``--plan`` only reads and lists what needs a personal decision;
``--apply`` takes the decisions file, backs up the whole Runtime, writes the new
facts and removes the v1 files.  Historical ``events.jsonl`` lines are never
rewritten.
"""

import json
import shutil
from datetime import datetime

from lifeos_projects import hydrate_projects_data, project_registry_errors

from .config import (
    DATA_DIR,
    ENTRIES_PATH,
    EVENTS_PATH,
    GLOSSARY_PATH,
    PROJECTS_PATH,
    TIMEZONE,
    entry_field_order,
)
from .errors import fail
from .model import (
    generate_entry_id,
    iso_now,
    make_event,
    now,
    validate_date,
    validate_month,
)
from .runtime import (
    DIR_MODE,
    FILE_MODE,
    _create_transaction_recovery,
    _ensure_private_directory,
    _pending_transaction_directories,
    _restore_transaction_snapshot,
    append_event,
    atomic_write_json,
    atomic_write_text,
    exclusive_lock,
    read_events,
    read_json,
    sha256_file,
)
from .validation import current_data_errors
from .views import current_view_contents


LEGACY_FACT_FILES = {
    "work-items.json": "work_items",
    "tasks.json": "tasks",
    "ideas.json": "ideas",
    "achievements.json": "achievements",
}
LEGACY_VIEW_FILES = ("work-items.md", "ideas.md", "achievements.md")
LEGACY_VALUE_LABELS = {
    "business": "业务价值",
    "capability": "能力提升",
    "relationship": "人脉积累",
    "reportable": "可汇报成果",
    "efficiency": "效率提升",
    "risk_reduction": "风险降低",
    "other": "其他价值",
}
TASK_STATUS_MAP = {
    "active": "open",
    "waiting": "open",
    "completed": "done",
    "cancelled": "dropped",
}


def _day(value):
    try:
        return datetime.fromisoformat(value).astimezone(TIMEZONE).date().isoformat()
    except (TypeError, ValueError):
        return now().date().isoformat()


def read_legacy_runtime():
    if ENTRIES_PATH.exists():
        fail("entries.json 已存在，Runtime 已是 v2，不需要迁移")
    missing = [
        name for name in (*LEGACY_FACT_FILES, "projects.json", "glossary.json")
        if not (DATA_DIR / name).exists()
    ]
    if missing:
        fail("找不到 v1 Runtime 的事实文件：" + "、".join(missing))
    legacy = {name: read_json(DATA_DIR / name) for name in LEGACY_FACT_FILES}
    for name, array in LEGACY_FACT_FILES.items():
        payload = legacy[name]
        if not isinstance(payload.get(array), list):
            fail(f"{name} 不是受支持的 v1 结构")
    projects = read_json(PROJECTS_PATH)
    glossary = read_json(GLOSSARY_PATH)
    if not isinstance(projects.get("projects"), list) or not isinstance(glossary.get("terms"), list):
        fail("projects.json 或 glossary.json 不是受支持的 v1 结构")
    return projects, glossary, {
        "work_items": legacy["work-items.json"]["work_items"],
        "tasks": legacy["tasks.json"]["tasks"],
        "ideas": legacy["ideas.json"]["ideas"],
        "achievements": legacy["achievements.json"]["achievements"],
    }


def decisions_needed(legacy):
    """Everything a person has to decide; nothing here is guessed."""
    work_items = {}
    milestones = {}
    for item in legacy["work_items"]:
        if item.get("state") == "closed":
            continue
        if item.get("next_gate"):
            work_items[item["id"]] = {
                "title": item.get("title"),
                "state": item.get("state"),
                "next_gate": item.get("next_gate"),
                "project_id": item.get("project_id"),
                "decision": None,
            }
        for milestone in item.get("milestones") or []:
            if milestone.get("status") == "current":
                milestones[milestone["id"]] = {
                    "work_item": item["id"],
                    "work_item_title": item.get("title"),
                    "title": milestone.get("title"),
                    "outcome": milestone.get("outcome"),
                    "target_at": milestone.get("target_at"),
                    "decision": None,
                }
    paused_tasks = {
        task["id"]: {
            "text": task.get("outcome"),
            "reason": task.get("status_reason"),
            "decision": None,
        }
        for task in legacy["tasks"]
        if task.get("status") == "paused"
    }
    return {
        "work_items": work_items,
        "milestones": milestones,
        "paused_tasks": paused_tasks,
    }


def plan_summary(legacy):
    tasks = legacy["tasks"]
    ideas = legacy["ideas"]
    work_items = legacy["work_items"]
    return {
        "tasks": {
            status: sum(1 for task in tasks if task.get("status") == status)
            for status in ("active", "waiting", "paused", "completed", "cancelled")
        },
        "work_items_closed_not_migrated": sum(
            1 for item in work_items if item.get("state") == "closed"
        ),
        "work_items_open": sum(1 for item in work_items if item.get("state") != "closed"),
        "milestones_not_migrated": sum(len(item.get("milestones") or []) for item in work_items),
        "ideas_to_notes": sum(1 for idea in ideas if idea.get("status") in {"inbox", "incubating"}),
        "ideas_not_migrated": sum(
            1 for idea in ideas if idea.get("status") not in {"inbox", "incubating"}
        ),
        "achievements_not_migrated": len(legacy["achievements"]),
    }


def render_plan(summary, needed):
    lines = [
        "# LifeOS v2 迁移清单",
        "",
        "会自动处理：",
        f"- 待办转为 •：待做 {summary['tasks']['active']}、原等人 {summary['tasks']['waiting']}（转为待做，等什么写进背景）、"
        f"完成 {summary['tasks']['completed']}、划掉（原 cancelled） {summary['tasks']['cancelled']}；保留原 ID",
        f"- 闪念 {summary['ideas_to_notes']} 条转为 – 随记；其余 {summary['ideas_not_migrated']} 条只留在备份与历史",
        f"- 已关闭事项 {summary['work_items_closed_not_migrated']} 个、里程碑 {summary['milestones_not_migrated']} 个、"
        f"成果胶囊 {summary['achievements_not_migrated']} 个不迁移，留在备份与历史",
        "",
        "需要你逐条决定（写进决定文件后用 --apply 执行）：",
    ]
    if needed["work_items"]:
        lines += ["", "## 未关闭事项的下一门槛（\"drop\" 丢弃，或 {\"task\": \"待办正文\", \"due\": \"YYYY-MM-DD\"} 转成待办）", ""]
        for item_id, item in needed["work_items"].items():
            lines.append(f"- {item_id} 【{item['title']}】（{item['state']}）下一门槛：{item['next_gate']}")
    if needed["milestones"]:
        lines += ["", "## 当前里程碑（\"drop\" 或 {\"task\": \"待办正文\", \"due\": \"YYYY-MM-DD\"}）", ""]
        for milestone_id, milestone in needed["milestones"].items():
            target = f" · 目标 {milestone['target_at']}" if milestone.get("target_at") else ""
            lines.append(
                f"- {milestone_id} 【{milestone['work_item_title']}】{milestone['title']}{target}"
            )
    if needed["paused_tasks"]:
        lines += ["", "## 暂停的待办（\"open\" 继续、{\"schedule\": \"YYYY-MM\"} 排到以后，或 \"drop\" 放弃）", ""]
        for task_id, task in needed["paused_tasks"].items():
            lines.append(f"- {task_id} {task['text']}（暂停原因：{task['reason']}）")
    lines += ["", "用 `lifeos work migrate-v2 --plan --json > 决定文件.json` 取得模板，填好每个 decision。"]
    return "\n".join(lines) + "\n"


def _check_decisions(needed, decisions):
    errors = []
    if not isinstance(decisions, dict):
        return ["决定文件必须是 JSON 对象"]
    for group, allowed in (
        ("work_items", {"drop", "task"}),
        ("milestones", {"drop", "task"}),
        ("paused_tasks", {"drop", "open", "schedule"}),
    ):
        provided = decisions.get(group) or {}
        if set(provided) != set(needed[group]):
            missing = sorted(set(needed[group]) - set(provided))
            extra = sorted(set(provided) - set(needed[group]))
            if missing:
                errors.append(f"{group} 缺少决定：{'、'.join(missing)}")
            if extra:
                errors.append(f"{group} 有清单外的条目：{'、'.join(extra)}")
        for key, record in provided.items():
            decision = record.get("decision") if isinstance(record, dict) else None
            if isinstance(decision, str):
                if decision not in allowed or decision in {"task", "schedule"}:
                    errors.append(f"{key} 的决定不合法：{decision}")
                continue
            if not isinstance(decision, dict) or len(set(decision) & allowed) != 1:
                errors.append(f"{key} 缺少合法决定")
                continue
            try:
                if "task" in decision:
                    if not isinstance(decision["task"], str) or not decision["task"].strip():
                        errors.append(f"{key} 转成待办时必须写待办正文")
                    if decision.get("due"):
                        validate_date(decision["due"])
                if "schedule" in decision:
                    validate_month(decision["schedule"])
                    if decision["schedule"] <= now().strftime("%Y-%m"):
                        errors.append(f"{key} 排到的月份必须在以后")
            except Exception:
                errors.append(f"{key} 的日期或月份格式不对")
    return errors


def _merged_context(*parts):
    text = "\n".join(part for part in parts if part)
    return text or None


def _task_entry(task, project, work_item_title, status, *, note, month):
    legacy_note = None
    if task.get("status") == "completed":
        legacy = task.get("completion") or {}
        lines = [legacy.get("summary", "").strip()]
        for value in legacy.get("values") or []:
            label = LEGACY_VALUE_LABELS.get(value.get("type"), value.get("type"))
            lines.append(f"价值（{label}）：{value.get('statement')}")
        for reflection in legacy.get("reflections") or []:
            lines.append(f"复盘：{reflection}")
        legacy_note = "\n".join(line for line in lines if line)
    party = task.get("responsible_party") or {}
    waiting = task.get("status") == "waiting" and task.get("status_reason")
    context = _merged_context(
        f"原事项：{work_item_title}" if work_item_title else None,
        f"等：{task['status_reason']}" if waiting else None,
        f"为什么：{task['why']}" if task.get("why") else None,
        f"完成标准：{task['completion_criteria']}" if task.get("completion_criteria") else None,
        f"下一步：{(task.get('next_action') or {}).get('text')}"
        if (task.get("next_action") or {}).get("text")
        else None,
        task.get("context"),
    )
    return {
        "id": task["id"],
        "kind": "task",
        "text": task["outcome"],
        "project": project,
        "status": status,
        "note": legacy_note if status == "done" else note,
        "ref": None,
        "starred": False,
        "owner": None if party.get("kind") == "self" else (party.get("name") or None),
        "due": task.get("due_at"),
        "month": month,
        "context": context,
        "created_at": task["created_at"],
        # 结束后的记录不再修改，updated_at 就是结束时间。
        "updated_at": task.get("closed_at") if status == "done" and task.get("closed_at") else task["updated_at"],
    }


def _new_task(entries, text, due, project, context, timestamp):
    return {
        "id": generate_entry_id("task", entries),
        "kind": "task",
        "text": text.strip(),
        "project": project,
        "status": "open",
        "note": None,
        "ref": None,
        "starred": False,
        "owner": None,
        "due": due,
        "month": None,
        "context": context,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def build_entries(projects, legacy, decisions):
    timestamp = iso_now()
    keys = {item.get("id"): item.get("project_key") for item in projects["projects"]}
    work_items = {item["id"]: item for item in legacy["work_items"]}
    entries = []
    for task in legacy["tasks"]:
        work_item = work_items.get(task.get("work_item_id"))
        project = keys.get(task.get("project_id") or (work_item or {}).get("project_id"))
        status = TASK_STATUS_MAP.get(task.get("status"))
        note = task.get("status_reason") if status == "dropped" else None
        month = None
        if task.get("status") == "paused":
            decision = decisions["paused_tasks"][task["id"]]["decision"]
            if decision == "open":
                status = "open"
            elif decision == "drop":
                status = "dropped"
                note = task.get("status_reason") or "迁移时划掉"
            else:
                status = "scheduled"
                note = task.get("status_reason")
                month = decision["schedule"]
        entries.append(
            _task_entry(
                task, project, (work_item or {}).get("title"), status, note=note, month=month
            )
        )
    for item_id, record in decisions["work_items"].items():
        decision = record["decision"]
        if decision == "drop":
            continue
        item = work_items[item_id]
        entries.append(
            _new_task(
                entries,
                decision["task"],
                decision.get("due"),
                keys.get(item.get("project_id")),
                f"原事项：{item.get('title')}；原下一门槛：{item.get('next_gate')}",
                timestamp,
            )
        )
    for milestone_id, record in decisions["milestones"].items():
        decision = record["decision"]
        if decision == "drop":
            continue
        item = work_items[record["work_item"]]
        milestone = next(m for m in item["milestones"] if m["id"] == milestone_id)
        entries.append(
            _new_task(
                entries,
                decision["task"],
                decision.get("due") or milestone.get("target_at"),
                keys.get(item.get("project_id")),
                f"原里程碑：{milestone.get('title')}；完成判定：{milestone.get('completion_criteria')}",
                timestamp,
            )
        )
    for idea in legacy["ideas"]:
        if idea.get("status") not in {"inbox", "incubating"}:
            continue
        entries.append(
            {
                "id": generate_entry_id("note", entries),
                "kind": "note",
                "text": idea["text"],
                "project": None,
                "status": "open",
                "note": None,
                "ref": None,
                "context": idea.get("context") or None,
                "created_at": idea.get("created_at") or timestamp,
                "updated_at": idea.get("updated_at") or timestamp,
            }
        )
    ordered = [
        {field: entry[field] for field in entry_field_order(entry["kind"])}
        for entry in entries
    ]
    return {"updated_at": timestamp, "entries": ordered}


def migrated_projects(projects, timestamp):
    return {
        "updated_at": timestamp,
        "projects": [
            {
                "project_key": item.get("project_key"),
                "tracking_state": item.get("tracking_state"),
                "status_reason": item.get("status_reason"),
                "updated_at": item.get("updated_at") or timestamp,
            }
            for item in projects["projects"]
        ],
    }


def migrated_glossary(glossary):
    result = json.loads(json.dumps(glossary))
    result.pop("schema_version", None)
    for term in result["terms"]:
        term.pop("related_items", None)
    return result


def command_migrate_v2(args):
    if args.plan:
        _projects, _glossary, legacy = read_legacy_runtime()
        needed = decisions_needed(legacy)
        if args.json:
            print(json.dumps(needed, ensure_ascii=False, indent=2))
        else:
            print(render_plan(plan_summary(legacy), needed), end="")
        return
    if not args.decisions:
        fail("--apply 需要 --decisions 指向填好的决定文件")
    if not args.source:
        fail("--apply 需要 --source 写明本人确认迁移清单的来源")
    try:
        decisions = json.loads(open(args.decisions, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"决定文件无法读取：{exc}")

    with exclusive_lock():
        pending = _pending_transaction_directories()
        if pending:
            fail("发现未完成的 Work 事务，已拒绝迁移：" + "、".join(str(path) for path in pending))
        projects, glossary, legacy = read_legacy_runtime()
        needed = decisions_needed(legacy)
        errors = _check_decisions(needed, decisions)
        if errors:
            fail("决定文件不完整：\n- " + "\n- ".join(errors))
        entries = build_entries(projects, legacy, decisions)
        new_projects = migrated_projects(projects, entries["updated_at"])
        new_glossary = migrated_glossary(glossary)
        new_glossary["updated_at"] = entries["updated_at"]
        events = read_events()
        counts = {}
        for entry in entries["entries"]:
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
        event = make_event(
            events,
            args,
            "runtime_migrated_v2",
            "迁移到子弹笔记式记法：" + "、".join(f"{kind} {count}" for kind, count in counts.items()),
            sources=args.source,
        )
        event["decisions"] = decisions
        errors = current_data_errors(new_projects, entries, new_glossary, [*events, event])
        errors.extend(project_registry_errors(new_projects))
        if errors:
            fail("迁移结果校验失败，未修改 Runtime：\n- " + "\n- ".join(errors))
        views = current_view_contents(hydrate_projects_data(new_projects), entries, new_glossary)

        backup_dir = DATA_DIR / "backups" / ("migrate-v2-" + now().strftime("%Y%m%dT%H%M%S%f"))
        _ensure_private_directory(backup_dir.parent)
        backup_dir.mkdir(mode=DIR_MODE, exist_ok=False)
        copied = {}
        for candidate in sorted(DATA_DIR.iterdir(), key=lambda value: value.name):
            if not candidate.is_file() or candidate.name.startswith("."):
                continue
            shutil.copy2(candidate, backup_dir / candidate.name)
            (backup_dir / candidate.name).chmod(FILE_MODE)
            copied[candidate.name] = sha256_file(candidate)
        atomic_write_json(
            backup_dir / "manifest.json",
            {
                "schema_version": 1,
                "operation": "lifeos-work-migrate-v2",
                "created_at": iso_now(),
                "source_runtime": str(DATA_DIR),
                "files": copied,
            },
        )

        legacy_paths = [DATA_DIR / name for name in (*LEGACY_FACT_FILES, *LEGACY_VIEW_FILES)]
        affected = [PROJECTS_PATH, ENTRIES_PATH, GLOSSARY_PATH, EVENTS_PATH, *views.keys(), *legacy_paths]
        try:
            recovery_dir, snapshots = _create_transaction_recovery(
                affected, ["projects", "entries", "glossary"], event
            )
        except Exception as exc:
            fail(f"迁移写入前快照创建失败，未修改 Runtime：{exc}")
        try:
            atomic_write_json(PROJECTS_PATH, new_projects)
            atomic_write_json(ENTRIES_PATH, entries)
            atomic_write_json(GLOSSARY_PATH, new_glossary)
            for path, content in views.items():
                atomic_write_text(path, content)
            for path in legacy_paths:
                if path.exists():
                    path.unlink()
            append_event(event)
            if read_json(ENTRIES_PATH) != entries:
                raise RuntimeError("entries.json 回读不一致")
        except Exception as exc:
            try:
                _restore_transaction_snapshot(snapshots)
                shutil.rmtree(recovery_dir)
            except Exception as rollback_exc:
                fail(
                    f"迁移失败且自动恢复未完成：{exc}；{rollback_exc}；"
                    f"恢复快照：{recovery_dir}；完整备份：{backup_dir}"
                )
            fail(f"迁移失败，已恢复迁移前状态：{exc}；完整备份：{backup_dir}")
        shutil.rmtree(recovery_dir, ignore_errors=True)
    print(
        f"{event['event_id']} {event['summary']}；备份：{backup_dir}"
    )
