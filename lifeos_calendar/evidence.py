"""Bounded read views over archived calendar scans."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from .store import CalendarStore


def build_index(store: CalendarStore, from_value: str, to_value: str) -> Dict[str, Any]:
    """Index the latest scan of exactly this window; never stitch windows together."""

    manifest = store.latest_scan(from_value, to_value)
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "evidence_level": "supporting",
        "window": {"from": from_value, "to": to_value},
        "source_scan_id": manifest["scan_id"] if manifest else None,
        "source_status": manifest.get("status") if manifest else "unknown",
        "warnings": list(manifest.get("warnings") or []) if manifest else [],
        "events": [],
        "summary": {"events": 0, "attend": 0, "skip": 0, "unlisted": 0},
    }
    if not manifest:
        return payload
    rules = store.series_rules()
    events: List[Dict[str, Any]] = []
    for ref in manifest.get("event_refs") or []:
        event = dict(store.read_event(ref["path"])["event"])
        rule = rules.get(event.get("series_key") or "")
        event["series_rule"] = rule["rule"] if rule else None
        events.append(event)
    by_span: Dict[tuple[str, str], List[str]] = defaultdict(list)
    for event in events:
        by_span[(event["dtstart"], event["dtend"])].append(event["instance_id"])
    for event in events:
        siblings = by_span[(event["dtstart"], event["dtend"])]
        event["overlaps"] = [item for item in siblings if item != event["instance_id"]]
    events.sort(key=lambda item: (item["dtstart"], item["dtend"], item["summary"]))
    payload["events"] = events
    payload["summary"] = {
        "events": len(events),
        "attend": sum(1 for item in events if item["series_rule"] == "attend"),
        "skip": sum(1 for item in events if item["series_rule"] == "skip"),
        "unlisted": sum(1 for item in events if item["series_rule"] is None),
    }
    return payload
