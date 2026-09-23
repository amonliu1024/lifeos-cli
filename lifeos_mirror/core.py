"""Push necessary Work data and a published read-only site to a mirror target.

Under the Work and Reports locks the push stages two trees: ``data/`` holds the
Work facts, audit events and reports needed to rebuild LifeOS, ``site/`` holds
the static workspace rendered on this machine. Derived views, Sessions, Git
and DChat evidence, backups and private configuration are never staged, so the
whitelist below is the whole outbound boundary. rsync then replaces exactly
``<target>/data`` and ``<target>/site``; other entries under the target stay
untouched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from lifeos_reports.store import ReportError, locked as reports_locked
from lifeos_web.publish import write_site
from lifeos_work.config import (
    ACHIEVEMENTS_PATH,
    EVENTS_PATH,
    GLOSSARY_PATH,
    IDEAS_PATH,
    PROJECTS_PATH,
    TASKS_PATH,
    WORK_ITEMS_PATH,
)
from lifeos_work.runtime import exclusive_lock

MIRROR_WORK_PATHS = (
    PROJECTS_PATH,
    WORK_ITEMS_PATH,
    TASKS_PATH,
    EVENTS_PATH,
    GLOSSARY_PATH,
    IDEAS_PATH,
    ACHIEVEMENTS_PATH,
)
SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR"


class MirrorError(RuntimeError):
    """Raised when the mirror cannot be pushed."""


def _stage_data(destination: Path, reports_root: Path) -> list[str]:
    destination.mkdir()
    staged = []
    for path in MIRROR_WORK_PATHS:
        if path.is_file():
            shutil.copy2(path, destination / path.name)
            staged.append(path.name)
    if reports_root.is_dir():
        shutil.copytree(
            reports_root,
            destination / reports_root.name,
            ignore=lambda _directory, names: [name for name in names if name.startswith(".")],
        )
        staged.append(reports_root.name)
    return staged


def _make_private(root: Path) -> None:
    for directory, _subdirectories, files in os.walk(root):
        os.chmod(directory, 0o700)
        for name in files:
            os.chmod(Path(directory) / name, 0o600)


def push(target: str, reports_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    rsync = shutil.which("rsync")
    if rsync is None:
        raise MirrorError("找不到 rsync")
    destination = target if target.endswith("/") else target + "/"
    with tempfile.TemporaryDirectory(prefix="lifeos-mirror-") as temporary:
        staging = Path(temporary)
        with exclusive_lock(), reports_locked(reports_root):
            data = _stage_data(staging / "data", reports_root)
            if not data:
                raise MirrorError("本机没有可镜像的 Work 或日报数据")
            try:
                reports = write_site(staging / "site", reports_root)
            except (ReportError, ValueError, OSError) as exc:
                raise MirrorError(f"无法生成只读站点：{exc}") from exc
            except SystemExit as exc:
                raise MirrorError(f"Work Runtime 无法读取（退出码 {exc.code}）") from exc
        _make_private(staging)
        command = [rsync, "-a", "--delete", "-e", SSH_COMMAND]
        if dry_run:
            command += ["--dry-run", "--itemize-changes"]
        command += ["--", str(staging / "data"), str(staging / "site"), destination]
        result = subprocess.run(command)
    if result.returncode != 0:
        raise MirrorError(f"rsync 退出码 {result.returncode}")
    return {
        "target": target,
        "dry_run": dry_run,
        "data": data,
        "reports_published": reports,
    }


__all__ = ["MIRROR_WORK_PATHS", "MirrorError", "push"]
