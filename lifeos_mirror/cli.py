"""``lifeos mirror`` public command composition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lifeos_config.core import ConfigError, configure_mirror, load_config

from .core import MirrorError, push


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def command_configure(args: Any) -> None:
    try:
        payload = configure_mirror(args.target)
    except ConfigError as exc:
        _fail(str(exc))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"镜像目标{'已更新' if payload['changed'] else '未变化'}：{payload['target']}")


def command_push(args: Any) -> None:
    try:
        target = load_config().mirror_target
    except ConfigError as exc:
        _fail(str(exc))
    if target is None:
        _fail("尚未配置镜像目标，先运行 lifeos mirror configure --target host:path")
    try:
        payload = push(target, args.reports_root, dry_run=args.dry_run)
    except MirrorError as exc:
        _fail(str(exc))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    action = "预演完成（未写入）" if payload["dry_run"] else "镜像完成"
    print(
        f"{action}：数据 {len(payload['data'])} 项、只读站点 {payload['reports_published']} 篇日报"
        f" → {payload['target']}"
    )


def register_mirror_parser(domains: argparse._SubParsersAction, data_dir: Path) -> None:
    mirror = domains.add_parser(
        "mirror",
        help="把 Work 数据、日报与只读站点单向镜像到自己的服务器",
        description=(
            "通过 SSH 与 rsync 向私有配置中的目标推送两部分：data/ 是 Work 事实、审计事件和日报，"
            "site/ 是在本机渲染好的只读工作台静态文件，可由任意静态文件服务托管。"
            "目标下这两个目录与本机保持一致，本机已删除的日报也会在目标端删除。"
        ),
        epilog="派生视图、Sessions、Git、DChat 证据、备份与私有配置从不离开本机。",
    )
    commands = mirror.add_subparsers(dest="command", required=True)
    command = commands.add_parser("configure", help="设置镜像目标（写入私有配置）")
    command.add_argument("--target", required=True, help="SSH 目标 host:path，例如 lab:lifeos-mirror")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_configure)
    command = commands.add_parser("push", help="生成只读站点并与 Work 数据、日报一起推送到镜像目标")
    command.add_argument("--dry-run", action="store_true", help="只列出将要变化的文件，不写入目标")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_push, reports_root=data_dir / "reports")


__all__ = ["register_mirror_parser"]
