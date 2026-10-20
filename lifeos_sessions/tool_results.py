"""Pair tool results and interpret explicit transport evidence, never prose claims."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


def call_id(payload: Mapping[str, Any]) -> str:
    return str(next((payload[key] for key in ("call_id", "tool_use_id", "toolCallId")
                     if payload.get(key)), ""))


def is_result(payload: Mapping[str, Any]) -> bool:
    kind = str(payload.get("type") or "").lower()
    return kind.endswith(("_output", "_end", "_result", "_response")) or kind in {
        "tool_result", "toolresult",
    }


def result_status(payload: Mapping[str, Any], *, returned: bool = False) -> str:
    """A call alone is pending; output alone is not proof a shell exited."""
    values = []
    texts = []
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            texts.append(value)
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                continue
            if isinstance(decoded, (Mapping, list)):
                pending.append(decoded)
        elif isinstance(value, list):
            pending.extend(block for block in value if isinstance(block, Mapping)
                           and block.get("type") in {"text", "output_text"})
        elif isinstance(value, Mapping):
            values.append(value)
            pending.extend(value[key] for key in ("output", "content", "result", "details")
                           if key in value)
            if value.get("type") in {"text", "output_text"}:
                pending.append(value.get("text"))
    statuses = {str(v.get(k) or "").lower() for v in values
                for k in ("status", "state", "outcome")}
    if payload.get("type") == "error":
        statuses.add("error")
    codes = [v[k] for v in values for k in ("exit_code", "exitCode", "returncode")
             if v.get(k) is not None]
    # These are native shell envelope lines, not arbitrary mentions of an exit.
    for text in texts:
        codes.extend(int(match.group(1)) for match in re.finditer(
            r"(?m)^(?:Process exited with code |Exit code:?\s*)(-?\d+)\s*$", text))
    if any(v.get("interrupted") is True or v.get("cancelled") is True for v in values) or statuses & {"interrupted", "cancelled", "aborted"}:
        return "interrupted"
    if (any(v.get("is_error") is True or v.get("isError") is True or v.get("success") is False for v in values)
            or any(str(code) != "0" for code in codes)
            or statuses & {"failed", "failure", "error", "errored", "rejected", "timeout", "timed_out"}):
        return "failed"
    if statuses & {"running", "pending", "in_progress"} or any(v.get("session_id") is not None for v in values):
        return "pending"
    if codes or statuses & {"success", "succeeded", "complete", "completed", "ok"} or any(v.get("success") is True for v in values):
        return "passed"
    # Claude/Pi result envelopes explicitly carry this native boolean.
    if returned and any(v.get("is_error") is False or v.get("isError") is False for v in values):
        return "passed"
    return "unknown" if returned else "pending"


def paired_status(payload: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> str:
    if not results:
        return result_status(payload, returned=is_result(payload))
    states = [result_status(result, returned=True) for result in results]
    # Multiple representations of a result must never hide an explicit failure.
    for state in ("failed", "interrupted", "passed", "pending", "unknown"):
        if state in states:
            return state
    return "unknown"


def failure_text(payload: Mapping[str, Any]) -> str:
    for key in ("error", "failure", "reason", "message", "output", "content"):
        value = payload.get(key)
        if value:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return " ".join(text.split())[:160]
    return "tool returned failure"
