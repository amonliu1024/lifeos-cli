"""Private Git evidence registry and immutable scan snapshots."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from lifeos_sessions.projects import normalize_path

from .core import GIT_SHA_RE, REPO_KEY_RE, GitEvidenceError, Repository, _parse_git_datetime


TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
DIR_MODE = 0o700
FILE_MODE = 0o600
SCAN_ID_RE = re.compile(r"^GITSCAN-[0-9]{8}T[0-9]{6}[+-][0-9]{4}-[0-9a-f]{8}$")


class GitStoreError(RuntimeError):
    """Raised when Git evidence storage is missing or malformed."""


def _now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _default_registry() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "repositories": []}


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _summary_count(summary: Mapping[str, Any], key: str) -> int:
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitStoreError(f"Git scan summary.{key} 必须是非负整数")
    return value


class GitStore:
    """Own the Git registry and immutable private scan manifests."""

    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root).expanduser()
        self.scans_dir = self.root / "scans"
        self.registry_path = self.root / "repos.json"
        self.lock_path = self.root.parent / ".lifeos-git.lock"

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
        self.root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        self.scans_dir.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        os.chmod(self.root, DIR_MODE)
        os.chmod(self.scans_dir, DIR_MODE)

    def _atomic_write(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, FILE_MODE)
            os.replace(temporary_name, path)
            os.chmod(path, FILE_MODE)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def load_registry(self) -> List[Repository]:
        if not self.registry_path.exists():
            return []
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GitStoreError(f"仓库注册表不可读：{self.registry_path}（{exc}）") from exc
        return self._parse_registry(payload)

    def _parse_registry(self, payload: Any) -> List[Repository]:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
            raise GitStoreError(f"仓库注册表 schema_version 必须为 {SCHEMA_VERSION}：{self.registry_path}")
        raw_repositories = payload.get("repositories") or []
        if not isinstance(raw_repositories, list):
            raise GitStoreError("仓库注册表 repositories 必须是数组")
        repositories: List[Repository] = []
        keys: set[str] = set()
        roots: set[str] = set()
        for index, raw in enumerate(raw_repositories):
            if not isinstance(raw, Mapping):
                raise GitStoreError(f"仓库注册表 repositories[{index}] 必须是对象")
            key = str(raw.get("key") or "").strip()
            root = normalize_path(raw.get("root"))
            enabled = raw.get("enabled", True)
            if not REPO_KEY_RE.fullmatch(key):
                raise GitStoreError(f"仓库 key 非法：{key!r}")
            if not root:
                raise GitStoreError(f"仓库 root 不能为空：{key}")
            if not isinstance(enabled, bool):
                raise GitStoreError(f"仓库 enabled 必须是布尔值：{key}")
            folded_root = normalize_path(root).casefold()
            if key in keys:
                raise GitStoreError(f"仓库 key 重复：{key}")
            if folded_root in roots:
                raise GitStoreError(f"仓库 root 重复：{root}")
            keys.add(key)
            roots.add(folded_root)
            repositories.append(Repository(key=key, root=root, enabled=enabled))
        return sorted(repositories, key=lambda item: item.key)

    def _write_registry(self, repositories: Sequence[Repository]) -> None:
        self._ensure_layout()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "repositories": [item.to_dict() for item in sorted(repositories, key=lambda item: item.key)],
        }
        self._parse_registry(payload)
        self._atomic_write(self.registry_path, payload)

    def add_repository(self, repository: Repository) -> Dict[str, Any]:
        with self.locked():
            repositories = self.load_registry()
            if any(item.key == repository.key for item in repositories):
                raise GitStoreError(f"仓库 key 已存在：{repository.key}")
            repositories.append(repository)
            self._write_registry(repositories)
        return {"changed": True, "repository": repository.to_dict()}

    def update_repository(
        self,
        key: str,
        *,
        root: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self.locked():
            repositories = self.load_registry()
            existing = next((item for item in repositories if item.key == key), None)
            if existing is None:
                raise GitStoreError(f"仓库 key 不存在：{key}")
            updated = Repository(
                key=existing.key,
                root=normalize_path(root) if root is not None else existing.root,
                enabled=existing.enabled if enabled is None else enabled,
            )
            repositories = [updated if item.key == key else item for item in repositories]
            self._write_registry(repositories)
        return {"changed": updated != existing, "repository": updated.to_dict()}

    def delete_repository(self, key: str) -> Dict[str, Any]:
        with self.locked():
            repositories = self.load_registry()
            remaining = [item for item in repositories if item.key != key]
            if len(remaining) == len(repositories):
                raise GitStoreError(f"仓库 key 不存在：{key}")
            self._write_registry(remaining)
        return {"changed": True, "key": key}

    def select_repositories(self, keys: Optional[Sequence[str]] = None) -> List[Repository]:
        repositories = self.load_registry()
        if keys:
            selected: List[Repository] = []
            by_key = {item.key: item for item in repositories}
            for key in keys:
                if key not in by_key:
                    raise GitStoreError(f"仓库 key 不存在：{key}")
                if by_key[key].enabled:
                    selected.append(by_key[key])
            return selected
        return [item for item in repositories if item.enabled]

    def write_scan(self, manifest: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(manifest)
        payload.setdefault("captured_at", _now_iso())
        scan_id = payload.get("scan_id") or self.new_scan_id()
        if not isinstance(scan_id, str) or not SCAN_ID_RE.fullmatch(scan_id):
            raise GitStoreError(f"scan_id 非法：{scan_id}")
        payload["scan_id"] = scan_id
        self.validate_scan(payload)
        path = self.scans_dir / f"{scan_id}.json"
        with self.locked():
            self._ensure_layout()
            if path.exists():
                existing = self.read_scan(scan_id)
                if _json_bytes(existing) != _json_bytes(payload):
                    raise GitStoreError(f"scan_id 已存在但内容不同：{scan_id}")
                return existing
            self._atomic_write(path, payload)
        return payload

    def new_scan_id(self) -> str:
        stamp = datetime.now(TIMEZONE).strftime("%Y%m%dT%H%M%S%z")
        return f"GITSCAN-{stamp}-{uuid.uuid4().hex[:8]}"

    def read_scan(self, scan_id: str) -> Dict[str, Any]:
        if not SCAN_ID_RE.fullmatch(str(scan_id or "")):
            raise GitStoreError(f"scan_id 非法：{scan_id}")
        path = self.scans_dir / f"{scan_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GitStoreError(f"scan 不存在：{scan_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise GitStoreError(f"scan 不可读：{path}（{exc}）") from exc
        self.validate_scan(payload)
        return payload

    def list_scans(self) -> List[Dict[str, Any]]:
        if not self.scans_dir.is_dir():
            return []
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.scans_dir.glob("GITSCAN-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.validate_scan(payload)
            except (OSError, json.JSONDecodeError, GitStoreError) as exc:
                rows.append({"path": str(path), "error": str(exc)})
                continue
            rows.append({
                "scan_id": payload["scan_id"],
                "captured_at": payload.get("captured_at"),
                "status": payload.get("status"),
                "window": payload.get("window"),
                "summary": payload.get("summary"),
                "path": str(path),
            })
        return rows

    def validate_scan(self, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise GitStoreError("Git scan 必须是对象")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise GitStoreError(f"Git scan schema_version 必须为 {SCHEMA_VERSION}")
        scan_id = payload.get("scan_id")
        if not isinstance(scan_id, str) or not SCAN_ID_RE.fullmatch(scan_id):
            raise GitStoreError(f"Git scan scan_id 非法：{scan_id}")
        if payload.get("status") not in {"complete", "partial", "failed"}:
            raise GitStoreError(f"Git scan status 非法：{payload.get('status')}")
        window = payload.get("window")
        if not isinstance(window, Mapping) or not window.get("from") or not window.get("to"):
            raise GitStoreError("Git scan 缺少 window.from/to")
        repositories = payload.get("repositories")
        if not isinstance(repositories, list):
            raise GitStoreError("Git scan repositories 必须是数组")
        seen_repositories: set[str] = set()
        total_commits = 0
        failures = 0
        for index, repository in enumerate(repositories):
            if not isinstance(repository, Mapping):
                raise GitStoreError(f"Git scan repositories[{index}] 必须是对象")
            key = str(repository.get("repo_key") or "")
            if not REPO_KEY_RE.fullmatch(key):
                raise GitStoreError(f"Git scan repo_key 非法：{key}")
            if key in seen_repositories:
                raise GitStoreError(f"Git scan repo_key 重复：{key}")
            seen_repositories.add(key)
            if repository.get("status") not in {"complete", "failed"}:
                raise GitStoreError(f"Git scan repository status 非法：{key}")
            if repository.get("status") == "failed":
                failures += 1
            if not isinstance(repository.get("project_key"), str) or not repository.get("project_key"):
                raise GitStoreError(f"Git scan 缺少 project_key：{key}")
            commits = repository.get("commits")
            if not isinstance(commits, list):
                raise GitStoreError(f"Git scan commits 必须是数组：{key}")
            seen_shas: set[str] = set()
            for commit in commits:
                if not isinstance(commit, Mapping):
                    raise GitStoreError(f"Git scan commit 必须是对象：{key}")
                sha = str(commit.get("sha") or "")
                if not GIT_SHA_RE.fullmatch(sha):
                    raise GitStoreError(f"Git scan commit SHA 非法：{sha}")
                if sha in seen_shas:
                    raise GitStoreError(f"Git scan commit SHA 重复：{key}@{sha}")
                seen_shas.add(sha)
                if not commit.get("committed_at"):
                    raise GitStoreError(f"Git scan commit 缺少 committed_at：{key}@{sha}")
                try:
                    _parse_git_datetime(str(commit["committed_at"]))
                except (GitEvidenceError, TypeError, ValueError) as exc:
                    raise GitStoreError(
                        f"Git scan committed_at 非法：{key}@{sha}"
                    ) from exc
                if not isinstance(commit.get("commit_message"), str):
                    raise GitStoreError(f"Git scan commit 缺少 commit_message：{key}@{sha}")
            total_commits += len(commits)
        summary = payload.get("summary")
        if not isinstance(summary, Mapping):
            raise GitStoreError("Git scan 缺少 summary")
        repos_total = _summary_count(summary, "repos_total")
        repos_complete = _summary_count(summary, "repos_complete")
        repos_failed = _summary_count(summary, "repos_failed")
        commits_count = _summary_count(summary, "commits")
        if repos_total != len(repositories):
            raise GitStoreError("Git scan summary.repos_total 与 repositories 不一致")
        if repos_complete != len(repositories) - failures:
            raise GitStoreError("Git scan summary.repos_complete 与 repositories 不一致")
        if repos_failed != failures:
            raise GitStoreError("Git scan summary.repos_failed 与 repositories 不一致")
        if commits_count != total_commits:
            raise GitStoreError("Git scan summary.commits 与 repositories 不一致")
        expected_status = "complete" if failures == 0 else "failed" if not repos_complete else "partial"
        if payload["status"] != expected_status:
            raise GitStoreError(f"Git scan status 与仓库结果不一致：{payload['status']} != {expected_status}")

    def validate(self, scan_id: Optional[str] = None) -> List[Dict[str, str]]:
        findings: List[Dict[str, str]] = []
        try:
            self.load_registry()
        except GitStoreError as exc:
            findings.append({"scope": "repos", "problem": str(exc)})
        for directory in (self.root, self.scans_dir):
            if directory.is_dir() and _mode(directory) != DIR_MODE:
                findings.append({
                    "scope": str(directory),
                    "problem": f"目录权限应为 {oct(DIR_MODE)}",
                })
        if self.registry_path.is_file() and _mode(self.registry_path) != FILE_MODE:
            findings.append({
                "scope": str(self.registry_path),
                "problem": f"文件权限应为 {oct(FILE_MODE)}",
            })
        if scan_id and not SCAN_ID_RE.fullmatch(scan_id):
            raise GitStoreError(f"scan_id 非法：{scan_id}")
        if scan_id:
            candidates = [self.scans_dir / f"{scan_id}.json"]
        else:
            candidates = sorted(self.scans_dir.glob("GITSCAN-*.json")) if self.scans_dir.is_dir() else []
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.validate_scan(payload)
                if _mode(path) != FILE_MODE:
                    findings.append({"scope": str(path), "problem": f"文件权限应为 {oct(FILE_MODE)}"})
            except (OSError, json.JSONDecodeError, GitStoreError) as exc:
                findings.append({"scope": str(path), "problem": str(exc)})
        return findings
