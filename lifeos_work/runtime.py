"""Runtime I/O and the complete Work mutation transaction."""

import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path

from .config import (
    ACHIEVEMENTS_PATH,
    ACHIEVEMENTS_VIEW_PATH,
    CURRENT_SCHEMA_VERSION,
    DATA_DIR,
    EVENTS_PATH,
    GLOSSARY_PATH,
    GLOSSARY_VIEW_PATH,
    IDEAS_PATH,
    IDEAS_VIEW_PATH,
    LOCK_PATH,
    NOW_PATH,
    PROJECTS_PATH,
    PROJECTS_VIEW_PATH,
    SELF_ENTITY_ID,
    TASKS_PATH,
    WORK_ITEMS_PATH,
    WORK_ITEMS_VIEW_PATH,
)
from .errors import fail
from .model import idempotent_event, iso_now, make_event, now, source_objects
from .validation import current_data_errors
from .views import current_view_contents
from lifeos_projects import (
    ProjectManifestError,
    compact_projects_data,
    hydrate_projects_data,
    project_registry_errors,
)


DIR_MODE = 0o700
FILE_MODE = 0o600
TRANSACTION_RECOVERY_PREFIX = ".work-transaction-"


def managed_runtime_paths():
    """Return the Work files whose confidentiality is owned by this module."""

    return (
        PROJECTS_PATH,
        WORK_ITEMS_PATH,
        TASKS_PATH,
        GLOSSARY_PATH,
        IDEAS_PATH,
        ACHIEVEMENTS_PATH,
        EVENTS_PATH,
        NOW_PATH,
        PROJECTS_VIEW_PATH,
        WORK_ITEMS_VIEW_PATH,
        GLOSSARY_VIEW_PATH,
        IDEAS_VIEW_PATH,
        ACHIEVEMENTS_VIEW_PATH,
    )


def _ensure_private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    path.chmod(DIR_MODE)


def _secure_existing_runtime_permissions():
    _ensure_private_directory(DATA_DIR)
    for path in managed_runtime_paths():
        if path.exists():
            path.chmod(FILE_MODE)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _pending_transaction_directories():
    if not DATA_DIR.exists():
        return []
    return sorted(
        (
            path
            for path in DATA_DIR.iterdir()
            if path.is_dir() and path.name.startswith(TRANSACTION_RECOVERY_PREFIX)
        ),
        key=lambda path: path.name,
    )


def read_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail(f"缺少事实文件：{path}")
    except json.JSONDecodeError as exc:
        fail(f"JSON 无法解析：{path}: {exc}")


def read_events():
    if not EVENTS_PATH.exists():
        return []
    events = []
    with EVENTS_PATH.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                fail(f"事件日志第 {line_number} 行无法解析：{exc}")
    return events


def atomic_write_bytes(path, content):
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        path.chmod(FILE_MODE)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def atomic_write_text(path, content):
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path, value):
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, content)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_event(event):
    _ensure_private_directory(EVENTS_PATH.parent)
    descriptor = os.open(
        EVENTS_PATH,
        os.O_RDWR | os.O_CREAT | os.O_APPEND,
        FILE_MODE,
    )
    os.fchmod(descriptor, FILE_MODE)
    size = os.fstat(descriptor).st_size
    needs_newline = size > 0 and os.pread(descriptor, 1, size - 1) != b"\n"
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        if needs_newline:
            handle.write("\n")
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def exclusive_lock():
    _ensure_private_directory(DATA_DIR)
    descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, FILE_MODE)
    os.fchmod(descriptor, FILE_MODE)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            _secure_existing_runtime_permissions()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_not_duplicate(events, args):
    existing = idempotent_event(events, getattr(args, "idempotency_key", None))
    if existing:
        print(f"{existing['event_id']} 已处理相同 idempotency key，未重复写入")
        return True
    return False


def current_runtime_active():
    return all(
        path.exists()
        for path in [
            PROJECTS_PATH,
            WORK_ITEMS_PATH,
            TASKS_PATH,
            IDEAS_PATH,
            GLOSSARY_PATH,
            ACHIEVEMENTS_PATH,
            EVENTS_PATH,
        ]
    )


def read_current_data_unvalidated():
    if not current_runtime_active():
        fail("当前 Runtime 尚未初始化；请先运行 lifeos work init")
    data = (
        read_json(PROJECTS_PATH),
        read_json(WORK_ITEMS_PATH),
        read_json(TASKS_PATH),
        read_json(GLOSSARY_PATH),
        read_json(IDEAS_PATH),
        read_json(ACHIEVEMENTS_PATH),
    )
    versions = {value.get("schema_version") for value in data}
    if versions != {CURRENT_SCHEMA_VERSION}:
        fail(f"当前 Runtime Schema 不一致或不受支持：{sorted(versions, key=str)}")
    return data


def read_current_data():
    data = read_current_data_unvalidated()
    errors = current_data_errors(*data, read_events())
    errors.extend(project_registry_errors(data[0]))
    if errors:
        fail("当前 Runtime 不符合 Schema 1：\n- " + "\n- ".join(errors))
    try:
        projects = hydrate_projects_data(data[0])
    except ProjectManifestError as exc:
        fail(str(exc))
    return (projects, *data[1:])


def require_current_runtime():
    read_current_data()


def command_init(args):
    """Create a new Schema 1 Work runtime without importing existing data."""

    moment = iso_now()
    sources = source_objects(args.source)
    current = [
        {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": moment, "projects": []},
        {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": moment, "work_items": []},
        {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": moment, "tasks": []},
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "updated_at": moment,
            "terms": [{
                "id": SELF_ENTITY_ID,
                "name": args.self_name,
                "kind": "self",
                "aliases": args.self_alias,
                "description": "LifeOS Work Runtime 初始化时确认的本人实体。",
                "related_items": [],
                "sources": sources,
                "confirmed_at": now().date().isoformat(),
            }],
        },
        {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": moment, "ideas": []},
        {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": moment, "achievements": []},
    ]
    event = make_event(
        [],
        args,
        "runtime_initialized",
        "初始化 LifeOS Work Runtime",
        sources=args.source,
    )
    errors = current_data_errors(*current, [event])
    errors.extend(project_registry_errors(current[0]))
    if errors:
        fail("初始化数据不符合 Schema 1：" + "；".join(errors))

    managed_paths = {
        PROJECTS_PATH,
        WORK_ITEMS_PATH,
        TASKS_PATH,
        GLOSSARY_PATH,
        IDEAS_PATH,
        ACHIEVEMENTS_PATH,
        EVENTS_PATH,
        *current_view_contents(*current).keys(),
    }
    created_paths = []
    with exclusive_lock():
        existing = sorted(path.name for path in managed_paths if path.exists())
        if existing:
            fail(
                "Work Runtime 已存在或不完整，init 不会覆盖："
                + "、".join(existing)
            )
        try:
            stored = list(current)
            stored[0] = compact_projects_data(stored[0])
            for path, payload in (
                (PROJECTS_PATH, stored[0]),
                (WORK_ITEMS_PATH, stored[1]),
                (TASKS_PATH, stored[2]),
                (GLOSSARY_PATH, stored[3]),
                (IDEAS_PATH, stored[4]),
                (ACHIEVEMENTS_PATH, stored[5]),
            ):
                atomic_write_json(path, payload)
                created_paths.append(path)
            atomic_write_text(
                EVENTS_PATH,
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n",
            )
            created_paths.append(EVENTS_PATH)
            for path, content in current_view_contents(*current).items():
                atomic_write_text(path, content)
                created_paths.append(path)
            validation_errors = current_validation_errors()
            if validation_errors:
                raise RuntimeError("；".join(validation_errors))
        except Exception as exc:
            for path in reversed(created_paths):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            fail(f"Work Runtime 初始化失败，已清理本次写入：{exc}")
    print(f"已初始化 LifeOS Work Runtime：{DATA_DIR}")


def write_current_views(
    projects_data=None,
    work_items_data=None,
    tasks_data=None,
    glossary_data=None,
    ideas_data=None,
    achievements_data=None,
):
    current = read_current_data()
    values = [
        projects_data,
        work_items_data,
        tasks_data,
        glossary_data,
        ideas_data,
        achievements_data,
    ]
    resolved = [
        value if value is not None else current[index]
        for index, value in enumerate(values)
    ]
    for path, content in current_view_contents(*resolved).items():
        atomic_write_text(path, content)


CURRENT_TARGETS = {
    "projects": (PROJECTS_PATH, 0),
    "work_items": (WORK_ITEMS_PATH, 1),
    "tasks": (TASKS_PATH, 2),
    "glossary": (GLOSSARY_PATH, 3),
    "ideas": (IDEAS_PATH, 4),
    "achievements": (ACHIEVEMENTS_PATH, 5),
}


def _create_transaction_recovery(paths, targets, event):
    """Persist a pre-write snapshot so failed or interrupted writes are visible."""

    _ensure_private_directory(DATA_DIR)
    recovery_dir = Path(
        tempfile.mkdtemp(prefix=TRANSACTION_RECOVERY_PREFIX, dir=str(DATA_DIR))
    )
    recovery_dir.chmod(DIR_MODE)
    snapshots = []
    try:
        for path in sorted(set(paths), key=lambda candidate: candidate.name):
            existed = path.exists()
            content = path.read_bytes() if existed else None
            snapshots.append((path, existed, content))
            if existed:
                atomic_write_bytes(recovery_dir / path.name, content)
        atomic_write_json(
            recovery_dir / "manifest.json",
            {
                "schema_version": 1,
                "operation": "lifeos-work-transaction-recovery",
                "created_at": iso_now(),
                "source_runtime": str(DATA_DIR),
                "targets": targets,
                "event_id": event.get("event_id"),
                "files": [
                    {
                        "name": path.name,
                        "existed": existed,
                        "sha256": hashlib.sha256(content).hexdigest()
                        if content is not None
                        else None,
                    }
                    for path, existed, content in snapshots
                ],
            },
        )
    except Exception:
        shutil.rmtree(recovery_dir, ignore_errors=True)
        raise
    return recovery_dir, snapshots


def _restore_transaction_snapshot(snapshots):
    for path, existed, content in snapshots:
        if existed:
            atomic_write_bytes(path, content)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


class WorkTransaction:
    """A locked, validated snapshot for one ordinary Work mutation."""

    def __init__(self, args, current, events):
        self.args = args
        self.current = current
        self.events = events
        self.original = deepcopy(current)

    def data(self, target):
        try:
            _path, index = CURRENT_TARGETS[target]
        except KeyError:
            raise ValueError(f"未知 Work transaction target：{target}") from None
        return self.current[index]

    def idempotent_result(self):
        return ensure_not_duplicate(self.events, self.args)

    def commit(self, target, event):
        targets = (target,) if isinstance(target, str) else tuple(target)
        unknown = [value for value in targets if value not in CURRENT_TARGETS]
        if unknown:
            raise ValueError(f"未知 Work transaction target：{unknown[0]}")
        expected_targets = [
            name for name in CURRENT_TARGETS if name in targets
        ]
        changed_targets = [
            name
            for name, (_candidate_path, candidate_index) in CURRENT_TARGETS.items()
            if self.current[candidate_index] != self.original[candidate_index]
        ]
        if changed_targets != expected_targets:
            fail(
                "Work transaction 必须且只能修改声明的事实源："
                f"targets={expected_targets}，changed={changed_targets}"
            )
        stored_current = list(self.current)
        stored_current[0] = compact_projects_data(stored_current[0])
        errors = current_data_errors(*stored_current, [*self.events, event])
        errors.extend(project_registry_errors(stored_current[0]))
        if errors:
            fail("写入结果校验失败：" + "；".join(errors))
        backup_dir = None
        if len(expected_targets) > 1:
            backup_name = "transaction-" + now().strftime("%Y%m%dT%H%M%S%f")
            backup_dir = DATA_DIR / "backups" / backup_name
            _ensure_private_directory(backup_dir.parent)
            backup_dir.mkdir(mode=DIR_MODE, exist_ok=False)
            copied = {}
            for candidate in sorted(DATA_DIR.iterdir(), key=lambda value: value.name):
                if not candidate.is_file() or candidate == LOCK_PATH:
                    continue
                shutil.copy2(candidate, backup_dir / candidate.name)
                (backup_dir / candidate.name).chmod(FILE_MODE)
                copied[candidate.name] = sha256_file(candidate)
            atomic_write_json(
                backup_dir / "manifest.json",
                {
                    "schema_version": 1,
                    "operation": "lifeos-multi-target-transaction",
                    "created_at": iso_now(),
                    "source_runtime": str(DATA_DIR),
                    "targets": expected_targets,
                    "files": copied,
                },
            )
        view_contents = current_view_contents(*self.current)
        affected_paths = [
            *(CURRENT_TARGETS[name][0] for name in expected_targets),
            *view_contents.keys(),
            EVENTS_PATH,
        ]
        try:
            recovery_dir, snapshots = _create_transaction_recovery(
                affected_paths, expected_targets, event
            )
        except Exception as exc:
            fail(f"Work 写入前快照创建失败，未修改 Runtime：{exc}")
        try:
            for name in expected_targets:
                path, index = CURRENT_TARGETS[name]
                atomic_write_json(path, stored_current[index])
            for view_path, content in view_contents.items():
                atomic_write_text(view_path, content)
            append_event(event)
        except Exception as exc:
            try:
                _restore_transaction_snapshot(snapshots)
                shutil.rmtree(recovery_dir)
            except Exception as rollback_exc:
                fail(
                    "Work 写入失败且自动恢复未完成；"
                    f"恢复快照保留于：{recovery_dir}；"
                    f"写入错误：{exc}；恢复错误：{rollback_exc}"
                )
            fail(f"Work 写入失败，已恢复事务前状态：{exc}")
        try:
            shutil.rmtree(recovery_dir)
        except Exception as exc:
            fail(
                "Work 写入已完成，但事务恢复标记清理失败；"
                f"请核验后处理：{recovery_dir}；错误：{exc}"
            )
        self.events.append(event)
        suffix = f"；备份：{backup_dir}" if backup_dir else ""
        print(f"{event['event_id']} {event['summary']}{suffix}")


@contextmanager
def transaction(args):
    """Hold the Work lock for a validated single-target mutation."""

    with exclusive_lock():
        pending = _pending_transaction_directories()
        if pending:
            fail(
                "发现未完成的 Work 事务，已拒绝继续写入："
                + "、".join(str(path) for path in pending)
            )
        stored = list(read_current_data_unvalidated())
        events = read_events()
        errors = current_data_errors(*stored, events)
        errors.extend(project_registry_errors(stored[0]))
        if errors:
            fail("当前 Runtime 不符合 Schema 1：\n- " + "\n- ".join(errors))
        current = list(stored)
        current[0] = hydrate_projects_data(stored[0])
        yield WorkTransaction(args, current, events)


def current_validation_errors(check_views=True):
    errors = []
    pending = _pending_transaction_directories()
    if pending:
        errors.append(
            "发现未完成的 Work 事务："
            + "、".join(path.name for path in pending)
        )
    (
        projects,
        work_items,
        tasks,
        glossary,
        ideas,
        achievements,
    ) = read_current_data_unvalidated()
    errors.extend(current_data_errors(
        projects,
        work_items,
        tasks,
        glossary,
        ideas,
        achievements,
        read_events(),
    ))
    errors.extend(project_registry_errors(projects))
    if DATA_DIR.exists() and _mode(DATA_DIR) != DIR_MODE:
        errors.append(
            f"Runtime 目录权限必须为 0700，当前为 {_mode(DATA_DIR):04o}：{DATA_DIR}"
        )
    if LOCK_PATH.exists() and _mode(LOCK_PATH) != FILE_MODE:
        errors.append(
            f"Work 锁文件权限必须为 0600，当前为 {_mode(LOCK_PATH):04o}：{LOCK_PATH}"
        )
    for path in managed_runtime_paths():
        if path.exists() and _mode(path) != FILE_MODE:
            errors.append(
                f"Work 文件权限必须为 0600，当前为 {_mode(path):04o}：{path}"
            )
    hydrated_projects = projects
    try:
        hydrated_projects = hydrate_projects_data(projects)
    except ProjectManifestError as exc:
        errors.append(str(exc))
    check_views = check_views and not errors
    if check_views:
        for path, expected in current_view_contents(
            hydrated_projects, work_items, tasks, glossary, ideas, achievements
        ).items():
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                errors.append(
                    f"{path.name} 与事实源不一致，请运行 lifeos work refresh"
                )
    return errors


def command_validate(_args):
    errors = current_validation_errors()
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print(
        "OK: 项目引用、事项、待办、闪念、成果胶囊、名词、"
        "内部审计与派生视图一致"
    )


def command_refresh(_args):
    with exclusive_lock():
        write_current_views()
    print(
        "已重建 now.md、projects.md、work-items.md、ideas.md、"
        "achievements.md 和 glossary.md"
    )


__all__ = [
    "ACHIEVEMENTS_PATH",
    "ACHIEVEMENTS_VIEW_PATH",
    "CURRENT_SCHEMA_VERSION",
    "DATA_DIR",
    "EVENTS_PATH",
    "GLOSSARY_PATH",
    "GLOSSARY_VIEW_PATH",
    "IDEAS_PATH",
    "IDEAS_VIEW_PATH",
    "LOCK_PATH",
    "NOW_PATH",
    "PROJECTS_PATH",
    "PROJECTS_VIEW_PATH",
    "TASKS_PATH",
    "WORK_ITEMS_PATH",
    "WORK_ITEMS_VIEW_PATH",
    "WorkTransaction",
    "append_event",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "command_refresh",
    "command_init",
    "command_validate",
    "current_runtime_active",
    "current_view_contents",
    "current_validation_errors",
    "ensure_not_duplicate",
    "exclusive_lock",
    "read_current_data",
    "read_current_data_unvalidated",
    "read_events",
    "read_json",
    "require_current_runtime",
    "sha256_file",
    "transaction",
    "write_current_views",
]
