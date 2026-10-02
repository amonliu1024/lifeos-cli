"""``lifeos push`` public command composition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lifeos_config.core import ConfigError, load_config

from .core import MirrorError, push


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def command_push(args: Any) -> None:
    try:
        target = load_config().mirror_target
    except ConfigError as exc:
        _fail(str(exc))
    if target is None:
        _fail("尚未配置镜像目标，先运行 lifeos config mirror --target host:path")
    try:
        payload = push(target, args.reports_root, dry_run=args.n)
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


def register_push_parser(domains: argparse._SubParsersAction, data_dir: Path) -> None:
    command = domains.add_parser(
        "push",
        help="把 Work 数据、日报与只读站点单向镜像到自己的服务器",
        description=(
            "通过 SSH 与 rsync 向私有配置中的镜像目标推送两部分：data/ 是 Work 事实、审计事件和日报，"
            "site/ 是在本机渲染好的只读工作台静态文件，由以 SSH 账户运行的静态服务作为站点根目录托管；"
            "推送后的文件只有该账户可读。"
            "目标下这两个目录与本机保持一致，本机已删除的日报也会在目标端删除。"
            "镜像目标用 lifeos config mirror --target host:path 设置。"
        ),
        epilog="派生视图、Sessions、Git、DChat 证据、备份与私有配置从不离开本机。",
    )
    command.add_argument("-n", action="store_true", help="只预览会发生的变化，不真正推送")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_push, reports_root=data_dir / "reports")


__all__ = ["register_push_parser"]
