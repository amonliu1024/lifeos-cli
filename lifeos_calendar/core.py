"""Calendar window handling and deterministic scan semantics."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import CALENDAR_SCHEMA_VERSION
from .client import CalendarClient, CalendarClientError, attendee_names


TIMEZONE = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
SEARCH_LIMIT = 10          # dws calendar search returns at most this many events
MIN_SPLIT_WINDOW = timedelta(minutes=15)
EVENT_TYPES = {"Single", "RecurringMaster", "Exception"}


class CalendarError(RuntimeError):
    """Raised for invalid calendar scan input or an unusable source answer."""


def _parse(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    if len(text) == 10:
        try:
            return datetime.combine(date.fromisoformat(text), time.min, tzinfo=TIMEZONE)
        except ValueError as exc:
            raise CalendarError(f"{field} 不是有效日期：{value}") from exc
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarError(f"{field} 不是有效 ISO 时间：{value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalendarError(f"{field} 必须带时区偏移")
    return parsed


def local_text(value: datetime) -> str:
    return value.astimezone(TIMEZONE).isoformat(timespec="seconds")


def ms_to_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return local_text(datetime.fromtimestamp(float(value) / 1000, tz=UTC))
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class TimeWindow:
    from_utc: datetime
    to_utc: datetime

    @classmethod
    def from_values(cls, from_value: str, to_value: str) -> "TimeWindow":
        start = _parse(from_value, "from").astimezone(UTC)
        end = _parse(to_value, "to").astimezone(UTC)
        if start >= end:
            raise CalendarError("日历扫描窗口必须满足 from < to")
        return cls(start, end)

    def to_dict(self) -> Dict[str, str]:
        return {"from": local_text(self.from_utc), "to": local_text(self.to_utc)}


def instance_id(event_id: str, dtstart: str) -> str:
    digest = hashlib.sha256(f"{event_id}|{dtstart}".encode("utf-8")).hexdigest()
    return "CAL-" + digest[:12]


class CalendarService:
    """Own one deterministic scan of a window; persistence stays in the store."""

    def __init__(self, client: CalendarClient):
        self.client = client

    def scan(self, window: TimeWindow) -> Dict[str, Any]:
        warnings: List[str] = []
        try:
            raw_events, queries = self._search_all(window, warnings)
        except CalendarClientError as exc:
            raise CalendarError(f"{exc.kind}：{exc}") from exc
        events: Dict[str, Dict[str, Any]] = {}
        details: Dict[str, Mapping[str, Any]] = {}
        detail_failures = 0
        for raw in raw_events:
            event_id = str(raw.get("id") or "").strip()
            if not event_id:
                warnings.append("event_id_missing")
                continue
            if event_id not in details:
                try:
                    details[event_id] = self.client.info(event_id)
                except CalendarClientError as exc:
                    details[event_id] = {}
                    detail_failures += 1
                    warnings.append(f"detail_unavailable:{exc.kind}")
            detail = details[event_id]
            summary = str(raw.get("name") or detail.get("summary") or "").strip()
            event_type = str(raw.get("type") or detail.get("eventType") or "").strip() or "Unknown"
            if event_type not in EVENT_TYPES:
                warnings.append(f"unknown_event_type:{event_type}")
            series_key = str(detail.get("icalendarUid") or "").strip() or event_id
            names = attendee_names(detail)
            # search 给出窗口内的实例时间；没有时退回详情里的主项时间。
            ranges = raw.get("time_range") or [{"start_time": detail.get("dtstart"), "end_time": detail.get("dtend")}]
            for item in ranges:
                if not isinstance(item, Mapping):
                    continue
                start_text = ms_to_text(item.get("start_time"))
                end_text = ms_to_text(item.get("end_time"))
                if not start_text or not end_text:
                    warnings.append("event_time_missing")
                    continue
                record = {
                    "instance_id": instance_id(event_id, start_text),
                    "event_id": event_id,
                    "series_key": series_key,
                    "summary": summary,
                    "dtstart": start_text,
                    "dtend": end_text,
                    "type": event_type,
                    "attendees": names,
                }
                events.setdefault(record["instance_id"], record)
        ordered = sorted(events.values(), key=lambda item: (item["dtstart"], item["dtend"], item["summary"]))
        status = "partial" if detail_failures or "range_truncated" in warnings else "complete"
        return {
            "schema_version": CALENDAR_SCHEMA_VERSION,
            "status": status,
            "window": window.to_dict(),
            "evidence_level": "supporting",
            "events": ordered,
            "warnings": sorted(set(warnings)),
            "summary": {
                "events": len(ordered),
                "queries": queries,
                "detail_failures": detail_failures,
            },
        }

    def _search_all(self, window: TimeWindow, warnings: List[str]) -> tuple[List[Mapping[str, Any]], int]:
        pending = [(window.from_utc, window.to_utc)]
        collected: List[Mapping[str, Any]] = []
        queries = 0
        while pending:
            start, end = pending.pop(0)
            queries += 1
            page = list(self.client.search(int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
            if len(page) >= SEARCH_LIMIT and end - start > MIN_SPLIT_WINDOW:
                midpoint = start + (end - start) / 2
                pending[0:0] = [(start, midpoint), (midpoint, end)]
                continue
            if len(page) >= SEARCH_LIMIT:
                warnings.append("range_truncated")
            collected.extend(page)
        return collected, queries
