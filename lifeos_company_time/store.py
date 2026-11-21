"""Read and update the one private company time ledger."""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict

from lifeos_work.config import TIMEZONE
from lifeos_work.runtime import atomic_write_json


FILENAME = "company-time.json"
_MINUTE_PATTERN = r"[0-9]{2}:[0-9]{2}"
_TIMESTAMP_PATTERN = rf"[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[ T]{_MINUTE_PATTERN}(?:[+-][0-9]{{2}}:[0-9]{{2}})?"


class CompanyTimeError(ValueError):
    """An invalid date, time or stored record."""


class Record(TypedDict):
    started_at: str | None
    ended_at: str | None


Records = dict[str, Record]


def _day(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("日期须为 YYYY-MM-DD")
    return parsed


def _timestamp(value: str, day: str) -> str:
    if re.fullmatch(_MINUTE_PATTERN, value):
        value = f"{day}T{value}"
    if not re.fullmatch(_TIMESTAMP_PATTERN, value):
        raise CompanyTimeError("时间请用 HH:MM 或 YYYY-MM-DD HH:MM")
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TIMEZONE)
        return moment.astimezone(TIMEZONE).isoformat(timespec="minutes")
    except (ValueError, OverflowError) as exc:
        raise CompanyTimeError(f"时间无效：{value}") from exc


def _validate_record(day: str, record: Record) -> None:
    start = record["started_at"]
    end = record["ended_at"]
    if not (start or end):
        raise CompanyTimeError("至少要有一项上班或下班时间")
    if start and start[:10] != day:
        raise CompanyTimeError("上班日期须与记录日期一致")
    if end and (end[:10] < day or (start and end < start)):
        raise CompanyTimeError("下班时间早于上班；跨天请填写实际下班日期")


def read_records(data_dir: Path) -> Records:
    path = data_dir / FILENAME
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        records = json.loads(contents)
        if not isinstance(records, dict):
            raise ValueError("上班簿须按归属日期存储")
        for day, record in records.items():
            _day(day)
            if not isinstance(record, dict) or set(record) != {"started_at", "ended_at"}:
                raise ValueError(f"{day} 的记录字段不完整")
            for value in record.values():
                if value is not None and (not isinstance(value, str) or _timestamp(value, day) != value):
                    raise ValueError(f"{day} 的时间格式无效")
            _validate_record(day, record)
    except ValueError as exc:
        raise CompanyTimeError(f"无法读取上班簿：{exc}") from exc
    return records


def set_record(data_dir: Path, day: str, *, start: str | None, end: str | None) -> Record:
    try:
        _day(day)
    except ValueError as exc:
        raise CompanyTimeError("日期请用有效的 YYYY-MM-DD") from exc
    if start is None and end is None:
        raise CompanyTimeError("至少填写 --start 或 --end")

    started_at = _timestamp(start, day) if start is not None else None
    ended_at = _timestamp(end, day) if end is not None else None
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(data_dir / ".lifeos-company-time.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read_records(data_dir)
        previous = records.get(day)
        record: Record = {
            "started_at": started_at if start is not None else (previous or {}).get("started_at"),
            "ended_at": ended_at if end is not None else (previous or {}).get("ended_at"),
        }
        _validate_record(day, record)
        if record != previous:
            records[day] = record
            atomic_write_json(data_dir / FILENAME, dict(sorted(records.items())))
        return record
