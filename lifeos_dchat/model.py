"""Validated DChat inventory and message helpers.

The upstream DChat payload is deliberately retained as an opaque object.  This
module extracts only the stable fields LifeOS needs for scope and indexing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


DCHAT_SCHEMA_VERSION = 1
DIRECT_TYPES = {"p2p", "extp2p"}
GROUP_TYPES = {"channel", "extchannel"}
EXCLUDED_TYPES = {"official", "p2bot", "p2ai"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_component(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def conversation_id(chat: Mapping[str, Any]) -> Optional[str]:
    for key in ("vchannel_id", "vid", "chat_id", "id"):
        value = chat.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def conversation_type(chat: Mapping[str, Any]) -> str:
    return str(chat.get("type") or chat.get("chat_type") or "").strip().lower()


def latest_activity_at(chat: Mapping[str, Any]) -> Optional[datetime]:
    """Return the inventory's latest message time, or None when it is unusable."""

    value = chat.get("latest_ts")
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value)
        else:
            text = str(value).strip()
            try:
                seconds = float(text)
            except ValueError:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return None
                return parsed.astimezone(timezone.utc)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def message_key(message: Mapping[str, Any]) -> Optional[str]:
    for key in ("message_key", "key", "msg_key", "id"):
        value = message.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def occurred_at(message: Mapping[str, Any]) -> Optional[str]:
    for key in (
        "occurred_at", "timestamp", "ts", "created_at", "created_ts", "create_time"
    ):
        value = message.get(key)
        if value is None or value == "":
            continue
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                seconds = float(value)
                if seconds > 10_000_000_000:
                    seconds /= 1000
                return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                continue
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (ValueError, TypeError, OSError, OverflowError):
            continue
    return None


def timestamp_field_types(message: Mapping[str, Any]) -> str:
    """Describe timestamp-like field shapes without retaining their values."""

    fields = []
    for raw_key, value in message.items():
        key = str(raw_key)
        lowered = key.lower()
        if not any(marker in lowered for marker in ("time", "timestamp", "created", "_ts", "ts_")):
            continue
        if not key.replace("_", "").replace("-", "").replace(".", "").isalnum():
            continue
        fields.append(f"{key[:64]}:{type(value).__name__}")
    return ",".join(sorted(fields)[:8]) or "none"


def scope_for(chat: Mapping[str, Any], attention_tag_id: str) -> tuple[str, list[str]]:
    """Return collect_body / metadata_only / excluded without guessing fields."""

    kind = conversation_type(chat)
    if kind in DIRECT_TYPES:
        return "collect_body", []
    if kind in GROUP_TYPES:
        tags = chat.get("tag_ids")
        if not isinstance(tags, list):
            return "metadata_only", ["tag_ids_missing_or_invalid"]
        normalized = {str(value) for value in tags if value is not None}
        return ("collect_body", []) if attention_tag_id in normalized else ("metadata_only", [])
    if kind in EXCLUDED_TYPES:
        return "excluded", []
    return "excluded", ["unsupported_conversation_type"]
