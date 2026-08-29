"""Public ``lifeos project`` manifest validation command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .manifest import ProjectManifestError, load_manifest, resolve_manifest_path


def command_validate(args: Any) -> None:
    path = resolve_manifest_path(args.path)
    try:
        manifest = load_manifest(path)
    except ProjectManifestError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    payload = {"manifest_path": str(path), **manifest}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"项目清单有效：{manifest['project_key']} · {manifest['scope']} · "
            f"DChat {len(manifest['sources'].get('dchat', {}).get('groups', []))} · "
            f"Cooper {len(manifest['sources'].get('cooper', {}).get('resources', []))}"
        )


def register_project_parser(domains: Any) -> None:
    project = domains.add_parser(
        "project",
        help="校验项目工作区的 LifeOS 清单",
        description=(
            "读取项目根 lifeos-project.json；该文件只保存项目静态身份与核心 "
            "DChat/Cooper 资源，个人跟踪状态仍位于 Private Runtime。"
        ),
    )
    commands = project.add_subparsers(dest="project_command", required=True)
    command = commands.add_parser(
        "validate",
        help="只读校验 lifeos-project.json",
        description=(
            "PATH 可以是项目目录或 lifeos-project.json；省略时使用当前目录。"
        ),
    )
    command.add_argument("path", nargs="?", default=".")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_validate)

__all__ = ["register_project_parser"]
