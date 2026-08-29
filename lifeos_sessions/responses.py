"""Shared deterministic normalizer for Responses-style Agent session records.

Codex and SmartWork own different discovery, metadata and checkpoint rules, but
their JSONL bodies use the same record family.  This module is the single owner
for rebuilding turns, removing protocol noise and projecting bounded
``ConversationSlice`` candidates.  It never discovers source files, opens the
SmartWork index or writes the private Sessions Store.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .core import (
    SCHEMA_VERSION,
    OMISSION_REASON_EXPLICIT_ABORT,
    SessionValidationError,
    TimeWindow,
    stable_omission_id,
    stable_slice_id,
)
from .semantics import (
    build_execution_evidence,
    classify_origin,
    classify_warnings,
    has_substantive_result,
    is_read_only_check,
    is_verifying_check,
    normalize_title,
)


_TOOL_TYPES = {
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "tool_call",
    "tool_result",
    "tool_use",
    "tool_output",
    "command_execution",
    "command_execution_output",
    "exec_command",
    "exec_command_output",
    # ``exec_command_end`` is where Codex reports the exit code, so without it
    # a failed command left no trace and ``failures`` only ever contained
    # user interrupts.  It matches neither ``*tool*`` nor ``command_*``.
    "exec_command_begin",
    "exec_command_end",
    "error",
    "shell_command",
    "shell_output",
    "file_change",
    "file_write",
    "file_edit",
    # Codex reports an applied patch as a begin/end pair and a turn-level
    # diff.  Only the bare ``patch_apply`` name was recognised, so the one
    # record that actually names the changed files was counted as an unknown
    # record type and its evidence was dropped.
    "patch_apply",
    "patch_apply_begin",
    "patch_apply_end",
    "turn_diff",
    "web_search_begin",
    "web_search_end",
    "test_result",
    "check_result",
    "lint_result",
    "turn_aborted",
    "web_search_call",
    "web_search_begin",
    "image_generation_call",
    "image_generation_begin",
    "image_generation_end",
    # Codex drives sub-agents through a ``collab_*`` lifecycle rather than a
    # tool whose name contains "tool".
    "collab_agent_spawn_begin",
    "collab_agent_spawn_end",
    "collab_agent_interaction_begin",
    "collab_agent_interaction_end",
    "collab_waiting_begin",
    "collab_waiting_end",
    "collab_close_begin",
    "collab_close_end",
    "collab_resume_begin",
    "collab_resume_end",
}

_USER_TYPES = {"user", "user_message", "input", "prompt"}
_AGENT_TYPES = {"assistant", "assistant_message", "agent", "agent_message", "final_message"}
_META_TYPES = {"session_meta", "turn_context", "session_started", "session_ended", "scan_meta"}
_KNOWN_NOISE_TYPES = {
    "compaction",
    "compacted",
    "context_compacted",
    "reasoning",
    "sub_agent_activity",
    "task_started",
    "task_complete",
    "token_count",
    "thread_settings_applied",
    # Internal deliberation and environment snapshots: deliberately not kept.
    "agent_reasoning",
    "agent_reasoning_raw_content",
    "world_state",
    # A safety assessment of a *pending* action; the action itself is recorded
    # separately, so keeping this would double-count intent as evidence.
    "guardian_assessment",
    # Process artefacts: a plan item, a thread objective, a renamed thread.
    "item_completed",
    "thread_goal_updated",
    "thread_name_updated",
    # Recognised, but also raises a quality flag below: turns were undone.
    "thread_rolled_back",
}

def _is_known_inter_agent_metadata(payload: Mapping[str, Any]) -> bool:
    # The only stable fact verified across the source corpus is the exact
    # record/payload type name.  Its fields are deliberately not guessed: a
    # near-miss type remains unknown so a source change cannot be discarded.
    return str(payload.get("type") or "").lower() == "inter_agent_communication_metadata"

# Undoing turns changes what the log contains, so it is reported rather than
# silently folded into the noise count.
_ROLLBACK_TYPE = "thread_rolled_back"


def _explicit_abort(payload: Mapping[str, Any], record: Mapping[str, Any]) -> Tuple[bool, Optional[str]]:
    """Recognise only the source's exact structured abort marker.

    User prose that mentions cancellation is ordinary input.  Lifecycle is
    therefore driven by a typed event (or an exact verified cancel subtype),
    never by matching text in a failure list.
    """

    ptype = str(payload.get("type") or record.get("type") or "").lower()
    if ptype == "turn_aborted":
        reason = _first(payload, "reason", "message", "error")
        text = clean_text(reason) or "turn_aborted"
        return True, text[:_MAX_SUMMARY_ITEM_LENGTH]
    return False, None

_MAX_SUMMARY_ITEMS = 8
_MAX_SUMMARY_ITEM_LENGTH = 200
_MAX_TEXT_LENGTH = 100_000
_MAX_SOURCE_REFS = 32
_MAX_SUMMARY_REFS = 8
_MAX_DELEGATION_REFS = 8
_MAX_DELEGATIONS = 12
_MAX_DELEGATION_TASK_LENGTH = 500
_MAX_DELEGATION_RESULT_LENGTH = 2_000
_MAX_COMMAND_LENGTH = 200


@dataclass(frozen=True)
class SourceProfile:
    """Source-specific policy for one Responses-style Agent application."""

    name: str
    adapter_version: str
    extra_noise_types: frozenset[str] = frozenset()
    extra_tool_types: frozenset[str] = frozenset()
    task_complete_is_final: bool = False
    include_incomplete_tail_warning: bool = False
    context_only_turn_prefixes: tuple[str, ...] = ()


@dataclass
class SessionDocument:
    """One already-discovered JSONL document plus source metadata."""

    source: str
    conversation_id: str
    title: Optional[str]
    workspace: Optional[str]
    locator: str
    parent_conversation_id: Optional[str]
    records: Sequence[Mapping[str, Any]]
    session_id: Optional[str] = None
    parent_turn_id: Optional[str] = None
    agent_path: Any = None
    source_meta: Mapping[str, Any] = field(default_factory=dict)
    incomplete_tail: bool = False
    malformed: bool = False


@dataclass(frozen=True)
class NormalizedSessionResult:
    slices: tuple[Mapping[str, Any], ...]
    omissions: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    stats: Mapping[str, int]


@dataclass
class _Record:
    raw: Mapping[str, Any]
    payload: Mapping[str, Any]
    timestamp_ms: Optional[int]
    line_no: int
    record_hash: str
    event_key: str
    thread_id: str
    session_id: str
    turn_id: str
    kind: str
    role: Optional[str]
    text: str
    final: bool
    metadata: Mapping[str, Any]
    file_locator: str
    incomplete_tail: bool
    malformed: bool
    explicit_abort: bool = False
    abort_reason: Optional[str] = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clean_text(value: Any) -> str:
    """Extract readable text without flattening tool payloads or state dumps."""

    if value is None:
        return ""
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        return value[:_MAX_TEXT_LENGTH]
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [clean_text(item) for item in value]
        return "\n".join(part for part in parts if part)[:_MAX_TEXT_LENGTH]
    if isinstance(value, Mapping):
        block_type = str(value.get("type", "")).lower()
        if block_type in {
            "thinking",
            "reasoning",
            "tool_use",
            "tool_call",
            "tool_result",
            "function_call",
            "function_call_output",
            "command_execution",
            "file_search_call",
        }:
            return ""
        for key in ("text", "output_text", "input_text", "message"):
            if key in value:
                text = clean_text(value[key])
                if text:
                    return text
        if "content" in value:
            return clean_text(value["content"])
    return ""


def _norm_for_dedupe(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_timestamp(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) < 10_000_000_000:
            number *= 1000
        return int(number)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return parse_timestamp(float(text))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def iso_from_ms(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] not in (None, ""):
            return value[key]
    return None


def payload_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _event_id(record: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    value = _first(record, "event_id", "id", "uuid", "message_id")
    if value is None:
        value = _first(payload, "event_id", "id", "uuid", "message_id")
    return str(value) if value is not None else None


def thread_from_record(record: Mapping[str, Any], payload: Mapping[str, Any], default: str) -> str:
    keys = ("thread_id", "thread", "conversation_id")
    if str(record.get("type", "")).lower() == "session_meta":
        keys = ("id",) + keys
    value = _first(payload, *keys)
    if value is None:
        value = _first(record, "thread_id", "thread", "conversation_id")
    return str(value) if value is not None else default


def _session_from_record(record: Mapping[str, Any], payload: Mapping[str, Any], thread_id: str) -> str:
    value = _first(payload, "session_id", "session", "root_session_id")
    if value is None:
        value = _first(record, "session_id", "session", "root_session_id")
    return str(value) if value is not None else thread_id


def _turn_from_record(record: Mapping[str, Any], payload: Mapping[str, Any], current: Optional[str]) -> Optional[str]:
    value = _first(payload, "turn_id", "turn", "turnId")
    if value is None:
        value = _first(record, "turn_id", "turn", "turnId")
    if value is None and isinstance(payload.get("turn"), Mapping):
        value = _first(payload["turn"], "id", "turn_id")
    return current if value is None else str(value)


def _agent_path(record: Mapping[str, Any], payload: Mapping[str, Any]) -> Any:
    return _first(payload, "agent_path", "agentPath", "path") or _first(record, "agent_path", "agentPath")


def _parent_thread(record: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    value = _first(payload, "parent_thread_id", "parentThreadId", "parent_thread", "parent_session_id")
    if value is None:
        value = _first(record, "parent_thread_id", "parentThreadId", "parent_thread")
    return str(value) if value is not None else None


def _forked_from(record: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    """Read only the exact structured fork relation field.

    A user message or a near-miss field name is ordinary content.  Fork
    lineage is trusted only when the source emits the exact metadata key.
    """

    value = payload.get("forked_from_id")
    if value in (None, ""):
        value = record.get("forked_from_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _parent_turn(record: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    value = _first(payload, "parent_turn_id", "parentTurnId", "forked_from_turn_id", "forkedFromTurnId")
    if value is None:
        value = _first(record, "parent_turn_id", "parentTurnId", "forked_from_turn_id")
    return str(value) if value is not None else None


def build_session_document(
    *,
    source: str,
    locator: str,
    records: Sequence[Mapping[str, Any]],
    title: Optional[str] = None,
    workspace: Optional[str] = None,
    source_meta: Optional[Mapping[str, Any]] = None,
    incomplete_tail: bool = False,
    malformed: bool = False,
) -> SessionDocument:
    """Derive one document identity from JSONL, then apply index enrichments."""

    fallback_thread = f"file-{_sha(locator)[:24]}"
    metadata: Dict[str, Any] = {
        "thread_id": fallback_thread,
        "session_id": fallback_thread,
        "workspace": None,
        "title": None,
        "agent_path": None,
        "parent_thread_id": None,
        "parent_turn_id": None,
        "forked_from_id": None,
    }
    derived_meta: Dict[str, Any] = {}
    generated_title: Optional[str] = None
    for raw in records:
        current_payload = payload_of(raw)
        record_type = str(current_payload.get("type") or raw.get("type") or "").lower()
        if record_type == "thread_name_updated":
            candidate = normalize_title(current_payload.get("thread_name"))
            if candidate is not None:
                generated_title = candidate
        if str(raw.get("type", "")).lower() == "session_meta":
            thread = thread_from_record(raw, current_payload, fallback_thread)
            metadata["thread_id"] = thread or fallback_thread
            metadata["session_id"] = _session_from_record(raw, current_payload, metadata["thread_id"])
            metadata["workspace"] = _first(current_payload, "cwd", "workspace", "workdir") or metadata["workspace"]
            metadata["agent_path"] = _agent_path(raw, current_payload) or metadata["agent_path"]
            metadata["parent_thread_id"] = _parent_thread(raw, current_payload) or metadata["parent_thread_id"]
            metadata["parent_turn_id"] = _parent_turn(raw, current_payload) or metadata["parent_turn_id"]
            forked_from = _forked_from(raw, current_payload)
            if forked_from is not None:
                metadata["forked_from_id"] = forked_from
                # A structured fork relation is also a parent relation, but it
                # remains distinguishable below so it is not projected as a
                # completed delegation.
                metadata["parent_thread_id"] = forked_from
            for key in ("thread_source", "forked_from_id", "originator", "source"):
                if current_payload.get(key) not in (None, ""):
                    derived_meta[key] = current_payload[key]
        else:
            thread = thread_from_record(raw, current_payload, "")
            if thread:
                metadata["thread_id"] = thread
            session = _session_from_record(raw, current_payload, "")
            if session:
                metadata["session_id"] = session
            metadata["parent_thread_id"] = _parent_thread(raw, current_payload) or metadata["parent_thread_id"]
            metadata["parent_turn_id"] = _parent_turn(raw, current_payload) or metadata["parent_turn_id"]
            metadata["agent_path"] = _agent_path(raw, current_payload) or metadata["agent_path"]
    merged_meta = dict(derived_meta)
    merged_meta.update(dict(source_meta or {}))
    forked_from = merged_meta.get("forked_from_id")
    if isinstance(forked_from, str) and forked_from.strip():
        forked_from = forked_from.strip()
        merged_meta["forked_from_id"] = forked_from
        metadata["forked_from_id"] = forked_from
        metadata["parent_thread_id"] = forked_from
    return SessionDocument(
        source=source,
        conversation_id=str(metadata["thread_id"]),
        title=normalize_title(title) if title is not None else generated_title,
        workspace=workspace if workspace is not None else metadata["workspace"],
        locator=locator,
        parent_conversation_id=metadata["parent_thread_id"],
        records=tuple(records),
        session_id=str(metadata["session_id"] or metadata["thread_id"]),
        parent_turn_id=metadata["parent_turn_id"],
        agent_path=metadata["agent_path"],
        source_meta=merged_meta,
        incomplete_tail=incomplete_tail,
        malformed=malformed,
    )


def _is_final(payload: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    phase = _first(payload, "phase", "message_phase", "status")
    if isinstance(phase, str) and phase.lower() in {
        "final",
        "final_answer",
        "final_response",
        "finalized",
        "completed",
        "complete",
        "done",
        "end_turn",
        "stop",
    }:
        return True
    return bool(
        payload.get("end_turn") is True
        or payload.get("is_final") is True
        or record.get("phase") in {"final", "completed"}
    )


def _classify(
    record: Mapping[str, Any], payload: Mapping[str, Any], profile: SourceProfile
) -> Tuple[str, Optional[str], str, bool]:
    outer_type = str(record.get("type", "")).lower()
    inner_type = str(payload.get("type", "")).lower()
    ptype = inner_type or outer_type

    role = str(payload.get("role", record.get("role", ""))).lower()
    if role in {"developer", "system"}:
        return "noise", None, "", False
    if profile.task_complete_is_final and outer_type == "event_msg" and ptype == "task_complete":
        text = clean_text(_first(payload, "last_agent_message", "message", "text"))
        return ("agent", "agent", text, True) if text else ("noise", None, "", False)
    if outer_type in _META_TYPES:
        return "meta", None, "", False
    if ptype == "inter_agent_communication_metadata" and (
        _is_known_inter_agent_metadata(payload) or outer_type == ptype
    ):
        return "noise", None, "", False
    if outer_type in (_KNOWN_NOISE_TYPES | profile.extra_noise_types) or ptype in (
        _KNOWN_NOISE_TYPES | profile.extra_noise_types
    ):
        return "noise", None, "", False
    if ptype in (_TOOL_TYPES | profile.extra_tool_types) or "tool" in ptype or ptype.startswith("command_"):
        return "tool", None, "", False
    if ptype in _USER_TYPES or role in {"user", "human", "self"}:
        text = clean_text(_first(payload, "message", "text", "content", "prompt") or record.get("message"))
        return ("user" if text else "other"), "self", text, False
    if ptype in _AGENT_TYPES or role in {"assistant", "agent", "model"}:
        text = clean_text(_first(payload, "message", "text", "content", "output") or record.get("message"))
        return ("agent" if text else "other"), "agent", text, _is_final(payload, record)
    if outer_type == "response_item" and inner_type == "message":
        text = clean_text(_first(payload, "message", "text", "content", "output"))
        if role in {"user", "human"}:
            return ("user" if text else "other"), "self", text, False
        return ("agent" if text else "other"), "agent", text, _is_final(payload, record)
    if outer_type == "message":
        text = clean_text(_first(payload, "message", "text", "content") or record.get("message"))
        if role in {"user", "human"}:
            return ("user" if text else "other"), "self", text, False
        return ("agent" if text else "other"), "agent", text, _is_final(payload, record)
    if outer_type == "event_msg" and ptype in {"agent_message", "assistant_message"}:
        text = clean_text(_first(payload, "message", "text", "content"))
        return ("agent" if text else "other"), "agent", text, _is_final(payload, record)
    if outer_type == "event_msg" and ptype in {"user_message", "input"}:
        text = clean_text(_first(payload, "message", "text", "content"))
        return ("user" if text else "other"), "self", text, False
    return "other", None, "", False


def _extract_structured(value: Any, keys: Sequence[str]) -> List[str]:
    result: List[str] = []
    if isinstance(value, Mapping):
        for key in keys:
            if key not in value:
                continue
            candidate = value[key]
            # ``patch_apply_end.changes`` maps each changed path to its diff,
            # so the targets are the mapping's keys rather than a ``path``
            # field inside it.
            if (
                isinstance(candidate, Mapping)
                and candidate
                and all(isinstance(item, Mapping) for item in candidate.values())
            ):
                for item in candidate:
                    text = clean_text(item)
                    if text:
                        result.append(text)
                continue
            values = candidate if isinstance(candidate, (list, tuple, set)) else [candidate]
            for item in values:
                if isinstance(item, Mapping):
                    item = _first(item, "path", "file", "filename", "target", "name")
                text = clean_text(item)
                if text:
                    result.append(text)
        for key in ("arguments", "input", "data", "result", "details"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.lstrip().startswith(("{", "[")):
                try:
                    nested = json.loads(nested)
                except json.JSONDecodeError:
                    continue
            if isinstance(nested, Mapping):
                result.extend(_extract_structured(nested, keys))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.extend(_extract_structured(item, keys))
    return result


def _bound(
    values: Iterable[str],
    warnings: List[str],
    label: str,
    max_length: int = _MAX_SUMMARY_ITEM_LENGTH,
) -> Tuple[List[str], int]:
    seen: set[str] = set()
    output: List[str] = []
    omitted = 0
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip()
        if not clean:
            continue
        if len(clean) > max_length:
            clean = clean[: max_length - 1] + "…"
        if clean in seen:
            continue
        seen.add(clean)
        if len(output) >= _MAX_SUMMARY_ITEMS:
            omitted += 1
            continue
        output.append(clean)
    if omitted:
        warnings.append(f"execution_evidence_{label}_omitted:{omitted}")
    return output, omitted


def _bound_refs(values: Iterable[Any], warnings: List[str], label: str, limit: int) -> List[str]:
    seen: set[str] = set()
    output: List[str] = []
    omitted = 0
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        if len(output) >= limit:
            omitted += 1
            continue
        output.append(value)
    if omitted:
        warnings.append(f"{label}_omitted:{omitted}")
    return output


def _clip_text(value: Any, limit: int) -> Tuple[Optional[str], bool]:
    if value is None:
        return None, False
    text = clean_text(value)
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)] + "…", True


@dataclass(frozen=True)
class _JSLiteral:
    start: int
    end: int
    value: str


@dataclass(frozen=True)
class _ParameterContainer:
    """One normalized ``input`` or ``arguments`` container.

    ``source`` is retained only for the bounded JS scanner.  Every value that
    extraction returns comes from ``text`` or the decoded literal table, so
    command/path extraction never performs a second escape pass.
    """

    kind: str
    encoding: str
    value: Any
    text: str = ""
    source: str = ""
    literals: Tuple[_JSLiteral, ...] = ()


def _skip_js_space(value: str, index: int) -> int:
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _quoted_js_value(value: str, start: int) -> Optional[Tuple[str, int]]:
    quote = value[start]
    if quote not in {"'", '"', "`"}:
        return None
    characters: List[str] = []
    index = start + 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while index < len(value):
        character = value[index]
        if character in {"\n", "\r"} and quote != "`":
            return None
        if character == "\\":
            if index + 1 >= len(value):
                return None
            escaped = value[index + 1]
            characters.append(escapes.get(escaped, escaped))
            index += 2
            continue
        if character == quote:
            return "".join(characters), index + 1
        characters.append(character)
        index += 1
    return None


def _cmd_key_value_start(value: str, index: int) -> Optional[int]:
    key_end: Optional[int] = None
    if value.startswith("cmd", index):
        before = value[index - 1] if index else ""
        if not before or not (before.isalnum() or before in {"_", "$"}):
            key_end = index + 3
    elif value[index:index + 5] in {'"cmd"', "'cmd'"}:
        key_end = index + 5
    if key_end is None:
        return None
    key_end = _skip_js_space(value, key_end)
    if key_end >= len(value) or value[key_end] != ":":
        return None
    return _skip_js_space(value, key_end + 1)


def _decode_escaped_newlines(value: str) -> str:
    """Decode the source encodings observed in session payloads once."""

    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")


def _normalize_structured(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _normalize_structured(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_structured(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_structured(item) for item in value)
    if isinstance(value, str):
        return _decode_escaped_newlines(value)
    return value


def _scan_js_literals(value: str) -> Tuple[_JSLiteral, ...]:
    literals: List[_JSLiteral] = []
    index = 0
    while index < len(value):
        if value[index] in {"'", '"', "`"}:
            parsed = _quoted_js_value(value, index)
            if parsed is not None:
                text, end = parsed
                literals.append(_JSLiteral(index, end, text))
                index = end
                continue
        index += 1
    return tuple(literals)


def _parameter_value(payload: Mapping[str, Any]) -> Optional[Tuple[Any, str]]:
    for key in ("input", "arguments"):
        if key in payload:
            value = payload[key]
            return value, "str" if isinstance(value, str) else "structured"
    return None


def _container_encoding(value: str) -> str:
    text = value.lstrip()
    if text.startswith("*** Begin Patch"):
        return "raw-patch"
    if text.startswith(("{", "[")):
        return "json-text"
    if text.startswith(("const", "let", "var", "await", "function", "(", "//")):
        return "js-source"
    return "plain"


def _normalize_container(payload: Mapping[str, Any]) -> Optional[_ParameterContainer]:
    parameter = _parameter_value(payload)
    if parameter is None:
        return None
    value, kind = parameter
    if kind == "structured":
        return _ParameterContainer(
            kind="structured",
            encoding="structured",
            value=_normalize_structured(value),
        )

    encoding = _container_encoding(value)
    if encoding == "json-text":
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            normalized = _decode_escaped_newlines(value)
            return _ParameterContainer(
                kind="str",
                encoding=encoding,
                value=normalized,
                text=normalized,
                source=value,
            )
        return _ParameterContainer(
            kind="structured",
            encoding=encoding,
            value=_normalize_structured(parsed),
        )

    normalized = _decode_escaped_newlines(value)
    literals = _scan_js_literals(value) if encoding == "js-source" else ()
    return _ParameterContainer(
        kind="str",
        encoding=encoding,
        value=normalized,
        text=normalized,
        source=value if encoding == "js-source" else "",
        literals=literals,
    )


def _scan_exec_command_object(
    value: str,
    object_start: int,
    literals: Mapping[int, _JSLiteral],
) -> Optional[Tuple[str, int]]:
    depth = 0
    index = object_start
    while index < len(value):
        character = value[index]
        if depth == 0:
            value_start = _cmd_key_value_start(value, index)
            if value_start is not None:
                literal = literals.get(value_start)
                if literal is None:
                    return None
                command = clean_text(literal.value)[:_MAX_COMMAND_LENGTH]
                return (command, literal.end) if command else None
        literal = literals.get(index)
        if literal is not None:
            index = literal.end
            continue
        if character == "{":
            depth += 1
            index += 1
            continue
        if character == "}":
            if depth == 0:
                return None
            depth -= 1
            index += 1
            continue
        index += 1
    return None


def _extract_js_commands(container: _ParameterContainer) -> Tuple[List[str], int, int]:
    """Extract bounded command literals from a normalized JS container."""

    commands: List[str] = []
    omitted = 0
    malformed = 0
    literals = {literal.start: literal for literal in container.literals}
    masked = list(container.source)
    for literal in container.literals:
        for index in range(literal.start, literal.end):
            masked[index] = " "
    searchable = "".join(masked)
    marker = "exec_command"
    cursor = 0
    while True:
        position = searchable.find(marker, cursor)
        if position < 0:
            break
        before = searchable[position - 1] if position else ""
        if before and (before.isalnum() or before in {"_", "$"}):
            cursor = position + len(marker)
            continue
        opening = _skip_js_space(container.source, position + len(marker))
        if opening >= len(container.source) or container.source[opening] != "(":
            malformed += 1
            cursor = position + len(marker)
            continue
        object_start = _skip_js_space(container.source, opening + 1)
        if object_start >= len(container.source) or container.source[object_start] != "{":
            malformed += 1
            cursor = position + len(marker)
            continue
        extracted = _scan_exec_command_object(container.source, object_start + 1, literals)
        if extracted is None:
            malformed += 1
            cursor = position + len(marker)
            continue
        command, end = extracted
        if len(commands) < 8:
            commands.append(command)
        else:
            omitted += 1
        cursor = end
    return commands, omitted, malformed


def _extract_patch_targets(value: str) -> Tuple[List[str], int]:
    """Extract bounded file paths from a normalized patch body."""

    targets: List[str] = []
    omitted = 0
    begin_marker = "*** Begin Patch"
    end_marker = "*** End Patch"
    file_prefixes = (
        "*** Update File: ",
        "*** Add File: ",
        "*** Delete File: ",
    )
    cursor = 0
    while True:
        begin = value.find(begin_marker, cursor)
        if begin < 0:
            break
        payload_start = begin + len(begin_marker)
        end = value.find(end_marker, payload_start)
        payload_end = end if end >= 0 else len(value)
        for line in value[payload_start:payload_end].splitlines():
            for prefix in file_prefixes:
                if line.startswith(prefix):
                    target = line[len(prefix):]
                    if target.strip():
                        if len(targets) < 8:
                            targets.append(target)
                        else:
                            omitted += 1
                    break
        cursor = end + len(end_marker) if end >= 0 else len(value)
    return targets, omitted


def _bounded_extracted(values: Iterable[str]) -> Tuple[List[str], int]:
    output: List[str] = []
    omitted = 0
    for value in values:
        text = clean_text(value)[:_MAX_COMMAND_LENGTH]
        if not text:
            continue
        if len(output) < _MAX_SUMMARY_ITEMS:
            output.append(text)
        else:
            omitted += 1
    return output, omitted


def _structured_command(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return clean_text(" ".join(clean_text(item) for item in value))[:_MAX_COMMAND_LENGTH]
    return clean_text(value)[:_MAX_COMMAND_LENGTH]


def _extract_structured_commands(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, Mapping):
        for key in ("command", "cmd", "script"):
            if key in value:
                command = _structured_command(value[key])
                if command:
                    found.append(command)
        for key in ("input", "arguments", "data", "result", "details"):
            nested = value.get(key)
            if isinstance(nested, (Mapping, list, tuple)):
                found.extend(_extract_structured_commands(nested))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_extract_structured_commands(item))
    return found


def _extract_structured_paths(value: Any) -> Tuple[List[str], List[str], int]:
    changed: List[str] = []
    other: List[str] = []
    omitted = 0
    keys = (
        "changed_targets",
        "changed_files",
        "files",
        "paths",
        "targets",
        "path",
        "file",
        "filename",
        "target",
        "name",
        "changes",
    )
    nested_keys = ("arguments", "input", "data", "result", "details")
    if isinstance(value, Mapping):
        for key in keys:
            if key not in value:
                continue
            destination = changed if key in {"changed_targets", "changed_files"} else other
            candidate = value[key]
            if (
                isinstance(candidate, Mapping)
                and candidate
                and all(isinstance(item, Mapping) for item in candidate.values())
            ):
                destination.extend(clean_text(item) for item in candidate)
                continue
            values = candidate if isinstance(candidate, (list, tuple, set)) else [candidate]
            for item in values:
                if isinstance(item, Mapping):
                    item = _first(item, "path", "file", "filename", "target", "name")
                text = clean_text(item)
                if text:
                    destination.append(text)
        for key in nested_keys:
            nested = value.get(key)
            nested_changed, nested_other, nested_omitted = _extract_structured_paths(nested)
            changed.extend(nested_changed)
            other.extend(nested_other)
            omitted += nested_omitted
            if isinstance(nested, str):
                nested_found, nested_omitted = _extract_patch_targets(nested)
                changed.extend(nested_found)
                omitted += nested_omitted
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested_changed, nested_other, nested_omitted = _extract_structured_paths(item)
            changed.extend(nested_changed)
            other.extend(nested_other)
            omitted += nested_omitted
    bounded_changed, changed_omitted = _bounded_extracted(changed)
    bounded_other, other_omitted = _bounded_extracted(other)
    return bounded_changed, bounded_other, omitted + changed_omitted + other_omitted


def _extract_commands(container: Optional[_ParameterContainer]) -> Tuple[List[str], int, int]:
    if container is None:
        return [], 0, 0
    if container.kind == "structured":
        commands, omitted = _bounded_extracted(_extract_structured_commands(container.value))
        return commands, omitted, 0
    if container.encoding == "js-source":
        return _extract_js_commands(container)
    return [], 0, 0


def _extract_paths(container: Optional[_ParameterContainer]) -> Tuple[List[str], List[str], int]:
    if container is None:
        return [], [], 0
    if container.kind == "structured":
        return _extract_structured_paths(container.value)
    if container.encoding in {"raw-patch", "js-source"}:
        changed, omitted = _extract_patch_targets(container.text)
        return changed, [], omitted
    return [], [], 0


def _evidence_priority(value: Any, *, command: bool = True) -> int:
    if is_verifying_check(value):
        return 0
    if command and is_read_only_check(value):
        return 2
    return 1


def _error_text(value: Any) -> str:
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return clean_text(value)
        if isinstance(parsed, Mapping):
            return clean_text(parsed.get("message"))
        return ""
    if isinstance(value, Mapping):
        return clean_text(value.get("message"))
    return clean_text(value)


def _tool_summary(records: Sequence[_Record], warnings: List[str]) -> Dict[str, Any]:
    changed: List[str] = []
    other: List[str] = []
    checks: List[Tuple[str, bool]] = []
    failures: List[str] = []
    successful_commands: List[str] = []
    omitted = 0
    for record in records:
        if record.kind != "tool":
            continue
        payload = record.payload
        structured_changed, structured_other, structured_omitted = _extract_structured_paths(
            {
                key: value
                for key, value in payload.items()
                if key not in {"input", "arguments", "params", "name"}
            }
        )
        changed.extend(structured_changed)
        other.extend(structured_other)
        omitted += structured_omitted
        status = str(_first(payload, "status", "result", "outcome", "state") or "").lower()
        exit_code = _first(payload, "exit_code", "exitCode", "returncode", "code")
        event_type = str(payload.get("type", "")).lower()
        failed = status in {"failed", "failure", "error", "errored", "rejected", "timeout", "timed_out"}
        if exit_code not in (None, "", 0, "0") or event_type == "error":
            failed = True
        # Codex reports patch application through ``success`` rather than a
        # status string or an exit code.
        if payload.get("success") is False:
            failed = True
        check_name = _first(payload, "check", "check_name", "name", "description", "test")
        container = _normalize_container(payload)
        extracted_commands, command_omitted, extracted_malformed = _extract_commands(container)
        patch_targets, other_targets, patch_omitted = _extract_paths(container)
        changed.extend(patch_targets)
        other.extend(other_targets)
        omitted += command_omitted + patch_omitted + extracted_malformed
        command = extracted_commands[0] if extracted_commands else ""
        if failed:
            error = _first(payload, "error", "failure", "reason", "message")
            description = _error_text(error)
            if not description:
                description = command
                if description and exit_code not in (None, ""):
                    description = f"{description} (exit {exit_code})"
            failures.append(description or event_type or clean_text(check_name) or "tool failure")
        elif event_type == "turn_aborted":
            # Lifecycle is carried separately; the marker itself is neither a
            # tool call nor substantive execution evidence.
            continue
        elif not (event_type.endswith("_output") and not check_name):
            # A ``*_output`` record is the result half of a call already
            # counted above.  Emitting it again would spend the per-slice
            # budget twice on one action and label it with a record type.
            if extracted_commands:
                checks.extend((f"{value}: passed", True) for value in extracted_commands)
                successful_commands.extend(f"{value}: passed" for value in extracted_commands)
                if extracted_malformed:
                    checks.append((f"{clean_text(check_name) or event_type or 'tool'}: passed", False))
            else:
                name = clean_text(check_name) or event_type or "tool"
                label = command or name
                checks.append((f"{label}: passed", bool(command)))
                if command:
                    successful_commands.append(f"{label}: passed")
    changed_values, changed_omitted = _bound(changed, warnings, "changed_targets")
    other_values, other_omitted = _bound(other, warnings, "other_targets")
    # ``_bound`` keeps the first N entries, so an ordinary tool call can push a
    # real test run out of a busy turn.  Verifications are the scarcer and more
    # load-bearing evidence, so they claim the budget first.
    checks.sort(key=lambda item: _evidence_priority(item[0], command=item[1]))
    check_values, check_omitted = _bound(
        (value for value, _command in checks),
        warnings,
        "checks",
        _MAX_COMMAND_LENGTH + len(": passed"),
    )
    failure_values, failure_omitted = _bound(
        failures, warnings, "failures", _MAX_COMMAND_LENGTH + 32
    )
    return {
        "changed_targets": changed_values,
        "other_targets": other_values,
        "checks": check_values,
        "failures": failure_values,
        "successful_commands": list(dict.fromkeys(successful_commands)),
        "omitted_count": omitted + changed_omitted + other_omitted + check_omitted + failure_omitted,
    }


def _matches_include(profile: SourceProfile, thread_id: str, session_id: str, includes: Sequence[str]) -> bool:
    if not includes:
        return True
    candidates = {f"{profile.name}:{thread_id}", f"{profile.name}:{session_id}"}
    return any(item in candidates for item in includes)


def _source_ref(profile: SourceProfile, record: _Record, thread_id: str, turn_id: str) -> str:
    event = record.event_key or record.record_hash[:24]
    if len(event) > 256:
        event = _sha(event)[:64]
    return f"{profile.name}://{thread_id}/turn/{turn_id}/event/{event}"


def normalize_session(
    document: SessionDocument,
    window: TimeWindow,
    profile: SourceProfile,
    *,
    includes: Sequence[str] = (),
) -> NormalizedSessionResult:
    return normalize_sessions((document,), window, profile, includes=includes)


def normalize_sessions(
    documents: Sequence[SessionDocument],
    window: TimeWindow,
    profile: SourceProfile,
    *,
    includes: Sequence[str] = (),
) -> NormalizedSessionResult:
    """Normalize already-discovered documents into deterministic turn slices."""

    from_ms, to_ms = int(window.from_ms), int(window.to_ms)
    parsed: List[_Record] = []
    for document in documents:
        if document.source != profile.name:
            raise ValueError(f"document source {document.source!r} does not match profile {profile.name!r}")
        metadata: Dict[str, Any] = {
            "thread_id": document.conversation_id,
            "session_id": document.session_id or document.conversation_id,
            "workspace": document.workspace,
            "title": document.title,
            "agent_path": document.agent_path,
            "parent_thread_id": document.parent_conversation_id,
            "parent_turn_id": document.parent_turn_id,
            **dict(document.source_meta or {}),
        }
        forked_from = metadata.get("forked_from_id")
        if isinstance(forked_from, str) and forked_from.strip():
            # Only the exact structured field establishes fork lineage.  It
            # takes precedence over a stale parent hint while preserving the
            # explicit marker for the no-delegation branch below.
            metadata["forked_from_id"] = forked_from.strip()
            metadata["parent_thread_id"] = forked_from.strip()
        current_turn: Optional[str] = None
        for line_no, raw in enumerate(document.records, start=1):
            current_payload = payload_of(raw)
            timestamp_ms = parse_timestamp(raw.get("timestamp"))
            current_turn = _turn_from_record(raw, current_payload, current_turn)
            kind, role, text, final = _classify(raw, current_payload, profile)
            explicit_abort, abort_reason = _explicit_abort(current_payload, raw)
            event_id = _event_id(raw, current_payload)
            record_hash = _sha(raw)
            if current_turn is None and kind in {"user", "agent", "tool"}:
                current_turn = f"turn-{event_id or record_hash[:24]}"
            turn_id = current_turn or f"event-{record_hash[:24]}"
            parsed.append(
                _Record(
                    raw=raw,
                    payload=current_payload,
                    timestamp_ms=timestamp_ms,
                    line_no=line_no,
                    record_hash=record_hash,
                    event_key=event_id or record_hash[:32],
                    thread_id=str(metadata["thread_id"]),
                    session_id=str(metadata["session_id"]),
                    turn_id=str(turn_id),
                    kind=kind,
                    role=role,
                    text=text,
                    final=final,
                    metadata=metadata,
                    file_locator=document.locator,
                    incomplete_tail=document.incomplete_tail,
                    malformed=document.malformed,
                    explicit_abort=explicit_abort,
                    abort_reason=abort_reason,
                )
            )

    deduped: List[_Record] = []
    seen_events: set[Tuple[str, str]] = set()
    for record in sorted(
        parsed,
        key=lambda item: (
            item.timestamp_ms is None,
            item.timestamp_ms or 0,
            item.file_locator,
            item.line_no,
        ),
    ):
        event_key = (record.thread_id, record.event_key)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        deduped.append(record)

    groups: Dict[Tuple[str, str], List[_Record]] = {}
    for record in deduped:
        groups.setdefault((record.thread_id, record.turn_id), []).append(record)
    for records in groups.values():
        records.sort(
            key=lambda item: (
                item.timestamp_ms is None,
                item.timestamp_ms or 0,
                item.file_locator,
                item.line_no,
            )
        )

    # A fork may replay a parent's turn under a different thread id.  Build
    # this map before projection so an inherited fork turn can be dropped only
    # when its parent group is part of this normalization input (and selected
    # by the same include filter).
    group_included: Dict[Tuple[str, str], bool] = {}
    fork_parents: Dict[Tuple[str, str], str] = {}
    for group_key, records in groups.items():
        thread_id, _turn_id = group_key
        metadata = records[0].metadata
        group_included[group_key] = _matches_include(
            profile,
            thread_id,
            str(metadata.get("session_id", thread_id)),
            includes,
        )
        forked_from = metadata.get("forked_from_id")
        if isinstance(forked_from, str) and forked_from.strip() and forked_from.strip() != thread_id:
            fork_parents[group_key] = forked_from.strip()

    spawn_parents: Dict[str, Tuple[str, str]] = {}
    for record in deduped:
        ptype = str(record.payload.get("type", "")).lower()
        child_thread = record.payload.get("new_thread_id") if ptype == "collab_agent_spawn_end" else None
        if child_thread:
            spawn_parents[str(child_thread)] = (record.thread_id, record.turn_id)

    parent_keys: set[Tuple[str, str, str]] = set()
    for (thread_id, _turn_id), records in groups.items():
        for record in records:
            if record.kind in {"user", "agent"} and record.text:
                parent_keys.add((thread_id, record.role or "", _norm_for_dedupe(record.text)))

    global_warnings: List[str] = []
    slices: List[Dict[str, Any]] = []
    omission_records: List[Dict[str, Any]] = []
    context_only_turns_excluded = 0
    pending_abort_without_work: Dict[int, Dict[str, Any]] = {}
    child_slice_info: List[Tuple[Dict[str, Any], _Record, List[_Record]]] = []
    for (thread_id, turn_id), records in groups.items():
        metadata = records[0].metadata
        if not _matches_include(profile, thread_id, str(metadata.get("session_id", thread_id)), includes):
            continue
        if any(turn_id.startswith(prefix) for prefix in profile.context_only_turn_prefixes):
            # Some Responses-family applications materialize referenced task
            # history as synthetic turns at import time.  The structured
            # native id is the authority: their text is useful context for the
            # live Agent, but it is not work that happened at this timestamp.
            context_only_turns_excluded += 1
            continue
        fork_parent = fork_parents.get((thread_id, turn_id))
        if fork_parent:
            parent_key = (fork_parent, turn_id)
            if parent_key in groups and group_included.get(parent_key, False):
                # The parent's turn is authoritative.  The fork copy is
                # context only and must not become a competing Slice or a
                # delegation candidate.
                continue
        meaningful = [record for record in records if record.kind in {"user", "agent", "tool"}]
        unknown_records = [
            record
            for record in records
            if record.kind == "other"
            and record.timestamp_ms is not None
            and from_ms <= record.timestamp_ms < to_ms
        ]
        if not meaningful:
            if unknown_records:
                unknown_types = sorted(
                    {
                        str(record.payload.get("type") or record.raw.get("type") or "unknown")
                        for record in unknown_records
                    }
                )
                global_warnings.append(
                    f"unsupported_format:{thread_id}:{turn_id}:" + ",".join(unknown_types[:4])
                )
            continue
        in_window = [
            record
            for record in meaningful
            if record.timestamp_ms is not None and from_ms <= record.timestamp_ms < to_ms
        ]
        if not in_window:
            continue

        warnings: List[str] = []
        quality_flags: List[str] = []
        turn_omissions: List[str] = []
        if unknown_records:
            unknown_types = sorted(
                {
                    str(record.payload.get("type") or record.raw.get("type") or "unknown")
                    for record in unknown_records
                }
            )
            warnings.append("unknown_record_type:" + ",".join(unknown_types[:4]))
        if any(record.timestamp_ms is None for record in meaningful):
            warnings.append("missing_event_timestamp")
        if profile.include_incomplete_tail_warning and any(record.incomplete_tail for record in records):
            warnings.append("incomplete_tail")
        source_corrupt = any(record.malformed for record in records)
        if source_corrupt:
            warnings.append("source_contains_malformed_record")

        blocks: List[Dict[str, Any]] = []
        block_keys: set[Tuple[str, str]] = set()
        for record in records:
            if record.kind != "user" or not record.text:
                continue
            key = (record.role or "self", _norm_for_dedupe(record.text))
            if key in block_keys:
                continue
            block_keys.add(key)
            blocks.append(
                {
                    "kind": "message",
                    "author_role": "self",
                    "origin": classify_origin("self", record.text),
                    "at": iso_from_ms(record.timestamp_ms),
                    "text": record.text,
                    "context": not (
                        record.timestamp_ms is not None and from_ms <= record.timestamp_ms < to_ms
                    ),
                    "source_refs": [_source_ref(profile, record, thread_id, turn_id)],
                }
            )

        agent_records = [record for record in records if record.kind == "agent" and record.text]
        final_records = [record for record in agent_records if record.final]
        selected = (final_records or agent_records)[-1:] if (final_records or agent_records) else []
        if selected:
            record = selected[0]
            key = ("agent", _norm_for_dedupe(record.text))
            if key not in block_keys:
                blocks.append(
                    {
                        "kind": "agent_message",
                        "author_role": "agent",
                        "origin": "agent",
                        "at": iso_from_ms(record.timestamp_ms),
                        "text": record.text,
                        "context": not (
                            record.timestamp_ms is not None and from_ms <= record.timestamp_ms < to_ms
                        ),
                        "source_refs": [_source_ref(profile, record, thread_id, turn_id)],
                    }
                )
        else:
            warnings.append("incomplete:missing_final_message")
            quality_flags.append("no_readable_outcome")
        if not final_records and agent_records:
            warnings.append("incomplete:final_message_unconfirmed")
            quality_flags.append("unconfirmed_outcome")

        omitted_types = sorted(
            {
                str(record.payload.get("type") or record.raw.get("type") or "known_noise")
                for record in records
                if record.kind == "noise"
            }
        )
        turn_omissions.extend(f"source_noise:{item}" for item in omitted_types)
        rolled_back = sum(
            int(record.payload.get("num_turns") or 0)
            for record in records
            if str(record.payload.get("type") or "") == _ROLLBACK_TYPE
        )
        if rolled_back:
            quality_flags.append(f"thread_rolled_back:{rolled_back}")

        tool_observations = _tool_summary(records, warnings)
        execution_evidence = build_execution_evidence(
            changed_targets=tool_observations["changed_targets"],
            other_targets=tool_observations["other_targets"],
            checks=tool_observations["checks"],
            failures=tool_observations["failures"],
            omitted_count=tool_observations["omitted_count"],
        )
        tool_refs = [
            _source_ref(profile, record, thread_id, turn_id)
            for record in records
            if record.kind == "tool" and not record.explicit_abort
        ]
        summary_refs = _bound_refs(
            tool_refs, warnings, "execution_evidence_source_refs", _MAX_SUMMARY_REFS
        )
        if summary_refs:
            execution_evidence["source_refs"] = summary_refs
        source_refs = _bound_refs(
            [ref for block in blocks for ref in block.get("source_refs", [])] + summary_refs,
            warnings,
            "source_refs",
            _MAX_SOURCE_REFS,
        )
        timestamps = [record.timestamp_ms for record in meaningful if record.timestamp_ms is not None]
        started_ms = min(timestamps) if timestamps else from_ms
        ended_ms = max(timestamps) if timestamps else started_ms
        if ended_ms <= started_ms:
            ended_ms = started_ms + 1

        spawn_parent = spawn_parents.get(thread_id)
        parent_thread_id = fork_parent or metadata.get("parent_thread_id") or (
            spawn_parent[0] if spawn_parent else None
        )
        parent_turn_id = metadata.get("parent_turn_id") or (
            spawn_parent[1] if spawn_parent else None
        )
        is_fork = bool(fork_parent)
        is_child = bool(parent_thread_id)
        if is_child and not is_fork and parent_turn_id and str(parent_turn_id) == turn_id:
            own_records = [
                record
                for record in meaningful
                if not (
                    record.kind in {"user", "agent"}
                    and (record.role or "", _norm_for_dedupe(record.text))
                    in {
                        (role, text)
                        for parent_thread, role, text in parent_keys
                        if parent_thread == str(parent_thread_id)
                    }
                )
            ]
            if not own_records:
                continue

        completeness = classify_warnings(warnings, source_corrupt=source_corrupt)
        # ``session_id`` identifies a fork family in Responses-family logs;
        # multiple child threads can legitimately reuse the same native turn
        # id while producing different results.  Keep the established root
        # session identity for a primary thread, but use session_meta ``id``
        # (thread_id) for child conversation/stable identity.
        root_session_id = str(metadata.get("session_id") or thread_id)
        conversation_id = thread_id if is_child else root_session_id
        item_source_meta = {
            "thread_id": thread_id,
            "thread": thread_id,
            "session_id": root_session_id,
            "agent_path": metadata.get("agent_path"),
            "parent_thread_id": parent_thread_id,
            "parent_turn_id": parent_turn_id,
        }
        for key, value in metadata.items():
            if key not in {
                "thread_id",
                "session_id",
                "workspace",
                "title",
                "agent_path",
                "parent_thread_id",
                "parent_turn_id",
            }:
                item_source_meta[key] = value
        item: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "slice_id": stable_slice_id(profile.name, conversation_id, "turn", turn_id),
            "source": profile.name,
            "conversation": {"id": conversation_id, "title": metadata.get("title")},
            "native_unit": {"kind": "turn", "id": turn_id},
            "started_at": iso_from_ms(started_ms),
            "ended_at": iso_from_ms(ended_ms),
            "workspace": metadata.get("workspace"),
            "blocks": blocks,
            "execution_evidence": execution_evidence,
            "delegations": [],
            "content_completeness": completeness["content_completeness"],
            "turn_completion": "completed" if final_records else (
                "interrupted_with_result" if any(record.explicit_abort for record in records) else "incomplete"
            ),
            "provenance_trimmed": completeness["provenance_trimmed"],
            "quality_flags": list(dict.fromkeys(quality_flags)),
            "omissions": list(dict.fromkeys(turn_omissions)),
            "warnings": list(dict.fromkeys(warnings)),
            "source_refs": source_refs,
            "source_meta": item_source_meta,
            "adapter": {"name": profile.name, "version": profile.adapter_version},
        }
        abort_records = [record for record in records if record.explicit_abort]
        if abort_records and not final_records:
            # Delegation outcomes are attached below; retain a candidate until
            # that pass so a completed child can count as substantive work.
            if not has_substantive_result(
                execution_evidence,
                successful_commands=tool_observations.get("successful_commands", []),
            ):
                pending_abort_without_work[id(item)] = {
                    "source": profile.name,
                    "conversation": {"id": conversation_id},
                    "native_unit": {"kind": "turn", "id": turn_id},
                    "at": iso_from_ms(abort_records[-1].timestamp_ms or ended_ms),
                    "reason": OMISSION_REASON_EXPLICIT_ABORT,
                    "source_ref": _source_ref(profile, abort_records[-1], thread_id, turn_id),
                    "adapter": {"name": profile.name, "version": profile.adapter_version},
                }
        slices.append(item)
        if is_child and not is_fork:
            child_slice_info.append((item, records[0], records))

    by_thread_turn = {
        (str(item["source_meta"]["thread_id"]), str(item["native_unit"]["id"])): item
        for item in slices
    }
    delegation_candidates: Dict[
        int, List[Tuple[Tuple[int, int, str, str], Dict[str, Any], Dict[str, Any]]]
    ] = {}
    for child, first_record, _records in child_slice_info:
        parent_thread = child["source_meta"].get("parent_thread_id")
        parent_turn = child["source_meta"].get("parent_turn_id")
        if not parent_thread:
            continue
        parent = by_thread_turn.get((str(parent_thread), str(parent_turn))) if parent_turn else None
        if parent is None:
            candidates = [
                item
                for item in slices
                if str(item["source_meta"]["thread_id"]) == str(parent_thread)
            ]
            parent = candidates[-1] if candidates else None
        if parent is None:
            continue
        outcomes = [
            block.get("text", "") for block in child.get("blocks", [])
            if block.get("kind") == "agent_message"
        ]
        raw_result = outcomes[-1] if outcomes else None
        if raw_result and any(
            _norm_for_dedupe(raw_result) == _norm_for_dedupe(text)
            for block in parent.get("blocks", [])
            if block.get("kind") == "agent_message"
            for text in [block.get("text", "")]
        ):
            continue
        raw_task = next(
            (block.get("text") for block in child.get("blocks", []) if block.get("kind") == "message"),
            None,
        )
        task, task_truncated = _clip_text(raw_task, _MAX_DELEGATION_TASK_LENGTH)
        result, result_truncated = _clip_text(raw_result, _MAX_DELEGATION_RESULT_LENGTH)
        delegation_warnings: List[str] = []
        if task_truncated:
            delegation_warnings.append("task_truncated")
        if result_truncated:
            delegation_warnings.append("result_truncated")
        child_refs = [
            ref for block in child.get("blocks", []) for ref in block.get("source_refs", [])
        ]
        summary = child.get("execution_evidence")
        if isinstance(summary, Mapping):
            child_refs.extend(summary.get("source_refs", []))
        delegation = {
            "agent_id": child["source_meta"]["thread_id"],
            "thread_id": child["source_meta"]["thread_id"],
            "child_thread_id": child["source_meta"]["thread_id"],
            "turn_id": child["native_unit"]["id"],
            "agent_path": child["source_meta"].get("agent_path"),
            "task": task,
            "status": "complete" if result else "incomplete",
            "result": result,
            "source_refs": _bound_refs(
                child_refs,
                delegation_warnings,
                "delegation_source_refs",
                _MAX_DELEGATION_REFS,
            ),
        }
        if delegation_warnings:
            delegation["warnings"] = delegation_warnings
            parent.setdefault("warnings", []).extend(
                f"delegation_{warning}" for warning in delegation_warnings
            )
        rank = (
            0 if result else 1,
            0
            if parent_turn and str(parent.get("native_unit", {}).get("id")) == str(parent_turn)
            else 1,
            str(child["source_meta"]["thread_id"]),
            str(child["native_unit"]["id"]),
        )
        delegation_candidates.setdefault(id(parent), []).append((rank, delegation, parent))

    for candidates in delegation_candidates.values():
        candidates.sort(key=lambda value: value[0])
        parent = candidates[0][2]
        retained = candidates[:_MAX_DELEGATIONS]
        omitted = max(0, len(candidates) - len(retained))
        parent["delegations"] = [value[1] for value in retained]
        if omitted:
            parent.setdefault("warnings", []).append(f"delegations_omitted:{omitted}")
        parent_refs = [
            ref for block in parent.get("blocks", []) for ref in block.get("source_refs", [])
        ]
        summary = parent.get("execution_evidence")
        if isinstance(summary, Mapping):
            parent_refs.extend(summary.get("source_refs", []))
        parent_refs.extend(ref for item in retained for ref in item[1].get("source_refs", []))
        parent["source_refs"] = _bound_refs(
            parent_refs,
            parent.setdefault("warnings", []),
            "source_refs",
            _MAX_SOURCE_REFS,
        )
        parent["warnings"] = list(dict.fromkeys(parent.get("warnings", [])))
        # Linking delegations can add trim warnings after the turn was first
        # assessed, so the parent's completeness is restated from its final
        # warning set rather than left at the earlier value.
        restated = classify_warnings(
            parent["warnings"],
            source_corrupt=parent.get("content_completeness") == "truncated",
        )
        parent["content_completeness"] = restated["content_completeness"]
        parent["provenance_trimmed"] = restated["provenance_trimmed"]

    # Explicit aborts with no observable work become contentless omission
    # records.  A completed delegation linked in the pass above rescues the
    # parent turn as an interrupted result.
    retained: List[Dict[str, Any]] = []
    for item in slices:
        omission = pending_abort_without_work.get(id(item))
        if omission is not None:
            if has_substantive_result(
                item.get("execution_evidence") or {},
                completed_delegations=(
                    value for value in item.get("delegations") or []
                    if isinstance(value, Mapping)
                ),
            ):
                item["turn_completion"] = "interrupted_with_result"
                retained.append(item)
            else:
                omission_records.append(omission)
        else:
            retained.append(item)
    slices = retained
    finalized_omissions: List[Dict[str, Any]] = []
    for value in omission_records:
        try:
            identity = stable_omission_id(
                str(value["source"]),
                str(value["conversation"]["id"]),
                str(value["native_unit"]["kind"]),
                str(value["native_unit"]["id"]),
                str(value["reason"]),
            )
            finalized = dict(value)
            finalized["omission_id"] = identity
            finalized_omissions.append(finalized)
        except (KeyError, TypeError, SessionValidationError):
            global_warnings.append("invalid_omission")

    slices.sort(key=lambda item: (item.get("started_at") or "", item.get("slice_id") or ""))
    return NormalizedSessionResult(
        slices=tuple(slices),
        omissions=tuple(finalized_omissions),
        warnings=tuple(dict.fromkeys(global_warnings)),
        stats={
            "records_examined": len(parsed),
            "records_deduplicated": len(parsed) - len(deduped),
            "known_noise_records": sum(1 for record in deduped if record.kind == "noise"),
            "context_only_turns_excluded": context_only_turns_excluded,
            "omissions": len(finalized_omissions),
        },
    )


__all__ = [
    "NormalizedSessionResult",
    "SessionDocument",
    "SourceProfile",
    "build_session_document",
    "clean_text",
    "iso_from_ms",
    "normalize_session",
    "normalize_sessions",
    "parse_timestamp",
    "payload_of",
    "thread_from_record",
]
