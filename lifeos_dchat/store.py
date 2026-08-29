"""Private DChat raw authority and indexes."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional
from zoneinfo import ZoneInfo

from .model import DCHAT_SCHEMA_VERSION, canonical_bytes, content_hash, stable_component


TIMEZONE = ZoneInfo("Asia/Shanghai")
DIR_MODE = 0o700
FILE_MODE = 0o600
IGNORED_RUNTIME_NAMES = {".DS_Store"}


class DChatStoreError(RuntimeError):
    """Raised when private DChat evidence is missing or malformed."""


def _now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="microseconds")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _managed_files(root: Path) -> List[Path]:
    """Return DChat-owned files, excluding known host filesystem metadata."""
    if not root.exists():
        return []
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name not in IGNORED_RUNTIME_NAMES
    ]


class DChatStore:
    """Own immutable message revisions and their query index."""

    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root).expanduser()
        self.database_path = self.root / "dchat.sqlite"
        self.scans_dir = self.root / "scans"
        self.messages_dir = self.root / "messages"
        self.metadata_dir = self.root / "metadata"
        self.lock_path = self.root.parent / ".lifeos-dchat.lock"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True, mode=FILE_MODE)
        os.chmod(self.lock_path, FILE_MODE)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _ensure_layout(self) -> None:
        for path in (self.root, self.scans_dir, self.messages_dir, self.metadata_dir):
            path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            os.chmod(path, DIR_MODE)

    def _atomic_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        current = path.parent
        while current == self.root or self.root in current.parents:
            os.chmod(current, DIR_MODE)
            if current == self.root:
                break
            current = current.parent
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(payload) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, FILE_MODE)
            os.replace(temporary, path)
            os.chmod(path, FILE_MODE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _connect(self) -> sqlite3.Connection:
        self._ensure_layout()
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_revisions (
              message_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              occurred_at TEXT,
              json_path TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              PRIMARY KEY (message_id, revision)
            );
            CREATE TABLE IF NOT EXISTS messages_current (
              message_id TEXT PRIMARY KEY,
              revision TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scans (
              scan_id TEXT PRIMARY KEY,
              from_value TEXT NOT NULL,
              to_value TEXT NOT NULL,
              status TEXT NOT NULL,
              manifest_path TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_messages (
              scan_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              PRIMARY KEY (scan_id, message_id, revision)
            );
            CREATE TABLE IF NOT EXISTS conversation_snapshots (
              scan_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              type TEXT NOT NULL,
              scope_state TEXT NOT NULL,
              status TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              PRIMARY KEY (scan_id, conversation_id)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
              conversation_id TEXT PRIMARY KEY,
              to_value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_revisions_time
              ON message_revisions (occurred_at, conversation_id);
            CREATE INDEX IF NOT EXISTS idx_scan_messages_scan
              ON scan_messages (scan_id);
            """
        )
        os.chmod(self.database_path, FILE_MODE)
        return connection

    @contextlib.contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                f"file:{self.database_path}?mode=ro", uri=True
            )
            connection.row_factory = sqlite3.Row
            try:
                yield connection
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise DChatStoreError(f"DChat SQLite 不可读：{exc}") from exc

    def write_scan(self, result: Mapping[str, Any]) -> Dict[str, Any]:
        if result.get("schema_version") != DCHAT_SCHEMA_VERSION:
            raise DChatStoreError(
                f"DChat scan schema_version 必须为 {DCHAT_SCHEMA_VERSION}"
            )
        if result.get("status") not in {"complete", "partial", "failed"}:
            raise DChatStoreError("DChat scan status 非法")
        scan_id = "DCHATSCAN-" + datetime.now(TIMEZONE).strftime("%Y%m%dT%H%M%S%z") + "-" + uuid.uuid4().hex[:8]
        observed_at = _now_iso()
        metadata_payload = {"schema_version": DCHAT_SCHEMA_VERSION, "scan_id": scan_id, "conversations": []}
        manifest = {
            "schema_version": DCHAT_SCHEMA_VERSION,
            "scan_id": scan_id,
            "captured_at": observed_at,
            "status": result["status"],
            "window": dict(result["window"]),
            "evidence_level": "supporting",
            "conversations": [],
            "summary": dict(result["summary"]),
            "metadata_ref": f"metadata/{scan_id}.json",
        }
        revision_rows: List[Dict[str, Any]] = []
        with self.locked():
            self._ensure_layout()
            for conversation in result.get("conversations") or []:
                cid = conversation.get("conversation_id")
                if not cid:
                    continue
                metadata = conversation.get("metadata") or {}
                metadata_payload["conversations"].append({"conversation_id": cid, "metadata": metadata})
                refs = []
                for item in conversation.get("messages") or []:
                    key = str(item["message_key"])
                    payload = item["payload"]
                    revision = content_hash(payload)
                    relative = Path("messages") / stable_component(str(cid)) / stable_component(key) / (revision.removeprefix("sha256:") + ".json")
                    path = self.root / relative
                    envelope = {
                        "schema_version": DCHAT_SCHEMA_VERSION,
                        "conversation_id": str(cid),
                        "message_key": key,
                        "revision": revision,
                        "occurred_at": item.get("occurred_at"),
                        "observed_at": observed_at,
                        "payload": payload,
                    }
                    if not path.exists():
                        self._atomic_json(path, envelope)
                    refs.append({"message_key": key, "revision": revision, "occurred_at": item.get("occurred_at")})
                    revision_rows.append({
                        "message_id": key, "revision": revision, "conversation_id": str(cid),
                        "occurred_at": item.get("occurred_at"), "json_path": str(relative),
                    })
                manifest["conversations"].append({
                    "conversation_id": str(cid),
                    "type": conversation.get("type"),
                    "scope": conversation.get("scope"),
                    "status": conversation.get("status"),
                    "warnings": list(conversation.get("warnings") or []),
                    "windows": list(conversation.get("windows") or []),
                    "message_refs": refs,
                })
            metadata_path = self.metadata_dir / f"{scan_id}.json"
            manifest_path = self.scans_dir / f"{scan_id}.json"
            self._atomic_json(metadata_path, metadata_payload)
            self._atomic_json(manifest_path, manifest)
            with self._connect() as connection:
                for row in revision_rows:
                    connection.execute(
                        "INSERT OR IGNORE INTO message_revisions VALUES (?, ?, ?, ?, ?, ?)",
                        (row["message_id"], row["revision"], row["conversation_id"], row["occurred_at"], row["json_path"], observed_at),
                    )
                    connection.execute(
                        "INSERT INTO messages_current VALUES (?, ?) ON CONFLICT(message_id) DO UPDATE SET revision=excluded.revision",
                        (row["message_id"], row["revision"]),
                    )
                    connection.execute("INSERT OR IGNORE INTO scan_messages VALUES (?, ?, ?)", (scan_id, row["message_id"], row["revision"]))
                for conversation in result.get("conversations") or []:
                    cid = conversation.get("conversation_id")
                    if not cid:
                        continue
                    connection.execute(
                        "INSERT INTO conversation_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                        (scan_id, str(cid), str(conversation.get("type") or ""), str(conversation.get("scope") or ""), str(conversation.get("status") or ""), json.dumps(conversation.get("metadata") or {}, ensure_ascii=False, sort_keys=True)),
                    )
                    if conversation.get("scope") == "collect_body" and conversation.get("status") == "complete":
                        existing = connection.execute(
                            "SELECT to_value FROM checkpoints WHERE conversation_id=?", (str(cid),)
                        ).fetchone()
                        if existing is None or str(existing["to_value"]) < str(manifest["window"]["to"]):
                            connection.execute(
                                "INSERT INTO checkpoints VALUES (?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET to_value=excluded.to_value, updated_at=excluded.updated_at",
                                (str(cid), manifest["window"]["to"], observed_at),
                            )
                connection.execute(
                    "INSERT INTO scans VALUES (?, ?, ?, ?, ?, ?)",
                    (scan_id, manifest["window"]["from"], manifest["window"]["to"], manifest["status"], str(manifest_path.relative_to(self.root)), observed_at),
                )
        return manifest

    def read_scan(self, scan_id: str) -> Dict[str, Any]:
        path = self.scans_dir / f"{scan_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DChatStoreError(f"DChat scan 不可读：{scan_id}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("scan_id") != scan_id:
            raise DChatStoreError(f"DChat scan 内容非法：{scan_id}")
        if payload.get("schema_version") != DCHAT_SCHEMA_VERSION:
            raise DChatStoreError(
                f"DChat scan schema_version 必须为 {DCHAT_SCHEMA_VERSION}：{scan_id}"
            )
        return payload

    def list_scans(self, from_value: Optional[str] = None, to_value: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.database_path.exists():
            return []
        query = "SELECT * FROM scans"
        values: List[Any] = []
        if from_value is not None or to_value is not None:
            if from_value is None or to_value is None:
                raise DChatStoreError("--from 与 --to 必须同时提供")
            query += " WHERE from_value = ? AND to_value = ?"
            values.extend([from_value, to_value])
        query += " ORDER BY created_at DESC, scan_id DESC"
        with self._read_connection() as connection:
            return [dict(row) for row in connection.execute(query, values)]

    def latest_scan(self, from_value: str, to_value: str) -> Optional[Dict[str, Any]]:
        rows = self.list_scans(from_value, to_value)
        if not rows:
            return None
        return self.read_scan(rows[0]["scan_id"])

    def query_messages(self, from_value: str, to_value: str, conversation: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.database_path.exists():
            return []
        query = """
          SELECT r.* FROM message_revisions r
          JOIN messages_current c ON c.message_id=r.message_id AND c.revision=r.revision
          WHERE r.occurred_at >= ? AND r.occurred_at < ?
        """
        values: List[Any] = [from_value, to_value]
        if conversation:
            query += " AND r.conversation_id = ?"
            values.append(conversation)
        query += " ORDER BY r.occurred_at, r.conversation_id, r.message_id"
        with self._read_connection() as connection:
            return [dict(row) for row in connection.execute(query, values)]

    def read_revision(self, relative_path: str) -> Dict[str, Any]:
        path = self.root / relative_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DChatStoreError(f"DChat revision 不可读：{relative_path}（{exc}）") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != DCHAT_SCHEMA_VERSION
        ):
            raise DChatStoreError(
                f"DChat revision schema_version 必须为 {DCHAT_SCHEMA_VERSION}：{relative_path}"
            )
        return payload

    def usage(self) -> Dict[str, Any]:
        files = _managed_files(self.root)
        revisions = 0
        messages = 0
        if self.database_path.exists():
            with self._read_connection() as connection:
                revisions = connection.execute("SELECT COUNT(*) FROM message_revisions").fetchone()[0]
                messages = connection.execute("SELECT COUNT(*) FROM messages_current").fetchone()[0]
        return {"bytes": sum(path.stat().st_size for path in files), "files": len(files), "messages": messages, "revisions": revisions}

    def validate(
        self,
        known_projects: Optional[set[str]] = None,
    ) -> List[Dict[str, str]]:
        findings: List[Dict[str, str]] = []
        if not self.root.exists():
            return findings
        for path in [self.root, self.scans_dir, self.messages_dir, self.metadata_dir]:
            if path.exists() and _mode(path) != DIR_MODE:
                findings.append({"scope": str(path), "problem": "目录权限应为 0o700"})
        for path in _managed_files(self.root):
            if _mode(path) != FILE_MODE:
                findings.append({"scope": str(path), "problem": "文件权限应为 0o600"})
        if self.database_path.exists():
            try:
                connection_context = self._read_connection()
                connection = connection_context.__enter__()
            except DChatStoreError as exc:
                findings.append({"scope": "sqlite", "problem": str(exc)})
                return findings
            try:
                for row in connection.execute("SELECT message_id, revision, json_path FROM message_revisions"):
                    path = self.root / row["json_path"]
                    if not path.is_file():
                        findings.append({"scope": row["message_id"], "problem": "revision 文件缺失"})
                        continue
                    try:
                        envelope = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        findings.append({"scope": str(path), "problem": "revision JSON 不可读"})
                        continue
                    if (
                        not isinstance(envelope, dict)
                        or envelope.get("schema_version") != DCHAT_SCHEMA_VERSION
                    ):
                        findings.append({
                            "scope": str(path),
                            "problem": f"revision schema_version 必须为 {DCHAT_SCHEMA_VERSION}",
                        })
                        continue
                    if content_hash(envelope.get("payload")) != row["revision"]:
                        findings.append({"scope": row["message_id"], "problem": "revision hash 不匹配"})
                    if (
                        envelope.get("message_key") != row["message_id"]
                        or envelope.get("revision") != row["revision"]
                    ):
                        findings.append({"scope": row["message_id"], "problem": "revision envelope 身份不匹配"})
                current_rows = connection.execute(
                    "SELECT c.message_id, c.revision FROM messages_current c LEFT JOIN message_revisions r ON r.message_id=c.message_id AND r.revision=c.revision WHERE r.message_id IS NULL"
                ).fetchall()
                for row in current_rows:
                    findings.append({"scope": row["message_id"], "problem": "current pointer 缺少对应 revision"})
                indexed_paths = {
                    str((self.root / row["json_path"]).resolve())
                    for row in connection.execute("SELECT json_path FROM message_revisions")
                }
                for path in self.messages_dir.rglob("*.json"):
                    if str(path.resolve()) not in indexed_paths:
                        findings.append({"scope": str(path), "problem": "孤儿 revision 文件"})
                indexed_manifests: set[str] = set()
                indexed_metadata: set[str] = set()
                for row in connection.execute("SELECT * FROM scans"):
                    manifest_path = self.root / row["manifest_path"]
                    if self.root.resolve() not in manifest_path.resolve().parents:
                        findings.append({"scope": row["scan_id"], "problem": "scan manifest 路径越界"})
                        continue
                    indexed_manifests.add(str(manifest_path.resolve()))
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        findings.append({"scope": row["scan_id"], "problem": "scan manifest 不可读"})
                        continue
                    if not isinstance(manifest, dict):
                        findings.append({"scope": row["scan_id"], "problem": "scan manifest 必须是对象"})
                        continue
                    if manifest.get("schema_version") != DCHAT_SCHEMA_VERSION:
                        findings.append({
                            "scope": row["scan_id"],
                            "problem": f"scan manifest schema_version 必须为 {DCHAT_SCHEMA_VERSION}",
                        })
                        continue
                    if (
                        manifest.get("scan_id") != row["scan_id"]
                        or manifest.get("status") != row["status"]
                        or (manifest.get("window") or {}).get("from") != row["from_value"]
                        or (manifest.get("window") or {}).get("to") != row["to_value"]
                    ):
                        findings.append({"scope": row["scan_id"], "problem": "scan manifest 与 SQLite 不一致"})
                    metadata_ref = manifest.get("metadata_ref")
                    if not isinstance(metadata_ref, str):
                        findings.append({"scope": row["scan_id"], "problem": "scan 缺少 metadata_ref"})
                    else:
                        metadata_path = self.root / metadata_ref
                        if self.root.resolve() not in metadata_path.resolve().parents:
                            findings.append({"scope": row["scan_id"], "problem": "metadata snapshot 路径越界"})
                            continue
                        indexed_metadata.add(str(metadata_path.resolve()))
                        try:
                            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            findings.append({"scope": row["scan_id"], "problem": "metadata snapshot 不可读或身份不匹配"})
                        else:
                            if not isinstance(metadata, dict) or metadata.get("scan_id") != row["scan_id"]:
                                findings.append({"scope": row["scan_id"], "problem": "metadata snapshot 不可读或身份不匹配"})
                            elif metadata.get("schema_version") != DCHAT_SCHEMA_VERSION:
                                findings.append({
                                    "scope": row["scan_id"],
                                    "problem": f"metadata snapshot schema_version 必须为 {DCHAT_SCHEMA_VERSION}",
                                })
                    manifest_refs = set()
                    malformed_refs = False
                    for item in manifest.get("conversations") or []:
                        if not isinstance(item, dict):
                            malformed_refs = True
                            continue
                        for ref in item.get("message_refs") or []:
                            if not isinstance(ref, dict):
                                malformed_refs = True
                                continue
                            manifest_refs.add((ref.get("message_key"), ref.get("revision")))
                    if malformed_refs:
                        findings.append({"scope": row["scan_id"], "problem": "scan message refs 结构非法"})
                    database_refs = {
                        (item["message_id"], item["revision"])
                        for item in connection.execute(
                            "SELECT message_id, revision FROM scan_messages WHERE scan_id=?",
                            (row["scan_id"],),
                        )
                    }
                    if manifest_refs != database_refs:
                        findings.append({"scope": row["scan_id"], "problem": "scan message refs 与 SQLite 不一致"})
                for path in self.scans_dir.glob("*.json"):
                    if str(path.resolve()) not in indexed_manifests:
                        findings.append({"scope": str(path), "problem": "孤儿 scan manifest"})
                for path in self.metadata_dir.glob("*.json"):
                    if str(path.resolve()) not in indexed_metadata:
                        findings.append({"scope": str(path), "problem": "孤儿 metadata snapshot"})
                for row in connection.execute("SELECT conversation_id FROM checkpoints"):
                    observed = connection.execute(
                        "SELECT 1 FROM conversation_snapshots WHERE conversation_id=? LIMIT 1",
                        (row["conversation_id"],),
                    ).fetchone()
                    if observed is None:
                        findings.append({"scope": row["conversation_id"], "problem": "checkpoint 缺少会话快照"})
            except sqlite3.DatabaseError as exc:
                findings.append({"scope": "sqlite", "problem": f"DChat SQLite 结构不可读：{exc}"})
            finally:
                connection_context.__exit__(None, None, None)
        return findings
