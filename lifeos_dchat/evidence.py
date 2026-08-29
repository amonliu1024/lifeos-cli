"""Deterministic bounded DChat evidence views for Daily."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .model import DCHAT_SCHEMA_VERSION
from .store import DChatStore


def build_index(
    store: DChatStore,
    from_value: str,
    to_value: str,
    conversation: Optional[str] = None,
    *,
    source_window: Optional[tuple[str, str]] = None,
    project_rows: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    rows = store.query_messages(from_value, to_value, conversation)
    projects: Dict[str, Dict[str, Any]] = {}
    for row in project_rows or []:
        projects[row["conversation_id"]] = row
    grouped: Dict[str, Dict[str, Any]] = {}
    source_scan = store.latest_scan(*source_window) if source_window else None
    contexts = {
        item.get("conversation_id"): item
        for item in (source_scan or {}).get("conversations", [])
        if item.get("conversation_id")
    }
    if source_scan:
        for item in contexts.values():
            cid = item.get("conversation_id")
            if not cid or item.get("scope") != "collect_body" or (conversation and cid != conversation):
                continue
            grouped[cid] = {
                "conversation_id": cid,
                "type": item.get("type"),
                "scope": item.get("scope"),
                "completeness": item.get("status"),
                "warnings": list(item.get("warnings") or []),
                "messages": 0,
                "from": None,
                "to": None,
                "project_candidates": (projects.get(cid) or {}).get("projects", []),
                "projects_confirmed": cid in projects,
                "projects_confirmed_at": (projects.get(cid) or {}).get("confirmed_at"),
            }
    for row in rows:
        cid = row["conversation_id"]
        context = contexts.get(cid)
        warnings = list((context or {}).get("warnings") or [])
        if context and context.get("scope") != "collect_body":
            warnings.append("body_not_read_in_source_scan")
        bucket = grouped.setdefault(cid, {
            "conversation_id": cid,
            "messages": 0,
            "from": row["occurred_at"],
            "to": row["occurred_at"],
            "project_candidates": (projects.get(cid) or {}).get("projects", []),
            "projects_confirmed": cid in projects,
            "projects_confirmed_at": (projects.get(cid) or {}).get("confirmed_at"),
            "type": (context or {}).get("type"),
            "scope": (context or {}).get("scope", "historically_collected"),
            "completeness": (context or {}).get("status", "unknown"),
            "warnings": warnings or ([] if context else ["source_scan_missing"]),
        })
        bucket["messages"] += 1
        bucket["from"] = row["occurred_at"] if bucket["from"] is None else min(bucket["from"], row["occurred_at"])
        bucket["to"] = row["occurred_at"] if bucket["to"] is None else max(bucket["to"], row["occurred_at"])
    return {
        "schema_version": DCHAT_SCHEMA_VERSION,
        "evidence_level": "supporting",
        "window": {"from": from_value, "to": to_value},
        "conversations": sorted(grouped.values(), key=lambda item: item["conversation_id"]),
        "summary": {"conversations": len(grouped), "messages": len(rows)},
        "source_scan_id": source_scan.get("scan_id") if source_scan else None,
        "source_status": source_scan.get("status") if source_scan else "unknown",
    }


def build_pack(
    store: DChatStore,
    from_value: str,
    to_value: str,
    conversation: Optional[str],
    max_bytes: int,
    *,
    source_window: Optional[tuple[str, str]] = None,
) -> Dict[str, Any]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes 必须是正整数")
    selected = []
    used = 0
    omitted = 0
    for row in store.query_messages(from_value, to_value, conversation):
        envelope = store.read_revision(row["json_path"])
        item = {
            "conversation_id": row["conversation_id"],
            "message_key": row["message_id"],
            "revision": row["revision"],
            "occurred_at": row["occurred_at"],
            "payload": envelope["payload"],
        }
        size = len(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if used + size > max_bytes:
            omitted += 1
            continue
        selected.append(item)
        used += size
    source_scan = store.latest_scan(*source_window) if source_window else None
    source_warnings = {
        warning
        for item in (source_scan or {}).get("conversations", [])
        if item.get("scope") == "collect_body" and (not conversation or item.get("conversation_id") == conversation)
        for warning in item.get("warnings") or []
    }
    source_scope = {
        item.get("conversation_id"): item.get("scope")
        for item in (source_scan or {}).get("conversations", [])
        if item.get("conversation_id")
    }
    selected_conversations = {item["conversation_id"] for item in selected}
    if any(source_scope.get(cid) not in {None, "collect_body"} for cid in selected_conversations):
        source_warnings.add("body_not_read_in_source_scan")
    return {
        "schema_version": DCHAT_SCHEMA_VERSION,
        "evidence_level": "supporting",
        "window": {"from": from_value, "to": to_value},
        "messages": selected,
        "budget": {"max_bytes": max_bytes, "used_bytes": used, "omitted_messages": omitted},
        "source_scan_id": source_scan.get("scan_id") if source_scan else None,
        "source_status": source_scan.get("status") if source_scan else "unknown",
        "warnings": (["budget_omitted"] if omitted else []) + sorted(source_warnings),
    }
