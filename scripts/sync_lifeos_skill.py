#!/usr/bin/env python3
"""Synchronize a repository-owned Skill by content manifest."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
DEFAULT_SKILLS_DIR = CODEX_HOME / "skills"
IGNORED_NAMES = {".DS_Store", "__pycache__"}


class SyncError(RuntimeError):
    """Raised when a source or target cannot be synchronized safely."""


def available_skills() -> list[str]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(path.name for path in SKILLS_DIR.iterdir() if path.is_dir())


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if any(part in IGNORED_NAMES for part in relative_path.parts):
            continue
        if path.is_symlink():
            raise SyncError(f"symlinks are not supported: {path}")
        if path.is_file():
            yield path, relative_path
        elif not path.is_dir():
            raise SyncError(f"unsupported filesystem entry: {path}")


def manifest(root: Path) -> dict[str, str]:
    return {
        relative_path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path, relative_path in iter_files(root)
    }


def validate_source(skill: str, source_dir: Path) -> dict[str, str]:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise SyncError(f"skill source must be a real directory: {source_dir}")

    skill_file = source_dir / "SKILL.md"
    agent_metadata = source_dir / "agents" / "openai.yaml"
    for required_file in (skill_file, agent_metadata):
        if required_file.is_symlink() or not required_file.is_file():
            raise SyncError(f"required skill file is missing: {required_file}")

    skill_text = skill_file.read_text(encoding="utf-8")
    if not re.search(rf"(?m)^name:\s*{re.escape(skill)}\s*$", skill_text):
        raise SyncError(f"SKILL.md must declare name: {skill}")

    metadata_text = agent_metadata.read_text(encoding="utf-8")
    if f"${skill}" not in metadata_text:
        raise SyncError(f"agents/openai.yaml must invoke ${skill}")

    source_manifest = manifest(source_dir)
    if not source_manifest:
        raise SyncError(f"skill source is empty: {source_dir}")
    return source_manifest


def validate_target_path(target: Path, source_dir: Path) -> None:
    if target.is_symlink():
        raise SyncError(f"skill target must be a real directory, not a symlink: {target}")
    source_path = source_dir.resolve()
    target_path = target.resolve(strict=False)
    if target_path == source_path or source_path in target_path.parents or target_path in source_path.parents:
        raise SyncError("target must be outside the skill source directory")
    if target.exists() and not target.is_dir():
        raise SyncError(f"target exists but is not a directory: {target}")


def copy_source(source_dir: Path, staging: Path) -> None:
    for source_file, relative_path in iter_files(source_dir):
        destination = staging / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)


def check_target(target: Path, source_dir: Path, source_manifest: dict[str, str]) -> int:
    validate_target_path(target, source_dir)
    if not target.is_dir():
        print(f"out of sync: target is missing: {target}", file=sys.stderr)
        return 1
    if manifest(target) != source_manifest:
        print(f"out of sync: {target}", file=sys.stderr)
        return 1
    print(f"in sync: {target}")
    return 0


def sync_target(
    skill: str, target: Path, source_dir: Path, source_manifest: dict[str, str]
) -> int:
    validate_target_path(target, source_dir)
    if target.is_dir() and manifest(target) == source_manifest:
        print(f"already in sync: {target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{skill}-sync-", dir=target.parent))
    backup = target.parent / f".{skill}-backup-{uuid.uuid4().hex}"
    previous_target_moved = False
    new_target_installed = False

    try:
        copy_source(source_dir, staging)
        if manifest(staging) != source_manifest:
            raise SyncError("staged copy does not match the skill source")

        if target.exists():
            os.replace(target, backup)
            previous_target_moved = True
        os.replace(staging, target)
        new_target_installed = True

        if manifest(target) != source_manifest:
            raise SyncError("installed skill copy does not match the skill source")
    except Exception:
        if new_target_installed and target.exists():
            shutil.rmtree(target)
        if previous_target_moved and backup.exists():
            os.replace(backup, target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    print(f"synchronized: {source_dir} -> {target}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize a repository-owned Skill into a local Skills directory."
    )
    parser.add_argument(
        "skill",
        choices=available_skills(),
        help="skill directory under skills/",
    )
    parser.add_argument(
        "--target",
        type=Path,
        help=f"destination directory (default: {DEFAULT_SKILLS_DIR}/<skill>)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check whether the target matches the Repo source without writing",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_dir = SKILLS_DIR / args.skill
    target = (args.target or DEFAULT_SKILLS_DIR / args.skill).expanduser()
    try:
        source_manifest = validate_source(args.skill, source_dir)
        if args.check:
            return check_target(target, source_dir, source_manifest)
        return sync_target(args.skill, target, source_dir, source_manifest)
    except (OSError, SyncError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
