"""Filesystem layout, frontmatter and state rules for daily reports.

The frontmatter is a deliberately small subset of YAML -- flat scalars and
lists of strings -- so it can be parsed by this module without a third-party
dependency, and so a malformed report fails loudly instead of being half
understood.  Everything a machine needs lives up here; the body below stays
plain prose with no identifiers in it.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import stat
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
DAILY_DIRNAME = "daily"
DIR_MODE = 0o700
FILE_MODE = 0o600
STATUSES = ("draft", "confirmed")
GIT_SCAN_ID = re.compile(r"^GITSCAN-[0-9]{8}T[0-9]{6}[+-][0-9]{4}-[0-9a-f]{8}$")
GIT_COMMIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*@[0-9a-f]{40}$")

SCALAR_KEYS = (
    "day",
    "status",
    "generated_at",
    "confirmed_at",
    "window",
    "git_scan_id",
)
COUNT_KEYS = (
    "sessions_activities",
    "sessions_partial",
    "sessions_interrupted",
    "sessions_omitted",
    "work_events",
    "user_notes",
    "unresolved",
    "git_commits",
)
LIST_KEYS = ("activity_ids", "work_event_ids", "git_commit_ids")
FIELD_ORDER = SCALAR_KEYS + COUNT_KEYS + LIST_KEYS
REQUIRED_KEYS = (
    "day",
    "status",
    "generated_at",
    "window",
    "sessions_activities",
    "sessions_partial",
    "sessions_interrupted",
    "sessions_omitted",
    "work_events",
    "user_notes",
    "unresolved",
    "activity_ids",
    "work_event_ids",
)

DAY_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
SUPERSEDED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.superseded-[^/]+\.md$")
KEY_LINE = re.compile(r"^([a-z][a-z0-9_]*):[ \t]*(.*)$")
LIST_ITEM = re.compile(r"^[ \t]+-[ \t]+(.*)$")

SKELETON_BODY = (
    "<!-- 正文由 lifeos skill 的 Daily 分支写入：概览、按项目分组的事实、推断区块、"
    "可能要进 work 的候选。 -->\n"
)


class ReportError(RuntimeError):
    """Raised when a report cannot be located, parsed or safely replaced."""


def daily_dir(reports_root: Path) -> Path:
    return reports_root / DAILY_DIRNAME


def report_path(reports_root: Path, day: date) -> Path:
    return daily_dir(reports_root) / f"{day.isoformat()}.md"


@contextlib.contextmanager
def locked(reports_root: Path) -> Iterator[None]:
    """Serialize report mutations without placing the lock in a replaceable tree."""

    reports_root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = reports_root.parent / ".lifeos-reports.lock"
    lock_path.touch(exist_ok=True, mode=FILE_MODE)
    os.chmod(lock_path, FILE_MODE)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def superseded_paths(reports_root: Path, day: date) -> List[Path]:
    directory = daily_dir(reports_root)
    if not directory.is_dir():
        return []
    prefix = f"{day.isoformat()}.superseded-"
    return sorted(
        path for path in directory.glob(f"{prefix}*.md") if path.is_file()
    )


def day_window(day: date) -> Tuple[datetime, datetime]:
    """The natural day in Asia/Shanghai.

    The day boundary is a structural rule, so it is decided here once rather
    than restated in every consumer that happens to need a window.
    """

    start = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    return start, start + timedelta(days=1)


def window_text(day: date) -> str:
    start, end = day_window(day)
    return f"{start.isoformat()}/{end.isoformat()}"


def ensure_daily_dir(reports_root: Path) -> Path:
    directory = daily_dir(reports_root)
    directory.mkdir(parents=True, exist_ok=True)
    for path in (reports_root, directory):
        os.chmod(path, DIR_MODE)
    return directory


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ReportError("缺少 frontmatter 起始行 ---")
    meta: Dict[str, Any] = {}
    last_key: Optional[str] = None
    for position, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            body = "\n".join(lines[position:])
            return meta, body.lstrip("\n")
        if not line.strip():
            continue
        item = LIST_ITEM.match(line)
        if item:
            if last_key is None:
                raise ReportError(f"第 {position} 行的列表项没有对应字段")
            if meta.get(last_key) is None:
                meta[last_key] = []
            if not isinstance(meta[last_key], list):
                raise ReportError(f"字段 {last_key} 既有标量值又有列表项")
            meta[last_key].append(item.group(1).strip())
            continue
        matched = KEY_LINE.match(line)
        if not matched:
            raise ReportError(f"第 {position} 行不是受支持的 frontmatter 形态")
        key, raw = matched.group(1), matched.group(2).strip()
        if key in meta:
            raise ReportError(f"字段 {key} 重复")
        if raw == "[]":
            meta[key] = []
        elif raw == "":
            meta[key] = None
        else:
            meta[key] = raw
        last_key = key
    raise ReportError("frontmatter 没有闭合的 --- 行")


def render_frontmatter(meta: Dict[str, Any]) -> str:
    lines = ["---"]
    for key in FIELD_ORDER:
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    for key in sorted(set(meta) - set(FIELD_ORDER)):
        lines.append(f"{key}: {meta[key]}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_report(meta: Dict[str, Any], body: str) -> str:
    return render_frontmatter(meta) + "\n" + body.lstrip("\n")


def read_report(path: Path) -> Tuple[Dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReportError(f"日报不存在：{path}")
    return parse_frontmatter(text)


def write_report(path: Path, meta: Dict[str, Any], body: str) -> None:
    """Atomic replace that never widens the file's permissions.

    ``mkstemp`` already creates the temporary file 0600 and ``os.replace``
    keeps the mode of the file it moves, so the report cannot briefly exist
    world-readable between write and rename.
    """

    content = render_report(meta, body)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, FILE_MODE)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def skeleton(day: date, generated_at: str) -> Dict[str, Any]:
    return {
        "day": day.isoformat(),
        "status": "draft",
        "generated_at": generated_at,
        "confirmed_at": None,
        "window": window_text(day),
        "sessions_activities": 0,
        "sessions_partial": 0,
        "sessions_interrupted": 0,
        "sessions_omitted": 0,
        "work_events": 0,
        "user_notes": 0,
        "unresolved": 0,
        "git_scan_id": None,
        "git_commits": 0,
        "activity_ids": [],
        "work_event_ids": [],
        "git_commit_ids": [],
    }


def now_text() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def superseded_name(day: date) -> str:
    stamp = datetime.now(TIMEZONE).strftime("%Y%m%dT%H%M%S%z")
    return f"{day.isoformat()}.superseded-{stamp}.md"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def check_report(path: Path, *, canonical: bool = True) -> List[str]:
    """Everything about a report that a machine can actually judge."""

    problems: List[str] = []
    matched = (DAY_NAME if canonical else SUPERSEDED_NAME).match(path.name)
    if not matched:
        return [f"文件名不符合日报命名：{path.name}"]
    filename_day = matched.group(1)
    try:
        meta, body = read_report(path)
    except ReportError as exc:
        return [str(exc)]

    if not canonical:
        if meta.get("day") != filename_day:
            problems.append(f"day 与文件名不一致：{meta.get('day')} != {filename_day}")
        return problems

    for key in REQUIRED_KEYS:
        if meta.get(key) in (None, ""):
            problems.append(f"缺少必填字段 {key}")
    if meta.get("day") != filename_day:
        problems.append(f"day 与文件名不一致：{meta.get('day')} != {filename_day}")
    status = meta.get("status")
    if status not in STATUSES:
        problems.append(f"status 非法：{status}")
    if status == "confirmed" and not meta.get("confirmed_at"):
        problems.append("confirmed 状态缺少 confirmed_at")
    if status == "draft" and meta.get("confirmed_at"):
        problems.append("draft 状态不应带 confirmed_at")
    try:
        expected_window = window_text(date.fromisoformat(filename_day))
    except ValueError:
        expected_window = None
    if expected_window and meta.get("window") != expected_window:
        problems.append(f"window 与自然日不一致，应为 {expected_window}")
    for key in COUNT_KEYS:
        value = meta.get(key)
        if value is None:
            continue
        if not (isinstance(value, str) and value.isdigit()):
            problems.append(f"{key} 必须是非负整数")
    for key in LIST_KEYS:
        if key in meta and not isinstance(meta[key], list):
            problems.append(f"{key} 必须是列表")
    activity_ids = meta.get("activity_ids")
    work_event_ids = meta.get("work_event_ids")
    if isinstance(activity_ids, list):
        activity_key_values = [value if isinstance(value, str) else repr(value) for value in activity_ids]
        if len(activity_key_values) != len(set(activity_key_values)):
            problems.append("activity_ids 不能重复")
        for value in activity_ids:
            if not isinstance(value, str) or not value.startswith("ACT-"):
                problems.append(f"activity_ids 前缀非法：{value}")
    if isinstance(work_event_ids, list):
        event_key_values = [value if isinstance(value, str) else repr(value) for value in work_event_ids]
        if len(event_key_values) != len(set(event_key_values)):
            problems.append("work_event_ids 不能重复")
        for value in work_event_ids:
            if not isinstance(value, str) or not value.startswith("EVT-"):
                problems.append(f"work_event_ids 前缀非法：{value}")
    git_scan_id = meta.get("git_scan_id")
    if git_scan_id not in (None, "") and (
        not isinstance(git_scan_id, str) or not GIT_SCAN_ID.fullmatch(git_scan_id)
    ):
        problems.append(f"git_scan_id 非法：{git_scan_id}")
    git_commit_ids = meta.get("git_commit_ids")
    if isinstance(git_commit_ids, list):
        git_commit_key_values = [value if isinstance(value, str) else repr(value) for value in git_commit_ids]
        if len(git_commit_key_values) != len(set(git_commit_key_values)):
            problems.append("git_commit_ids 不能重复")
        for value in git_commit_ids:
            if not isinstance(value, str) or not GIT_COMMIT_ID.fullmatch(value):
                problems.append(f"git_commit_ids 格式非法：{value}")
    if isinstance(activity_ids, list) and isinstance(meta.get("sessions_activities"), str) and meta["sessions_activities"].isdigit():
        if int(meta["sessions_activities"]) != len(set(activity_key_values)):
            problems.append("sessions_activities 必须等于唯一 activity_ids 数量")
    if isinstance(work_event_ids, list) and isinstance(meta.get("work_events"), str) and meta["work_events"].isdigit():
        if int(meta["work_events"]) != len(set(event_key_values)):
            problems.append("work_events 必须等于唯一 work_event_ids 数量")
    if isinstance(git_commit_ids, list) and isinstance(meta.get("git_commits"), str) and meta["git_commits"].isdigit():
        if int(meta["git_commits"]) != len(set(git_commit_key_values)):
            problems.append("git_commits 必须等于唯一 git_commit_ids 数量")
    if not body.strip():
        problems.append("正文为空")
    if _mode(path) != FILE_MODE:
        problems.append(f"文件权限应为 {oct(FILE_MODE)}，实际 {oct(_mode(path))}")
    return problems


def check_directory(reports_root: Path) -> List[Tuple[Path, str]]:
    directory = daily_dir(reports_root)
    if not directory.is_dir():
        return []
    findings: List[Tuple[Path, str]] = []
    for path in (reports_root, directory):
        if _mode(path) != DIR_MODE:
            findings.append(
                (path, f"目录权限应为 {oct(DIR_MODE)}，实际 {oct(_mode(path))}")
            )
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            findings.append((path, "日报目录下不应出现子目录"))
            continue
        if path.name.startswith("."):
            continue
        if DAY_NAME.match(path.name):
            findings.extend((path, problem) for problem in check_report(path))
        elif SUPERSEDED_NAME.match(path.name):
            findings.extend(
                (path, problem) for problem in check_report(path, canonical=False)
            )
        else:
            findings.append((path, "文件名既不是日报也不是 superseded 快照"))
    return findings


def list_reports(reports_root: Path) -> List[Dict[str, Any]]:
    directory = daily_dir(reports_root)
    if not directory.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        matched = DAY_NAME.match(path.name)
        if not matched:
            continue
        try:
            meta, _body = read_report(path)
        except ReportError as exc:
            rows.append({"day": matched.group(1), "path": str(path), "error": str(exc)})
            continue
        row = {"day": matched.group(1), "path": str(path)}
        for key in ("status", "generated_at", "confirmed_at", *COUNT_KEYS):
            row[key] = meta.get(key)
        row["superseded"] = len(superseded_paths(reports_root, date.fromisoformat(row["day"])))
        rows.append(row)
    return rows


def missing_days(rows: List[Dict[str, Any]], window_from: date, window_to: date) -> List[str]:
    """Days in ``[from, to)`` that have no report at all.

    A missing file is not the same as an empty day: an empty day still gets a
    report saying so, so absence here means the day was never generated.
    """

    present = {row["day"] for row in rows}
    cursor = window_from
    missing = []
    while cursor < window_to:
        if cursor.isoformat() not in present:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return missing
