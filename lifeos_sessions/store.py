"""Private sessions-derived storage.

The Work runtime has its own lock, facts and event log.  This module never
opens those files.  It owns a separate directory, a separate lock and a
SQLite index whose rows point at immutable revision JSON files.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from .core import (
    AdapterResult,
    ConversationSlice,
    TurnOmission,
    SessionValidationError,
    TimeWindow,
    canonical_json,
    canonical_revision,
    parse_iso_datetime,
    validate_slice,
)


UTC = timezone.utc
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_PRESERVED_SIDECARS = ("retention.json",)


class StoreError(Exception):
    """Storage or integrity error."""


class SessionNotFound(StoreError, KeyError):
    """Requested slice or revision is not in the index."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_component(value: str) -> str:
    return _SAFE_COMPONENT.sub("_", str(value)).strip(".") or "unknown"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


class SessionsStore:
    """Sessions directory, SQLite index and atomic manifest writer."""

    def __init__(self, root: os.PathLike[str] | str, *, create: bool = False):
        self.root = Path(root).expanduser()
        self.slices_dir = self.root / "slices"
        self.scans_dir = self.root / "scans"
        self.lock_path = self.root / ".sessions.lock"
        # This lock lives beside the active tree, so replacing the tree cannot
        # move the lock away from writers that are waiting for a switch.
        self.switch_lock_path = self.root.parent / f".{self.root.name}.switch.lock"
        self.db_path = self.root / "index.sqlite3"
        self.create = bool(create)
        if self.create:
            self.ensure_initialized()

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.slices_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scans_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.slices_dir, 0o700)
        os.chmod(self.scans_dir, 0o700)
        self.lock_path.touch(exist_ok=True, mode=0o600)
        os.chmod(self.lock_path, 0o600)
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)

    def ensure_initialized(self) -> None:
        """Create the private store for a scan write path.

        Read-only commands intentionally do not call this method.  In
        particular, ``validate`` on a never-used runtime reports missing
        paths instead of silently turning a diagnostic into a write.
        """

        # Initialization creates the active root.  It therefore participates
        # in the same sibling lock as the rest of the write path; otherwise a
        # writer could recreate ``root`` while ``activate_staging`` had moved
        # the old tree away and was waiting to install the staging tree.
        with self._switch_locked():
            self._ensure_layout()
            self._initialize_db()

    def activate_staging(self, staging_root: os.PathLike[str] | str) -> dict[str, Any]:
        """Atomically replace this store with a validated staging tree.

        The previous Sessions tree is moved as one directory to a sibling
        backup before the staging tree is renamed into place.  If activation
        fails, the old tree is restored and this method raises without
        changing the active store.
        """

        staging = Path(staging_root)
        if not staging.is_dir():
            raise StoreError(f"staging sessions root is missing: {staging}")
        with self._switch_locked():
            backup_parent = self.root.parent / "sessions-backups"
            backup_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(backup_parent, 0o700)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup = backup_parent / f"{self.root.name}-{stamp}"
            # If an active tree exists, wait for its root lock while still
            # holding the stable sibling lock.  The root lock is then kept
            # across both renames and any rollback, and is never reacquired
            # through ``_locked`` (which would recursively take the sibling
            # lock again).
            root_guard = self._root_locked() if self.root.exists() else contextlib.nullcontext()
            with root_guard:
                moved = False
                try:
                    for name in _PRESERVED_SIDECARS:
                        source = self.root / name
                        target = staging / name
                        if source.is_file():
                            shutil.copyfile(source, target)
                            os.chmod(target, 0o600)
                    if self.root.exists():
                        os.replace(self.root, backup)
                        moved = True
                    os.replace(staging, self.root)
                except Exception:
                    if moved and not self.root.exists() and backup.exists():
                        os.replace(backup, self.root)
                    raise
        return {
            "active": str(self.root),
            "backup": str(backup) if backup.exists() else None,
            "staging": str(staging),
        }

    def _connect(self, *, allow_create: bool = False) -> sqlite3.Connection:
        if not self.db_path.exists() and not allow_create:
            raise StoreError(f"sessions index is not initialized: {self.db_path}")
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            if enabled != 1:
                connection.close()
                raise StoreError("SQLite foreign keys could not be enabled")
        except sqlite3.DatabaseError:
            connection.close()
            raise
        return connection

    def _initialize_db(self) -> None:
        connection = self._connect(allow_create=True)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    from_ms INTEGER NOT NULL,
                    to_ms INTEGER NOT NULL,
                    from_iso TEXT NOT NULL,
                    to_iso TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    manifest_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slice_revisions (
                    slice_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    source TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    title TEXT,
                    workspace TEXT,
                    started_ms INTEGER NOT NULL,
                    ended_ms INTEGER NOT NULL,
                    content_completeness TEXT NOT NULL,
                    provenance_trimmed INTEGER NOT NULL DEFAULT 0,
                    adapter_version TEXT,
                    content_hash TEXT NOT NULL,
                    json_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (slice_id, revision)
                );
                CREATE TABLE IF NOT EXISTS slices_current (
                    slice_id TEXT PRIMARY KEY,
                    revision TEXT NOT NULL,
                    FOREIGN KEY (slice_id, revision)
                        REFERENCES slice_revisions(slice_id, revision)
                        ON UPDATE CASCADE ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS scan_slices (
                    scan_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    PRIMARY KEY (scan_id, slice_id, revision),
                    FOREIGN KEY (scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE,
                    FOREIGN KEY (slice_id, revision)
                        REFERENCES slice_revisions(slice_id, revision) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS source_checkpoints (
                    source TEXT PRIMARY KEY,
                    checkpoint_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turn_omissions (
                    omission_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    native_unit_kind TEXT NOT NULL,
                    native_unit_id TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    source_ref TEXT,
                    adapter_name TEXT,
                    adapter_version TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_omissions (
                    scan_id TEXT NOT NULL,
                    omission_id TEXT NOT NULL,
                    PRIMARY KEY (scan_id, omission_id),
                    FOREIGN KEY (scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE,
                    FOREIGN KEY (omission_id) REFERENCES turn_omissions(omission_id) ON DELETE RESTRICT
                );
                /* One row per pruned source-day.  A query whose window
                   overlaps these days must say so instead of reporting an
                   empty result as a complete one. */
                CREATE TABLE IF NOT EXISTS pruned_days (
                    source TEXT NOT NULL,
                    day TEXT NOT NULL,
                    slice_count INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    pruned_at TEXT NOT NULL,
                    PRIMARY KEY (source, day, scope)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS slice_fts USING fts5(
                    slice_id UNINDEXED,
                    revision UNINDEXED,
                    text,
                    title,
                    label,
                    tokenize='trigram'
                );
                CREATE INDEX IF NOT EXISTS idx_slice_revisions_time
                    ON slice_revisions(started_ms DESC, slice_id ASC);
                CREATE INDEX IF NOT EXISTS idx_slice_revisions_source
                    ON slice_revisions(source, conversation_id);
                CREATE INDEX IF NOT EXISTS idx_scan_slices_slice
                    ON scan_slices(slice_id, revision);
                CREATE INDEX IF NOT EXISTS idx_turn_omissions_time
                    ON turn_omissions(at_ms DESC, omission_id ASC);
                """
            )
            # FTS5 is a required capability, not an optional performance hint.
            connection.execute("INSERT INTO slice_fts(slice_id, revision, text, title, label) VALUES (?, ?, ?, ?, ?)", ("__probe__", "__probe__", "", "", ""))
            connection.execute("DELETE FROM slice_fts WHERE slice_id = ?", ("__probe__",))
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            raise StoreError(f"SQLite FTS5 is required for sessions store: {exc}") from exc
        finally:
            connection.close()
        os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def _switch_locked(self) -> Iterator[None]:
        """Serialize writes and directory switches without moving the lock."""

        self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.switch_lock_path.touch(exist_ok=True, mode=0o600)
        os.chmod(self.switch_lock_path, 0o600)
        with self.switch_lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _root_locked(self) -> Iterator[None]:
        """Lock the current active tree without taking the sibling lock."""

        self.lock_path.touch(exist_ok=True, mode=0o600)
        os.chmod(self.lock_path, 0o600)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the stable switch lock before the active-tree lock."""

        with self._switch_locked():
            with self._root_locked():
                yield

    def _atomic_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # ``Path.mkdir(parents=True, mode=...)`` only guarantees the requested
        # mode for the leaf it creates.  A newly-created source directory such
        # as ``slices/codex`` would otherwise inherit the process umask and can
        # become 0755, exposing private conversation metadata and filenames.
        current = path.parent
        while current != self.root:
            os.chmod(current, 0o700)
            current = current.parent
        os.chmod(self.root, 0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(_json_bytes(value))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            os.chmod(path, 0o600)
        finally:
            if fd != -1:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _slice_path(self, item: ConversationSlice) -> Path:
        # ``revision`` is a validated ``sha256:<hex>`` value.  Keep the
        # canonical value in the filename rather than replacing its ``:``;
        # this makes the immutable path directly traceable to the public
        # revision contract.
        revision = item.revision or ""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", revision):
            raise StoreError("invalid revision path component")
        return self.slices_dir / _safe_component(item.source) / _safe_component(item.slice_id or "") / f"{revision}.json"

    @staticmethod
    def _adapter_version(item: ConversationSlice) -> Optional[str]:
        value = item.adapter.get("version") if isinstance(item.adapter, Mapping) else None
        return str(value) if value is not None else None

    @staticmethod
    def _fts_payload(item: ConversationSlice) -> tuple[str, str, str]:
        texts: list[str] = []
        for block in item.blocks:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        title = ""
        if isinstance(item.conversation, Mapping) and item.conversation.get("title") is not None:
            title = str(item.conversation.get("title"))
        label = " ".join(value for value in (item.workspace or "", title) if value)
        return "\n".join(texts), title, label

    @staticmethod
    def _normalise_results(results: Mapping[str, AdapterResult] | Sequence[AdapterResult]) -> list[AdapterResult]:
        values = list(results.values()) if isinstance(results, Mapping) else list(results)
        normalised: list[AdapterResult] = []
        for value in values:
            if isinstance(value, AdapterResult):
                normalised.append(value)
            elif isinstance(value, Mapping):
                normalised.append(AdapterResult(**value))
            else:
                raise StoreError("scan result must be AdapterResult")
        return normalised

    def commit_scan(
        self,
        scan_id: str,
        results: Mapping[str, AdapterResult] | Sequence[AdapterResult],
        window: Optional[TimeWindow] = None,
        *,
        status: Optional[str] = None,
        request: Any = None,
    ) -> dict[str, Any]:
        """Commit immutable revisions and one scan relation atomically.

        Revision JSON is written while the sessions lock is held and before
        the SQLite transaction.  A crash can therefore leave an orphan file,
        which ``validate`` reports, but no committed row points at absent JSON.
        """

        self.ensure_initialized()
        if request is not None:
            window = getattr(request, "window", window)
        if not isinstance(window, TimeWindow):
            raise StoreError("commit_scan requires a TimeWindow")
        values = self._normalise_results(results)
        if not scan_id or not isinstance(scan_id, str):
            raise StoreError("scan_id is required")
        if status is None:
            status = "complete" if values and all(item.status == "complete" for item in values) else "partial"
        if not values:
            status = "failed" if status not in {"complete", "partial"} else status
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "scan_id": scan_id,
            "window": window.to_dict(),
            "status": status,
            "created_at": _now_iso(),
            "sources": [],
        }
        with self._locked():
            connection = self._connect()
            try:
                source_manifest: list[dict[str, Any]] = []
                prepared: list[tuple[AdapterResult, list[ConversationSlice], list[sqlite3.Row], list[TurnOmission], list[sqlite3.Row], dict[str, int]]] = []
                for result in values:
                    slices: list[ConversationSlice] = []
                    reused_rows: list[sqlite3.Row] = []
                    omissions: list[TurnOmission] = []
                    reused_omission_rows: list[sqlite3.Row] = []
                    counts = {
                        "examined": int(result.stats.get("examined", len(result.slices))),
                        "candidate_count": len(result.slices) + len(result.reused_slice_ids),
                        "matched": 0,
                        "created": 0,
                        "reused": 0,
                        "revised": 0,
                        "omitted": 0,
                        "reused_omissions": 0,
                        "duplicate_candidates": 0,
                        "skipped": int(result.stats.get("skipped", 0)),
                    }
                    seen_candidates: dict[str, str] = {}
                    for candidate in result.slices:
                        item = candidate.finalize()
                        errors = validate_slice(item, require_identity=True)
                        if errors:
                            counts["skipped"] += 1
                            continue
                        prior_revision = seen_candidates.get(item.slice_id or "")
                        if prior_revision is not None:
                            if prior_revision != item.revision:
                                raise StoreError(
                                    f"scan contains conflicting revisions for source {item.source} slice: {item.slice_id}"
                                )
                            counts["duplicate_candidates"] += 1
                            continue
                        seen_candidates[item.slice_id or ""] = item.revision or ""
                        path = self._slice_path(item)
                        payload = item.to_dict()
                        if path.exists():
                            try:
                                existing = json.loads(path.read_text(encoding="utf-8"))
                            except (OSError, json.JSONDecodeError) as exc:
                                raise StoreError(f"immutable revision file cannot be read: {path}") from exc
                            # ``materialized_at`` is intentionally excluded
                            # from the revision.  A repeated scan can build
                            # an otherwise identical object at a later wall
                            # clock time; retain the first immutable JSON
                            # instead of treating that mutable field as a
                            # conflict.
                            if canonical_revision(existing) != item.revision or existing.get("revision") != item.revision:
                                raise StoreError(
                                    f"immutable revision conflict for source {item.source} slice {item.slice_id}: {path}"
                                )
                        else:
                            self._atomic_json(path, payload)
                        existing_row = connection.execute(
                            "SELECT revision FROM slices_current WHERE slice_id = ?", (item.slice_id,)
                        ).fetchone()
                        if existing_row is None:
                            counts["created"] += 1
                        elif existing_row["revision"] == item.revision:
                            counts["reused"] += 1
                        else:
                            counts["revised"] += 1
                        slices.append(item)
                    seen_omissions: set[str] = set()
                    for candidate in result.omissions:
                        item = candidate.finalize()
                        errors = item.validate(require_identity=True)
                        if errors:
                            counts["skipped"] += 1
                            continue
                        omission_id = item.omission_id or ""
                        if omission_id in seen_omissions:
                            continue
                        seen_omissions.add(omission_id)
                        existing = connection.execute(
                            "SELECT omission_id FROM turn_omissions WHERE omission_id = ?", (omission_id,)
                        ).fetchone()
                        omissions.append(item)
                        counts["omitted"] += 1
                    candidate_ids = {item.slice_id for item in slices}
                    seen_reused: set[str] = set()
                    for slice_id in result.reused_slice_ids:
                        if slice_id in candidate_ids or slice_id in seen_reused:
                            counts["duplicate_candidates"] += 1
                            continue
                        seen_reused.add(slice_id)
                        row = connection.execute(
                            """
                            SELECT r.slice_id, r.revision, r.source, r.json_path
                            FROM slices_current c
                            JOIN slice_revisions r
                              ON r.slice_id = c.slice_id AND r.revision = c.revision
                            WHERE c.slice_id = ?
                            """,
                            (slice_id,),
                        ).fetchone()
                        if row is None:
                            raise StoreError(f"checkpoint reused slice is missing: {slice_id}")
                        if row["source"] != result.source:
                            raise StoreError(f"checkpoint reused slice source mismatch: {slice_id}")
                        path = self.root / row["json_path"]
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            raise StoreError(f"checkpoint reused slice content is unavailable: {slice_id}") from exc
                        if (
                            not isinstance(payload, Mapping)
                            or payload.get("slice_id") != slice_id
                            or payload.get("revision") != row["revision"]
                            or canonical_revision(payload) != row["revision"]
                        ):
                            raise StoreError(f"checkpoint reused slice content is invalid: {slice_id}")
                        reused_rows.append(row)
                        counts["reused"] += 1
                    seen_reused_omissions: set[str] = set()
                    for omission_id in result.reused_omission_ids:
                        if omission_id in seen_reused_omissions:
                            counts["reused_omissions"] += 1
                            continue
                        seen_reused_omissions.add(omission_id)
                        row = connection.execute(
                            "SELECT omission_id, source FROM turn_omissions WHERE omission_id = ?",
                            (omission_id,),
                        ).fetchone()
                        if row is None:
                            raise StoreError(f"checkpoint reused omission is missing: {omission_id}")
                        if row["source"] != result.source:
                            raise StoreError(f"checkpoint reused omission source mismatch: {omission_id}")
                        reused_omission_rows.append(row)
                        counts["reused_omissions"] += 1
                    counts["matched"] = len(slices) + len(reused_rows)
                    prepared.append((result, slices, reused_rows, omissions, reused_omission_rows, counts))

                connection.execute(
                    "INSERT OR REPLACE INTO scans(scan_id, from_ms, to_ms, from_iso, to_iso, status, created_at, manifest_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (scan_id, window.from_ms, window.to_ms, window.from_iso, window.to_iso, status, manifest["created_at"], f"scans/{_safe_component(scan_id)}.json"),
                )
                for result, slices, reused_rows, omissions, reused_omission_rows, counts in prepared:
                    for item in slices:
                        path = self._slice_path(item)
                        payload = item.to_dict()
                        started_ms = _epoch(item.started_at)
                        ended_ms = _epoch(item.ended_at)
                        content_hash = item.revision or canonical_revision(item)
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO slice_revisions(
                                slice_id, revision, source, conversation_id, title, workspace,
                                started_ms, ended_ms, content_completeness, provenance_trimmed,
                                adapter_version, content_hash, json_path, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                item.slice_id, item.revision, item.source, item.conversation_id,
                                item.conversation.get("title") if isinstance(item.conversation, Mapping) else None,
                                item.workspace, started_ms, ended_ms, item.content_completeness,
                                1 if item.provenance_trimmed else 0,
                                self._adapter_version(item), content_hash,
                                str(path.relative_to(self.root)), manifest["created_at"],
                            ),
                        )
                        connection.execute(
                            "INSERT INTO slices_current(slice_id, revision) VALUES (?, ?) ON CONFLICT(slice_id) DO UPDATE SET revision = excluded.revision",
                            (item.slice_id, item.revision),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO scan_slices(scan_id, slice_id, revision) VALUES (?, ?, ?)",
                            (scan_id, item.slice_id, item.revision),
                        )
                        text, title, label = self._fts_payload(item)
                        connection.execute("DELETE FROM slice_fts WHERE slice_id = ?", (item.slice_id,))
                        connection.execute("INSERT INTO slice_fts(slice_id, revision, text, title, label) VALUES (?, ?, ?, ?, ?)", (item.slice_id, item.revision, text, title, label))
                    for row in reused_rows:
                        connection.execute(
                            "INSERT OR IGNORE INTO scan_slices(scan_id, slice_id, revision) VALUES (?, ?, ?)",
                            (scan_id, row["slice_id"], row["revision"]),
                        )
                    for item in omissions:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO turn_omissions(
                                omission_id, source, conversation_id, native_unit_kind,
                                native_unit_id, at_ms, reason, source_ref,
                                adapter_name, adapter_version, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                item.omission_id, item.source, item.conversation_id,
                                item.native_unit.get("kind"), item.native_unit_id,
                                _epoch(item.at), item.reason, item.source_ref,
                                item.adapter.get("name") if isinstance(item.adapter, Mapping) else None,
                                item.adapter.get("version") if isinstance(item.adapter, Mapping) else None,
                                manifest["created_at"],
                            ),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO scan_omissions(scan_id, omission_id) VALUES (?, ?)",
                            (scan_id, item.omission_id),
                        )
                    for row in reused_omission_rows:
                        connection.execute(
                            "INSERT OR IGNORE INTO scan_omissions(scan_id, omission_id) VALUES (?, ?)",
                            (scan_id, row["omission_id"]),
                        )
                    if result.checkpoint is not None:
                        connection.execute(
                            "INSERT INTO source_checkpoints(source, checkpoint_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(source) DO UPDATE SET checkpoint_json = excluded.checkpoint_json, updated_at = excluded.updated_at",
                            (result.source, canonical_json(result.checkpoint), manifest["created_at"]),
                        )
                    source_stats = dict(result.stats)
                    source_stats.update(counts)
                    source_manifest.append({
                        "source": result.source,
                        "status": result.status,
                        "stats": source_stats,
                        "warnings": _copy_json(list(result.warnings)),
                        "error": _copy_json(result.error) if result.error is not None else None,
                    })
                manifest["sources"] = source_manifest
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            # The index is the authority.  A manifest write failure must not
            # roll back a committed transaction; report it to the caller.
            manifest_path = self.scans_dir / f"{_safe_component(scan_id)}.json"
            try:
                self._atomic_json(manifest_path, manifest)
            except OSError as exc:
                manifest["manifest_error"] = str(exc)
                # The committed index remains queryable, but this scan is not
                # complete without its immutable manifest.  Persist and return
                # the degraded state so the CLI exits non-zero instead of
                # silently reporting success.
                manifest["status"] = "partial" if manifest.get("status") == "complete" else manifest.get("status", "failed")
                connection = self._connect()
                try:
                    connection.execute(
                        "UPDATE scans SET status = ? WHERE scan_id = ?",
                        (manifest["status"], scan_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
        return manifest

    def load_checkpoint(self, source: str) -> Optional[dict[str, Any]]:
        if not self.db_path.exists():
            return None
        connection = self._connect()
        try:
            row = connection.execute("SELECT checkpoint_json FROM source_checkpoints WHERE source = ?", (source,)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row["checkpoint_json"])
            except json.JSONDecodeError as exc:
                raise StoreError(f"checkpoint for {source} is invalid JSON") from exc
            if not isinstance(value, dict):
                raise StoreError(f"checkpoint for {source} must be an object")
            return value
        finally:
            connection.close()

    def pruned_overlap(self, from_ms: Optional[int] = None, to_ms: Optional[int] = None) -> list[dict[str, Any]]:
        """Return pruned source-days that intersect a window.

        A caller that reports on a window is responsible for saying that part
        of it no longer has evidence; this is how it finds out.
        """

        if not self.db_path.exists():
            return []
        where = ["1 = 1"]
        params: list[Any] = []
        if from_ms is not None:
            where.append("day >= ?")
            params.append(_day_from_epoch(int(from_ms)))
        if to_ms is not None:
            where.append("day <= ?")
            params.append(_day_from_epoch(int(to_ms)))
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT source, day, slice_count, scope, pruned_at FROM pruned_days "
                f"WHERE {' AND '.join(where)} ORDER BY day ASC, source ASC, scope ASC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            return []
        finally:
            connection.close()

    def usage(self) -> dict[str, Any]:
        """Report real footprint by component, plus an observed growth rate.

        Every number here is measured, not estimated, apart from
        ``projection``, which extrapolates the observed daily rate and is
        labelled as such.
        """

        result: dict[str, Any] = {
            "root": str(self.root),
            "components": {},
            "total_bytes": 0,
            "slices": {},
            "projection": None,
        }
        components: dict[str, int] = {}
        components["slices_json"] = _tree_bytes(self.slices_dir)
        components["scans_json"] = _tree_bytes(self.scans_dir)
        components["index_sqlite"] = self.db_path.stat().st_size if self.db_path.exists() else 0
        result["components"] = components
        result["total_bytes"] = sum(components.values())
        if not self.db_path.exists():
            return result

        connection = self._connect()
        try:
            tables: dict[str, int] = {}
            try:
                for name, size in connection.execute(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
                ).fetchall():
                    tables[str(name)] = int(size or 0)
            except sqlite3.DatabaseError:
                tables = {}
            fts_bytes = sum(size for name, size in tables.items() if name.startswith("slice_fts"))
            result["index_breakdown"] = {
                "full_text_search": fts_bytes,
                "checkpoints": tables.get("source_checkpoints", 0),
                "other": max(0, components["index_sqlite"] - fts_bytes - tables.get("source_checkpoints", 0)),
            }
            row = connection.execute(
                "SELECT count(*) n, min(r.started_ms) lo, max(r.ended_ms) hi "
                "FROM slices_current c JOIN slice_revisions r "
                "  ON r.slice_id = c.slice_id AND r.revision = c.revision"
            ).fetchone()
            count = int(row["n"] or 0)
            result["slices"] = {
                "current": count,
                "revisions": int(connection.execute("SELECT count(*) FROM slice_revisions").fetchone()[0]),
                "earliest": _iso_from_epoch(row["lo"]) if row["lo"] is not None else None,
                "latest": _iso_from_epoch(row["hi"]) if row["hi"] is not None else None,
            }
            result["by_source"] = [
                dict(item) for item in connection.execute(
                    "SELECT r.source, count(*) slices FROM slices_current c "
                    "JOIN slice_revisions r ON r.slice_id = c.slice_id AND r.revision = c.revision "
                    "GROUP BY r.source ORDER BY slices DESC"
                ).fetchall()
            ]
            try:
                result["pruned_days"] = int(
                    connection.execute("SELECT count(*) FROM pruned_days").fetchone()[0]
                )
            except sqlite3.DatabaseError:
                # A store written before retention existed has no tombstone
                # table yet.  Reporting usage must not create one.
                result["pruned_days"] = 0
                result["schema_upgrade_pending"] = True
            if count and row["lo"] is not None and row["hi"] is not None:
                span_days = max(1.0, (int(row["hi"]) - int(row["lo"])) / 86_400_000)
                bytes_per_day = result["total_bytes"] / span_days
                result["projection"] = {
                    "observed_span_days": round(span_days, 1),
                    "bytes_per_day": int(bytes_per_day),
                    "bytes_per_year_unbounded": int(bytes_per_day * 365),
                    "basis": "observed total footprint divided by the stored time span",
                }
        finally:
            connection.close()
        return result

    def list_scans(
        self,
        window: Optional[TimeWindow] = None,
        *,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List persisted scan manifests without reading any source.

        The SQLite row is the status filter authority. The immutable JSON
        manifest is joined into the result so a caller can distinguish a
        complete all-source scan from a complete single-source scan,
        including the valid zero-match case.
        """

        if limit < 1:
            raise ValueError("scan limit must be positive")
        if not self.db_path.exists():
            return []
        where = ["1 = 1"]
        params: list[Any] = []
        if window is not None:
            if not isinstance(window, TimeWindow):
                raise StoreError("scan query requires a TimeWindow")
            where.extend(["from_ms = ?", "to_ms = ?"])
            params.extend([window.from_ms, window.to_ms])
        if status:
            where.append("status = ?")
            params.append(status)
        params.append(int(limit))
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT scan_id, from_iso, to_iso, status, created_at, manifest_path
                FROM scans
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC, scan_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            connection.close()

        records: list[dict[str, Any]] = []
        for row in rows:
            record: dict[str, Any] = {
                "scan_id": row["scan_id"],
                "window": {
                    "from_iso": row["from_iso"],
                    "to_iso": row["to_iso"],
                },
                "status": row["status"],
                "created_at": row["created_at"],
                "sources": [],
                "manifest_valid": False,
            }
            manifest_path = self.root / str(row["manifest_path"])
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, Mapping):
                    raise StoreError("manifest must contain an object")
                if manifest.get("scan_id") != row["scan_id"]:
                    raise StoreError("manifest scan_id does not match index")
                record["window"] = _copy_json(manifest.get("window") or record["window"])
                record["sources"] = _copy_json(manifest.get("sources") or [])
                record["manifest_valid"] = True
                if manifest.get("status") != row["status"]:
                    record["manifest_status"] = manifest.get("status")
            except (OSError, json.JSONDecodeError, StoreError) as exc:
                record["manifest_error"] = str(exc)
            records.append(record)
        return records

    def compact(self) -> dict[str, Any]:
        """Reclaim space without removing a single fact.

        Merges FTS segments left behind by revision churn and rewrites the
        database file.  Nothing here changes what can be queried.
        """

        self.ensure_initialized()
        before = self.db_path.stat().st_size
        with self._locked():
            connection = self._connect()
            try:
                connection.execute("INSERT INTO slice_fts(slice_fts) VALUES('optimize')")
                connection.commit()
            finally:
                connection.close()
            # VACUUM cannot run inside a transaction and needs its own
            # connection with autocommit behaviour.
            connection = sqlite3.connect(str(self.db_path), timeout=300, isolation_level=None)
            try:
                connection.execute("VACUUM")
            finally:
                connection.close()
        os.chmod(self.db_path, 0o600)
        after = self.db_path.stat().st_size
        return {"index_bytes_before": before, "index_bytes_after": after, "reclaimed_bytes": before - after}

    def prune(
        self,
        *,
        slices_before_ms: Optional[int] = None,
        fts_before_ms: Optional[int] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Apply a retention boundary, recording what was removed.

        The two boundaries are independent and are applied least destructive
        first: dropping full-text coverage keeps every fact and only narrows
        search, while dropping revision JSON destroys evidence.  Either way a
        per-source-day tombstone survives, so a later query over the window
        can tell "pruned" from "nothing happened".
        """

        if not self.db_path.exists():
            return {"dry_run": dry_run, "fts_rows": 0, "slices": 0, "bytes_freed": 0}
        # Pruning writes tombstones, so the store must carry the current
        # schema before it runs.
        self.ensure_initialized()
        plan = {
            "dry_run": bool(dry_run),
            "fts_rows": 0,
            "slices": 0,
            "index_rows": 0,
            "bytes_freed": 0,
            "pruned_days": [],
        }
        now = _now_iso()
        with self._locked():
            connection = self._connect()
            try:
                if fts_before_ms is not None:
                    rows = connection.execute(
                        "SELECT c.slice_id, r.source, r.started_ms FROM slices_current c "
                        "JOIN slice_revisions r "
                        "  ON r.slice_id = c.slice_id AND r.revision = c.revision "
                        "WHERE r.ended_ms < ? AND EXISTS "
                        "  (SELECT 1 FROM slice_fts f WHERE f.slice_id = c.slice_id)",
                        (int(fts_before_ms),),
                    ).fetchall()
                    plan["fts_rows"] = len(rows)
                    fts_days: Dict[tuple[str, str], int] = {}
                    for row in rows:
                        key = (row["source"], _day_from_epoch(int(row["started_ms"])))
                        fts_days[key] = fts_days.get(key, 0) + 1
                    if not dry_run:
                        for row in rows:
                            connection.execute("DELETE FROM slice_fts WHERE slice_id = ?", (row["slice_id"],))
                        # Recorded so ``validate`` can tell a deliberately
                        # un-indexed slice from a missing index row.
                        for (source, day), count in sorted(fts_days.items()):
                            connection.execute(
                                "INSERT INTO pruned_days(source, day, slice_count, scope, pruned_at) "
                                "VALUES (?, ?, ?, 'fts', ?) "
                                "ON CONFLICT(source, day, scope) DO UPDATE SET "
                                "  slice_count = excluded.slice_count, pruned_at = excluded.pruned_at",
                                (source, day, count, now),
                            )

                if slices_before_ms is not None:
                    rows = connection.execute(
                        "SELECT r.slice_id, r.revision, r.source, r.json_path, r.started_ms "
                        "FROM slice_revisions r WHERE r.ended_ms < ?",
                        (int(slices_before_ms),),
                    ).fetchall()
                    by_day: Dict[tuple[str, str], int] = {}
                    freed = 0
                    for row in rows:
                        path = self.root / row["json_path"]
                        try:
                            freed += path.stat().st_size
                        except OSError:
                            pass
                        by_day[(row["source"], _day_from_epoch(int(row["started_ms"])))] = (
                            by_day.get((row["source"], _day_from_epoch(int(row["started_ms"]))), 0) + 1
                        )
                    plan["slices"] = len(rows)
                    plan["bytes_freed"] = freed
                    plan["pruned_days"] = [
                        {"source": source, "day": day, "slice_count": count}
                        for (source, day), count in sorted(by_day.items())
                    ]
                    if not dry_run:
                        for row in rows:
                            connection.execute(
                                "DELETE FROM scan_slices WHERE slice_id = ? AND revision = ?",
                                (row["slice_id"], row["revision"]),
                            )
                            connection.execute(
                                "DELETE FROM slices_current WHERE slice_id = ? AND revision = ?",
                                (row["slice_id"], row["revision"]),
                            )
                            connection.execute("DELETE FROM slice_fts WHERE slice_id = ?", (row["slice_id"],))
                            connection.execute(
                                "DELETE FROM slice_revisions WHERE slice_id = ? AND revision = ?",
                                (row["slice_id"], row["revision"]),
                            )
                        for (source, day), count in sorted(by_day.items()):
                            connection.execute(
                                "INSERT INTO pruned_days(source, day, slice_count, scope, pruned_at) "
                                "VALUES (?, ?, ?, 'content', ?) "
                                "ON CONFLICT(source, day, scope) DO UPDATE SET "
                                "  slice_count = slice_count + excluded.slice_count, "
                                "  pruned_at = excluded.pruned_at",
                                (source, day, count, now),
                            )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            if not dry_run and slices_before_ms is not None:
                for entry in self._orphan_slice_files():
                    try:
                        entry.unlink()
                    except OSError:
                        pass
                _prune_empty_dirs(self.slices_dir)
        return plan

    def _orphan_slice_files(self) -> list[Path]:
        if not self.slices_dir.exists() or not self.db_path.exists():
            return []
        connection = self._connect()
        try:
            indexed = {str(row[0]) for row in connection.execute("SELECT json_path FROM slice_revisions")}
        finally:
            connection.close()
        orphans: list[Path] = []
        for path in self.slices_dir.rglob("*.json"):
            try:
                relative = str(path.relative_to(self.root))
            except ValueError:
                continue
            if relative not in indexed:
                orphans.append(path)
        return orphans

    def list_slices(self, filters: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        params_filters: dict[str, Any] = dict(filters or {})
        params_filters.update(kwargs)
        where = ["1 = 1"]
        params: list[Any] = []
        source = params_filters.get("source")
        if source:
            where.append("c.source = ?")
            params.append(source)
        conversation = params_filters.get("conversation") or params_filters.get("conversation_id")
        if conversation:
            where.append("c.conversation_id = ?")
            params.append(conversation)
        workspace = params_filters.get("workspace")
        if workspace:
            where.append("c.workspace = ?")
            params.append(workspace)
        completeness = params_filters.get("content_completeness") or params_filters.get("completeness")
        if completeness:
            where.append("c.content_completeness = ?")
            params.append(completeness)
        trimmed = params_filters.get("provenance_trimmed")
        if trimmed is not None:
            where.append("c.provenance_trimmed = ?")
            params.append(1 if trimmed else 0)
        adapter_version = params_filters.get("adapter_version")
        if adapter_version:
            where.append("c.adapter_version = ?")
            params.append(adapter_version)
        from_ms = params_filters.get("from_ms")
        to_ms = params_filters.get("to_ms")
        window = params_filters.get("window")
        if isinstance(window, TimeWindow):
            from_ms, to_ms = window.from_ms, window.to_ms
        elif isinstance(window, Mapping):
            parsed_window = TimeWindow.from_mapping(window)
            from_ms, to_ms = parsed_window.from_ms, parsed_window.to_ms
        if from_ms is not None:
            where.append("c.ended_ms >= ?")
            params.append(int(from_ms))
        if to_ms is not None:
            where.append("c.started_ms < ?")
            params.append(int(to_ms))
        scan_id = params_filters.get("scan") or params_filters.get("scan_id")
        join = ""
        if scan_id:
            join = "JOIN scan_slices ss ON ss.slice_id = c.slice_id AND ss.revision = c.revision"
            where.append("ss.scan_id = ?")
            params.append(scan_id)
        query = params_filters.get("query")
        if query:
            join += " JOIN slice_fts f ON f.slice_id = c.slice_id AND f.revision = c.revision"
            query_text = str(query)
            if len(query_text) < 3:
                # FTS5 trigram MATCH cannot resolve one- or two-character CJK
                # terms.  Keep this edge-case scan inside the derived FTS
                # table rather than reopening every revision JSON in Python.
                escaped = (
                    query_text.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                where.append(
                    "(f.text LIKE ? ESCAPE '\\' OR f.title LIKE ? ESCAPE '\\' "
                    "OR f.label LIKE ? ESCAPE '\\')"
                )
                params.extend([f"%{escaped}%"] * 3)
            else:
                where.append("slice_fts MATCH ?")
                params.append('"' + query_text.replace('"', '""') + '"')
        sql = f"""
            SELECT c.slice_id, c.revision, c.source, c.conversation_id, c.title,
                   c.workspace, c.started_ms, c.ended_ms, c.content_completeness,
                   c.provenance_trimmed, c.adapter_version, c.json_path
            FROM slices_current current
            JOIN slice_revisions c
              ON c.slice_id = current.slice_id AND c.revision = current.revision
            {join}
            WHERE {' AND '.join(where)}
            ORDER BY c.started_ms DESC, c.slice_id ASC
        """
        connection = self._connect()
        try:
            rows = connection.execute(sql, params).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["conversation"] = {
                    "id": item["conversation_id"],
                    "title": item.get("title"),
                }
                item["provenance_trimmed"] = bool(item.get("provenance_trimmed"))
                item["started_at"] = _iso_from_epoch(item.pop("started_ms"))
                item["ended_at"] = _iso_from_epoch(item.pop("ended_ms"))
                items.append(item)
            return items
        except sqlite3.OperationalError as exc:
            if query:
                raise StoreError(f"FTS query failed: {exc}") from exc
            raise
        finally:
            connection.close()

    def list(self, filters: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_slices(filters, **kwargs)

    def list_omissions(self, filters: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> list[dict[str, Any]]:
        """List contentless explicit-abort records; never joins FTS."""

        if not self.db_path.exists():
            return []
        values = dict(filters or {})
        values.update(kwargs)
        where = ["1 = 1"]
        params: list[Any] = []
        if values.get("source"):
            where.append("source = ?")
            params.append(str(values["source"]))
        if values.get("reason"):
            where.append("reason = ?")
            params.append(str(values["reason"]))
        if values.get("from_ms") is not None:
            where.append("at_ms >= ?")
            params.append(int(values["from_ms"]))
        if values.get("to_ms") is not None:
            where.append("at_ms < ?")
            params.append(int(values["to_ms"]))
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT omission_id, source, conversation_id, native_unit_kind, native_unit_id, at_ms, reason, source_ref, adapter_name, adapter_version FROM turn_omissions WHERE {' AND '.join(where)} ORDER BY at_ms DESC, omission_id ASC",
                params,
            ).fetchall()
            return [
                {
                    **dict(row),
                    "conversation": {"id": row["conversation_id"]},
                    "native_unit": {"kind": row["native_unit_kind"], "id": row["native_unit_id"]},
                    "at": _iso_from_epoch(row["at_ms"]),
                    "adapter": {"name": row["adapter_name"], "version": row["adapter_version"]},
                }
                for row in rows
            ]
        finally:
            connection.close()

    def show(self, slice_id: str, revision: Optional[str] = None) -> dict[str, Any]:
        if not self.db_path.exists():
            raise SessionNotFound(f"slice or revision not found: {slice_id} {revision or ''}".strip())
        connection = self._connect()
        try:
            if revision is None:
                row = connection.execute(
                    "SELECT r.json_path, r.revision FROM slices_current c JOIN slice_revisions r ON r.slice_id = c.slice_id AND r.revision = c.revision WHERE c.slice_id = ?",
                    (slice_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT json_path, revision FROM slice_revisions WHERE slice_id = ? AND revision = ?",
                    (slice_id, revision),
                ).fetchone()
            if row is None:
                raise SessionNotFound(f"slice or revision not found: {slice_id} {revision or ''}".strip())
            path = self.root / row["json_path"]
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise StoreError(f"slice revision file is missing: {path}") from exc
            except json.JSONDecodeError as exc:
                raise StoreError(f"slice revision file is invalid JSON: {path}") from exc
            if not isinstance(payload, Mapping):
                raise StoreError(f"slice revision file must contain an object: {path}")
            if canonical_revision(payload) != row["revision"] or payload.get("revision") != row["revision"]:
                raise StoreError(f"slice revision hash mismatch: {slice_id} {row['revision']}")
            errors = validate_slice(payload, require_identity=True)
            if errors:
                raise StoreError("invalid slice revision: " + "; ".join(errors))
            return payload
        finally:
            connection.close()

    def validate(self, scan_id: Optional[str] = None) -> list[str]:
        errors: list[str] = []
        if not self.root.exists():
            return [f"sessions root missing: {self.root}"]
        for path, expected in ((self.root, 0o700), (self.slices_dir, 0o700), (self.scans_dir, 0o700), (self.lock_path, 0o600), (self.db_path, 0o600)):
            if not path.exists():
                errors.append(f"missing required path: {path}")
            else:
                try:
                    if _mode(path) != expected:
                        errors.append(f"permissions for {path} are {_mode(path):04o}, expected {expected:04o}")
                except OSError as exc:
                    errors.append(f"cannot stat {path}: {exc}")
        connection = None
        indexed_paths: set[str] = set()
        if not self.db_path.exists():
            return errors
        try:
            connection = self._connect()
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                errors.append("SQLite foreign_keys is disabled")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')")}
            required = {
                "scans", "slice_revisions", "slices_current", "scan_slices",
                "source_checkpoints", "slice_fts", "pruned_days",
                "turn_omissions", "scan_omissions",
            }
            for table in sorted(required - tables):
                if table == "pruned_days":
                    # Purely additive and empty on arrival: a store written
                    # before retention existed is out of date, not damaged.
                    errors.append(
                        "missing SQLite table: pruned_days"
                        "（存量库尚未升级到当前 schema；执行 lifeos sessions compact 或下一次 scan 会创建）"
                    )
                    continue
                errors.append(f"missing SQLite table: {table}")
            try:
                errors.extend(f"foreign key: {dict(row)}" for row in connection.execute("PRAGMA foreign_key_check").fetchall())
            except sqlite3.DatabaseError as exc:
                errors.append(f"foreign key check failed: {exc}")
            scan_clause = " WHERE scan_id = ?" if scan_id else ""
            scan_params = (scan_id,) if scan_id else ()
            scans = connection.execute(f"SELECT * FROM scans{scan_clause}", scan_params).fetchall()
            if scan_id and not scans:
                errors.append(f"scan not found: {scan_id}")
            for scan in scans:
                manifest_path = self.root / scan["manifest_path"]
                if not manifest_path.exists():
                    errors.append(f"missing scan manifest: {manifest_path}")
                else:
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        if not isinstance(manifest, Mapping):
                            errors.append(f"invalid scan manifest {manifest_path}: expected object")
                            continue
                        if manifest.get("scan_id") != scan["scan_id"]:
                            errors.append(f"manifest scan id mismatch: {manifest_path}")
                    except (OSError, json.JSONDecodeError) as exc:
                        errors.append(f"invalid scan manifest {manifest_path}: {exc}")
            revisions = connection.execute("SELECT * FROM slice_revisions").fetchall()
            revision_payloads: dict[tuple[str, str], dict[str, Any]] = {}
            for row in revisions:
                indexed_paths.add(str(row["json_path"]))
                path = self.root / row["json_path"]
                if not path.exists():
                    errors.append(f"missing revision JSON: {path}")
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if _mode(path) != 0o600:
                        errors.append(f"permissions for {path} are {_mode(path):04o}, expected 0600")
                    if not isinstance(payload, Mapping):
                        errors.append(f"invalid revision JSON {path}: expected object")
                        continue
                    if payload.get("slice_id") != row["slice_id"] or payload.get("revision") != row["revision"]:
                        errors.append(f"index identity mismatch: {path}")
                    if canonical_revision(payload) != row["revision"]:
                        errors.append(f"revision hash mismatch: {path}")
                    errors.extend(f"{path}: {error}" for error in validate_slice(payload, require_identity=True))
                    if isinstance(payload, dict):
                        revision_payloads[(row["slice_id"], row["revision"])] = payload
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"invalid revision JSON {path}: {exc}")
            currents = connection.execute("SELECT slice_id, revision FROM slices_current").fetchall()
            current_keys = {(row["slice_id"], row["revision"]) for row in currents}
            fts_rows = connection.execute("SELECT slice_id, revision, text, title, label FROM slice_fts").fetchall()
            fts_by_key: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for row in fts_rows:
                fts_by_key.setdefault((row["slice_id"], row["revision"]), []).append(row)
            # A slice whose day was deliberately dropped from full-text search
            # is not a missing index row.
            try:
                fts_pruned = {
                    (row["source"], row["day"])
                    for row in connection.execute(
                        "SELECT source, day FROM pruned_days WHERE scope = 'fts'"
                    ).fetchall()
                }
            except sqlite3.DatabaseError:
                fts_pruned = set()
            revision_days = {
                (row["slice_id"], row["revision"]): (row["source"], _day_from_epoch(int(row["started_ms"])))
                for row in revisions
            }
            for current in currents:
                key = (current["slice_id"], current["revision"])
                row = connection.execute("SELECT 1 FROM slice_revisions WHERE slice_id = ? AND revision = ?", (current["slice_id"], current["revision"])).fetchone()
                if row is None:
                    errors.append(f"current pointer has no revision: {current['slice_id']}")
                matching_fts = fts_by_key.get(key, [])
                if not matching_fts:
                    if revision_days.get(key) in fts_pruned:
                        continue
                    errors.append(f"missing FTS row: {current['slice_id']} {current['revision']}")
                elif len(matching_fts) != 1:
                    errors.append(f"duplicate FTS rows: {current['slice_id']} {current['revision']}")
                elif key in revision_payloads:
                    try:
                        expected = self._fts_payload(ConversationSlice.from_dict(revision_payloads[key]))
                    except (SessionValidationError, TypeError, ValueError) as exc:
                        errors.append(f"cannot derive FTS payload for {current['slice_id']} {current['revision']}: {exc}")
                        continue
                    actual = (matching_fts[0]["text"], matching_fts[0]["title"], matching_fts[0]["label"])
                    if actual != expected:
                        errors.append(f"FTS content mismatch: {current['slice_id']} {current['revision']}")
            for key in sorted(set(fts_by_key) - current_keys):
                errors.append(f"orphan FTS row: {key[0]} {key[1]}")
            for checkpoint in connection.execute("SELECT source, checkpoint_json FROM source_checkpoints").fetchall():
                try:
                    value = json.loads(checkpoint["checkpoint_json"])
                    if not isinstance(value, dict):
                        errors.append(f"checkpoint is not an object: {checkpoint['source']}")
                except json.JSONDecodeError:
                    errors.append(f"checkpoint is invalid JSON: {checkpoint['source']}")
            try:
                omission_rows = connection.execute("SELECT * FROM turn_omissions").fetchall()
                for row in omission_rows:
                    if row["reason"] != "explicit_abort_without_work":
                        errors.append(f"invalid omission reason: {row['omission_id']}")
                    if row["native_unit_kind"] != "turn":
                        errors.append(f"invalid omission native unit: {row['omission_id']}")
                    if not row["adapter_name"]:
                        errors.append(f"omission missing adapter: {row['omission_id']}")
                    try:
                        payload = TurnOmission(
                            source=row["source"],
                            conversation={"id": row["conversation_id"]},
                            native_unit={"kind": row["native_unit_kind"], "id": row["native_unit_id"]},
                            at=_iso_from_epoch(row["at_ms"]),
                            reason=row["reason"],
                            source_ref=row["source_ref"],
                            adapter={"name": row["adapter_name"], "version": row["adapter_version"]},
                            omission_id=row["omission_id"],
                        )
                        errors.extend(f"omission {row['omission_id']}: {error}" for error in payload.validate(require_identity=True))
                    except Exception as exc:
                        errors.append(f"invalid omission row {row['omission_id']}: {exc}")
            except sqlite3.DatabaseError as exc:
                errors.append(f"omission validation failed: {exc}")
        except (sqlite3.DatabaseError, StoreError) as exc:
            errors.append(f"SQLite validation failed: {exc}")
        finally:
            if connection is not None:
                connection.close()
        if self.slices_dir.exists():
            for path in self.slices_dir.rglob("*"):
                if path.is_dir() and _mode(path) != 0o700:
                    errors.append(f"permissions for {path} are {_mode(path):04o}, expected 0700")
            for path in self.slices_dir.rglob("*.json"):
                try:
                    relative = str(path.relative_to(self.root))
                except ValueError:
                    continue
                if relative not in indexed_paths:
                    errors.append(f"orphan revision JSON: {path}")
        if self.scans_dir.exists():
            for path in self.scans_dir.rglob("*"):
                if path.is_dir() and _mode(path) != 0o700:
                    errors.append(f"permissions for {path} are {_mode(path):04o}, expected 0700")
                elif path.is_file() and _mode(path) != 0o600:
                    errors.append(f"permissions for {path} are {_mode(path):04o}, expected 0600")
        return errors


def _epoch(value: str) -> int:
    return int(parse_iso_datetime(value, field_name="slice time").timestamp() * 1000)


def _iso_from_epoch(value: int) -> str:
    moment = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    text = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return text[:-5] + "Z" if text.endswith(".000Z") else text


def _day_from_epoch(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).strftime("%Y-%m-%d")


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            try:
                path.rmdir()
            except OSError:
                pass
        except OSError:
            continue
