"""Private calendar evidence: immutable event snapshots, scan manifests, series rules."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional
from zoneinfo import ZoneInfo

from . import CALENDAR_SCHEMA_VERSION


TIMEZONE = ZoneInfo("Asia/Shanghai")
DIR_MODE = 0o700
FILE_MODE = 0o600
IGNORED_RUNTIME_NAMES = {".DS_Store"}
SERIES_RULES = ("attend", "skip")


class CalendarStoreError(RuntimeError):
    """Raised when private calendar evidence is missing or malformed."""


def _now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="microseconds")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _managed_files(root: Path) -> List[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and path.name not in IGNORED_RUNTIME_NAMES]


class CalendarStore:
    """Own scan manifests, content-addressed event snapshots and the series rule log."""

    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root).expanduser()
        self.scans_dir = self.root / "scans"
        self.events_dir = self.root / "events"
        self.series_path = self.root / "series.jsonl"
        self.lock_path = self.root.parent / ".lifeos-calendar.lock"

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
        for path in (self.root, self.scans_dir, self.events_dir):
            path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            os.chmod(path, DIR_MODE)

    def _atomic_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.chmod(path.parent, DIR_MODE)
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

    # ---- scans -----------------------------------------------------------

    def write_scan(self, result: Mapping[str, Any]) -> Dict[str, Any]:
        if result.get("schema_version") != CALENDAR_SCHEMA_VERSION:
            raise CalendarStoreError(f"calendar scan schema_version 必须为 {CALENDAR_SCHEMA_VERSION}")
        if result.get("status") not in {"complete", "partial", "failed"}:
            raise CalendarStoreError("calendar scan status 非法")
        scan_id = "CALSCAN-" + datetime.now(TIMEZONE).strftime("%Y%m%dT%H%M%S%z") + "-" + uuid.uuid4().hex[:8]
        captured_at = _now_iso()
        manifest: Dict[str, Any] = {
            "schema_version": CALENDAR_SCHEMA_VERSION,
            "scan_id": scan_id,
            "captured_at": captured_at,
            "status": result["status"],
            "window": dict(result["window"]),
            "evidence_level": "supporting",
            "warnings": list(result.get("warnings") or []),
            "summary": dict(result.get("summary") or {}),
            "event_refs": [],
        }
        with self.locked():
            self._ensure_layout()
            for event in result.get("events") or []:
                payload = dict(event)
                revision = content_hash(payload)
                relative = Path("events") / payload["instance_id"] / (revision.removeprefix("sha256:") + ".json")
                path = self.root / relative
                if not path.exists():
                    self._atomic_json(path, {
                        "schema_version": CALENDAR_SCHEMA_VERSION,
                        "revision": revision,
                        "observed_at": captured_at,
                        "event": payload,
                    })
                manifest["event_refs"].append({
                    "instance_id": payload["instance_id"],
                    "revision": revision,
                    "path": str(relative),
                })
            self._atomic_json(self.scans_dir / f"{scan_id}.json", manifest)
        return manifest

    def _manifest_paths(self) -> List[Path]:
        if not self.scans_dir.is_dir():
            return []
        return sorted(path for path in self.scans_dir.glob("CALSCAN-*.json") if path.is_file())

    def read_scan(self, scan_id: str) -> Dict[str, Any]:
        path = self.scans_dir / f"{scan_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CalendarStoreError(f"calendar scan 不可读：{scan_id}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("scan_id") != scan_id:
            raise CalendarStoreError(f"calendar scan 内容非法：{scan_id}")
        if payload.get("schema_version") != CALENDAR_SCHEMA_VERSION:
            raise CalendarStoreError(f"calendar scan schema_version 必须为 {CALENDAR_SCHEMA_VERSION}：{scan_id}")
        return payload

    def list_scans(self, from_value: Optional[str] = None, to_value: Optional[str] = None) -> List[Dict[str, Any]]:
        if (from_value is None) != (to_value is None):
            raise CalendarStoreError("--from 与 --to 必须同时提供")
        rows: List[Dict[str, Any]] = []
        for path in self._manifest_paths():
            try:
                manifest = self.read_scan(path.stem)
            except CalendarStoreError:
                continue
            window = manifest.get("window") or {}
            if from_value is not None and (window.get("from") != from_value or window.get("to") != to_value):
                continue
            rows.append({
                "scan_id": manifest["scan_id"],
                "status": manifest.get("status"),
                "from_value": window.get("from"),
                "to_value": window.get("to"),
                "created_at": manifest.get("captured_at"),
                "events": len(manifest.get("event_refs") or []),
            })
        rows.sort(key=lambda row: (row["created_at"] or "", row["scan_id"]), reverse=True)
        return rows

    def latest_scan(self, from_value: str, to_value: str) -> Optional[Dict[str, Any]]:
        rows = self.list_scans(from_value, to_value)
        return self.read_scan(rows[0]["scan_id"]) if rows else None

    def read_event(self, relative_path: str) -> Dict[str, Any]:
        path = self.root / relative_path
        if self.root.resolve() not in path.resolve().parents:
            raise CalendarStoreError(f"calendar event 路径越界：{relative_path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CalendarStoreError(f"calendar event 不可读：{relative_path}（{exc}）") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != CALENDAR_SCHEMA_VERSION:
            raise CalendarStoreError(f"calendar event schema_version 必须为 {CALENDAR_SCHEMA_VERSION}：{relative_path}")
        return payload

    # ---- series rules ----------------------------------------------------

    def _series_events(self) -> List[Dict[str, Any]]:
        if not self.series_path.is_file():
            return []
        events: List[Dict[str, Any]] = []
        for number, line in enumerate(self.series_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CalendarStoreError(f"series.jsonl 第 {number} 行不是 JSON：{exc}") from exc
            if not isinstance(item, dict) or not item.get("series_key"):
                raise CalendarStoreError(f"series.jsonl 第 {number} 行缺少 series_key")
            events.append(item)
        return events

    def series_rules(self) -> Dict[str, Dict[str, Any]]:
        """Current rule per series: the last logged event wins; a cleared rule disappears."""

        current: Dict[str, Dict[str, Any]] = {}
        for item in self._series_events():
            key = str(item["series_key"])
            if item.get("rule") in SERIES_RULES:
                current[key] = {"rule": item["rule"], "title": item.get("title") or "", "at": item.get("at")}
            else:
                current.pop(key, None)
        return current

    def set_series(self, series_key: str, rule: Optional[str], title: str = "") -> Dict[str, Any]:
        key = str(series_key or "").strip()
        if not key:
            raise CalendarStoreError("series_key 不能为空")
        if rule is not None and rule not in SERIES_RULES:
            raise CalendarStoreError("rule 只能是 attend、skip 或清除")
        entry = {"at": _now_iso(), "series_key": key, "rule": rule, "title": str(title or "").strip()}
        with self.locked():
            self._ensure_layout()
            self.series_path.touch(exist_ok=True, mode=FILE_MODE)
            os.chmod(self.series_path, FILE_MODE)
            with self.series_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    # ---- housekeeping ----------------------------------------------------

    def usage(self) -> Dict[str, Any]:
        files = _managed_files(self.root)
        return {
            "bytes": sum(path.stat().st_size for path in files),
            "files": len(files),
            "scans": len(self._manifest_paths()),
            "events": sum(1 for path in files if path.parent.parent == self.events_dir),
        }

    def validate(self) -> List[Dict[str, str]]:
        findings: List[Dict[str, str]] = []
        if not self.root.exists():
            return findings
        for path in (self.root, self.scans_dir, self.events_dir):
            if path.exists() and _mode(path) != DIR_MODE:
                findings.append({"scope": str(path), "problem": "目录权限应为 0o700"})
        for path in _managed_files(self.root):
            if _mode(path) != FILE_MODE:
                findings.append({"scope": str(path), "problem": "文件权限应为 0o600"})
        referenced: set[str] = set()
        for manifest_path in self._manifest_paths():
            try:
                manifest = self.read_scan(manifest_path.stem)
            except CalendarStoreError as exc:
                findings.append({"scope": manifest_path.name, "problem": str(exc)})
                continue
            for ref in manifest.get("event_refs") or []:
                if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                    findings.append({"scope": manifest["scan_id"], "problem": "event ref 结构非法"})
                    continue
                referenced.add(str((self.root / ref["path"]).resolve()))
                try:
                    envelope = self.read_event(ref["path"])
                except CalendarStoreError as exc:
                    findings.append({"scope": ref.get("instance_id", "?"), "problem": str(exc)})
                    continue
                if content_hash(envelope.get("event")) != ref.get("revision") or envelope.get("revision") != ref.get("revision"):
                    findings.append({"scope": ref.get("instance_id", "?"), "problem": "event revision hash 不匹配"})
        if self.events_dir.is_dir():
            for path in self.events_dir.rglob("*.json"):
                if str(path.resolve()) not in referenced:
                    findings.append({"scope": str(path), "problem": "孤儿 event 快照"})
        try:
            self.series_rules()
        except CalendarStoreError as exc:
            findings.append({"scope": "series", "problem": str(exc)})
        return findings
