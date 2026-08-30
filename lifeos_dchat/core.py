"""DChat selection, bounded window splitting and scan result semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from zoneinfo import ZoneInfo

from .client import DChatClient, DChatClientError
from .model import (
    DCHAT_SCHEMA_VERSION,
    conversation_id,
    conversation_type,
    latest_activity_at,
    message_key,
    occurred_at,
    scope_for,
    stable_component,
    timestamp_field_types,
)


TIMEZONE = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
MAX_SOURCE_WINDOW = timedelta(days=30)
MIN_SPLIT_WINDOW = timedelta(seconds=1)
DEFAULT_LIMIT = 500
MAX_MISSING_TIME_DETAILS = 8


class DChatError(RuntimeError):
    """Raised for invalid DChat scan input or an unusable inventory."""


def _parse(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    if len(text) == 10:
        try:
            return datetime.combine(date.fromisoformat(text), time.min, tzinfo=TIMEZONE)
        except ValueError as exc:
            raise DChatError(f"{field} 不是有效日期：{value}") from exc
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DChatError(f"{field} 不是有效 ISO 时间：{value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DChatError(f"{field} 必须带时区偏移")
    return parsed


def _local(value: datetime) -> str:
    return value.astimezone(TIMEZONE).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TimeWindow:
    from_utc: datetime
    to_utc: datetime

    @classmethod
    def from_values(cls, from_value: str, to_value: str) -> "TimeWindow":
        start = _parse(from_value, "from").astimezone(UTC)
        end = _parse(to_value, "to").astimezone(UTC)
        if start >= end:
            raise DChatError("DChat 扫描窗口必须满足 from < to")
        return cls(start, end)

    def to_dict(self) -> Dict[str, str]:
        return {"from": _local(self.from_utc), "to": _local(self.to_utc)}

    def query_bounds(self) -> tuple[str, str]:
        return (
            self.from_utc.isoformat(timespec="seconds"),
            self.to_utc.isoformat(timespec="seconds"),
        )


class DChatService:
    """Own one deterministic scan; persistence remains the store's job."""

    def __init__(self, client: DChatClient, project_group_ids: Iterable[str], *, limit: int = DEFAULT_LIMIT):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 2:
            raise DChatError("limit 必须是大于 1 的整数")
        self.client = client
        self.project_group_ids = {
            str(value).strip() for value in project_group_ids if str(value).strip()
        }
        self.limit = limit

    def scan(self, window: TimeWindow) -> Dict[str, Any]:
        try:
            inventory = self.client.list_chats()
        except DChatClientError as exc:
            raise DChatError(f"{exc.kind}：{exc}") from exc
        rows: List[Dict[str, Any]] = []
        total_messages = 0
        total_revisions = 0
        body_queries = 0
        skipped_before_window = 0
        for raw_chat in inventory:
            if not isinstance(raw_chat, Mapping):
                continue
            cid = conversation_id(raw_chat)
            if not cid:
                rows.append({
                    "conversation_id": None, "type": conversation_type(raw_chat),
                    "scope": "excluded", "status": "partial",
                    "warnings": ["conversation_id_missing"], "metadata": dict(raw_chat), "messages": [],
                })
                continue
            scope, warnings = scope_for(raw_chat, self.project_group_ids)
            row: Dict[str, Any] = {
                "conversation_id": cid,
                "type": conversation_type(raw_chat),
                "scope": scope,
                "status": "complete",
                "warnings": list(warnings),
                "metadata": dict(raw_chat),
                "messages": [],
                "windows": [],
            }
            if warnings:
                row["status"] = "partial"
            if scope == "collect_body":
                latest = latest_activity_at(raw_chat)
                if latest is not None and latest < window.from_utc:
                    row["windows"].append({
                        **window.to_dict(),
                        "returned": 0,
                        "queried": False,
                        "strategy": "inventory_latest_ts",
                    })
                    skipped_before_window += 1
                    rows.append(row)
                    continue
                try:
                    body_queries += 1
                    messages, windows, read_warnings = self._read_window(cid, window)
                    row["messages"] = messages
                    row["windows"] = windows
                    row["warnings"].extend(read_warnings)
                    if read_warnings:
                        row["status"] = "partial"
                    total_messages += len(messages)
                    total_revisions += len(messages)
                except DChatClientError as exc:
                    row["status"] = "failed" if exc.kind in {"history_forbidden", "conversation_unavailable"} else "partial"
                    row["warnings"].append(exc.kind)
            rows.append(row)
        failed = sum(1 for row in rows if row["status"] == "failed")
        partial = sum(1 for row in rows if row["status"] == "partial")
        status = "failed" if rows and failed == len(rows) else "partial" if failed or partial else "complete"
        return {
            "schema_version": DCHAT_SCHEMA_VERSION,
            "status": status,
            "window": window.to_dict(),
            "evidence_level": "supporting",
            "conversations": rows,
            "summary": {
                "conversations_total": len(rows),
                "collect_body": sum(row["scope"] == "collect_body" for row in rows),
                "metadata_only": sum(row["scope"] == "metadata_only" for row in rows),
                "excluded": sum(row["scope"] == "excluded" for row in rows),
                "failed": failed,
                "partial": partial,
                "messages": total_messages,
                "message_observations": total_revisions,
                "body_queries": body_queries,
                "skipped_before_window": skipped_before_window,
            },
        }

    def _read_window(self, cid: str, window: TimeWindow) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        pending: List[tuple[datetime, datetime]] = []
        cursor = window.from_utc
        while cursor < window.to_utc:
            end = min(cursor + MAX_SOURCE_WINDOW, window.to_utc)
            pending.append((cursor, end))
            cursor = end
        messages: Dict[str, Dict[str, Any]] = {}
        windows: List[Dict[str, Any]] = []
        warnings: List[str] = []
        missing_time_count = 0
        while pending:
            start, end = pending.pop(0)
            start_text, end_text = _local(start), _local(end)
            page = list(self.client.dump_messages(cid, start_text, end_text, self.limit))
            windows.append({"from": start_text, "to": end_text, "returned": len(page)})
            if len(page) >= self.limit:
                if end - start <= MIN_SPLIT_WINDOW:
                    warnings.append("range_truncated")
                    continue
                midpoint = start + (end - start) / 2
                pending[0:0] = [(start, midpoint), (midpoint, end)]
                continue
            for raw in page:
                key = message_key(raw)
                when = occurred_at(raw)
                if not key:
                    warnings.append("message_key_missing")
                    continue
                if not when:
                    missing_time_count += 1
                    if missing_time_count <= MAX_MISSING_TIME_DETAILS:
                        warnings.append(
                            "message_time_missing:"
                            f"message_ref={stable_component(key)[:12]}:"
                            f"timestamp_fields={timestamp_field_types(raw)}"
                        )
                messages[key] = {"message_key": key, "occurred_at": when, "payload": dict(raw)}
        if missing_time_count > MAX_MISSING_TIME_DETAILS:
            warnings.append(
                "message_time_missing:"
                f"omitted={missing_time_count - MAX_MISSING_TIME_DETAILS}"
            )
        ordered = sorted(messages.values(), key=lambda item: (item.get("occurred_at") or "", item["message_key"]))
        return ordered, windows, sorted(set(warnings))
