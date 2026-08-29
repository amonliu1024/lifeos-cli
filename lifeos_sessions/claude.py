"""Claude Code source adapter.

The Claude JSONL format is an implementation detail of Claude Code rather
than a public interchange format.  This module therefore deliberately keeps
the parser defensive: records which are not understood are reported as
warnings, while the records that can be identified unambiguously are still
materialised as ``ConversationSlice`` objects.

The adapter does not write the sessions store.  It reads the source outside of
the store lock and returns ``AdapterResult`` to the sessions orchestration
layer.  In particular, importing this module never touches ``~/.claude``;
tests and callers can pass a synthetic projects directory to the constructor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .core import (
    SCHEMA_VERSION,
    AdapterResult,
    ConversationSlice,
    SourceScanRequest,
    adapter_cache_generation,
    stable_omission_id,
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


ADAPTER_NAME = "claude"
# Bumped only for Claude-specific parsing changes; shared extraction and
# Slice Schema revisions enter ADAPTER_CACHE_GENERATION.
ADAPTER_VERSION = "1"
ADAPTER_CACHE_GENERATION = adapter_cache_generation(ADAPTER_NAME, ADAPTER_VERSION)
_UTC = timezone.utc
# A source can contain a very large number of malformed branches (especially
# in long-lived Claude project logs).  Keep both the adapter result and each
# materialized slice inspectable without allowing one noisy file to produce an
# unbounded warning payload.
_MAX_WARNING_ITEMS = 32
_MAX_DELEGATION_TASK_LENGTH = 500
_MAX_COMMAND_LENGTH = 200

_KNOWN_RECORD_TYPES = frozenset(
    {
        "assistant",
        "agent-name",
        "agent_name",
        "ai-title",
        "ai_title",
        "attachment",
        "compact_boundary",
        "custom-title",
        "custom_title",
        "file-history",
        "file-history-delta",
        "file-history-snapshot",
        "file_history",
        "file_history_delta",
        "file_history_snapshot",
        "last-prompt",
        "last_prompt",
        "mode",
        "permission-mode",
        "permission_mode",
        "message",
        "progress",
        "queue-operation",
        "queue_operation",
        "result",
        "summary",
        "system",
        "tool_result",
        "tool_use",
        "user",
    }
)


@dataclass(frozen=True)
class _Event:
    """A parsed source record with its physical file location."""

    data: Mapping[str, Any]
    path: Path
    line_no: int
    order: int
    offset_start: int
    offset_end: int
    raw_hash: str
    timestamp_ms: Optional[int]
    session_id: str
    sidechain: bool

    @property
    def uuid(self) -> Optional[str]:
        value = self.data.get("uuid")
        return str(value) if value not in (None, "") else None

    @property
    def parent_uuid(self) -> Optional[str]:
        value = self.data.get("parentUuid", self.data.get("parent_uuid"))
        return str(value) if value not in (None, "") else None


@dataclass
class _ParsedFile:
    path: Path
    events: List[_Event]
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    last_complete_offset: int
    last_record_hash: Optional[str]
    last_event_ms: Optional[int]
    incomplete_tail: bool = False


@dataclass
class _Sidechain:
    agent_id: str
    session_id: str
    initial_prompt: Optional[str]
    final_text: Optional[str]
    final_event: Optional[_Event]
    path: Path


class ClaudeAdapter:
    """Read Claude Code project sessions and produce standardised slices.

    ``root`` may point at ``~/.claude`` or directly at its ``projects``
    directory.  A root is intentionally injected by tests; the default is
    only resolved when :meth:`scan` is called, so merely importing the adapter
    cannot read a user's private data.
    """

    name = ADAPTER_NAME
    adapter_version = ADAPTER_VERSION

    def __init__(self, root: Optional[os.PathLike[str] | str] = None) -> None:
        self.root = Path(root).expanduser() if root is not None else None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    def scan(self, request: SourceScanRequest) -> AdapterResult:
        """Scan the selected Claude sessions in ``request.window``.

        Source reading is best-effort.  A malformed middle record does not
        discard valid records from the same file, but it changes coverage to
        ``partial`` and is reported in ``warnings``.  A missing source root is
        a source failure rather than an empty successful scan.
        """

        projects_root = self._projects_root()
        if not projects_root.exists() or not projects_root.is_dir():
            return self._result(
                status="failed",
                checkpoint=self._checkpoint_from_request(request),
                warnings=(),
                error={
                    "code": "source_unavailable",
                    "source": ADAPTER_NAME,
                    "message": "Claude projects directory is unavailable",
                },
                stats={"examined": 0, "matched": 0, "created": 0, "reused": 0, "revised": 0},
            )

        files = sorted(projects_root.rglob("*.jsonl"), key=lambda p: str(p))
        main_files = [p for p in files if not self._is_sidechain_path(p)]
        side_files = [p for p in files if self._is_sidechain_path(p)]
        includes = self._normalise_includes(getattr(request, "includes", ()) or ())
        window = self._window_bounds(request)
        cached = self._cached_result(request, files, includes, window)
        if cached is not None:
            return cached

        # Read warnings are retained by path until a session/turn has been
        # proven to overlap this request.  Appending them while enumerating
        # files would make an unrelated historical session change this scan's
        # status.
        all_warnings: List[str] = []
        read_warnings: Dict[str, List[str]] = {}
        parsed_main: List[_ParsedFile] = []
        parsed_side: List[_ParsedFile] = []
        checkpoint_files: Dict[str, Dict[str, Any]] = {}
        order = 0

        for path in main_files:
            parsed, warnings, order = self._read_file(path, order, sidechain=False)
            parsed_main.append(parsed)
            read_warnings[str(path)] = list(warnings)
            checkpoint_files.update(self._checkpoint_entry(parsed))

        for path in side_files:
            parsed, warnings, order = self._read_file(path, order, sidechain=True)
            parsed_side.append(parsed)
            read_warnings[str(path)] = list(warnings)
            checkpoint_files.update(self._checkpoint_entry(parsed))

        # Sidechain discovery is deliberately kept separate from result
        # warnings.  A sidechain file which is not linked from a selected
        # turn is context outside this scan and must not make the source
        # partial.
        sidechain_warnings: List[str] = []
        sidechains = self._collect_sidechains(parsed_side, sidechain_warnings)
        grouped = self._group_main_events(parsed_main, [])

        slices: List[Any] = []
        omissions: List[Mapping[str, Any]] = []
        matched_sessions = 0
        for session_id in sorted(grouped):
            if includes and session_id not in includes:
                continue
            events = grouped[session_id]
            built, warnings, turn_omissions = self._build_session_slices(
                session_id=session_id,
                events=events,
                window=window,
                sidechains=sidechains,
                read_warnings=read_warnings,
            )
            if built:
                matched_sessions += 1
                slices.extend(built)
            all_warnings.extend(warnings)
            omissions.extend(turn_omissions)

        # Aggregation is deterministic and happens after session/window
        # scoping.  As a result, warnings from an unselected or out-of-window
        # session cannot poison status, while a genuinely malformed selected
        # turn remains visible.
        all_warnings = _aggregate_warnings(all_warnings)
        has_partial_warning = bool(all_warnings)
        status = "partial" if has_partial_warning else "complete"
        checkpoint = self._build_checkpoint(checkpoint_files, grouped, request)
        checkpoint.update({
            "version": 2,
            "adapter_version": ADAPTER_VERSION,
            "cache_generation": ADAPTER_CACHE_GENERATION,
            "cacheable": self._files_unchanged(files, checkpoint_files),
            "scope": {
                "from_ms": window[0],
                "to_ms": window[1],
                "includes": sorted(includes),
            },
            "result": {
                "status": status,
                "slice_ids": [
                    str(item.slice_id if isinstance(item, ConversationSlice) else item.get("slice_id"))
                    for item in slices
                    if (
                        (isinstance(item, ConversationSlice) and item.slice_id)
                        or (isinstance(item, Mapping) and item.get("slice_id"))
                    )
                ],
                "omission_ids": [str(item.get("omission_id")) for item in omissions if item.get("omission_id")],
                "warnings": list(all_warnings),
            },
        })
        stats = {
            "examined": len(files),
            "files_read": len(files),
            "files_skipped": 0,
            "matched": len(slices),
            "matched_sessions": matched_sessions,
            "created": len(slices),
            "reused": 0,
            "revised": 0,
            "skipped": max(0, len(grouped) - matched_sessions),
            "warnings": len(all_warnings),
            "errors": 0,
        }
        return self._result(
            status=status,
            slices=tuple(slices),
            omissions=tuple(omissions),
            checkpoint=checkpoint,
            warnings=tuple(all_warnings),
            error=None,
            stats=stats,
        )

    # ------------------------------------------------------------------
    # Files and checkpoint
    # ------------------------------------------------------------------
    def _projects_root(self) -> Path:
        root = self.root if self.root is not None else Path.home() / ".claude" / "projects"
        root = root.expanduser()
        if root.name == "projects":
            return root
        if (root / "projects").is_dir():
            return root / "projects"
        return root

    @staticmethod
    def _is_sidechain_path(path: Path) -> bool:
        return any(part.lower() == "subagents" for part in path.parts)

    def _read_file(
        self,
        path: Path,
        order: int,
        *,
        sidechain: bool,
    ) -> Tuple[_ParsedFile, List[str], int]:
        warnings: List[str] = []
        events: List[_Event] = []
        try:
            stat = path.stat()
        except OSError as exc:
            warnings.append(f"source_unavailable path={path.name}: {type(exc).__name__}")
            return (
                _ParsedFile(path, [], 0, 0, 0, 0, 0, None, None, False),
                warnings,
                order,
            )

        last_complete_offset = 0
        last_record_hash: Optional[str] = None
        last_event_ms: Optional[int] = None
        incomplete_tail = False
        offset = 0
        try:
            with path.open("rb") as handle:
                for line_no, raw_line in enumerate(handle, 1):
                    start = offset
                    offset += len(raw_line)
                    stripped = raw_line.strip()
                    if not stripped:
                        last_complete_offset = offset
                        continue
                    raw_hash = hashlib.sha256(raw_line).hexdigest()
                    try:
                        decoded = json.loads(stripped.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        # Claude appends records while a process is running.
                        # A final unterminated line is a recoverable tail; an
                        # invalid line with a newline is an invalid record.
                        if not raw_line.endswith(b"\n"):
                            incomplete_tail = True
                            warnings.append(
                                f"incomplete_tail path={path.name} line={line_no}"
                            )
                            break
                        warnings.append(
                            f"invalid_record path={path.name} line={line_no} "
                            f"error={type(exc).__name__}"
                        )
                        continue
                    if not isinstance(decoded, Mapping):
                        warnings.append(f"unsupported_format path={path.name} line={line_no}")
                        last_complete_offset = offset
                        last_record_hash = raw_hash
                        continue

                    timestamp_ms = _parse_timestamp_ms(decoded.get("timestamp"))
                    session_id = _session_id(decoded, path)
                    event = _Event(
                        data=decoded,
                        path=path,
                        line_no=line_no,
                        order=order,
                        offset_start=start,
                        offset_end=offset,
                        raw_hash=raw_hash,
                        timestamp_ms=timestamp_ms,
                        session_id=session_id,
                        sidechain=sidechain or bool(decoded.get("isSidechain")),
                    )
                    order += 1
                    events.append(event)
                    last_complete_offset = offset
                    last_record_hash = raw_hash
                    if timestamp_ms is not None:
                        last_event_ms = max(last_event_ms or timestamp_ms, timestamp_ms)
        except OSError as exc:
            warnings.append(f"source_unavailable path={path.name}: {type(exc).__name__}")

        return (
            _ParsedFile(
                path=path,
                events=events,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                ctime_ns=stat.st_ctime_ns,
                inode=stat.st_ino,
                last_complete_offset=last_complete_offset,
                last_record_hash=last_record_hash,
                last_event_ms=last_event_ms,
                incomplete_tail=incomplete_tail,
            ),
            warnings,
            order,
        )

    @staticmethod
    def _checkpoint_entry(parsed: _ParsedFile) -> Dict[str, Dict[str, Any]]:
        entry = {
            "locator": str(parsed.path),
            "size": parsed.size,
            "mtime_ns": parsed.mtime_ns,
            "ctime_ns": parsed.ctime_ns,
            "inode": parsed.inode,
            "offset": parsed.last_complete_offset,
            "last_complete_offset": parsed.last_complete_offset,
            "record_hash": parsed.last_record_hash,
            "last_record_hash": parsed.last_record_hash,
            "last_event_ms": parsed.last_event_ms,
            "incomplete_tail": parsed.incomplete_tail,
        }
        return {str(parsed.path): entry}

    @staticmethod
    def _files_unchanged(
        paths: Sequence[Path],
        entries: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        if set(entries) != {str(path) for path in paths}:
            return False
        for path in paths:
            entry = entries.get(str(path))
            if not isinstance(entry, Mapping):
                return False
            try:
                current = path.stat()
                if (
                    int(entry.get("size")) != current.st_size
                    or int(entry.get("mtime_ns")) != current.st_mtime_ns
                    or int(entry.get("ctime_ns")) != current.st_ctime_ns
                    or int(entry.get("inode")) != current.st_ino
                ):
                    return False
            except (OSError, TypeError, ValueError):
                return False
        return True

    def _cached_result(
        self,
        request: SourceScanRequest,
        files: Sequence[Path],
        includes: set[str],
        window: Tuple[int, int],
    ) -> Optional[AdapterResult]:
        checkpoint = getattr(request, "checkpoint", None)
        if not isinstance(checkpoint, Mapping):
            return None
        if (
            checkpoint.get("version") != 2
            or checkpoint.get("source") != ADAPTER_NAME
            or checkpoint.get("adapter_version") != ADAPTER_VERSION
            or checkpoint.get("cache_generation") != ADAPTER_CACHE_GENERATION
            or checkpoint.get("cacheable") is not True
        ):
            return None
        scope = checkpoint.get("scope")
        expected_scope = {
            "from_ms": window[0],
            "to_ms": window[1],
            "includes": sorted(includes),
        }
        if scope != expected_scope:
            return None
        entries = checkpoint.get("files")
        if not isinstance(entries, Mapping) or not self._files_unchanged(files, entries):
            return None
        cached_result = checkpoint.get("result")
        if not isinstance(cached_result, Mapping):
            return None
        slice_ids = cached_result.get("slice_ids")
        omission_ids = cached_result.get("omission_ids", [])
        warnings = cached_result.get("warnings", [])
        status = cached_result.get("status")
        if (
            status not in {"complete", "partial"}
            or not isinstance(slice_ids, list)
            or any(not isinstance(item, str) or not item.startswith("SLC-") for item in slice_ids)
            or not isinstance(warnings, list)
            or not isinstance(omission_ids, list)
            or any(not isinstance(item, str) or not item.startswith("OMN-") for item in omission_ids)
        ):
            return None
        return self._result(
            status=str(status),
            slices=(),
            reused_slice_ids=tuple(slice_ids),
            reused_omission_ids=tuple(omission_ids),
            checkpoint=checkpoint,
            warnings=tuple(warnings),
            error=None,
            stats={
                "examined": len(files),
                "files_read": 0,
                "files_skipped": len(files),
                "matched": len(slice_ids),
                "matched_sessions": None,
                "created": 0,
                "reused": len(slice_ids),
                "revised": 0,
                "skipped": 0,
                "warnings": len(warnings),
                "errors": 0,
            },
        )

    def _build_checkpoint(
        self,
        files: Mapping[str, Mapping[str, Any]],
        grouped: Mapping[str, Sequence[_Event]],
        request: SourceScanRequest,
    ) -> Dict[str, Any]:
        sessions: Dict[str, Dict[str, Any]] = {}
        for session_id, events in grouped.items():
            main = [event for event in events if not event.sidechain]
            if not main:
                continue
            paths = sorted({str(event.path) for event in main})
            sessions[session_id] = {
                "session_id": session_id,
                "locator": paths[0] if paths else None,
                "files": paths,
                "last_event_ms": max(
                    (event.timestamp_ms for event in main if event.timestamp_ms is not None),
                    default=None,
                ),
            }
        return {
            "version": 1,
            "source": ADAPTER_NAME,
            "files": dict(files),
            "sessions": sessions,
        }

    @staticmethod
    def _checkpoint_from_request(request: SourceScanRequest) -> Any:
        value = getattr(request, "checkpoint", None)
        return value if value is not None else {"version": 1, "source": ADAPTER_NAME, "files": {}}

    # ------------------------------------------------------------------
    # Session grouping and turn construction
    # ------------------------------------------------------------------
    def _group_main_events(
        self,
        parsed: Sequence[_ParsedFile],
        warnings: Optional[List[str]] = None,
    ) -> Dict[str, List[_Event]]:
        # Parent/timestamp diagnostics are intentionally evaluated after a
        # turn is matched to the requested window.  Emitting them here would
        # make every historical event a source-level warning even when it is
        # never materialized for this scan.
        grouped: Dict[str, List[_Event]] = {}
        for item in parsed:
            for event in item.events:
                if event.sidechain:
                    # Sidechain files are parsed separately.  A record marked
                    # sidechain in the main file is not a canonical turn.
                    continue
                grouped.setdefault(event.session_id, []).append(event)
        for session_events in grouped.values():
            session_events.sort(key=lambda event: event.order)
        return grouped

    def _build_session_slices(
        self,
        *,
        session_id: str,
        events: Sequence[_Event],
        window: Tuple[int, int],
        sidechains: Mapping[str, _Sidechain],
        read_warnings: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> Tuple[List[Any], List[str], List[Mapping[str, Any]]]:
        warnings: List[str] = []
        read_warnings = read_warnings or {}
        session_title = _session_title(events)
        event_by_uuid = {event.uuid: event for event in events if event.uuid}
        users = [event for event in events if _is_real_user(event.data)]
        if not users:
            # A selected session may contain a future/unknown record without
            # a recoverable user root.  It is still a real source warning when
            # that record is in this window; an unrelated historical session
            # never reaches this method.
            for event in events:
                if _in_window(event.timestamp_ms, window) and _is_unknown_record(event.data):
                    warnings.append(
                        _format_warning(
                            "unsupported_format",
                            session=session_id,
                            uuid=event.uuid or str(event.line_no),
                            type=str(event.data.get("type") or "unknown"),
                        )
                    )
                if _in_window(event.timestamp_ms, window) and event.parent_uuid:
                    warnings.append(
                        _format_warning(
                            "orphan_branch",
                            session=session_id,
                            uuid=event.uuid or str(event.line_no),
                            parent=event.parent_uuid,
                        )
                    )
            return [], _aggregate_warnings(warnings), []

        # Parent UUIDs are the reliable relation when tool events are
        # interleaved.  Physical order remains the tie-breaker for old files
        # that omit parentUuid.
        root_by_uuid: Dict[str, Optional[str]] = {}

        def find_root(event: _Event, seen: Optional[set[str]] = None) -> Optional[str]:
            if not event.uuid:
                return None
            if event.uuid in root_by_uuid:
                return root_by_uuid[event.uuid]
            seen = set() if seen is None else seen
            if event.uuid in seen:
                root_by_uuid[event.uuid] = None
                return None
            seen.add(event.uuid)
            if _is_real_user(event.data):
                root_by_uuid[event.uuid] = event.uuid
                return event.uuid
            parent = event.parent_uuid
            if parent and parent in event_by_uuid:
                result = find_root(event_by_uuid[parent], seen)
            else:
                result = None
            root_by_uuid[event.uuid] = result
            return result

        turn_events: Dict[str, List[_Event]] = {user.uuid or "": [] for user in users}
        orphan_events: Dict[str, List[_Event]] = {user.uuid or "": [] for user in users}
        unassigned_orphans: List[_Event] = []
        unassigned_unknowns: List[_Event] = []
        current_user: Optional[str] = None
        for event in events:
            if _is_real_user(event.data) and event.uuid:
                current_user = event.uuid
            root = find_root(event)
            if root in turn_events:
                turn_events[root].append(event)
            elif current_user and event.parent_uuid is None:
                # Legacy records without relation metadata: physical order is
                # the only available boundary.
                turn_events[current_user].append(event)
            elif event.parent_uuid:
                # Keep the orphan attached to its nearest real user, but do
                # not let it become retained content.  This gives us a precise
                # turn/window seam for diagnostics below.
                if current_user and current_user in orphan_events:
                    orphan_events[current_user].append(event)
                else:
                    unassigned_orphans.append(event)
            elif _is_unknown_record(event.data):
                # An unknown root-less record cannot be safely projected into
                # a turn, but it still needs a scoped diagnostic when it is in
                # the requested window.
                unassigned_unknowns.append(event)

        slices: List[Any] = []
        omissions: List[Mapping[str, Any]] = []
        for index, user in enumerate(users):
            if not user.uuid:
                continue
            group = sorted(turn_events.get(user.uuid, ()), key=lambda event: event.order)
            related_orphans = sorted(orphan_events.get(user.uuid, ()), key=lambda event: event.order)
            related = [*group, *related_orphans]
            next_user_ts = users[index + 1].timestamp_ms if index + 1 < len(users) else None
            hit = any(_in_window(event.timestamp_ms, window) for event in related)
            if not hit:
                continue
            scoped_warnings: List[str] = []
            for event in related:
                in_window = _in_window(event.timestamp_ms, window)
                if event in related_orphans and (in_window or event.timestamp_ms is None):
                    scoped_warnings.append(
                        _format_warning(
                            "orphan_branch",
                            session=session_id,
                            turn=user.uuid,
                            uuid=event.uuid or str(event.line_no),
                            parent=event.parent_uuid or "unknown",
                        )
                    )
                if event.timestamp_ms is None and str(event.data.get("type") or "").lower() in {"user", "assistant"}:
                    # Missing timestamps can only be scoped when the turn has
                    # another event in this window; otherwise there is no
                    # reliable evidence that this record contributes coverage.
                    scoped_warnings.append(
                        _format_warning(
                            "orphan_timestamp",
                            session=session_id,
                            turn=user.uuid,
                            uuid=event.uuid or str(event.line_no),
                        )
                    )
                if _is_unknown_record(event.data) and (in_window or event.timestamp_ms is None):
                    scoped_warnings.append(
                        _format_warning(
                            "unsupported_format",
                            session=session_id,
                            turn=user.uuid,
                            uuid=event.uuid or str(event.line_no),
                            type=str(event.data.get("type") or "unknown"),
                        )
                    )

            # A malformed record has no trustworthy timestamp, so the narrow
            # safe boundary is the file containing a matched turn.  This keeps
            # read errors local to a selected session/slice while retaining a
            # visible partial signal for a file that actually supplied it.
            for path in sorted({str(event.path) for event in related}):
                for raw_warning in read_warnings.get(path, ()):
                    scoped_warnings.append(
                        _contextualise_warning(
                            raw_warning,
                            session=session_id,
                            turn=user.uuid,
                        )
                    )
            scoped_warnings = _aggregate_warnings(scoped_warnings)
            payload, turn_warnings = self._turn_payload(
                session_id=session_id,
                user=user,
                events=group,
                session_title=session_title,
                next_user_ts=next_user_ts,
                window=window,
                sidechains=sidechains,
                initial_warnings=scoped_warnings,
            )
            warnings.extend(turn_warnings)
            try:
                obj = ConversationSlice.from_dict(payload)
                final = obj.finalize()
                if payload.pop("_explicit_abort_without_work", False):
                    abort_record = next(
                        (event for event in reversed(group) if _is_explicit_cancel(event.data)),
                        None,
                    )
                    if abort_record is not None:
                        native_id = str(payload.get("native_unit", {}).get("id") or user.uuid)
                        omission = {
                            "source": ADAPTER_NAME,
                            "conversation": {"id": session_id},
                            "native_unit": {"kind": "turn", "id": native_id},
                            "at": _iso_ms(
                                abort_record.timestamp_ms
                                or user.timestamp_ms
                                or max(
                                    (event.timestamp_ms for event in group if event.timestamp_ms is not None),
                                    default=0,
                                )
                            ),
                            "reason": "explicit_abort_without_work",
                            "source_ref": _source_ref(abort_record),
                            "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
                        }
                        omission["omission_id"] = stable_omission_id(
                            ADAPTER_NAME, session_id, "turn", native_id, "explicit_abort_without_work"
                        )
                        omissions.append(omission)
                    continue
                slices.append(final if final is not None else obj)
            except Exception as exc:
                # A malformed source record should not make the complete
                # source unavailable.  Keep the warning bounded and continue
                # with other turns; the caller will receive partial coverage.
                warnings.append(
                    f"unsupported_format session={session_id} turn={user.uuid} "
                    f"error={type(exc).__name__}"
                )

        # Orphans before any real user root cannot be attached to a slice, but
        # remain actionable when they occur inside the requested window.
        for event in unassigned_orphans:
            if _in_window(event.timestamp_ms, window):
                warnings.append(
                    _format_warning(
                        "orphan_branch",
                        session=session_id,
                        uuid=event.uuid or str(event.line_no),
                        parent=event.parent_uuid or "unknown",
                    )
                )
        for event in unassigned_unknowns:
            if _in_window(event.timestamp_ms, window):
                warnings.append(
                    _format_warning(
                        "unsupported_format",
                        session=session_id,
                        uuid=event.uuid or str(event.line_no),
                        type=str(event.data.get("type") or "unknown"),
                    )
                )
        return slices, _aggregate_warnings(warnings), omissions

    def _turn_payload(
        self,
        *,
        session_id: str,
        user: _Event,
        events: Sequence[_Event],
        session_title: Optional[str],
        next_user_ts: Optional[int],
        window: Tuple[int, int],
        sidechains: Mapping[str, _Sidechain],
        initial_warnings: Sequence[str] = (),
    ) -> Tuple[Dict[str, Any], List[str]]:
        warnings: List[str] = list(initial_warnings)
        user_text = _extract_text(user.data, role="user")
        started_ms = user.timestamp_ms
        if started_ms is None:
            started_ms = next((event.timestamp_ms for event in events if event.timestamp_ms is not None), None)
        # Compact boundaries and file-history snapshots are source noise, not
        # turn activity.  They must not extend an otherwise complete turn's
        # ended_at (nor defeat turn_duration reconstruction).
        all_timestamps = [
            event.timestamp_ms
            for event in events
            if event.timestamp_ms is not None and not _is_compact_or_noise(event.data)
        ]
        last_ms = max(all_timestamps, default=started_ms)
        duration_ms = _turn_duration_ms(events)
        readable: List[Tuple[_Event, str, bool]] = []
        terminal: List[Tuple[_Event, str]] = []
        explicit_cancels = [event for event in events if _is_explicit_cancel(event.data)]
        pending_ask: Dict[str, List[str]] = {}

        # The user's own message is always the first block, even when it is
        # outside the explicit window and is needed as turn context.
        blocks: List[Dict[str, Any]] = []
        if user_text:
            blocks.append(
                _block(
                    kind="message",
                    author_role="self",
                    at_ms=user.timestamp_ms or started_ms,
                    text=user_text,
                    context=not _in_window(user.timestamp_ms, window),
                    source_ref=_source_ref(user),
                )
            )

        for event in events:
            data = event.data
            if _is_assistant_record(data):
                text = _extract_text(data, role="assistant")
                if text:
                    stop_reason = _stop_reason(data)
                    readable.append((event, text, stop_reason == "end_turn"))
                    if stop_reason == "end_turn":
                        terminal.append((event, text))
                for tool_id, questions in _ask_question_tools(data).items():
                    pending_ask[tool_id] = questions
                continue

            answer_blocks = _ask_answer_blocks(event, pending_ask, window)
            blocks.extend(answer_blocks)

        if terminal:
            final_event, final_text = terminal[-1]
            blocks.append(
                _block(
                    kind="agent_message",
                    author_role="agent",
                    at_ms=final_event.timestamp_ms or last_ms or started_ms,
                    text=final_text,
                    context=not _in_window(final_event.timestamp_ms, window),
                    source_ref=_source_ref(final_event),
                )
            )
        elif readable:
            final_event, final_text, _ = readable[-1]
            blocks.append(
                _block(
                    kind="agent_message",
                    author_role="agent",
                    at_ms=final_event.timestamp_ms or last_ms or started_ms,
                    text=final_text,
                    context=not _in_window(final_event.timestamp_ms, window),
                    source_ref=_source_ref(final_event),
                )
            )
            warnings.append(f"incomplete_turn session={session_id} turn={user.uuid}")
        else:
            warnings.append(f"incomplete_turn session={session_id} turn={user.uuid} no_readable_text")

        delegations = self._delegations(
            session_id=session_id,
            events=events,
            sidechains=sidechains,
            window=window,
        )
        for delegation in delegations:
            text = _delegation_text(delegation)
            if text:
                blocks.append(
                    _block(
                        kind="delegation",
                        author_role="agent",
                        at_ms=delegation.get("at_ms") or last_ms or started_ms,
                        text=text,
                        context=not _in_window(delegation.get("at_ms"), window),
                        source_ref=(delegation.get("source_refs") or [None])[0],
                    )
                )

        if started_ms is None:
            # The turn could not be window-matched without an event timestamp;
            # this warning is retained for validate/diagnostic output.
            started_ms = 0
            warnings.append(f"orphan_timestamp session={session_id} turn={user.uuid}")

        ended_ms = last_ms or started_ms
        if duration_ms is not None:
            ended_ms = max(ended_ms, started_ms + duration_ms)
        elif not terminal and next_user_ts is not None:
            # A missing turn_duration makes the next real user input the
            # deterministic boundary for an incomplete turn.
            ended_ms = max(ended_ms, next_user_ts)
        if ended_ms <= started_ms:
            # The public schema uses a strict start/end ordering.  A
            # user-only or zero-duration source turn still has one observable
            # event, so represent it as a one-millisecond interval rather
            # than dropping the otherwise valid partial slice.
            ended_ms = started_ms + 1

        completeness_partial = not terminal
        turn_warnings = _aggregate_warnings(warnings)
        if any(_is_compact_or_noise(event.data) for event in events):
            # Noise is intentionally omitted rather than making a healthy
            # turn partial; source_meta records the omission count.
            pass
        # Missing timestamps on compact/file-history records and other source
        # noise do not affect the retained turn.  Only a missing timestamp on
        # a retained user/assistant record (or a warning already scoped to the
        # turn) can downgrade coverage.
        if any(
            event.timestamp_ms is None
            and (
                _is_real_user(event.data)
                or _is_assistant_record(event.data)
                or _is_unknown_record(event.data)
            )
            for event in events
        ):
            completeness_partial = True
        if warnings:
            completeness_partial = True

        cwd = _first_value(events, "cwd", "workingDirectory", "workdir")
        project = user.path.parent.name
        title = session_title
        # Source refs are an audit trail for retained public blocks, not a
        # replay of every tool/system event.  Keep only refs that are already
        # exposed by message/outcome/delegation blocks; tool-only records are
        # represented by the structured execution summary instead.
        source_refs: List[str] = []
        for block in blocks:
            refs = block.get("source_refs") if isinstance(block, Mapping) else None
            if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                source_refs.extend(str(ref) for ref in refs if ref)
        source_refs = list(dict.fromkeys(source_refs))
        native_id = user.uuid or _payload_hash(user.data)
        slice_id = _stable_slice_id(session_id, native_id)
        tool_observations, summary_refs = _tool_observations(events)
        execution_evidence = build_execution_evidence(
            changed_targets=tool_observations.get("changed_targets") or [],
            checks=tool_observations.get("checks") or [],
            failures=tool_observations.get("failures") or [],
            omitted_count=tool_observations.get("omitted_count") or 0,
        )
        if tool_observations.get("source_refs"):
            execution_evidence["source_refs"] = tool_observations["source_refs"]
        turn_completion = "completed" if terminal else (
            "interrupted_with_result" if explicit_cancels and has_substantive_result(
                execution_evidence,
                successful_commands=tool_observations.get("successful_commands", []),
                completed_delegations=delegations,
            ) else "incomplete"
        )
        classified = classify_warnings(turn_warnings)
        source_refs.extend(summary_refs)
        source_refs = list(dict.fromkeys(source_refs))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "slice_id": slice_id,
            # ``revision`` is assigned by ConversationSlice.finalize().  An
            # empty string would be treated by the core validator as an
            # invalid explicit revision.
            "revision": None,
            "source": ADAPTER_NAME,
            "conversation": {"id": session_id, "title": title},
            "native_unit": {"kind": "turn", "id": native_id},
            "started_at": _iso_ms(started_ms),
            "ended_at": _iso_ms(ended_ms),
            "workspace": cwd,
            "blocks": blocks,
            "execution_evidence": execution_evidence,
            "delegations": [
                {key: value for key, value in delegation.items() if key != "at_ms"}
                for delegation in delegations
            ],
            # A missing terminal message degrades the turn on its own; the
            # warning set can degrade it further, so the stronger of the two
            # wins rather than being flattened to ``partial``.
            "content_completeness": (
                classified["content_completeness"]
                if classified["content_completeness"] != "complete"
                else ("partial" if completeness_partial else "complete")
            ),
            "turn_completion": turn_completion,
            "provenance_trimmed": classified["provenance_trimmed"],
            "warnings": turn_warnings,
            "source_refs": source_refs,
            "source_meta": {
                "session_id": session_id,
                "project": project,
                "files": sorted({str(event.path) for event in events}),
                "turn_duration_ms": duration_ms,
            },
            "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
            "materialized_at": _iso_ms(int(datetime.now(tz=_UTC).timestamp() * 1000)),
        }
        if explicit_cancels and not terminal and turn_completion == "incomplete":
            payload["_explicit_abort_without_work"] = True
        return payload, turn_warnings

    # ------------------------------------------------------------------
    # Sidechains and delegation handling
    # ------------------------------------------------------------------
    def _collect_sidechains(
        self,
        parsed: Sequence[_ParsedFile],
        warnings: List[str],
    ) -> Dict[str, _Sidechain]:
        result: Dict[str, _Sidechain] = {}
        for item in parsed:
            events = sorted(item.events, key=lambda event: event.order)
            if not events:
                continue
            agent_id = _agent_id_from_path(item.path)
            for event in events:
                candidate = _first_value_from_mapping(event.data, "agentId", "agent_id", "agentID")
                if candidate:
                    agent_id = str(candidate)
                    break
            if not agent_id:
                warnings.append(f"orphan_sidechain path={item.path.name}")
                continue
            initial = next((_extract_text(event.data, role="user") for event in events if _is_real_user(event.data)), None)
            finals = [
                (event, _extract_text(event.data, role="assistant"))
                for event in events
                if _is_assistant_record(event.data)
                and _stop_reason(event.data) == "end_turn"
                and _extract_text(event.data, role="assistant")
            ]
            fallback = [
                (event, _extract_text(event.data, role="assistant"))
                for event in events
                if _is_assistant_record(event.data) and _extract_text(event.data, role="assistant")
            ]
            final_event, final_text = (finals[-1] if finals else fallback[-1] if fallback else (None, None))
            result[agent_id] = _Sidechain(
                agent_id=agent_id,
                session_id=events[0].session_id,
                initial_prompt=initial,
                final_text=final_text,
                final_event=final_event,
                path=item.path,
            )
        return result

    def _delegations(
        self,
        *,
        session_id: str,
        events: Sequence[_Event],
        sidechains: Mapping[str, _Sidechain],
        window: Tuple[int, int],
    ) -> List[Dict[str, Any]]:
        pending_results: Dict[str, str] = {}
        for event in events:
            if event.data.get("type") != "user":
                continue
            for block in _content_blocks(event.data):
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id") or block.get("toolUseId")
                if tool_id:
                    text = _flatten_tool_result(block.get("content"))
                    if text:
                        pending_results[str(tool_id)] = text

        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for event in events:
            if not _is_assistant_record(event.data):
                continue
            for block in _content_blocks(event.data):
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or block.get("function", {}).get("name") or "")
                input_data = block.get("input") if isinstance(block.get("input"), Mapping) else {}
                if not _is_delegation_tool(name, input_data):
                    continue
                agent_id = _first_value_from_mapping(block, "agentId", "agent_id", "agentID")
                agent_id = str(agent_id) if agent_id else None
                if not agent_id:
                    for key in ("agentId", "agent_id", "agentID"):
                        if input_data.get(key):
                            agent_id = str(input_data[key])
                            break
                # Claude's Task tool result commonly exposes the child agent
                # id in a later result.  Use the tool id until then; this still
                # gives deterministic deduplication when no sidechain exists.
                key = agent_id or str(block.get("id") or _payload_hash(block))
                if key in seen:
                    continue
                seen.add(key)
                # ``agent_path`` names which agent ran, not what it was asked
                # to do.  Claude Code carries that as ``subagent_type``; the
                # prompt belongs in ``task`` and nowhere else.
                agent_path = _first_value_from_mapping(input_data, "subagent_type", "agent_type")
                agent_path = _clean_text(agent_path) if agent_path else None
                raw_task = _first_value_from_mapping(input_data, "prompt", "description")
                task = _clean_text(raw_task) if raw_task else None
                task_truncated = bool(task and len(task) > _MAX_DELEGATION_TASK_LENGTH)
                if task_truncated:
                    task = task[: _MAX_DELEGATION_TASK_LENGTH - 1] + "…"
                result = pending_results.get(str(block.get("id")))
                side = sidechains.get(agent_id) if agent_id else None
                if not result and side:
                    result = side.final_text
                if not result and pending_results:
                    # A malformed export may omit tool_use_id in the result;
                    # only use a result when there is a single pending one.
                    if len(pending_results) == 1:
                        result = next(iter(pending_results.values()))
                if side and result and side.final_text and _clean_text(result) == _clean_text(side.final_text):
                    result = side.final_text
                status = "complete" if result else "partial"
                ref = _source_ref(event)
                delegation = {
                    "agent_id": agent_id,
                    "agent_path": agent_path,
                    "thread_id": side.session_id if side else None,
                    "child_thread_id": side.session_id if side else None,
                    "turn_id": None,
                    "task": task,
                    "status": status,
                    "result": result,
                    "source_refs": [ref] if ref else [],
                    "at_ms": event.timestamp_ms,
                }
                if task_truncated:
                    delegation["warnings"] = ["task_truncated"]
                out.append(delegation)
        return out

    # ------------------------------------------------------------------
    # Helpers and model conversion
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_includes(values: Iterable[Any]) -> set[str]:
        result: set[str] = set()
        for value in values:
            text = str(value)
            source, separator, native = text.partition(":")
            if separator and source == ADAPTER_NAME and native:
                result.add(native)
        return result

    @staticmethod
    def _window_bounds(request: SourceScanRequest) -> Tuple[int, int]:
        window = getattr(request, "window", request)
        from_ms = getattr(window, "from_ms", None)
        to_ms = getattr(window, "to_ms", None)
        if from_ms is None:
            from_ms = _parse_timestamp_ms(getattr(window, "from_iso", None))
        if to_ms is None:
            to_ms = _parse_timestamp_ms(getattr(window, "to_iso", None))
        return int(from_ms or 0), int(to_ms if to_ms is not None else 2**63 - 1)

    @staticmethod
    def _result(
        *,
        status: str,
        slices: Sequence[Any] = (),
        omissions: Sequence[Any] = (),
        reused_slice_ids: Sequence[str] = (),
        reused_omission_ids: Sequence[str] = (),
        checkpoint: Any = None,
        warnings: Sequence[str] = (),
        error: Any = None,
        stats: Optional[Mapping[str, Any]] = None,
    ) -> AdapterResult:
        return AdapterResult(
            source=ADAPTER_NAME,
            status=status,
            slices=tuple(slices),
            omissions=tuple(omissions),
            reused_slice_ids=tuple(reused_slice_ids),
            reused_omission_ids=tuple(reused_omission_ids),
            checkpoint=checkpoint,
            warnings=tuple(warnings),
            error=error,
            stats=dict(stats or {}),
        )


# ----------------------------------------------------------------------
# Pure parsing helpers
# ----------------------------------------------------------------------
def _session_id(data: Mapping[str, Any], path: Path) -> str:
    for key in ("sessionId", "session_id", "conversationId", "conversation_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    # A project session file is usually named <session-id>.jsonl.  Keep the
    # path-derived fallback deterministic but do not expose it as a user title.
    return path.stem


def _agent_id_from_path(path: Path) -> Optional[str]:
    stem = path.stem
    for prefix in ("agent-", "agent_", "subagent-"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return stem or None


def _parse_timestamp_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # Epoch seconds are ~1e9, epoch milliseconds ~1e12.
        if abs(number) < 100_000_000_000:
            number *= 1000
        return int(number)
    text = str(value).strip()
    if not text:
        return None
    try:
        return _parse_timestamp_ms(float(text))
    except ValueError:
        pass
    normalised = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return int(dt.astimezone(_UTC).timestamp() * 1000)


def _iso_ms(value: Optional[int]) -> str:
    dt = datetime.fromtimestamp(int(value or 0) / 1000, tz=_UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _in_window(timestamp_ms: Optional[int], window: Tuple[int, int]) -> bool:
    return timestamp_ms is not None and window[0] <= timestamp_ms < window[1]


def _format_warning(code: str, **fields: Any) -> str:
    """Render a compact, grep-friendly warning with deterministic location."""

    parts = [str(code)]
    for key in sorted(fields):
        value = fields[key]
        if value in (None, ""):
            continue
        text = re.sub(r"\s+", " ", str(value)).strip()
        if len(text) > 240:
            text = text[:239] + "…"
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _warning_code(value: Any) -> str:
    text = str(value)
    return text.split(None, 1)[0].split(":", 1)[0] if text else "warning"


def _warning_field(value: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(name)}=([^\s]+)", value)
    return match.group(1) if match else ""


def _contextualise_warning(value: Any, *, session: str, turn: str) -> str:
    text = str(value)
    # Keep an existing source locator, while adding the selected turn that
    # makes the warning actionable and prevents it from looking global.
    if "session=" not in text:
        text = f"{text} session={session}"
    if "turn=" not in text:
        text = f"{text} turn={turn}"
    return text


def _aggregate_warnings(values: Sequence[Any], limit: int = _MAX_WARNING_ITEMS) -> List[str]:
    """Aggregate same-class warnings and cap the result with omitted_count."""

    grouped: Dict[Tuple[str, str, str, str], List[Any]] = {}
    order: List[Tuple[str, str, str, str]] = []
    for raw in values:
        text = str(raw)
        code = _warning_code(text)
        # Aggregate by the smallest actionable location.  Distinct selected
        # turns remain separately addressable; repeated UUIDs/parents collapse.
        key = (code, _warning_field(text, "session"), _warning_field(text, "turn"), _warning_field(text, "path"))
        if key not in grouped:
            grouped[key] = [text, 1]
            order.append(key)
        else:
            grouped[key][1] = int(grouped[key][1]) + 1

    rendered: List[str] = []
    for key in order:
        first, count = grouped[key]
        if int(count) > 1:
            rendered.append(f"{first} count={count} omitted_count={int(count) - 1}")
        else:
            rendered.append(str(first))
    if len(rendered) <= limit:
        return rendered
    # Reserve one slot for an explicit cap marker so callers can distinguish
    # a complete warning list from a bounded projection.
    keep = max(0, limit - 1)
    omitted = len(rendered) - keep
    return [*rendered[:keep], f"warning_summary limit={limit} omitted_count={omitted}"]


def _message(data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = data.get("message")
    return value if isinstance(value, Mapping) else data


def _content_value(data: Mapping[str, Any]) -> Any:
    message = _message(data)
    return message.get("content")


def _content_blocks(data: Mapping[str, Any]) -> List[Any]:
    value = _content_value(data)
    if isinstance(value, list):
        return list(value)
    return []


def _extract_text(data: Mapping[str, Any], *, role: str) -> str:
    message = _message(data)
    value = message.get("content")
    if value is None and isinstance(data.get("message"), str):
        value = data.get("message")
    if isinstance(value, str):
        return _clean_text(value)
    if not isinstance(value, list):
        return ""
    parts: List[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "").lower()
        if kind in {
            "text",
            "output_text",
            "input_text",
        }:
            text = item.get("text", item.get("value", ""))
            if isinstance(text, str):
                parts.append(text)
        # A bare block with a text field is accepted only for known message
        # roles; this handles older exports without treating tool inputs as
        # prose.
        elif not kind and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return _clean_text("\n".join(parts))


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").strip()
    return text


def _is_compact_or_noise(data: Mapping[str, Any]) -> bool:
    typ = str(data.get("type") or "").lower()
    if typ in {
        "agent-name",
        "agent_name",
        "ai-title",
        "ai_title",
        "custom-title",
        "custom_title",
        "file-history-delta",
        "file-history-snapshot",
        "file_history_delta",
        "file_history_snapshot",
        "compact_boundary",
        "last-prompt",
        "last_prompt",
        "mode",
        "permission-mode",
        "permission_mode",
        "queue-operation",
        "queue_operation",
        "summary",
        "attachment",
        "file-history",
        "file_history",
    }:
        return True
    if data.get("isCompactSummary") or data.get("compactMetadata") or data.get("isMeta"):
        return True
    subtype = str(data.get("subtype") or "").lower()
    if (
        "compact" in subtype
        or "file-history" in subtype
        or "file_history" in subtype
        or subtype in {"away_summary", "turn_duration"}
    ):
        return True
    return False


_SYSTEM_TEXT_MARKERS = (
    "<task-notification",
    "<system-reminder",
    "<task-completed",
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<deferred-tools>",
)


def _is_real_user(data: Mapping[str, Any]) -> bool:
    if str(data.get("type") or "").lower() != "user":
        return False
    if _is_compact_or_noise(data):
        return False
    for key in ("toolUseResult", "tool_use_result", "toolResult", "isMeta", "meta"):
        if key in data and (key not in {"isMeta", "meta"} or data.get(key)):
            # A tool result is parsed separately for AskUserQuestion and must
            # never become a user turn boundary.
            return False
    content = _content_value(data)
    blocks = content if isinstance(content, list) else []
    if blocks and all(
        isinstance(block, Mapping)
        and str(block.get("type") or "").lower() in {"tool_result", "toolresult", "thinking", "attachment", "image", "document"}
        for block in blocks
    ):
        return False
    text = _extract_text(data, role="user")
    if not text:
        return False
    lowered = text.lstrip().lower()
    if any(lowered.startswith(marker) for marker in _SYSTEM_TEXT_MARKERS):
        return False
    if "isCompactSummary" in text or "compact_boundary" in lowered:
        return False
    return True


def _is_assistant_record(data: Mapping[str, Any]) -> bool:
    if str(data.get("type") or "").lower() != "assistant":
        return False
    if _is_compact_or_noise(data):
        return False
    return True


def _is_explicit_cancel(data: Mapping[str, Any]) -> bool:
    """Recognise only Claude's verified structured cancel markers."""

    record_type = str(data.get("type") or "").lower()
    subtype = str(data.get("subtype") or data.get("status") or "").lower()
    if record_type in {"result", "system", "event"} and subtype in {
        "turn_aborted", "turn_cancelled", "turn_canceled", "cancelled", "canceled",
        "user_cancelled", "user_canceled",
    }:
        return True
    message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
    stop = str(
        message.get("stop_reason", message.get("stopReason", data.get("stop_reason", data.get("stopReason", ""))))
        or ""
    ).lower()
    return stop in {"user_cancelled", "user_canceled", "cancelled", "canceled"}


def _stop_reason(data: Mapping[str, Any]) -> Optional[str]:
    message = _message(data)
    value = message.get("stop_reason", message.get("stopReason", data.get("stop_reason", data.get("stopReason"))))
    if value is None and data.get("end_turn") is True:
        return "end_turn"
    return str(value).lower() if value not in (None, "") else None


def _turn_duration_ms(events: Sequence[_Event]) -> Optional[int]:
    keys = (
        "turn_duration",
        "turnDuration",
        "turn_duration_ms",
        "turnDurationMs",
        "duration_ms",
        "durationMs",
    )
    for event in events:
        for key in keys:
            if key not in event.data:
                continue
            value = event.data.get(key)
            if isinstance(value, Mapping):
                value = value.get("ms", value.get("milliseconds", value.get("durationMs")))
            if isinstance(value, bool) or value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            # Duration fields in Claude exports are milliseconds.  Permit
            # fractional seconds only when the value is clearly in seconds.
            if number < 1000 and isinstance(value, float):
                number *= 1000
            return max(0, int(number))
    return None


def _is_unknown_record(data: Mapping[str, Any]) -> bool:
    """Return whether a mapping has a source record type we do not model.

    Known system/compact/tool records are intentionally allowed to be
    omitted from public blocks.  A genuinely new type is different: when it
    overlaps a selected turn/window it must leave a partial diagnostic rather
    than silently looking complete.
    """

    value = data.get("type")
    if value in (None, ""):
        return False
    return str(value).strip().lower() not in _KNOWN_RECORD_TYPES


def _summary_values(value: Any, keys: Sequence[str]) -> List[str]:
    """Extract bounded, human-readable fields without retaining tool raw."""

    result: List[str] = []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _summary_values(json.loads(stripped), keys)
            except (TypeError, ValueError):
                return []
        return result
    if isinstance(value, Mapping):
        for key in keys:
            if key not in value:
                continue
            candidate = value.get(key)
            candidates = candidate if isinstance(candidate, (list, tuple, set)) else [candidate]
            for item in candidates:
                if isinstance(item, Mapping):
                    item = _first_value_from_mapping(item, "file_path", "path", "file", "filename", "target", "name")
                text = _clean_text(item)
                if text:
                    result.append(text)
        # Claude commonly nests tool output under result/data/output/content.
        for key in ("arguments", "input", "result", "data", "output", "content", "details"):
            nested = value.get(key)
            if isinstance(nested, Mapping) or isinstance(nested, (list, tuple, set)):
                result.extend(_summary_values(nested, keys))
            elif isinstance(nested, str) and nested.lstrip().startswith(("{", "[")):
                result.extend(_summary_values(nested, keys))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            result.extend(_summary_values(item, keys))
    return result


def _shell_command(input_data: Mapping[str, Any]) -> str:
    """Return the bounded command a shell-style tool was asked to run."""

    for key in ("command", "cmd", "script"):
        value = input_data.get(key)
        if isinstance(value, (list, tuple)):
            value = " ".join(str(part) for part in value)
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text:
            return text[:_MAX_COMMAND_LENGTH]
    return ""


def _bounded_summary_values(values: Iterable[str], limit: int = 100) -> Tuple[List[str], int]:
    seen: set[str] = set()
    output: List[str] = []
    omitted = 0
    for value in values:
        text = re.sub(r"\s+", " ", str(value)).strip()
        if not text:
            continue
        # Paths/check names are intentionally short; avoid accidentally
        # copying command output or an environment blob into the summary.
        if len(text) > 512:
            text = text[:511] + "…"
        if text in seen:
            continue
        seen.add(text)
        if len(output) >= limit:
            omitted += 1
            continue
        output.append(text)
    return output, omitted


def _tool_observations(events: Sequence[_Event]) -> Tuple[Dict[str, Any], List[str]]:
    """Project raw deterministic tool facts before they are classified."""

    changed: List[str] = []
    checks: List[str] = []
    failures: List[str] = []
    successful_commands: List[str] = []
    refs: List[str] = []
    omitted = 0
    edit_tools = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "patch"}
    check_tools = {"test", "pytest", "check", "lint", "verify", "typecheck"}
    # Claude Code runs every test through a general shell tool, so the tool
    # name proves nothing and the command has to be read.
    shell_tools = {"bash", "shell", "sh", "zsh", "exec", "run_command",
                   "run_terminal_cmd", "terminal"}
    delegation_tools = {"task", "agent", "delegate", "spawn", "subagent", "run_subagent"}

    for event in events:
        if not _is_assistant_record(event.data):
            continue
        for block in _content_blocks(event.data):
            if not isinstance(block, Mapping) or str(block.get("type") or "").lower() != "tool_use":
                continue
            name = str(block.get("name") or block.get("function", {}).get("name") or "").strip()
            lowered = name.lower()
            input_data = block.get("input") if isinstance(block.get("input"), Mapping) else {}
            if lowered in delegation_tools or lowered == "askuserquestion":
                continue
            extracted = False
            if lowered in edit_tools:
                values = _summary_values(input_data, ("changed_targets", "changed_files", "files", "paths", "file_path", "path", "file", "filename", "target"))
                changed.extend(values)
                extracted = bool(values)
            if lowered in check_tools or any(token in lowered for token in check_tools):
                checks.append(name or lowered)
                extracted = True
            elif lowered in shell_tools:
                command = _shell_command(input_data)
                # Keep the command in both evidence buckets.  The semantic
                # owner decides whether it is a verification; the tool name
                # is only a fallback when the container has no command.
                checks.append(command or name or lowered)
                if command:
                    successful_commands.append(f"{command}: passed")
                extracted = True
            # A structured tool input can explicitly carry summary fields even
            # when its vendor-specific name is unknown.
            explicit_changed = _summary_values(input_data, ("changed_targets", "changed_files"))
            explicit_checks = _summary_values(input_data, ("checks", "check", "check_name", "tests"))
            if explicit_changed:
                changed.extend(explicit_changed)
                extracted = True
            if explicit_checks:
                checks.extend(explicit_checks)
                extracted = True
            if extracted:
                refs.append(_source_ref(event))
            else:
                omitted += 1

    # Tool results are user records in Claude JSONL.  They may expose a
    # structured status/changed-target payload even when the corresponding
    # tool_use input did not.
    for event in events:
        if str(event.data.get("type") or "").lower() != "user":
            continue
        for block in _content_blocks(event.data):
            if not isinstance(block, Mapping) or str(block.get("type") or "").lower() not in {"tool_result", "toolresult"}:
                continue
            value = block.get("content")
            changed_values = _summary_values(value, ("changed_targets", "changed_files", "files", "paths", "file_path", "path", "file", "filename", "target"))
            check_values = _summary_values(value, ("checks", "check", "check_name", "tests"))
            status = str(_first_value_from_mapping(value, "status", "outcome", "state") if isinstance(value, Mapping) else "").lower()
            exit_code = _first_value_from_mapping(value, "exit_code", "exitCode", "returncode") if isinstance(value, Mapping) else None
            failed = status in {"failed", "failure", "error", "errored", "rejected", "timeout", "timed_out"} or exit_code not in (None, "", 0, "0")
            failure_values = _summary_values(value, ("failure", "error", "reason")) if failed else []
            changed.extend(changed_values)
            checks.extend(check_values)
            failures.extend(failure_values or (["tool failure"] if failed else []))
            if changed_values or check_values or failure_values or failed:
                refs.append(_source_ref(event))
            else:
                omitted += 1

    changed_values, changed_omitted = _bounded_summary_values(changed)
    check_values, check_omitted = _bounded_summary_values(checks)
    failure_values, failure_omitted = _bounded_summary_values(failures)
    summary = {
        "changed_targets": changed_values,
        "checks": check_values,
        "failures": failure_values,
        "successful_commands": list(dict.fromkeys(successful_commands)),
        "omitted_count": omitted + changed_omitted + check_omitted + failure_omitted,
    }
    return summary, list(dict.fromkeys(refs))


def _ask_question_tools(data: Mapping[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    if not _is_assistant_record(data):
        return result
    for block in _content_blocks(data):
        if not isinstance(block, Mapping) or str(block.get("type") or "").lower() != "tool_use":
            continue
        name = str(block.get("name") or block.get("function", {}).get("name") or "")
        if name.lower() != "askuserquestion":
            continue
        tool_id = block.get("id") or block.get("tool_use_id") or block.get("toolUseId")
        if not tool_id:
            continue
        input_data = block.get("input") if isinstance(block.get("input"), Mapping) else {}
        questions: List[str] = []
        raw_questions = input_data.get("questions", input_data.get("question"))
        if isinstance(raw_questions, Mapping):
            raw_questions = [raw_questions]
        if isinstance(raw_questions, list):
            for item in raw_questions:
                if isinstance(item, Mapping):
                    text = item.get("question") or item.get("header")
                else:
                    text = item
                if text:
                    questions.append(_clean_text(text))
        result[str(tool_id)] = questions
    return result


def _ask_answer_blocks(
    event: _Event,
    pending: Mapping[str, Sequence[str]],
    window: Tuple[int, int],
) -> List[Dict[str, Any]]:
    data = event.data
    if str(data.get("type") or "").lower() != "user":
        return []
    candidates: List[Tuple[Optional[str], Any]] = []
    direct = data.get("toolUseResult", data.get("tool_use_result"))
    if direct is not None:
        direct_id = None
        if isinstance(direct, Mapping):
            direct_id = direct.get("tool_use_id") or direct.get("toolUseId") or direct.get("id")
        candidates.append((str(direct_id) if direct_id is not None else None, direct))
    for block in _content_blocks(data):
        if not isinstance(block, Mapping):
            continue
        if str(block.get("type") or "").lower() not in {"tool_result", "toolresult"}:
            continue
        tool_id = block.get("tool_use_id") or block.get("toolUseId")
        if tool_id is not None:
            tool_id = str(tool_id)
        candidates.append((tool_id, block.get("content")))

    blocks: List[Dict[str, Any]] = []
    for tool_id, value in candidates:
        if tool_id is not None and tool_id not in pending:
            continue
        # Claude also emits a top-level ``toolUseResult`` object for Bash and
        # other tools.  Only an object carrying an answer-like field (or a
        # textual "answered:" result) is eligible for AskUserQuestion
        # promotion; stdout/stderr wrappers must never become human text.
        if tool_id is None and not _looks_like_ask_answer(value):
            continue
        answers = _extract_answers(value)
        if not answers:
            continue
        questions = list(pending.get(tool_id or "", ()))
        lines: List[str] = []
        if isinstance(answers, Mapping):
            for key, answer in answers.items():
                answer_text = _clean_text(answer)
                if not answer_text:
                    continue
                label = _clean_text(key)
                lines.append(f"{label}: {answer_text}" if label else answer_text)
        elif isinstance(answers, list):
            lines.extend(_clean_text(item) for item in answers if _clean_text(item))
        else:
            lines.append(_clean_text(answers))
        if not lines:
            continue
        text = "\n".join(lines)
        blocks.append(
            _block(
                kind="message",
                author_role="self",
                at_ms=event.timestamp_ms,
                text=text,
                context=not _in_window(event.timestamp_ms, window),
                source_ref=_source_ref(event),
            )
        )
    return blocks


def _looks_like_ask_answer(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(key in value for key in ("answers", "answer", "responses", "response", "selected"))
    if isinstance(value, list):
        return any(_looks_like_ask_answer(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return "answers" in lowered or bool(re.search(r"\banswered\s*:", lowered))
    return False


def _extract_answers(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("answers", "answer", "responses", "response", "selected"):
            if key in value:
                return value[key]
        # Some exports put the answer under a nested result object.
        for key in ("result", "data", "output"):
            if key in value:
                nested = _extract_answers(value[key])
                if nested not in (None, "", {}, []):
                    return nested
        return value if value else None
    if isinstance(value, list):
        text_parts: List[str] = []
        for item in value:
            if isinstance(item, Mapping) and str(item.get("type") or "").lower() in {"text", "output_text"}:
                text_parts.append(_clean_text(item.get("text", "")))
            else:
                text_parts.append(_clean_text(item))
        text = "\n".join(part for part in text_parts if part)
        return _extract_answers(text) if text else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Prefer a JSON object embedded in known tool-result prose.
        candidates = [text]
        match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if match:
            candidates.insert(0, match.group(1))
        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            extracted = _extract_answers(decoded)
            if extracted not in (None, "", {}, []):
                return extracted
        # Claude has emitted strings such as ``User answered: Choice A``;
        # preserve the answer while dropping the wrapper label.
        match = re.match(r"(?:user\s+)?answered(?:\s+the\s+questions?)?\s*:\s*(.*)$", text, re.I | re.S)
        if match:
            return match.group(1).strip()
        return text
    return value


def _flatten_tool_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, Mapping):
        answers = _extract_answers(value)
        if answers is not value:
            return _flatten_tool_result(answers)
        return ""
    if isinstance(value, list):
        return _clean_text("\n".join(_flatten_tool_result(item) for item in value if _flatten_tool_result(item)))
    return _clean_text(value)


def _is_delegation_tool(name: str, input_data: Mapping[str, Any]) -> bool:
    """True only when a tool hands work to a sub-agent.

    Carrying a ``prompt`` or ``description`` proves nothing -- ``TaskCreate``
    describes a todo item and delegates to no one -- so identity is what
    counts: a known delegation tool, or an input naming the agent to run.
    """

    if name.lower() in {"task", "agent", "delegate", "spawn", "subagent", "run_subagent"}:
        return True
    return any(
        input_data.get(key)
        for key in ("agentId", "agent_id", "agentID", "subagent_type", "agent_type")
    )


def _delegation_text(delegation: Mapping[str, Any]) -> str:
    target = _clean_text(delegation.get("task"))
    result = _clean_text(delegation.get("result"))
    if target and result:
        return f"委托：{target}\n结果：{result}"
    return target or result


def _session_title(events: Sequence[_Event]) -> Optional[str]:
    custom: Optional[str] = None
    generated: Optional[str] = None
    for event in events:
        record_type = str(event.data.get("type") or "").lower()
        if record_type in {"custom-title", "custom_title"}:
            candidate = normalize_title(
                _first_value_from_mapping(event.data, "customTitle", "custom_title")
            )
            if candidate is not None:
                custom = candidate
        elif record_type in {"ai-title", "ai_title"}:
            candidate = normalize_title(
                _first_value_from_mapping(event.data, "aiTitle", "ai_title")
            )
            if candidate is not None:
                generated = candidate
    return custom or generated


def _source_ref(event: _Event) -> str:
    native = event.uuid or str(event.line_no)
    return f"claude:{event.session_id}:{native}"


def _stable_slice_id(session_id: str, native_id: str) -> str:
    payload = f"{ADAPTER_NAME}{session_id}turn{native_id}".encode("utf-8")
    return "SLC-" + hashlib.sha256(payload).hexdigest()


def _payload_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _first_value(events: Sequence[_Event], *keys: str) -> Any:
    for event in events:
        value = _first_value_from_mapping(event.data, *keys)
        if value not in (None, ""):
            return value
    return None


def _first_value_from_mapping(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _block(
    *,
    kind: str,
    author_role: str,
    at_ms: Optional[int],
    text: str,
    context: bool,
    source_ref: Optional[str],
) -> Dict[str, Any]:
    cleaned = _clean_text(text)
    block: Dict[str, Any] = {
        "kind": kind,
        "author_role": author_role,
        "origin": classify_origin(author_role, cleaned),
        "at": _iso_ms(at_ms),
        "text": cleaned,
        "context": bool(context),
        "source_refs": [source_ref] if source_ref else [],
    }
    return block


__all__ = ["ClaudeAdapter"]
