"""Deterministic discovery and checkpointing for DeepSeek harness session logs.

Sessions live under ``~/.dsh/sessions`` as one directory per session named
``session-<uuid>`` containing a single ``session.jsonl.zstd`` file.  Each line
is one event record with ``type``, ``seq``, ``time`` (epoch milliseconds) and
a ``data`` payload.  File discovery, partial-tail handling, checkpointing and
turn normalization reuse the shared Responses-style machinery in
``responses.py``; source specifics are declared on the profile below.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .core import AdapterResult, adapter_cache_generation
from .responses import (
    SourceProfile,
    clean_text,
    build_session_document,
    iso_from_ms,
    normalize_sessions,
    parse_timestamp,
    payload_of,
    thread_from_record,
)


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


# Event types that are pure harness protocol or streaming internals: they
# never carry evidence and only inflate the noise counts if classified
# individually.
_DEEPSEEK_NOISE_TYPES = frozenset(
    {
        # Session bootstrap and policy declarations.
        "permission/preset",
        "sandbox/mode",
        "approval/policy",
        "request/header",
        "request/context",
        # Streaming fragments; the assembled messages are read instead.
        "assistant/chunk",
        "text-chunks",
        "reasoning-chunks",
        "tool-call-chunks",
        # Inbox bookkeeping duplicated by user/message and assistant/message.
        "agent/inbox/spliced",
        # Derived title events; the title is kept as conversation metadata.
        "session/title-llm-request",
    }
)

# Event types that record real tool activity and are summarised into
# execution evidence rather than quoted.
_DEEPSEEK_TOOL_TYPES = frozenset({"tool/call", "tool/result"})


@dataclass
class _FileRead:
    path: Path
    locator: str
    records: List[Mapping[str, Any]]
    last_hash: Optional[str]
    min_timestamp_ms: Optional[int]
    max_timestamp_ms: Optional[int]
    thread_ids: List[str]
    incomplete_tail: bool
    malformed: bool


def _zstd_available() -> bool:
    return shutil.which("zstd") is not None


def _decompress(path: Path) -> bytes:
    completed = subprocess.run(
        ["zstd", "-dc", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise OSError(f"zstd failed with exit code {completed.returncode}")
    return completed.stdout


def _read_records(path: Path) -> Tuple[List[Mapping[str, Any]], bool]:
    """Read one session.jsonl.zstd into records.

    A decompression or parse failure that leaves no complete records marks the
    whole file malformed; a trailing partial line marks an incomplete tail so
    the next scan re-reads the file.
    """

    raw = _decompress(path)
    records: List[Mapping[str, Any]] = []
    incomplete_tail = False
    malformed = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if line is raw.splitlines()[-1]:
                incomplete_tail = True
            else:
                malformed = True
            continue
        if not isinstance(decoded, Mapping):
            malformed = True
            continue
        records.append(decoded)
    if malformed or incomplete_tail:
        if not records:
            malformed = True
    return records, incomplete_tail or malformed


def _project_record(record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Project one .dsh event onto the Responses-shaped record contract.

    The shared normalization layer understands ``timestamp``/``payload``
    records with a small set of outer/payload types.  The projection is pure
    and deterministic: it only renames fields and drops streaming fragments;
    no evidence is invented or merged across events.
    """

    outer_type = str(record.get("type", "")).strip()
    data = record.get("data")
    payload: Dict[str, Any] = dict(data) if isinstance(data, Mapping) else {}
    timestamp_ms = record.get("time") if "time" in record else record.get("timestamp")
    timestamp = iso_from_ms(parse_timestamp(timestamp_ms))
    turn = payload.get("turn", record.get("turn"))
    if isinstance(turn, int) or (isinstance(turn, str) and turn.isdigit()):
        turn = f"turn-{turn}"
    elif turn is not None:
        turn = str(turn)

    if outer_type == "session":
        return {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": record.get("id"), "cwd": record.get("cwd")},
        }
    if outer_type == "session/title":
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "thread_name_updated", "thread_name": title},
        }
    if outer_type in {"user/message"}:
        payload_out: Dict[str, Any] = {
            "role": "user",
            "content": payload.get("content"),
        }
        if payload.get("id") is not None:
            payload_out["id"] = payload["id"]
        if turn is not None:
            payload_out["turn_id"] = turn
        return {"timestamp": timestamp, "type": "message", "payload": payload_out}
    if outer_type in {"assistant/message"}:
        message = payload.get("message")
        content = message.get("content") if isinstance(message, Mapping) else payload.get("content")
        # Keep only spoken text; reasoning and tool-call blocks are summarised
        # from the dedicated tool events instead of quoted here.
        if isinstance(content, list):
            content = [item for item in content if isinstance(item, Mapping) and item.get("type") == "text"]
        # A step that only emitted reasoning or tool calls produces no spoken
        # text; dropping it here keeps such events from being flagged as
        # unknown record types.
        if not content:
            return None
        payload_out: Dict[str, Any] = {"role": "assistant", "content": content, "phase": "final"}
        source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        if isinstance(message, Mapping) and message.get("id") is not None:
            payload_out["id"] = message["id"]
        if source.get("model") is not None:
            payload_out["model"] = source["model"]
        if turn is not None:
            payload_out["turn_id"] = turn
        return {"timestamp": timestamp, "type": "message", "payload": payload_out}
    if outer_type in {"tool/call", "tool/result"}:
        payload_out = {"type": "tool_call" if outer_type == "tool/call" else "tool_result_output"}
        if turn is not None:
            payload_out["turn_id"] = turn
        if payload.get("callId") is not None:
            payload_out["call_id"] = payload["callId"]
        message = payload.get("message") if "message" in payload else payload
        if isinstance(message, Mapping):
            if message.get("name") is not None:
                payload_out["name"] = message["name"]
            arguments = message.get("arguments")
            if isinstance(arguments, str):
                payload_out["input"] = arguments
            elif arguments is not None:
                payload_out["input"] = _canonical_json(arguments)
            if message.get("id") is not None:
                payload_out["id"] = message["id"]
            if isinstance(message.get("source"), Mapping) and message["source"].get("callId") is not None:
                payload_out["call_id"] = message["source"]["callId"]
            if "callId" in payload:
                payload_out.pop("call_id", None)
                payload_out["call_id"] = payload["callId"]
            if outer_type == "tool/result":
                # Result halves end in ``_output`` so the summarizer counts the
                # call once instead of spending the evidence budget twice.
                payload_out["type"] = "tool_result_output"
                blocks = message.get("content")
                if isinstance(blocks, list):
                    texts = []
                    failed = False
                    for block in blocks:
                        if not isinstance(block, Mapping):
                            continue
                        if block.get("isError"):
                            failed = True
                        for part in block.get("content") or []:
                            if isinstance(part, Mapping) and part.get("type") == "text":
                                texts.append(clean_text(part.get("text")))
                    if failed:
                        payload_out["status"] = "failed"
                    if texts:
                        payload_out["message"] = "\n".join(texts)
        return {"timestamp": timestamp, "type": "response_item", "payload": payload_out}
    if outer_type in {"turn/start", "turn/end"}:
        payload_out = {"type": "turn_context", "turn_id": turn}
        if outer_type == "turn/end":
            reason = payload.get("reason")
            if isinstance(reason, Mapping):
                payload_out["turn_end_reason"] = reason.get("kind")
        return {"timestamp": timestamp, "type": "turn_context", "payload": payload_out}
    # Streaming chunks, inbox splices, request headers and policy events are
    # dropped here; they are already counted as noise by the outer-type
    # filter in the profile and never carry quotable evidence.
    return None


class DeepseekAdapter:
    """Read DeepSeek harness session archives and delegate turn parsing."""

    name = "deepseek"
    # Bumped only for DeepSeek-specific parsing changes. Shared extraction and
    # Slice Schema revisions enter the composed checkpoint generation below.
    adapter_version = "1"
    source_format = "deepseek-dsh-session-jsonl-zstd"

    @property
    def cache_generation(self) -> str:
        return adapter_cache_generation(self.name, self.adapter_version)

    def __init__(self, root: Optional[os.PathLike[str] | str] = None):
        self.root = Path(root).expanduser() if root is not None else Path.home() / ".dsh" / "sessions"

    def _files(self) -> List[Path]:
        if not self.root.is_dir():
            return []
        paths = [
            path
            for path in self.root.rglob("session.jsonl.zstd")
            if path.is_file()
        ]
        unique: Dict[str, Path] = {}
        for path in paths:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            unique.setdefault(key, path)
        return sorted(unique.values(), key=lambda path: _relative_locator(self.root, path))

    def _read(self, path: Path, locator: str, warnings: List[str]) -> Optional[_FileRead]:
        records: List[Mapping[str, Any]] = []
        incomplete_tail = False
        malformed = False
        try:
            records, incomplete_tail = _read_records(path)
        except (OSError, ValueError) as exc:
            warnings.append(f"read_error:{locator}:{type(exc).__name__}")
            return None
        if not records:
            warnings.append(f"malformed_record:{locator}")
            return _FileRead(
                path=path,
                locator=locator,
                records=[],
                last_hash=None,
                min_timestamp_ms=None,
                max_timestamp_ms=None,
                thread_ids=[],
                incomplete_tail=False,
                malformed=True,
            )
        last_hash: Optional[str] = None
        min_timestamp_ms: Optional[int] = None
        max_timestamp_ms: Optional[int] = None
        thread_ids: List[str] = []
        projected: List[Mapping[str, Any]] = []
        for record in records:
            projected_record = _project_record(record)
            if projected_record is None:
                continue
            projected.append(projected_record)
        records = projected
        for record in records:
            last_hash = _sha(record)
            timestamp_ms = parse_timestamp(record.get("time") if "time" in record else record.get("timestamp"))
            if timestamp_ms is not None:
                min_timestamp_ms = min(min_timestamp_ms or timestamp_ms, timestamp_ms)
                max_timestamp_ms = max(max_timestamp_ms or timestamp_ms, timestamp_ms)
            session_record = record if str(record.get("type", "")).lower() == "session" else {}
            thread = thread_from_record(session_record, payload_of(record), "")
            if thread and thread not in thread_ids:
                thread_ids.append(thread)
        if malformed:
            warnings.append(f"malformed_record:{locator}")
        if incomplete_tail:
            warnings.append(f"incomplete_tail:{locator}")
        return _FileRead(
            path=path,
            locator=locator,
            records=records,
            last_hash=last_hash,
            min_timestamp_ms=min_timestamp_ms,
            max_timestamp_ms=max_timestamp_ms,
            thread_ids=thread_ids,
            incomplete_tail=incomplete_tail,
            malformed=malformed,
        )

    def _checkpoint_entry(self, read: _FileRead) -> Dict[str, Any]:
        try:
            stat = read.path.stat()
        except OSError:
            raise
        entry: Dict[str, Any] = {
            "locator": read.locator,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "inode": stat.st_ino,
            "threads": read.thread_ids,
            "record_hash": read.last_hash,
            "last_record_hash": read.last_hash,
            "last_complete_record_hash": read.last_hash,
            "first_event_ms": read.min_timestamp_ms,
            "min_event_ms": read.min_timestamp_ms,
            "last_event_ms": read.max_timestamp_ms,
            "max_event_time": iso_from_ms(read.max_timestamp_ms),
            "incomplete_tail": read.incomplete_tail,
            "malformed": read.malformed,
        }
        return entry

    def scan(self, request: Any) -> AdapterResult:
        window = getattr(request, "window", None)
        from_ms = getattr(window, "from_ms", None)
        to_ms = getattr(window, "to_ms", None)
        if from_ms is None or to_ms is None:
            return AdapterResult(
                source=self.name,
                status="failed",
                error={"code": "invalid_window", "message": "DeepSeek scan requires a UTC TimeWindow with from_ms and to_ms"},
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
        if not self.root.exists() or not self.root.is_dir():
            return AdapterResult(
                source=self.name,
                status="failed",
                checkpoint=getattr(request, "checkpoint", None) if isinstance(getattr(request, "checkpoint", None), Mapping) else None,
                error={
                    "code": "source_unavailable",
                    "source": self.name,
                    "message": "DeepSeek sessions directory is unavailable",
                },
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )

        includes = [str(value) for value in request.includes]
        global_warnings: List[str] = []
        file_reads: List[_FileRead] = []
        all_paths = self._files()
        previous_checkpoint = getattr(request, "checkpoint", None)
        previous_files = {}
        if isinstance(previous_checkpoint, Mapping):
            value = previous_checkpoint.get("files", {})
            if isinstance(value, Mapping):
                previous_files = value
            if previous_checkpoint.get("cache_generation") != self.cache_generation:
                previous_files = {}
        expected_scope = {"from_ms": from_ms, "to_ms": to_ms, "includes": sorted(includes)}
        cached_result = previous_checkpoint.get("result") if isinstance(previous_checkpoint, Mapping) else None
        if (
            isinstance(previous_checkpoint, Mapping)
            and previous_checkpoint.get("version") == 2
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
                and isinstance(cached_warnings, list)
                and isinstance(omission_ids, list)
                and all(isinstance(value, str) and value.startswith("OMN-") for value in omission_ids)
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
                        "records_deduplicated": 0,
                        "known_noise_records": 0,
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
        skipped_files = 0
        for path in all_paths:
            locator = _relative_locator(self.root, path)
            previous_entry = previous_files.get(locator)
            if isinstance(previous_entry, Mapping) and _can_skip_unchanged_file(path, previous_entry, from_ms, to_ms):
                checkpoint["files"][locator] = dict(previous_entry)
                skipped_files += 1
                continue
            read = self._read(path, locator, global_warnings)
            if read is None:
                continue
            file_reads.append(read)
            try:
                checkpoint["files"][locator] = self._checkpoint_entry(read)
            except OSError as exc:
                global_warnings.append(f"stat_error:{locator}:{type(exc).__name__}")
                file_reads.pop()

        documents = [
            build_session_document(
                source=self.name,
                locator=read.locator,
                records=read.records,
                incomplete_tail=read.incomplete_tail,
                malformed=read.malformed,
            )
            for read in file_reads
        ]
        normalized = normalize_sessions(
            documents,
            request.window,
            SourceProfile(
                self.name,
                self.adapter_version,
                extra_noise_types=_DEEPSEEK_NOISE_TYPES,
                extra_tool_types=_DEEPSEEK_TOOL_TYPES,
            ),
            includes=includes,
        )
        global_warnings.extend(normalized.warnings)
        slices = list(normalized.slices)
        omissions = list(normalized.omissions)
        status = "complete"
        if global_warnings or any(item.get("content_completeness") != "complete" for item in slices):
            status = "partial"
        if not slices and any(value.startswith("read_error:") for value in global_warnings):
            status = "failed"
        checkpoint.update(
            {
                "cacheable": not any(
                    value.startswith(("read_error:", "stat_error:")) for value in global_warnings
                ),
                "scope": expected_scope,
                "result": {
                    "status": status,
                    "slice_ids": [str(item.get("slice_id")) for item in slices if item.get("slice_id")],
                    "omission_ids": [str(item.get("omission_id")) for item in omissions if item.get("omission_id")],
                    "warnings": list(dict.fromkeys(global_warnings)),
                },
            }
        )
        return AdapterResult(
            source=self.name,
            status=status,
            slices=slices,
            omissions=omissions,
            checkpoint=checkpoint,
            warnings=list(dict.fromkeys(global_warnings)),
            stats={
                "files_examined": len(all_paths),
                "files_read": len(file_reads),
                "files_skipped": skipped_files,
                **normalized.stats,
                "slices": len(slices),
            },
        )


def _stat_matches(path: Path, entry: Mapping[str, Any]) -> bool:
    try:
        current = path.stat()
    except OSError:
        return False
    try:
        if int(entry.get("size")) != current.st_size or int(entry.get("mtime_ns")) != current.st_mtime_ns:
            return False
        if entry.get("ctime_ns") is None or int(entry.get("ctime_ns")) != current.st_ctime_ns:
            return False
        if entry.get("inode") is not None and int(entry.get("inode")) != current.st_ino:
            return False
        return True
    except (TypeError, ValueError):
        return False


def _checkpoint_max_event_ms(entry: Mapping[str, Any]) -> Optional[int]:
    value = entry.get("last_event_ms", entry.get("max_event_ms"))
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return parse_timestamp(entry.get("max_event_time", entry.get("last_event_time")))


def _can_skip_unchanged_file(path: Path, entry: Mapping[str, Any], from_ms: int, to_ms: int) -> bool:
    if entry.get("incomplete_tail") or entry.get("malformed"):
        return False
    if not _stat_matches(path, entry):
        return False
    max_event_ms = _checkpoint_max_event_ms(entry)
    min_event_value = entry.get("first_event_ms", entry.get("min_event_ms"))
    try:
        min_event_ms = int(min_event_value) if min_event_value is not None else None
    except (TypeError, ValueError):
        min_event_ms = None
    return bool(
        (max_event_ms is not None and max_event_ms < from_ms)
        or (min_event_ms is not None and min_event_ms >= to_ms)
    )


__all__ = ["DeepseekAdapter"]
