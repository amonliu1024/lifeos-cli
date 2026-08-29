"""Read SmartWork Agent JSONL sessions with read-only index enrichment."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .core import AdapterResult, SourceScanRequest, adapter_cache_generation
from .responses import (
    SourceProfile,
    build_session_document,
    iso_from_ms,
    normalize_sessions,
    parse_timestamp,
    payload_of,
    thread_from_record,
)


_NON_DEGRADING_WARNINGS = ("index_unavailable:", "index_entry_missing:")
_SMARTWORK_NOISE_TYPES = frozenset(
    {
        "world_state",
        "agent_reasoning_raw_content",
        "guardian_assessment",
        "collab_waiting_end",
        "collab_close_end",
    }
)
_SMARTWORK_TOOL_TYPES = frozenset(
    {
        "exec_command_end",
        "patch_apply_end",
        "collab_agent_spawn_end",
        "collab_agent_interaction_end",
        "dynamic_tool_call_request",
        "dynamic_tool_call_response",
        "error",
        "mcp_tool_call_end",
        "tool_search_call",
        "tool_search_output",
        "view_image_tool_call",
    }
)


@dataclass(frozen=True)
class _IndexEntry:
    session_id: str
    title: Optional[str]
    custom_title: Optional[str]
    last_activity_ms: Optional[int]
    workdir: Optional[str]
    path: Path
    is_subagent: bool

    @property
    def fingerprint(self) -> str:
        payload = {
            "id": self.session_id,
            "title": self.title,
            "custom_title": self.custom_title,
            "last_activity_ms": self.last_activity_ms,
            "workdir": self.workdir,
            "path": str(self.path),
            "is_subagent": self.is_subagent,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class _FileRead:
    path: Path
    locator: str
    records: List[Mapping[str, Any]]
    complete_offset: int
    last_hash: Optional[str]
    min_timestamp_ms: Optional[int]
    max_timestamp_ms: Optional[int]
    thread_ids: List[str]
    incomplete_tail: bool
    malformed: bool
    tail_offset: Optional[int]


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _relative_locator(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_jsonl(path: Path, root: Path) -> _FileRead:
    records: List[Mapping[str, Any]] = []
    complete_offset = 0
    last_hash: Optional[str] = None
    min_timestamp_ms: Optional[int] = None
    max_timestamp_ms: Optional[int] = None
    thread_ids: List[str] = []
    incomplete_tail = False
    malformed = False
    tail_offset: Optional[int] = None
    locator = _relative_locator(root, path)
    with path.open("rb") as handle:
        offset = 0
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            start = offset
            offset += len(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                complete_offset = offset
                continue
            try:
                decoded = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if handle.peek(1) == b"":
                    incomplete_tail = True
                    tail_offset = start
                else:
                    malformed = True
                continue
            if not isinstance(decoded, Mapping):
                malformed = True
                complete_offset = offset
                continue
            records.append(decoded)
            complete_offset = offset
            last_hash = _canonical_hash(decoded)
            timestamp_ms = parse_timestamp(decoded.get("timestamp"))
            if timestamp_ms is not None:
                min_timestamp_ms = min(min_timestamp_ms or timestamp_ms, timestamp_ms)
                max_timestamp_ms = max(max_timestamp_ms or timestamp_ms, timestamp_ms)
            current_payload = payload_of(decoded)
            thread = thread_from_record(decoded, current_payload, "")
            if thread and thread not in thread_ids:
                thread_ids.append(thread)
    return _FileRead(
        path=path,
        locator=locator,
        records=records,
        complete_offset=complete_offset,
        last_hash=last_hash,
        min_timestamp_ms=min_timestamp_ms,
        max_timestamp_ms=max_timestamp_ms,
        thread_ids=thread_ids,
        incomplete_tail=incomplete_tail,
        malformed=malformed,
        tail_offset=tail_offset,
    )


def _stat_fingerprint(path: Path) -> Optional[Dict[str, int]]:
    try:
        value = path.stat()
    except OSError:
        return None
    return {
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "inode": value.st_ino,
    }


def _stat_matches(path: Path, entry: Mapping[str, Any]) -> bool:
    current = _stat_fingerprint(path)
    if current is None:
        return False
    try:
        return all(int(entry.get(key)) == value for key, value in current.items())
    except (TypeError, ValueError):
        return False


def _can_skip_unchanged_file(path: Path, entry: Mapping[str, Any], from_ms: int, to_ms: int) -> bool:
    if entry.get("incomplete_tail") or entry.get("malformed") or entry.get("changed_during_scan"):
        return False
    if not _stat_matches(path, entry):
        return False
    try:
        max_event_ms = int(entry["last_event_ms"]) if entry.get("last_event_ms") is not None else None
        min_event_ms = int(entry["first_event_ms"]) if entry.get("first_event_ms") is not None else None
    except (TypeError, ValueError):
        return False
    return bool(
        (max_event_ms is not None and max_event_ms < from_ms)
        or (min_event_ms is not None and min_event_ms >= to_ms)
    )


def _index_timestamp(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if abs(number) < 10_000_000_000:
        number *= 1000
    return int(number)


class SmartworkAdapter:
    """Discover SmartWork Agent sessions and emit one Slice per Agent turn."""

    name = "smartwork"
    # Bumped only for SmartWork-specific parsing changes; shared extraction
    # and Slice Schema revisions enter the composed checkpoint generation.
    adapter_version = "1"
    source_format = "smartwork-responses-jsonl"

    @property
    def cache_generation(self) -> str:
        return adapter_cache_generation(self.name, self.adapter_version)

    def __init__(self, root: Optional[os.PathLike[str] | str] = None):
        self.root = Path(root).expanduser() if root is not None else Path.home() / ".SmartWork"
        self.sessions_root = self.root / "sessions"
        self.index_path = self.root / "session-index.sqlite"

    def _load_index(self) -> Tuple[Dict[str, _IndexEntry], List[str]]:
        if not self.index_path.is_file():
            return {}, ["index_unavailable:session-index.sqlite"]
        entries: Dict[str, _IndexEntry] = {}
        warnings: List[str] = []
        uri = self.index_path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT id, title, customTitle, lastActivityAt, workdir, sessionPath, isSubAgent FROM sessions"
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            return {}, [f"index_unavailable:{type(exc).__name__}"]
        for row in rows:
            raw_path = str(row["sessionPath"] or "")
            if not raw_path:
                warnings.append(f"index_path_missing:{row['id']}")
                continue
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self.root / path
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path.absolute()
            if not _is_within(self.sessions_root, resolved):
                warnings.append(f"index_path_outside_sessions:{row['id']}")
                continue
            entries[str(resolved)] = _IndexEntry(
                session_id=str(row["id"]),
                title=str(row["title"]) if row["title"] not in (None, "") else None,
                custom_title=str(row["customTitle"]) if row["customTitle"] not in (None, "") else None,
                last_activity_ms=_index_timestamp(row["lastActivityAt"]),
                workdir=str(row["workdir"]) if row["workdir"] not in (None, "") else None,
                path=resolved,
                is_subagent=bool(row["isSubAgent"]),
            )
        return entries, warnings

    def _discover(self, index: Mapping[str, _IndexEntry]) -> Tuple[List[Path], List[str], int]:
        warnings: List[str] = []
        paths: Dict[str, Path] = {}
        if self.sessions_root.is_dir():
            for path in self.sessions_root.rglob("rollout-*.jsonl"):
                if path.is_file():
                    try:
                        resolved = path.resolve()
                    except OSError:
                        resolved = path.absolute()
                    if not _is_within(self.sessions_root, resolved):
                        warnings.append(
                            f"source_path_outside_sessions:{_relative_locator(self.sessions_root, path)}"
                        )
                        continue
                    paths[str(resolved)] = resolved
        for key, entry in index.items():
            if entry.path.is_file():
                paths.setdefault(key, entry.path)
            else:
                warnings.append(f"index_path_missing:{entry.session_id}")
        unindexed = 0
        for key, path in paths.items():
            if key not in index:
                unindexed += 1
                warnings.append(f"index_entry_missing:{_relative_locator(self.sessions_root, path)}")
        return sorted(paths.values(), key=lambda path: _relative_locator(self.sessions_root, path)), warnings, unindexed

    @staticmethod
    def _includes(values: Sequence[str]) -> List[str]:
        result: List[str] = []
        for value in values:
            source, separator, native_id = str(value).partition(":")
            if separator and source == "smartwork" and native_id and native_id not in result:
                result.append(native_id)
        return result

    def scan(self, request: SourceScanRequest) -> AdapterResult:
        if not self.sessions_root.is_dir() and not self.index_path.is_file():
            return AdapterResult(
                source=self.name,
                status="failed",
                error={"code": "source_unavailable", "message": "SmartWork Agent sessions are unavailable"},
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )

        index, index_warnings = self._load_index()
        all_paths, discovery_warnings, unindexed_count = self._discover(index)
        warnings: List[str] = [*index_warnings, *discovery_warnings]
        includes = self._includes(request.includes)
        # The SQLite session id is enrichment, not content authority.  Even an
        # exact include must discover identity from JSONL so a stale index row
        # cannot hide the requested Agent session.
        candidate_paths = list(all_paths)
        from_ms, to_ms = request.window.from_ms, request.window.to_ms
        previous = request.checkpoint if isinstance(request.checkpoint, Mapping) else {}
        previous_files = previous.get("files", {}) if isinstance(previous.get("files"), Mapping) else {}
        if previous.get("cache_generation") != self.cache_generation:
            previous_files = {}
        expected_scope = {
            "from_ms": from_ms,
            "to_ms": to_ms,
            "includes": sorted(includes),
            "source_format": self.source_format,
        }
        inventory = {_relative_locator(self.sessions_root, path): path for path in candidate_paths}
        cached = previous.get("result")
        if (
            previous.get("version") == 3
            and previous.get("source") == self.name
            and previous.get("adapter_version") == self.adapter_version
            and previous.get("cache_generation") == self.cache_generation
            and previous.get("source_format") == self.source_format
            and previous.get("cacheable") is True
            and previous.get("scope") == expected_scope
            and isinstance(cached, Mapping)
            and set(previous_files) == set(inventory)
            and all(
                isinstance(previous_files.get(locator), Mapping)
                and _stat_matches(path, previous_files[locator])
                and previous_files[locator].get("index_fingerprint")
                == (index.get(str(path)).fingerprint if index.get(str(path)) else None)
                and not previous_files[locator].get("incomplete_tail")
                and not previous_files[locator].get("malformed")
                and not previous_files[locator].get("changed_during_scan")
                for locator, path in inventory.items()
            )
        ):
            slice_ids = cached.get("slice_ids")
            omission_ids = cached.get("omission_ids", [])
            cached_status = cached.get("status")
            cached_warnings = cached.get("warnings", [])
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
                    checkpoint=previous,
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
                        "indexed_files": len(index),
                        "unindexed_files": unindexed_count,
                    },
                )

        checkpoint: Dict[str, Any] = {
            "version": 3,
            "source": self.name,
            "adapter_version": self.adapter_version,
            "cache_generation": self.cache_generation,
            "source_format": self.source_format,
            "scope": expected_scope,
            "files": {},
        }
        file_reads: List[_FileRead] = []
        skipped = len(all_paths) - len(candidate_paths)
        read_errors = 0
        changed_during_scan = False
        for path in candidate_paths:
            locator = _relative_locator(self.sessions_root, path)
            entry = index.get(str(path))
            index_fingerprint = entry.fingerprint if entry else None
            previous_entry = previous_files.get(locator)
            if (
                isinstance(previous_entry, Mapping)
                and previous_entry.get("index_fingerprint") == index_fingerprint
                and _can_skip_unchanged_file(path, previous_entry, from_ms, to_ms)
            ):
                checkpoint["files"][locator] = dict(previous_entry)
                skipped += 1
                continue
            before = _stat_fingerprint(path)
            if before is None:
                warnings.append(f"read_error:{locator}:FileNotFoundError")
                read_errors += 1
                continue
            try:
                read = _read_jsonl(path, self.sessions_root)
            except OSError as exc:
                warnings.append(f"read_error:{locator}:{type(exc).__name__}")
                read_errors += 1
                continue
            after = _stat_fingerprint(path)
            source_changed = before != after
            if source_changed:
                changed_during_scan = True
                warnings.append(f"source_changed_during_scan:{locator}")
            if read.incomplete_tail:
                warnings.append(f"incomplete_tail:{locator}")
            if read.malformed:
                warnings.append(f"malformed_record:{locator}")
            file_reads.append(read)
            fingerprint = after or before
            checkpoint["files"][locator] = {
                "locator": locator,
                **fingerprint,
                "index_fingerprint": index_fingerprint,
                "index_last_activity_ms": entry.last_activity_ms if entry else None,
                "offset": read.complete_offset,
                "last_complete_offset": read.complete_offset,
                "last_complete_record_hash": read.last_hash,
                "first_event_ms": read.min_timestamp_ms,
                "last_event_ms": read.max_timestamp_ms,
                "max_event_time": iso_from_ms(read.max_timestamp_ms),
                "incomplete_tail": read.incomplete_tail,
                "malformed": read.malformed,
                "changed_during_scan": source_changed,
                "slice_ids": [],
            }
            if read.incomplete_tail:
                checkpoint["files"][locator]["incomplete_tail_offset"] = read.tail_offset

        documents = []
        doc_index_ids: Dict[str, str] = {}
        for read in file_reads:
            entry = index.get(str(read.path))
            workspace = entry.workdir if entry else None
            document = build_session_document(
                source=self.name,
                locator=read.locator,
                records=read.records,
                workspace=workspace,
                source_meta={"is_subagent": entry.is_subagent if entry else False},
                incomplete_tail=read.incomplete_tail,
                malformed=read.malformed,
            )
            if entry and document.conversation_id != entry.session_id:
                warnings.append(f"index_identity_mismatch:{read.locator}")
            if includes and document.conversation_id not in includes and (
                document.session_id or ""
            ) not in includes:
                continue
            documents.append(document)
            doc_index_ids[read.locator] = document.conversation_id

        normalized = normalize_sessions(
            documents,
            request.window,
            SourceProfile(
                self.name,
                self.adapter_version,
                extra_noise_types=_SMARTWORK_NOISE_TYPES,
                extra_tool_types=_SMARTWORK_TOOL_TYPES,
                task_complete_is_final=True,
                include_incomplete_tail_warning=True,
            ),
            includes=[f"smartwork:{value}" for value in includes],
        )
        warnings.extend(normalized.warnings)
        slices = list(normalized.slices)
        omissions = list(normalized.omissions)
        for locator, thread_id in doc_index_ids.items():
            checkpoint["files"][locator]["slice_ids"] = [
                str(item["slice_id"])
                for item in slices
                if item.get("source_meta", {}).get("thread_id") == thread_id
            ]

        degrading_warnings = [
            warning
            for warning in warnings
            if not warning.startswith(_NON_DEGRADING_WARNINGS)
        ]
        status = "complete"
        if degrading_warnings or any(item.get("content_completeness") != "complete" for item in slices):
            status = "partial"
        if read_errors and not slices:
            status = "failed"
        checkpoint.update(
            {
                "cacheable": not read_errors and not changed_during_scan,
                "result": {
                    "status": status,
                    "slice_ids": [str(item["slice_id"]) for item in slices],
                    "omission_ids": [str(item["omission_id"]) for item in omissions],
                    "warnings": list(dict.fromkeys(warnings)),
                },
            }
        )
        return AdapterResult(
            source=self.name,
            status=status,
            slices=slices,
            omissions=omissions,
            checkpoint=checkpoint,
            warnings=list(dict.fromkeys(warnings)),
            error=(
                {"code": "source_unavailable", "message": "SmartWork Agent session files could not be read"}
                if status == "failed"
                else None
            ),
            stats={
                "files_examined": len(all_paths),
                "files_read": len(file_reads),
                "files_skipped": skipped,
                **normalized.stats,
                "slices": len(slices),
                "indexed_files": len(index),
                "unindexed_files": unindexed_count,
                "index_missing_paths": sum(
                    1 for warning in warnings if warning.startswith("index_path_missing:")
                ),
            },
        )


__all__ = ["SmartworkAdapter"]
