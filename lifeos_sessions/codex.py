"""Deterministic discovery and checkpointing for Codex rollout JSONL."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .core import AdapterResult, adapter_cache_generation
from .responses import (
    SourceProfile,
    build_session_document,
    iso_from_ms,
    normalize_sessions,
    parse_timestamp,
    payload_of,
    thread_from_record,
)


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
            last_hash = _sha(decoded)
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


def _window_values(request: Any) -> Tuple[int, int]:
    window = request.window
    from_ms = getattr(window, "from_ms", None)
    to_ms = getattr(window, "to_ms", None)
    if from_ms is None or to_ms is None:
        raise ValueError("Codex scan requires a UTC TimeWindow with from_ms and to_ms")
    return int(from_ms), int(to_ms)


def _include_values(request: Any) -> List[str]:
    return [str(value) for value in request.includes]


def _checkpoint(request: Any) -> Mapping[str, Any]:
    value = getattr(request, "checkpoint", None)
    return value if isinstance(value, Mapping) else {}


class CodexAdapter:
    """Read active and archived Codex files and delegate turn parsing."""

    name = "codex"
    # Bumped only for Codex-specific parsing changes. Shared extraction and
    # Slice Schema revisions enter the composed checkpoint generation below.
    adapter_version = "1"
    source_format = "codex-responses-jsonl"

    @property
    def cache_generation(self) -> str:
        return adapter_cache_generation(self.name, self.adapter_version)

    def __init__(self, root: Optional[os.PathLike[str] | str] = None):
        self.root = Path(root).expanduser() if root is not None else Path.home() / ".codex"

    def _files(self) -> List[Path]:
        paths: List[Path] = []
        for base_name in ("sessions", "archived_sessions"):
            base = self.root / base_name
            if not base.exists():
                continue
            paths.extend(path for path in base.rglob("rollout-*.jsonl") if path.is_file())
        unique: Dict[str, Path] = {}
        for path in paths:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            unique.setdefault(key, path)
        return sorted(unique.values(), key=lambda path: _relative_locator(self.root, path))

    def scan(self, request: Any) -> AdapterResult:
        try:
            from_ms, to_ms = _window_values(request)
        except (AttributeError, TypeError, ValueError) as exc:
            return AdapterResult(
                source=self.name,
                status="failed",
                error={"code": "invalid_window", "message": str(exc)},
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )
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
                checkpoint=_checkpoint(request),
                error={
                    "code": "source_unavailable",
                    "source": self.name,
                    "message": "Codex sessions directory is unavailable",
                },
                stats={"files_examined": 0, "records_examined": 0, "slices": 0},
            )

        includes = _include_values(request)
        global_warnings: List[str] = []
        file_reads: List[_FileRead] = []
        skipped_files = 0
        all_paths = self._files()
        previous_checkpoint = _checkpoint(request)
        previous_files = previous_checkpoint.get("files", {})
        if not isinstance(previous_files, Mapping):
            previous_files = {}
        if previous_checkpoint.get("cache_generation") != self.cache_generation:
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
            and set(previous_files) == {
                _relative_locator(self.root, path) for path in all_paths
            }
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

        # Only ``files``, ``scope`` and ``result`` decide whether the next scan
        # may skip work.  A per-thread projection was persisted here as well
        # (twice, under ``threads`` and ``sessions``); nothing ever read it and
        # it is fully derivable from ``files``, so it is no longer stored.
        checkpoint: Dict[str, Any] = {
            "files": {},
            "source": self.name,
            "version": 2,
            "adapter_version": self.adapter_version,
            "cache_generation": self.cache_generation,
        }
        for path in all_paths:
            locator = _relative_locator(self.root, path)
            previous_entry = previous_files.get(locator)
            if isinstance(previous_entry, Mapping) and _can_skip_unchanged_file(
                path, previous_entry, from_ms, to_ms
            ):
                checkpoint["files"][locator] = dict(previous_entry)
                skipped_files += 1
                continue
            try:
                read = _read_jsonl(path, self.root)
            except OSError as exc:
                global_warnings.append(f"read_error:{locator}:{type(exc).__name__}")
                continue
            file_reads.append(read)
            try:
                stat = path.stat()
            except OSError as exc:
                global_warnings.append(f"stat_error:{locator}:{type(exc).__name__}")
                continue
            file_entry: Dict[str, Any] = {
                "locator": read.locator,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "inode": stat.st_ino,
                "thread": read.thread_ids[0] if len(read.thread_ids) == 1 else read.thread_ids,
                "threads": read.thread_ids,
                "offset": read.complete_offset,
                "last_complete_offset": read.complete_offset,
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
            if read.incomplete_tail:
                file_entry["incomplete_tail_offset"] = read.tail_offset
                global_warnings.append(f"incomplete_tail:{read.locator}")
            if read.malformed:
                global_warnings.append(f"malformed_record:{read.locator}")
            checkpoint["files"][read.locator] = file_entry


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
                context_only_turn_prefixes=("external-import-turn-",),
            ),
            includes=includes,
        )
        global_warnings.extend(normalized.warnings)
        slices = list(normalized.slices)
        omissions = list(normalized.omissions)
        status = "complete"
        if global_warnings or any(item.get("content_completeness") != "complete" for item in slices):
            status = "partial"
        if not slices and any(item.startswith("read_error:") for item in global_warnings):
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
                "active_files": sum(
                    1
                    for path in all_paths
                    if not _relative_locator(self.root, path).startswith("archived_sessions/")
                ),
                "archived_files": sum(
                    1
                    for path in all_paths
                    if _relative_locator(self.root, path).startswith("archived_sessions/")
                ),
            },
        )


__all__ = ["CodexAdapter"]
