"""``lifeos git`` -- local Git commit evidence for Daily."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from lifeos_projects.manifest import ProjectManifestError
from lifeos_projects.registry import project_map_payload
from lifeos_sessions.projects import ProjectMap

from .core import GitEvidenceError, GitScanner, GitWindow, Repository, verify_repository_root
from .store import GitStore, GitStoreError


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def _emit(args: Any, payload: Dict[str, Any], lines: Sequence[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in lines:
        print(line)


def _store(args: Any) -> GitStore:
    return GitStore(args.git_root)


def _project_map(args: Any) -> ProjectMap:
    try:
        return ProjectMap.from_dict(project_map_payload(args.data_dir))
    except (ProjectManifestError, ValueError) as exc:
        _fail(str(exc))


def command_repos_list(args: Any) -> None:
    try:
        repositories = _store(args).load_registry()
    except GitStoreError as exc:
        _fail(str(exc))
    rows = [item.to_dict() for item in repositories]
    _emit(
        args,
        {"repositories": rows, "total": len(rows)},
        [
            f"{item['key']} · {'启用' if item['enabled'] else '停用'} · {item['root']}"
            for item in rows
        ] or ["没有已注册的 Git 仓库。"],
    )


def _verified_root(args: Any, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return verify_repository_root(value)
    except GitEvidenceError as exc:
        _fail(str(exc))
    return None


def command_repos_add(args: Any) -> None:
    root = _verified_root(args, args.root)
    repository = Repository(key=args.key, root=root or args.root, enabled=True)
    try:
        payload = _store(args).add_repository(repository)
    except GitStoreError as exc:
        _fail(str(exc))
    _emit(args, payload, [f"已注册 Git 仓库：{args.key} · {repository.root}"])


def command_repos_update(args: Any) -> None:
    root = _verified_root(args, args.root)
    enabled = None
    if args.enable:
        enabled = True
    elif args.disable:
        enabled = False
    if root is None and enabled is None:
        _fail("update 至少需要 --root、--enable 或 --disable")
    try:
        payload = _store(args).update_repository(args.key, root=root, enabled=enabled)
    except GitStoreError as exc:
        _fail(str(exc))
    repository = payload["repository"]
    _emit(
        args,
        payload,
        [
            f"{'已更新' if payload['changed'] else '未变化'} Git 仓库：{repository['key']}"
            f" · {'启用' if repository['enabled'] else '停用'} · {repository['root']}"
        ],
    )


def command_repos_delete(args: Any) -> None:
    try:
        payload = _store(args).delete_repository(args.key)
    except GitStoreError as exc:
        _fail(str(exc))
    _emit(args, payload, [f"已移除 Git 仓库注册：{args.key}（历史 scan 快照保留）"])


def command_scan(args: Any) -> None:
    try:
        window = GitWindow.from_values(args.from_value, args.to_value)
        store = _store(args)
        repositories = store.select_repositories(args.repo_keys or None)
        manifest = GitScanner().scan(repositories, window, _project_map(args))
        payload = store.write_scan(manifest)
    except (GitEvidenceError, GitStoreError, ValueError) as exc:
        _fail(str(exc))
    summary = payload["summary"]
    _emit(
        args,
        payload,
        [
            f"{payload['scan_id']} · {payload['status']} · 仓库 {summary['repos_total']}"
            f" · 提交 {summary['commits']}"
        ],
    )
    if payload["status"] != "complete":
        raise SystemExit(1)


def _filter_scans(rows: List[Dict[str, Any]], args: Any) -> List[Dict[str, Any]]:
    if not args.from_value and not args.to_value:
        return rows
    if not args.from_value or not args.to_value:
        _fail("--from 与 --to 必须同时提供")
    window = GitWindow.from_values(args.from_value, args.to_value).to_dict()
    return [row for row in rows if row.get("window") == window]


def command_scans(args: Any) -> None:
    try:
        rows = _filter_scans(_store(args).list_scans(), args)
    except (GitEvidenceError, GitStoreError) as exc:
        _fail(str(exc))
    _emit(
        args,
        {"scans": rows, "total": len(rows)},
        [
            f"{row.get('scan_id', row.get('path'))} · {row.get('status', 'invalid')}"
            f" · 提交 {((row.get('summary') or {}).get('commits', '?'))}"
            for row in rows
        ] or ["没有符合条件的 Git scan。"],
    )


def command_show(args: Any) -> None:
    try:
        payload = _store(args).read_scan(args.scan_id)
    except GitStoreError as exc:
        _fail(str(exc))
    _emit(
        args,
        payload,
        [
            f"{payload['scan_id']} · {payload['status']} · "
            f"窗口 {payload['window']['from']} → {payload['window']['to']}",
            *[
                f"  {repository['repo_key']} · {repository['status']} · 提交 {len(repository['commits'])}"
                for repository in payload["repositories"]
            ],
        ],
    )


def command_validate(args: Any) -> None:
    try:
        findings = _store(args).validate(args.scan_id)
    except GitStoreError as exc:
        _fail(str(exc))
    payload = {"ok": not findings, "findings": findings}
    _emit(
        args,
        payload,
        ["Git evidence 校验通过。"] if not findings else [
            f"! {item['scope']}：{item['problem']}" for item in findings
        ],
    )
    if findings:
        raise SystemExit(1)


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")


def register_git_parser(domains: Any, data_dir: Path) -> None:
    git = domains.add_parser(
        "git",
        help="读取本地 Git 提交作为日报辅助证据",
        description=(
            "管理显式注册的本地 Git 仓库和不可变提交证据快照。"
            "只读取本地历史，不修改仓库、不访问 remote、不写 Work。"
        ),
        epilog=(
            "scan 会在 Private Runtime 写入 Git evidence 快照；"
            "提交只证明 commit 阶段，不能推出测试、推送或部署状态。"
        ),
    )
    git.set_defaults(git_root=data_dir / "git", data_dir=data_dir)
    commands = git.add_subparsers(dest="command", required=True)

    repos = commands.add_parser(
        "repos", help="管理显式注册的本地 Git 仓库（不修改仓库）"
    )
    repos.set_defaults(command_group="repos")
    repo_commands = repos.add_subparsers(dest="repo_command", required=True)

    command = repo_commands.add_parser("list", help="列出已注册仓库")
    _add_json(command)
    command.set_defaults(handler=command_repos_list)

    command = repo_commands.add_parser("add", help="注册一个 Git 仓库根目录")
    command.add_argument("--key", required=True, help="稳定仓库 key（字母、数字、点、下划线和短横线）")
    command.add_argument("--root", required=True, help="Git checkout 根目录")
    _add_json(command)
    command.set_defaults(handler=command_repos_add)

    command = repo_commands.add_parser("update", help="更新仓库路径或启用状态")
    command.add_argument("key")
    command.add_argument("--root", help="新的 Git checkout 根目录")
    toggle = command.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true", help="启用扫描")
    toggle.add_argument("--disable", action="store_true", help="停用扫描")
    _add_json(command)
    command.set_defaults(handler=command_repos_update)

    command = repo_commands.add_parser("delete", help="移除仓库注册（历史 scan 保留）")
    command.add_argument("key")
    _add_json(command)
    command.set_defaults(handler=command_repos_delete)

    command = commands.add_parser(
        "scan", help="扫描窗口内提交并写入一个 Private Runtime 快照"
    )
    command.add_argument("--from", dest="from_value", required=True, help="窗口起点（YYYY-MM-DD 或带时区 ISO）")
    command.add_argument("--to", dest="to_value", required=True, help="窗口终点（YYYY-MM-DD 或带时区 ISO）")
    command.add_argument("--repo-key", dest="repo_keys", action="append", default=[], help="只扫描指定仓库；可重复")
    _add_json(command)
    command.set_defaults(handler=command_scan)

    command = commands.add_parser("scans", help="列出已写入的 Git scan 快照")
    command.add_argument("--from", dest="from_value", help="精确窗口起点")
    command.add_argument("--to", dest="to_value", help="精确窗口终点")
    _add_json(command)
    command.set_defaults(handler=command_scans)

    command = commands.add_parser("show", help="读取一个 Git scan 快照")
    command.add_argument("scan_id")
    _add_json(command)
    command.set_defaults(handler=command_show)

    command = commands.add_parser("validate", help="校验仓库注册表和 Git scan 快照")
    command.add_argument("--scan", dest="scan_id", help="只校验一个 scan 快照")
    _add_json(command)
    command.set_defaults(handler=command_validate)
