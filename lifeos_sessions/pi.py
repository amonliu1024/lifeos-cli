"""Pi Coding Agent source adapter.

Pi stores one tree-shaped JSONL file per session under
``~/.pi/agent/sessions``.  This adapter owns only source discovery, tree
relations and projection onto the shared Responses-shaped normalizer.  It
never imports Pi, invokes its CLI or reads credentials and settings.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .core import AdapterResult, adapter_cache_generation
from .responses import (
    SourceProfile,
    build_session_document,
    clean_text,
    iso_from_ms,
    normalize_sessions,
    parse_timestamp,
)


ADAPTER_NAME = "pi"
ADAPTER_VERSION = "1"
SOURCE_FORMAT = "pi-session-jsonl-v2-v3"
_SUPPORTED_SESSION_VERSIONS = {2, 3}
_NOISE_TYPES = frozenset(
    {
        "branch_summary",
        "compaction",
        "custom",
        "custom_message",
        "label",
        "model_change",
        "pi_custom_message",
        "session_info",
        "thinking_level_change",
    }
)
_TOOL_TYPES = frozenset({"pi_bash_execution", "pi_tool_call", "pi_tool_result"})
_TERMINAL_PARTIAL_REASONS = {"length", "error"}


@dataclass
class _FileRead:
    path: Path
    locator: str
    records: List[Mapping[str, Any]]
    projected: List[Mapping[str, Any]]
    last_hash: Optional[str]
    min_timestamp_ms: Optional[int]
    max_timestamp_ms: Optional[int]
    incomplete_tail: bool
    malformed: bool
    projection_warnings: List[str]
    turn_warnings: Dict[str, List[str]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _relative_locator(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _text_blocks(value: Any) -> List[Mapping[str, str]]:
    if isinstance(value, str):
        text = clean_text(value)
        return [{"type": "text", "text": text}] if text else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: List[Mapping[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or str(item.get("type", "")) != "text":
            continue
        text = clean_text(item.get("text"))
        if text:
            result.append({"type": "text", "text": text})
    return result


def _has_image_block(value: Any) -> bool:
    return bool(
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and any(
            isinstance(item, Mapping) and str(item.get("type") or "") == "image"
            for item in value
        )
    )


def _entry_timestamp(record: Mapping[str, Any]) -> Optional[int]:
    value = parse_timestamp(record.get("timestamp"))
    if value is not None:
        return value
    message = record.get("message")
    if isinstance(message, Mapping):
        return parse_timestamp(message.get("timestamp"))
    return None


def _message_role(record: Mapping[str, Any]) -> str:
    message = record.get("message")
    if not isinstance(message, Mapping):
        return ""
    return str(message.get("role") or "").strip()


def _changed_targets(tool_name: str, arguments: Any) -> List[str]:
    """Project only Pi built-in write targets, never edit bodies."""

    if tool_name not in {"edit", "write"} or not isinstance(arguments, Mapping):
        return []
    path = clean_text(arguments.get("path"))
    return [path] if path else []


def _project_records(
    records: Sequence[Mapping[str, Any]], locator: str
) -> Tuple[List[Mapping[str, Any]], List[str], Dict[str, List[str]]]:
    warnings: List[str] = []
    turn_warnings: Dict[str, List[str]] = {}
    terminal_reasons: Dict[str, str] = {}
    header = next((item for item in records if item.get("type") == "session"), None)
    if header is None:
        warnings.append(f"missing_session_header:{locator}")
        session_id = f"file-{_sha(locator)[:24]}"
        workspace = None
    else:
        session_id = clean_text(header.get("id")) or f"file-{_sha(locator)[:24]}"
        workspace = clean_text(header.get("cwd")) or None
        version = header.get("version")
        if version not in _SUPPORTED_SESSION_VERSIONS:
            warnings.append(f"unsupported_session_version:{locator}:{version}")

    entries: List[Mapping[str, Any]] = []
    by_id: Dict[str, Mapping[str, Any]] = {}
    for record in records:
        if record.get("type") == "session":
            continue
        entry_id = clean_text(record.get("id"))
        if not entry_id:
            warnings.append(f"missing_entry_id:{locator}")
            continue
        if entry_id in by_id:
            warnings.append(f"duplicate_entry_id:{locator}:{entry_id}")
            continue
        by_id[entry_id] = record
        entries.append(record)

    turn_cache: Dict[str, Optional[str]] = {}
    relation_warning_keys: set[str] = set()

    def turn_for(entry: Mapping[str, Any]) -> Optional[str]:
        entry_id = clean_text(entry.get("id"))
        if entry_id in turn_cache:
            return turn_cache[entry_id]
        current = entry
        visited: set[str] = set()
        path: List[str] = []
        resolved: Optional[str] = None
        while True:
            current_id = clean_text(current.get("id"))
            if not current_id:
                break
            if current_id in turn_cache:
                resolved = turn_cache[current_id]
                break
            if current_id in visited:
                key = f"parent_cycle:{locator}:{current_id}"
                if key not in relation_warning_keys:
                    warnings.append(key)
                    relation_warning_keys.add(key)
                break
            visited.add(current_id)
            path.append(current_id)
            if current.get("type") == "message" and _message_role(current) == "user":
                resolved = current_id
                break
            parent_id = clean_text(current.get("parentId"))
            if not parent_id:
                break
            parent = by_id.get(parent_id)
            if parent is None:
                key = f"missing_parent:{locator}:{current_id}:{parent_id}"
                if key not in relation_warning_keys:
                    warnings.append(key)
                    relation_warning_keys.add(key)
                break
            current = parent
        for value in path:
            turn_cache[value] = resolved
        return resolved

    projected: List[Mapping[str, Any]] = [
        {
            "timestamp": iso_from_ms(_entry_timestamp(header or {})),
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "cwd": workspace,
            },
        }
    ]

    for record in entries:
        entry_type = str(record.get("type") or "").strip()
        entry_id = clean_text(record.get("id"))
        timestamp = iso_from_ms(_entry_timestamp(record))
        turn_id = turn_for(record)
        base_payload: Dict[str, Any] = {}
        if turn_id:
            base_payload["turn_id"] = turn_id

        if entry_type == "session_info":
            name = clean_text(record.get("name"))
            if name:
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "id": entry_id,
                        "payload": {
                            **base_payload,
                            "type": "thread_name_updated",
                            "thread_name": name,
                        },
                    }
                )
            continue

        if entry_type == "message":
            message = record.get("message")
            if not isinstance(message, Mapping):
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "pi_unknown_message",
                        "id": entry_id,
                        "payload": base_payload,
                    }
                )
                continue
            role = _message_role(record)
            if role == "user":
                if turn_id and _has_image_block(message.get("content")):
                    turn_warnings.setdefault(turn_id, []).append("pi_image_content_unparsed")
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "message",
                        "id": entry_id,
                        "payload": {
                            **base_payload,
                            "role": "user",
                            "content": _text_blocks(message.get("content")),
                        },
                    }
                )
                continue
            if role == "assistant":
                content = message.get("content")
                if turn_id and _has_image_block(content):
                    turn_warnings.setdefault(turn_id, []).append("pi_image_content_unparsed")
                text = _text_blocks(content)
                stop_reason = str(message.get("stopReason") or "").strip()
                if text:
                    payload: Dict[str, Any] = {
                        **base_payload,
                        "role": "assistant",
                        "content": text,
                    }
                    if stop_reason in {"stop", "length"}:
                        payload["phase"] = "final"
                    projected.append(
                        {
                            "timestamp": timestamp,
                            "type": "message",
                            "id": entry_id,
                            "payload": payload,
                        }
                    )
                if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
                    for index, block in enumerate(content):
                        if not isinstance(block, Mapping) or block.get("type") != "toolCall":
                            continue
                        call_id = clean_text(block.get("id")) or f"{entry_id}-{index}"
                        tool_name = clean_text(block.get("name")) or "tool"
                        arguments = block.get("arguments")
                        projected.append(
                            {
                                "timestamp": timestamp,
                                "type": "message",
                                "id": f"{entry_id}:tool:{call_id}",
                                "payload": {
                                    **base_payload,
                                    "type": "pi_tool_call",
                                    "call_id": call_id,
                                    "name": tool_name,
                                    "input": arguments,
                                    "changed_targets": _changed_targets(tool_name, arguments),
                                },
                            }
                        )
                if stop_reason == "aborted":
                    projected.append(
                        {
                            "timestamp": timestamp,
                            "type": "event_msg",
                            "id": f"{entry_id}:abort",
                            "payload": {
                                **base_payload,
                                "type": "turn_aborted",
                                "reason": "assistant_aborted",
                            },
                        }
                    )
                elif stop_reason == "error":
                    projected.append(
                        {
                            "timestamp": timestamp,
                            "type": "event_msg",
                            "id": f"{entry_id}:error",
                            "payload": {
                                **base_payload,
                                "type": "error",
                                "message": clean_text(message.get("errorMessage")) or "assistant error",
                            },
                        }
                    )
                if turn_id and stop_reason not in {"", "toolUse"}:
                    terminal_reasons[turn_id] = stop_reason
                continue
            if role == "toolResult":
                details = message.get("details")
                status = "error" if message.get("isError") is True else "complete"
                payload = {
                    **base_payload,
                    "type": "pi_tool_result",
                    "call_id": clean_text(message.get("toolCallId")) or None,
                    "name": clean_text(message.get("toolName")) or "tool",
                    "status": status,
                    "details": details if isinstance(details, (Mapping, list, tuple)) else None,
                }
                if status == "error":
                    payload["error"] = f"{payload['name']} failed"
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "message",
                        "id": entry_id,
                        "payload": payload,
                    }
                )
                continue
            if role == "bashExecution":
                # Pi can run a direct ``!`` shell command before any user
                # message.  The Sessions contract is Agent Turn evidence, so
                # an unowned shell action must not invent a synthetic Turn.
                if not turn_id:
                    continue
                exit_code = message.get("exitCode")
                cancelled = message.get("cancelled") is True
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "message",
                        "id": entry_id,
                        "payload": {
                            **base_payload,
                            "type": "pi_bash_execution",
                            "name": "bash",
                            "input": {"command": message.get("command")},
                            "exit_code": exit_code,
                            "status": "error" if cancelled or exit_code not in (None, 0, "0") else "complete",
                            "error": "bash execution cancelled" if cancelled else None,
                        },
                    }
                )
                continue
            if role in {"branchSummary", "compactionSummary", "custom", "hookMessage"}:
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "message",
                        "id": entry_id,
                        "payload": {**base_payload, "type": "pi_custom_message"},
                    }
                )
                continue
            projected.append(
                {
                    "timestamp": timestamp,
                    "type": "pi_unknown_message_role",
                    "id": entry_id,
                    "payload": {**base_payload, "role": role},
                }
            )
            continue

        if entry_type == "custom_message":
            projected.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "id": entry_id,
                    "payload": {**base_payload, "type": "custom_message"},
                }
            )
            continue
        if entry_type in _NOISE_TYPES:
            projected.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "id": entry_id,
                    "payload": {**base_payload, "type": entry_type},
                }
            )
            continue
        projected.append(
            {
                "timestamp": timestamp,
                "type": entry_type or "pi_unknown_entry",
                "id": entry_id,
                "payload": base_payload,
            }
        )

    for turn_id, reason in terminal_reasons.items():
        if reason in _TERMINAL_PARTIAL_REASONS:
            turn_warnings.setdefault(turn_id, []).append(
                f"pi_assistant_stop_reason:{reason}"
            )
        elif reason not in {"stop", "aborted"}:
            turn_warnings.setdefault(turn_id, []).append(
                f"pi_unknown_stop_reason:{reason}"
            )
    return projected, warnings, turn_warnings


def _read_jsonl(path: Path, root: Path) -> _FileRead:
    records: List[Mapping[str, Any]] = []
    last_hash: Optional[str] = None
    min_timestamp_ms: Optional[int] = None
    max_timestamp_ms: Optional[int] = None
    incomplete_tail = False
    malformed = False
    locator = _relative_locator(root, path)

    with path.open("rb") as handle:
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if handle.peek(1) == b"":
                    incomplete_tail = True
                else:
                    malformed = True
                continue
            if not isinstance(decoded, Mapping):
                malformed = True
                continue
            records.append(decoded)
            last_hash = _sha(decoded)
            timestamp_ms = _entry_timestamp(decoded)
            if timestamp_ms is not None:
                min_timestamp_ms = min(min_timestamp_ms or timestamp_ms, timestamp_ms)
                max_timestamp_ms = max(max_timestamp_ms or timestamp_ms, timestamp_ms)

    projected, projection_warnings, turn_warnings = _project_records(records, locator)
    if incomplete_tail:
        projection_warnings.append(f"incomplete_tail:{locator}")
    if malformed:
        projection_warnings.append(f"malformed_record:{locator}")
    return _FileRead(
        path=path,
        locator=locator,
        records=records,
        projected=projected,
        last_hash=last_hash,
        min_timestamp_ms=min_timestamp_ms,
        max_timestamp_ms=max_timestamp_ms,
        incomplete_tail=incomplete_tail,
        malformed=malformed,
        projection_warnings=projection_warnings,
        turn_warnings=turn_warnings,
    )


def _stat_matches(path: Path, entry: Mapping[str, Any]) -> bool:
    try:
        current = path.stat()
        return bool(
            int(entry.get("size")) == current.st_size
            and int(entry.get("mtime_ns")) == current.st_mtime_ns
            and int(entry.get("ctime_ns")) == current.st_ctime_ns
            and (entry.get("inode") is None or int(entry.get("inode")) == current.st_ino)
        )
    except (OSError, TypeError, ValueError):
        return False


def _checkpoint_max_event_ms(entry: Mapping[str, Any]) -> Optional[int]:
    value = entry.get("last_event_ms", entry.get("max_event_ms"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _can_skip_unchanged_file(
    path: Path, entry: Mapping[str, Any], from_ms: int, to_ms: int
) -> bool:
    if entry.get("incomplete_tail") or entry.get("malformed") or not _stat_matches(path, entry):
        return False
    max_event_ms = _checkpoint_max_event_ms(entry)
    try:
        min_event_ms = int(entry.get("first_event_ms")) if entry.get("first_event_ms") is not None else None
    except (TypeError, ValueError):
        min_event_ms = None
    return bool(
        (max_event_ms is not None and max_event_ms < from_ms)
        or (min_event_ms is not None and min_event_ms >= to_ms)
    )


class PiAdapter:
    """Read Pi session trees and return standardised turn slices."""

    name = ADAPTER_NAME
    adapter_version = ADAPTER_VERSION
    source_format = SOURCE_FORMAT

    @property
    def cache_generation(self) -> str:
        return adapter_cache_generation(self.name, self.adapter_version)

    def __init__(self, root: Optional[os.PathLike[str] | str] = None) -> None:
        self.root = (
            Path(root).expanduser()
            if root is not None
            else Path.home() / ".pi" / "agent" / "sessions"
        )

    def _files(self) -> List[Path]:
        if not self.root.exists():
            return []
        return sorted(
            (path for path in self.root.rglob("*.jsonl") if path.is_file()),
            key=lambda path: _relative_locator(self.root, path),
        )

    def _checkpoint_entry(self, read: _FileRead) -> Dict[str, Any]:
        stat = read.path.stat()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "inode": stat.st_ino,
            "last_hash": read.last_hash,
            "first_event_ms": read.min_timestamp_ms,
            "last_event_ms": read.max_timestamp_ms,
            "incomplete_tail": read.incomplete_tail,
            "malformed": read.malformed,
        }

    def scan(self, request: Any) -> AdapterResult:
        window = getattr(request, "window", None)
        from_ms = getattr(window, "from_ms", None)
        to_ms = getattr(window, "to_ms", None)
        if from_ms is None or to_ms is None:
            return AdapterResult(
                source=self.name,
                status="failed",
                error={"code": "invalid_window", "message": "Pi scan requires a UTC TimeWindow"},
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )
        from_ms, to_ms = int(from_ms), int(to_ms)
        if from_ms >= to_ms:
            return AdapterResult(
                source=self.name,
                status="failed",
                error={"code": "invalid_window", "message": "from_ms must be less than to_ms"},
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )
        previous_checkpoint = getattr(request, "checkpoint", None)
        previous_checkpoint = previous_checkpoint if isinstance(previous_checkpoint, Mapping) else {}
        if not self.root.exists() or not self.root.is_dir():
            return AdapterResult(
                source=self.name,
                status="failed",
                checkpoint=previous_checkpoint,
                error={
                    "code": "source_unavailable",
                    "source": self.name,
                    "message": "Pi sessions directory is unavailable",
                },
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )

        includes = [str(value) for value in getattr(request, "includes", ())]
        all_paths = self._files()
        previous_files = previous_checkpoint.get("files", {})
        if not isinstance(previous_files, Mapping) or previous_checkpoint.get("cache_generation") != self.cache_generation:
            previous_files = {}
        expected_scope = {"from_ms": from_ms, "to_ms": to_ms, "includes": sorted(includes)}
        cached_result = previous_checkpoint.get("result")
        if (
            previous_checkpoint.get("version") == 2
            and previous_checkpoint.get("source") == self.name
            and previous_checkpoint.get("adapter_version") == self.adapter_version
            and previous_checkpoint.get("cache_generation") == self.cache_generation
            and previous_checkpoint.get("cacheable") is True
            and previous_checkpoint.get("scope") == expected_scope
            and isinstance(cached_result, Mapping)
            and set(previous_files) == {_relative_locator(self.root, path) for path in all_paths}
            and all(
                isinstance(previous_files.get(_relative_locator(self.root, path)), Mapping)
                and _stat_matches(path, previous_files[_relative_locator(self.root, path)])
                for path in all_paths
            )
        ):
            slice_ids = cached_result.get("slice_ids")
            omission_ids = cached_result.get("omission_ids", [])
            cached_warnings = cached_result.get("warnings", [])
            cached_status = cached_result.get("status")
            if (
                cached_status in {"complete", "partial"}
                and isinstance(slice_ids, list)
                and all(isinstance(value, str) and value.startswith("SLC-") for value in slice_ids)
                and isinstance(omission_ids, list)
                and all(isinstance(value, str) and value.startswith("OMN-") for value in omission_ids)
                and isinstance(cached_warnings, list)
            ):
                return AdapterResult(
                    source=self.name,
                    status=str(cached_status),
                    reused_slice_ids=tuple(slice_ids),
                    reused_omission_ids=tuple(omission_ids),
                    checkpoint=previous_checkpoint,
                    warnings=tuple(cached_warnings),
                    stats={
                        "files_examined": len(all_paths),
                        "files_read": 0,
                        "files_skipped": len(all_paths),
                        "records_examined": 0,
                        "slices": len(slice_ids),
                        "reused_from_checkpoint": len(slice_ids),
                    },
                )

        checkpoint: Dict[str, Any] = {
            "files": {},
            "source": self.name,
            "version": 2,
            "adapter_version": self.adapter_version,
            "cache_generation": self.cache_generation,
        }
        reads: List[_FileRead] = []
        warnings: List[str] = []
        skipped_files = 0
        for path in all_paths:
            locator = _relative_locator(self.root, path)
            previous = previous_files.get(locator)
            if isinstance(previous, Mapping) and _can_skip_unchanged_file(path, previous, from_ms, to_ms):
                checkpoint["files"][locator] = dict(previous)
                skipped_files += 1
                continue
            try:
                read = _read_jsonl(path, self.root)
                checkpoint["files"][locator] = self._checkpoint_entry(read)
            except OSError as exc:
                warnings.append(f"read_error:{locator}:{type(exc).__name__}")
                continue
            reads.append(read)
            warnings.extend(read.projection_warnings)

        documents = []
        turn_warnings: Dict[Tuple[str, str], List[str]] = {}
        for read in reads:
            header = next((item for item in read.records if item.get("type") == "session"), {})
            conversation_id = clean_text(header.get("id")) or f"file-{_sha(read.locator)[:24]}"
            for turn_id, values in read.turn_warnings.items():
                turn_warnings[(conversation_id, turn_id)] = list(values)
            documents.append(
                build_session_document(
                    source=self.name,
                    locator=read.locator,
                    records=read.projected,
                    workspace=clean_text(header.get("cwd")) or None,
                    source_meta={
                        "source_format": SOURCE_FORMAT,
                        "session_version": header.get("version"),
                        "has_parent_session": bool(header.get("parentSession")),
                    },
                    incomplete_tail=read.incomplete_tail,
                    malformed=read.malformed,
                )
            )

        normalized = normalize_sessions(
            documents,
            request.window,
            SourceProfile(
                self.name,
                self.adapter_version,
                extra_noise_types=_NOISE_TYPES,
                extra_tool_types=_TOOL_TYPES,
                include_incomplete_tail_warning=True,
            ),
            includes=includes,
        )
        slices: List[Mapping[str, Any]] = []
        for raw_slice in normalized.slices:
            item = dict(raw_slice)
            conversation = item.get("conversation") if isinstance(item.get("conversation"), Mapping) else {}
            native = item.get("native_unit") if isinstance(item.get("native_unit"), Mapping) else {}
            extra = turn_warnings.get((str(conversation.get("id")), str(native.get("id"))), [])
            if extra:
                item["warnings"] = list(dict.fromkeys([*(item.get("warnings") or []), *extra]))
                item["quality_flags"] = list(
                    dict.fromkeys([*(item.get("quality_flags") or []), "pi_source_content_partial"])
                )
                item["content_completeness"] = "partial"
            slices.append(item)
        warnings.extend(normalized.warnings)
        omissions = list(normalized.omissions)
        status = "complete"
        if warnings or any(item.get("content_completeness") != "complete" for item in slices):
            status = "partial"
        if not slices and any(value.startswith("read_error:") for value in warnings):
            status = "failed"
        warnings = list(dict.fromkeys(warnings))
        checkpoint.update(
            {
                "cacheable": not any(
                    value.startswith(("read_error:", "incomplete_tail:", "malformed_record:"))
                    for value in warnings
                ),
                "scope": expected_scope,
                "result": {
                    "status": status,
                    "slice_ids": [str(item.get("slice_id")) for item in slices if item.get("slice_id")],
                    "omission_ids": [str(item.get("omission_id")) for item in omissions if item.get("omission_id")],
                    "warnings": warnings,
                },
            }
        )
        return AdapterResult(
            source=self.name,
            status=status,
            slices=slices,
            omissions=omissions,
            checkpoint=checkpoint,
            warnings=warnings,
            stats={
                "files_examined": len(all_paths),
                "files_read": len(reads),
                "files_skipped": skipped_files,
                **normalized.stats,
                "slices": len(slices),
            },
        )


__all__ = ["PiAdapter"]
