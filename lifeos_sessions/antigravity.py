"""AntiGravity CLI source adapter.

AntiGravity CLI stores per-conversation transcripts under
``~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/``
(``transcript_full.jsonl`` with ``transcript.jsonl`` fallback) and optional
metadata in ``~/.gemini/antigravity-cli/conversation_summaries.db``.

This adapter owns only source discovery, read-only SQLite enrichment,
transcript projection onto the shared Responses-shaped normalizer, and
checkpointing.  It never reads ``history.jsonl``, ``settings.json``, or binary
protobuf state, and never writes to the AntiGravity directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from .core import AdapterResult, SourceScanRequest, adapter_cache_generation
from .evidence import record_locator
from .responses import (
    SourceProfile,
    build_session_document,
    clean_text,
    iso_from_ms,
    normalize_sessions,
    parse_timestamp,
)


ADAPTER_NAME = "antigravity"
ADAPTER_VERSION = "1"
SOURCE_FORMAT = "antigravity-cli-transcript-jsonl"

_NON_DEGRADING_WARNINGS = ("index_unavailable:", "index_entry_missing:")
_NOISE_TYPES = frozenset({"system_message"})
_TOOL_TYPES = frozenset({"ag_tool_call", "ag_tool_result"})

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_STRIP_WRAPPER_TAGS_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>",
    re.DOTALL,
)
_COMMAND_EXIT_RE = re.compile(r"(?m)^Command exited with code (-?\d+)\.\s*$")
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_TERMINAL_ABORT_STATUSES = frozenset({"CANCELLED", "CANCELED", "ABORTED"})


@dataclass(frozen=True)
class _IndexEntry:
    conversation_id: str
    title: Optional[str]
    workspace: Optional[str]
    parent_conversation_id: Optional[str]
    last_modified_ms: Optional[int]

    @property
    def fingerprint(self) -> str:
        payload = {
            "conversation_id": self.conversation_id,
            "title": self.title,
            "workspace": self.workspace,
            "parent_conversation_id": self.parent_conversation_id,
            "last_modified_ms": self.last_modified_ms,
        }
        return _sha(payload)


@dataclass
class _FileRead:
    path: Path
    locator: str
    checkpoint_key: str
    conversation_id: str
    entry: Optional[_IndexEntry]
    records: List[Mapping[str, Any]]
    projected: List[Mapping[str, Any]]
    last_hash: Optional[str]
    min_timestamp_ms: Optional[int]
    max_timestamp_ms: Optional[int]
    incomplete_tail: bool
    malformed: bool
    used_compact_fallback: bool
    projection_warnings: List[str]
    turn_warnings: Dict[str, List[str]]


def default_antigravity_roots(home: Optional[Path] = None) -> List[Path]:
    """Return known AntiGravity session roots (standalone CLI and T3 antigravity-acp)."""
    base_home = home or Path.home()
    roots = [base_home / ".gemini" / "antigravity-cli"]
    t3_provider_dir = base_home / ".t3" / "userdata" / "providers" / "antigravity"
    if t3_provider_dir.is_dir():
        roots.extend(
            path
            for path in sorted(t3_provider_dir.glob("*/antigravity-acp"), key=lambda item: str(item))
            if path.is_dir()
        )
    return roots


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _relative_locator(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return path.name


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _decode_workspace_uris(raw: Any) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, list):
        return None
    for item in decoded:
        if not isinstance(item, str) or not item.strip():
            continue
        text = item.strip()
        if text.startswith("file://"):
            parsed = urlparse(text)
            if parsed.path:
                return unquote(parsed.path)
        elif text.startswith("/"):
            return text
    return None


def _extract_user_text(content: Any) -> str:
    if not isinstance(content, str):
        return clean_text(content)
    match = _USER_REQUEST_RE.search(content)
    if match:
        return clean_text(match.group(1))
    stripped = _STRIP_WRAPPER_TAGS_RE.sub("", content)
    return clean_text(stripped)


def _compact_tool_input(tool_name: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    if tool_name == "run_command":
        cmd = clean_text(args.get("CommandLine") or args.get("command") or args.get("cmd"))
        return {"command": cmd} if cmd else {}
    if tool_name in _WRITE_TOOLS or tool_name == "read_file":
        path = clean_text(args.get("Path") or args.get("path") or args.get("file"))
        return {"path": path} if path else {}
    if tool_name == "invoke_subagent":
        summary: Dict[str, Any] = {}
        for src_key, dst_key in (("TypeName", "type"), ("Role", "role")):
            val = clean_text(args.get(src_key))
            if val:
                summary[dst_key] = val
        return summary
    compact: Dict[str, Any] = {}
    for src_key, dst_key in (
        ("Path", "path"),
        ("path", "path"),
        ("CommandLine", "command"),
        ("command", "command"),
        ("query", "query"),
        ("url", "url"),
    ):
        if dst_key in compact:
            continue
        val = clean_text(args.get(src_key))
        if val:
            compact[dst_key] = val[:400]
    return compact


def _changed_targets(tool_name: str, args: Mapping[str, Any]) -> List[str]:
    if tool_name not in _WRITE_TOOLS:
        return []
    path = clean_text(args.get("Path") or args.get("path"))
    return [path] if path else []


def _parse_exit_code(content: Any) -> Optional[int]:
    if not isinstance(content, str) or not content:
        return None
    matches = list(_COMMAND_EXIT_RE.finditer(content))
    if not matches:
        return None
    try:
        return int(matches[-1].group(1))
    except (TypeError, ValueError):
        return None


def _project_records(
    records: Sequence[Mapping[str, Any]],
    conversation_id: str,
    locator: str,
    *,
    entry: Optional[_IndexEntry],
    used_compact_fallback: bool,
) -> Tuple[List[Mapping[str, Any]], List[str], Dict[str, List[str]]]:
    warnings: List[str] = []
    turn_warnings: Dict[str, List[str]] = {}
    first_ts = next(
        (parse_timestamp(record.get("created_at")) for record in records if parse_timestamp(record.get("created_at")) is not None),
        None,
    )
    projected: List[Mapping[str, Any]] = [
        {
            "timestamp": iso_from_ms(first_ts),
            "type": "session_meta",
            "payload": {
                "id": conversation_id,
                "session_id": conversation_id,
                "cwd": entry.workspace if entry else None,
                "parent_thread_id": entry.parent_conversation_id if entry else None,
            },
        }
    ]
    if used_compact_fallback:
        warnings.append(f"antigravity_compact_transcript:{locator}")

    current_turn: Optional[str] = None
    pending_calls: List[Tuple[str, str]] = []

    for line_no, record in enumerate(records, start=1):
        step_index = record.get("step_index")
        step_id = f"step-{step_index}" if isinstance(step_index, int) and not isinstance(step_index, bool) else f"line-{line_no}"
        timestamp = iso_from_ms(parse_timestamp(record.get("created_at")))
        step_type = str(record.get("type") or "").strip()
        step_source = str(record.get("source") or "").strip()
        step_status = str(record.get("status") or "").strip().upper()
        evidence = record.get("_evidence_record")

        if used_compact_fallback and isinstance(record.get("truncated_fields"), list) and record["truncated_fields"]:
            if current_turn:
                turn_warnings.setdefault(current_turn, []).append("antigravity_compact_transcript_truncated")
            else:
                warnings.append(f"antigravity_compact_transcript_truncated:{locator}:{step_id}")

        if step_type == "USER_INPUT":
            current_turn = step_id
            pending_calls.clear()
            if isinstance(record.get("media"), Sequence) and not isinstance(record.get("media"), (str, bytes, bytearray)) and record["media"]:
                turn_warnings.setdefault(current_turn, []).append("antigravity_media_unparsed")
            user_text = _extract_user_text(record.get("content"))
            projected.append(
                {
                    "timestamp": timestamp,
                    "type": "message",
                    "id": step_id,
                    "payload": {
                        "turn_id": current_turn,
                        "role": "user",
                        "content": [{"type": "text", "text": user_text}] if user_text else [],
                    },
                }
            )
            continue

        base_payload: Dict[str, Any] = {}
        if current_turn:
            base_payload["turn_id"] = current_turn

        if step_type == "PLANNER_RESPONSE":
            raw_calls = record.get("tool_calls")
            tool_calls = [item for item in raw_calls if isinstance(item, Mapping)] if isinstance(raw_calls, list) else []
            for idx, tc in enumerate(tool_calls):
                cid = f"{step_id}:tc:{idx}"
                tname = clean_text(tc.get("name")) or "tool"
                targs = tc.get("args") if isinstance(tc.get("args"), Mapping) else {}
                pending_calls.append((cid, tname))
                call_record: Dict[str, Any] = {
                    "timestamp": timestamp,
                    "type": "message",
                    "id": cid,
                    "payload": {
                        **base_payload,
                        "type": "ag_tool_call",
                        "call_id": cid,
                        "name": tname,
                        "input": _compact_tool_input(tname, targs),
                        "changed_targets": _changed_targets(tname, targs),
                    },
                }
                if evidence:
                    call_record["_evidence_record"] = evidence
                projected.append(call_record)

            content_text = clean_text(record.get("content"))
            if content_text:
                is_final = (not tool_calls) and (step_status == "DONE")
                msg_payload: Dict[str, Any] = {
                    **base_payload,
                    "role": "assistant",
                    "content": [{"type": "text", "text": content_text}],
                }
                if is_final:
                    msg_payload["phase"] = "final"
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "message",
                        "id": step_id,
                        "payload": msg_payload,
                    }
                )

            if step_status in _TERMINAL_ABORT_STATUSES:
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "id": f"{step_id}:abort",
                        "payload": {
                            **base_payload,
                            "type": "turn_aborted",
                            "reason": step_status.lower(),
                        },
                    }
                )
            elif step_status == "ERROR" or record.get("error"):
                projected.append(
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "id": f"{step_id}:error",
                        "payload": {
                            **base_payload,
                            "type": "error",
                            "message": clean_text(record.get("error")) or "planner response error",
                        },
                    }
                )
            continue

        if step_type == "GENERIC":
            if pending_calls:
                cid, tname = pending_calls.pop(0)
            else:
                cid, tname = None, "tool"
            exit_code = _parse_exit_code(record.get("content")) if tname in {"run_command", "tool"} else None
            err_text = clean_text(record.get("error"))
            if step_status in _TERMINAL_ABORT_STATUSES:
                tool_status = "interrupted"
            elif step_status == "ERROR" or err_text or (exit_code is not None and exit_code != 0):
                tool_status = "error"
            elif step_status == "RUNNING":
                tool_status = "running"
            elif step_status == "DONE":
                tool_status = "complete"
            else:
                tool_status = "unknown"

            payload: Dict[str, Any] = {
                **base_payload,
                "type": "ag_tool_result",
                "name": tname,
                "status": tool_status,
            }
            if cid:
                payload["call_id"] = cid
            if exit_code is not None:
                payload["exit_code"] = exit_code
            if tool_status in {"error", "interrupted"}:
                if err_text:
                    payload["error"] = err_text
                elif exit_code is not None and exit_code != 0:
                    payload["error"] = f"{tname} exited with code {exit_code}"
                else:
                    payload["error"] = f"{tname} {tool_status}"
            result_record: Dict[str, Any] = {
                "timestamp": timestamp,
                "type": "message",
                "id": step_id,
                "payload": payload,
            }
            if evidence:
                result_record["_evidence_record"] = evidence
            projected.append(result_record)
            continue

        if step_type == "SYSTEM_MESSAGE" or step_source == "SYSTEM":
            projected.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "id": step_id,
                    "payload": {**base_payload, "type": "system_message"},
                }
            )
            continue

        projected.append(
            {
                "timestamp": timestamp,
                "type": step_type or "antigravity_unknown_step",
                "id": step_id,
                "payload": base_payload,
            }
        )

    return projected, warnings, turn_warnings


def _read_jsonl(
    path: Path,
    root: Path,
    conversation_id: str,
    *,
    checkpoint_key: str,
    entry: Optional[_IndexEntry],
    used_compact_fallback: bool,
) -> _FileRead:
    records: List[Mapping[str, Any]] = []
    last_hash: Optional[str] = None
    min_timestamp_ms: Optional[int] = None
    max_timestamp_ms: Optional[int] = None
    incomplete_tail = False
    malformed = False
    locator = _relative_locator(root, path)

    with path.open("rb") as handle:
        position = 0
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            start = position
            position += len(raw_line)
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
            records.append({**decoded, "_evidence_record": record_locator(locator, raw_line, start)})
            last_hash = _sha(decoded)
            timestamp_ms = parse_timestamp(decoded.get("created_at"))
            if timestamp_ms is not None:
                min_timestamp_ms = min(min_timestamp_ms or timestamp_ms, timestamp_ms)
                max_timestamp_ms = max(max_timestamp_ms or timestamp_ms, timestamp_ms)

    projected, projection_warnings, turn_warnings = _project_records(
        records,
        conversation_id,
        locator,
        entry=entry,
        used_compact_fallback=used_compact_fallback,
    )
    if incomplete_tail:
        projection_warnings.append(f"incomplete_tail:{locator}")
    if malformed:
        projection_warnings.append(f"malformed_record:{locator}")
    return _FileRead(
        path=path,
        locator=locator,
        checkpoint_key=checkpoint_key,
        conversation_id=conversation_id,
        entry=entry,
        records=records,
        projected=projected,
        last_hash=last_hash,
        min_timestamp_ms=min_timestamp_ms,
        max_timestamp_ms=max_timestamp_ms,
        incomplete_tail=incomplete_tail,
        malformed=malformed,
        used_compact_fallback=used_compact_fallback,
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


def _can_skip_unchanged_file(
    path: Path,
    entry: Mapping[str, Any],
    from_ms: int,
    to_ms: int,
    index_fingerprint: Optional[str],
) -> bool:
    if entry.get("incomplete_tail") or entry.get("malformed"):
        return False
    if entry.get("index_fingerprint") != index_fingerprint:
        return False
    if not _stat_matches(path, entry):
        return False
    max_event_ms = entry.get("last_event_ms")
    min_event_ms = entry.get("first_event_ms")
    try:
        max_ms = int(max_event_ms) if max_event_ms is not None else None
        min_ms = int(min_event_ms) if min_event_ms is not None else None
    except (TypeError, ValueError):
        return False
    return bool(
        (max_ms is not None and max_ms < from_ms)
        or (min_ms is not None and min_ms >= to_ms)
    )


class AntigravityAdapter:
    """Read AntiGravity CLI conversation transcripts and return turn slices."""

    name = ADAPTER_NAME
    adapter_version = ADAPTER_VERSION
    source_format = SOURCE_FORMAT

    @property
    def cache_generation(self) -> str:
        return adapter_cache_generation(self.name, self.adapter_version)

    def __init__(
        self,
        root: Optional[os.PathLike[str] | str] = None,
        *,
        roots: Optional[Sequence[os.PathLike[str] | str]] = None,
    ) -> None:
        if roots is not None:
            resolved_roots = [Path(item).expanduser() for item in roots]
            if not resolved_roots:
                raise ValueError("AntiGravity roots must not be empty")
            self.roots = resolved_roots
        elif root is not None:
            self.roots = [Path(root).expanduser()]
        else:
            self.roots = default_antigravity_roots()
        self.root = self.roots[0]
        self.brain_root = self.root / "brain"
        self.index_path = self.root / "conversation_summaries.db"

    def _query_index(self, uri: str) -> Tuple[Dict[str, _IndexEntry], List[str]]:
        entries: Dict[str, _IndexEntry] = {}
        parents: Dict[str, str] = {}
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "conversation_parents" in tables:
                for row in connection.execute(
                    "SELECT conversation_id, parent_id FROM conversation_parents"
                ).fetchall():
                    cid = clean_text(row["conversation_id"])
                    pid = clean_text(row["parent_id"])
                    if cid and pid:
                        parents[cid] = pid
            if "conversation_summaries" not in tables:
                return {}, ["index_unavailable:missing_conversation_summaries_table"]
            rows = connection.execute(
                "SELECT conversation_id, title, workspace_uris, "
                "parent_conversation_id, last_modified_time "
                "FROM conversation_summaries"
            ).fetchall()
        finally:
            connection.close()

        for row in rows:
            cid = clean_text(row["conversation_id"])
            if not cid:
                continue
            parent_id = clean_text(row["parent_conversation_id"]) or parents.get(cid)
            entries[cid] = _IndexEntry(
                conversation_id=cid,
                title=clean_text(row["title"]) or None,
                workspace=_decode_workspace_uris(row["workspace_uris"]),
                parent_conversation_id=parent_id or None,
                last_modified_ms=parse_timestamp(row["last_modified_time"]),
            )
        return entries, []

    def _load_index_from_root(self, root: Path) -> Tuple[Dict[str, _IndexEntry], List[str]]:
        index_path = root / "conversation_summaries.db"
        if not index_path.is_file():
            return {}, ["index_unavailable:conversation_summaries.db"]
        base_uri = index_path.resolve().as_uri()
        wal_path = index_path.parent / f"{index_path.name}-wal"
        try:
            try:
                return self._query_index(base_uri + "?mode=ro")
            except sqlite3.OperationalError:
                # SQLite databases with WAL header flags but no -wal/-shm sidecars
                # fail to open under ?mode=ro unless immutable=1 is set.
                if wal_path.exists():
                    raise
                return self._query_index(base_uri + "?mode=ro&immutable=1")
        except (OSError, sqlite3.DatabaseError) as exc:
            return {}, [f"index_unavailable:{type(exc).__name__}"]

    def _load_index(self) -> Tuple[Dict[str, _IndexEntry], List[str]]:
        return self._load_index_from_root(self.root)

    def _discover_from_root(self, root: Path) -> Tuple[List[Tuple[str, Path, bool]], List[str]]:
        brain_root = root / "brain"
        if not brain_root.is_dir():
            return [], []
        discovered: List[Tuple[str, Path, bool]] = []
        warnings: List[str] = []
        for conv_dir in sorted((p for p in brain_root.iterdir() if p.is_dir()), key=lambda p: p.name):
            logs_dir = conv_dir / ".system_generated" / "logs"
            full_path = logs_dir / "transcript_full.jsonl"
            compact_path = logs_dir / "transcript.jsonl"
            if full_path.is_file():
                target, used_compact = full_path, False
            elif compact_path.is_file():
                target, used_compact = compact_path, True
            else:
                continue
            try:
                resolved = target.resolve()
            except OSError:
                resolved = target.absolute()
            if not _is_within(root, resolved):
                warnings.append(f"source_path_outside_root:{_relative_locator(root, target)}")
                continue
            discovered.append((conv_dir.name, resolved, used_compact))
        return discovered, warnings

    def _discover(self) -> Tuple[List[Tuple[str, Path, bool]], List[str]]:
        return self._discover_from_root(self.root)

    def _checkpoint_entry(self, read: _FileRead, index_fingerprint: Optional[str]) -> Dict[str, Any]:
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
            "index_fingerprint": index_fingerprint,
        }

    def scan(self, request: Any) -> AdapterResult:
        window = getattr(request, "window", None)
        from_ms = getattr(window, "from_ms", None)
        to_ms = getattr(window, "to_ms", None)
        if from_ms is None or to_ms is None:
            return AdapterResult(
                source=self.name,
                status="failed",
                error={"code": "invalid_window", "message": "AntiGravity scan requires a UTC TimeWindow"},
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
        if len(self.roots) == 1:
            active_roots = [self.roots[0]] if self.roots[0].is_dir() else []
        else:
            existing_roots = [item for item in self.roots if item.is_dir()]
            active_roots = [item for item in existing_roots if (item / "brain").is_dir()] or existing_roots[:1]
        if not active_roots:
            return AdapterResult(
                source=self.name,
                status="failed",
                checkpoint=previous_checkpoint,
                error={
                    "code": "source_unavailable",
                    "source": self.name,
                    "message": "AntiGravity CLI directory is unavailable",
                },
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )

        multi_root = len(self.roots) > 1
        index_warnings: List[str] = []
        discovery_warnings: List[str] = []
        discovered_items: List[Tuple[Path, str, Path, bool, Optional[_IndexEntry], bool]] = []
        for root in active_roots:
            root_index, root_idx_warnings = self._load_index_from_root(root)
            root_discovered, root_disc_warnings = self._discover_from_root(root)
            index_warnings.extend(root_idx_warnings)
            discovery_warnings.extend(root_disc_warnings)
            has_index = bool(root_index)
            for cid, path, used_compact in root_discovered:
                discovered_items.append((root, cid, path, used_compact, root_index.get(cid), has_index))

        includes = [str(value) for value in getattr(request, "includes", ())]
        previous_files = previous_checkpoint.get("files", {})
        if not isinstance(previous_files, Mapping) or previous_checkpoint.get("cache_generation") != self.cache_generation:
            previous_files = {}
        expected_scope = {
            "from_ms": from_ms,
            "to_ms": to_ms,
            "includes": sorted(includes),
            "source_format": self.source_format,
        }
        locators: Dict[str, Tuple[Path, str, Path, bool, Optional[_IndexEntry], bool]] = {}
        for root, cid, path, used_compact, entry, has_index in discovered_items:
            rel = _relative_locator(root, path)
            ckey = f"{root.resolve()}|{rel}" if multi_root else rel
            locators[ckey] = (root, cid, path, used_compact, entry, has_index)

        cached_result = previous_checkpoint.get("result")
        if (
            previous_checkpoint.get("version") == 1
            and previous_checkpoint.get("source") == self.name
            and previous_checkpoint.get("adapter_version") == self.adapter_version
            and previous_checkpoint.get("cache_generation") == self.cache_generation
            and previous_checkpoint.get("cacheable") is True
            and previous_checkpoint.get("scope") == expected_scope
            and isinstance(cached_result, Mapping)
            and set(previous_files) == set(locators)
            and all(
                isinstance(previous_files.get(ckey), Mapping)
                and _stat_matches(path, previous_files[ckey])
                and previous_files[ckey].get("index_fingerprint")
                == (entry.fingerprint if entry else None)
                for ckey, (_root, _cid, path, _used_compact, entry, _has_index) in locators.items()
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
                        "files_examined": len(discovered_items),
                        "files_read": 0,
                        "files_skipped": len(discovered_items),
                        "records_examined": 0,
                        "slices": len(slice_ids),
                        "reused_from_checkpoint": len(slice_ids),
                    },
                )

        checkpoint: Dict[str, Any] = {
            "files": {},
            "source": self.name,
            "version": 1,
            "adapter_version": self.adapter_version,
            "cache_generation": self.cache_generation,
        }
        reads: List[_FileRead] = []
        warnings: List[str] = [*index_warnings, *discovery_warnings]
        skipped_files = 0
        for ckey, (root, cid, path, used_compact, entry, has_index) in locators.items():
            locator = _relative_locator(root, path)
            if entry is None and has_index:
                warnings.append(f"index_entry_missing:{cid}")
            index_fingerprint = entry.fingerprint if entry else None
            previous = previous_files.get(ckey)
            if isinstance(previous, Mapping) and _can_skip_unchanged_file(
                path, previous, from_ms, to_ms, index_fingerprint
            ):
                checkpoint["files"][ckey] = dict(previous)
                skipped_files += 1
                continue
            try:
                read = _read_jsonl(
                    path,
                    root,
                    cid,
                    checkpoint_key=ckey,
                    entry=entry,
                    used_compact_fallback=used_compact,
                )
                checkpoint["files"][ckey] = self._checkpoint_entry(read, index_fingerprint)
            except OSError as exc:
                warnings.append(f"read_error:{locator}:{type(exc).__name__}")
                continue
            reads.append(read)
            warnings.extend(read.projection_warnings)

        documents = []
        turn_warnings: Dict[Tuple[str, str], List[str]] = {}
        for read in reads:
            entry = read.entry
            for turn_id, values in read.turn_warnings.items():
                turn_warnings[(read.conversation_id, turn_id)] = list(values)
            documents.append(
                build_session_document(
                    source=self.name,
                    locator=read.locator,
                    records=read.projected,
                    title=entry.title if entry else None,
                    workspace=entry.workspace if entry else None,
                    source_meta={
                        "source_format": SOURCE_FORMAT,
                        "transcript_kind": "compact" if read.used_compact_fallback else "full",
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
                    dict.fromkeys([*(item.get("quality_flags") or []), "antigravity_source_content_partial"])
                )
                item["content_completeness"] = "partial"
            slices.append(item)
        warnings.extend(normalized.warnings)
        omissions = list(normalized.omissions)
        degrading_warnings = [
            warning for warning in warnings if not warning.startswith(_NON_DEGRADING_WARNINGS)
        ]
        status = "complete"
        if degrading_warnings or any(item.get("content_completeness") != "complete" for item in slices):
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
                "files_examined": len(discovered_items),
                "files_read": len(reads),
                "files_skipped": skipped_files,
                **normalized.stats,
                "slices": len(slices),
            },
        )


__all__ = ["AntigravityAdapter", "default_antigravity_roots"]
