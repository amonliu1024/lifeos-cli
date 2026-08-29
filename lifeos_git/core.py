"""Read-only Git commit evidence extraction.

This module deliberately owns only the local Git adapter and deterministic
metadata normalization.  Daily prose, project importance, and delivery
claims remain consumers' responsibilities.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from lifeos_sessions.projects import ProjectMap, normalize_path


UTC = timezone.utc
TIMEZONE = ZoneInfo("Asia/Shanghai")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MAX_MESSAGE_CHARS = 8_000
MAX_ERROR_CHARS = 2_000
GIT_TIMEOUT_SECONDS = 30


class GitEvidenceError(RuntimeError):
    """Raised when Git evidence cannot be read or validated."""


class GitCommandError(GitEvidenceError):
    """A local Git command failed without changing repository state."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        self.args = tuple(args)
        self.returncode = int(returncode)
        self.stderr = str(stderr or "").strip()
        detail = self.stderr[:MAX_ERROR_CHARS] or f"exit code {returncode}"
        super().__init__(f"git {' '.join(args)} failed: {detail}")


@dataclass(frozen=True)
class GitWindow:
    """A timezone-aware half-open window used by Git scans."""

    from_utc: datetime
    to_utc: datetime
    from_iso: str
    to_iso: str

    @classmethod
    def from_values(cls, from_value: str, to_value: str) -> "GitWindow":
        from_dt = _parse_window_value(from_value, "from")
        to_dt = _parse_window_value(to_value, "to")
        from_utc = from_dt.astimezone(UTC)
        to_utc = to_dt.astimezone(UTC)
        if from_utc >= to_utc:
            raise GitEvidenceError("Git 扫描窗口必须满足 from < to")
        return cls(
            from_utc=from_utc,
            to_utc=to_utc,
            from_iso=_local_iso(from_utc),
            to_iso=_local_iso(to_utc),
        )

    def to_dict(self) -> Dict[str, str]:
        return {"from": self.from_iso, "to": self.to_iso}


@dataclass(frozen=True)
class Repository:
    """One explicitly registered local checkout."""

    key: str
    root: str
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "root": self.root, "enabled": self.enabled}


GitRunner = Callable[[Path, Sequence[str], int], str]


def _parse_window_value(value: str, field_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise GitEvidenceError(f"{field_name} 不能为空")
    if len(text) == 10:
        try:
            return datetime.combine(date.fromisoformat(text), time.min, tzinfo=TIMEZONE)
        except ValueError as exc:
            raise GitEvidenceError(f"{field_name} 不是有效的 YYYY-MM-DD：{value}") from exc
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitEvidenceError(f"{field_name} 不是有效的 ISO 时间：{value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GitEvidenceError(f"{field_name} 必须带时区偏移：{value}")
    return parsed


def _local_iso(value: datetime) -> str:
    return value.astimezone(TIMEZONE).isoformat(timespec="seconds")


def _parse_git_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GitEvidenceError(f"Git 时间缺少时区：{value}")
    return parsed.astimezone(UTC)


def _truncate_message(value: str) -> tuple[str, bool]:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) <= MAX_MESSAGE_CHARS:
        return text, False
    return text[:MAX_MESSAGE_CHARS].rstrip() + "…", True


def run_git(root: Path, args: Sequence[str], timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    """Run one local Git command without a shell or network operation."""

    command = ["git", "-C", str(root), *[str(arg) for arg in args]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitEvidenceError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitEvidenceError(f"git 命令超时（{timeout}s）：{' '.join(args)}") from exc
    if completed.returncode != 0:
        raise GitCommandError(args, completed.returncode, completed.stderr)
    return completed.stdout


def verify_repository_root(root: os.PathLike[str] | str, runner: GitRunner = run_git) -> str:
    """Verify that ``root`` is a Git checkout and return its canonical path."""

    path = Path(root).expanduser()
    if not path.is_dir():
        raise GitEvidenceError(f"仓库目录不存在：{path}")
    reported = runner(path, ["rev-parse", "--show-toplevel"], GIT_TIMEOUT_SECONDS).strip()
    if not reported:
        raise GitEvidenceError(f"Git 未返回仓库根目录：{path}")
    expected = os.path.realpath(str(path))
    actual = os.path.realpath(reported)
    if os.path.normcase(expected) != os.path.normcase(actual):
        raise GitEvidenceError(f"注册路径不是 Git 仓库根目录：{path}（实际为 {reported}）")
    return normalize_path(reported)


class GitScanner:
    """Scan explicit repositories into bounded commit metadata."""

    def __init__(self, runner: GitRunner = run_git):
        self.runner = runner

    def scan(
        self,
        repositories: Iterable[Repository],
        window: GitWindow,
        project_map: Optional[ProjectMap] = None,
    ) -> Dict[str, Any]:
        project_map = project_map or ProjectMap.default()
        rows: List[Dict[str, Any]] = []
        for repository in repositories:
            rows.append(self._scan_repository(repository, window, project_map))

        failed = sum(1 for row in rows if row["status"] == "failed")
        complete = len(rows) - failed
        if failed == 0:
            status = "complete"
        elif complete == 0:
            status = "failed"
        else:
            status = "partial"
        return {
            "schema_version": 1,
            "status": status,
            "window": window.to_dict(),
            "repositories": rows,
            "summary": {
                "repos_total": len(rows),
                "repos_complete": complete,
                "repos_failed": failed,
                "commits": sum(len(row.get("commits") or []) for row in rows),
            },
        }

    def _scan_repository(
        self,
        repository: Repository,
        window: GitWindow,
        project_map: ProjectMap,
    ) -> Dict[str, Any]:
        root = Path(repository.root).expanduser()
        base = {
            "repo_key": repository.key,
            "root": normalize_path(repository.root),
            "status": "failed",
            "project_key": project_map.resolve(repository.root).project_key,
            "head_sha": None,
            "commits": [],
            "warnings": [],
        }
        try:
            verify_repository_root(root, self.runner)
            try:
                head_sha = self.runner(
                    root, ["rev-parse", "--verify", "HEAD"], GIT_TIMEOUT_SECONDS
                ).strip()
            except GitCommandError as exc:
                if exc.returncode == 128:
                    base["status"] = "complete"
                    base["warnings"] = ["head_unborn"]
                    return base
                raise
            if head_sha and not GIT_SHA_RE.fullmatch(head_sha):
                raise GitEvidenceError(f"HEAD 不是有效 commit SHA：{head_sha}")
            base["head_sha"] = head_sha or None
            base["commits"] = self._read_commits(
                root, repository.key, window, base["project_key"]
            )
            base["status"] = "complete"
            return base
        except (GitEvidenceError, OSError) as exc:
            base["warnings"] = [str(exc)[:MAX_ERROR_CHARS]]
            return base

    def _read_commits(
        self,
        root: Path,
        repo_key: str,
        window: GitWindow,
        project_key: str,
    ) -> List[Dict[str, Any]]:
        # HEAD covers detached worktrees; --branches covers local branches but
        # intentionally excludes remote refs and never contacts a remote.
        since = (window.from_utc - timedelta(seconds=1)).isoformat()
        until = window.to_utc.isoformat()
        output = self.runner(
            root,
            [
                "log",
                "--no-ext-diff",
                "--no-color",
                "--format=%H%x00%cI%x00%B%x00",
                f"--since={since}",
                f"--until={until}",
                "HEAD",
                "--branches",
            ],
            GIT_TIMEOUT_SECONDS,
        )
        parts = output.split("\x00")
        commits: List[Dict[str, Any]] = []
        seen: set[str] = set()
        index = 0
        while index + 2 < len(parts):
            sha = parts[index].strip()
            committed_raw = parts[index + 1].strip()
            message = parts[index + 2]
            index += 3
            if not sha and not committed_raw and not message:
                continue
            if not GIT_SHA_RE.fullmatch(sha):
                raise GitEvidenceError(f"Git log 返回非法 SHA：{sha!r}")
            committed_at = _parse_git_datetime(committed_raw)
            if not (window.from_utc <= committed_at < window.to_utc):
                continue
            if sha in seen:
                continue
            seen.add(sha)
            commit_message, truncated = _truncate_message(message)
            row: Dict[str, Any] = {
                "repo_key": repo_key,
                "project_key": project_key,
                "sha": sha,
                "committed_at": _local_iso(committed_at),
                "commit_message": commit_message,
            }
            if truncated:
                row["commit_message_truncated"] = True
            commits.append(row)
        commits.sort(key=lambda item: (item["committed_at"], item["sha"]))
        return commits
