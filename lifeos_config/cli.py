"""CLI commands for private configuration and capability inspection."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .capabilities import capability_report
from .core import (
    ConfigError,
    configure_project_root,
    initialize_config,
    load_config,
)


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_config_init(args: Any) -> None:
    try:
        path = initialize_config()
    except (ConfigError, OSError) as exc:
        print(f"lifeos: {exc}", file=sys.stderr)
        raise SystemExit(1)
    payload = {"status": "created", "path": str(path)}
    if args.json:
        _emit(payload, True)
    else:
        print(f"LifeOS 配置已创建：{path}")


def command_config_validate(args: Any) -> None:
    try:
        config = load_config(allow_missing=False)
    except ConfigError as exc:
        print(f"lifeos: {exc}", file=sys.stderr)
        raise SystemExit(1)
    payload = {
        "status": "valid",
        "path": str(config.path),
        "schema_version": 1,
    }
    if args.json:
        _emit(payload, True)
    else:
        print(f"LifeOS 配置校验通过：{config.path}")


def command_capabilities(args: Any) -> None:
    try:
        report = capability_report(load_config())
    except ConfigError as exc:
        print(f"lifeos: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        _emit(report, True)
        return
    for name, value in report["modules"].items():
        if name == "sessions":
            print(f"{name}: {value['status']}")
            for source, status in value["sources"].items():
                print(f"  {source}: {status['status']} ({status['reason']})")
        elif name == "project_sources":
            print(f"{name}:")
            for source, status in value.items():
                print(f"  {source}: {status['status']} ({status['reason']})")
        else:
            print(f"{name}: {value['status']} ({value['reason']})")


def command_project_root(args: Any) -> None:
    try:
        if args.project_root_command == "list":
            config = load_config()
            payload = {
                "roots": list(config.project_roots),
                "exclude": list(config.project_excludes),
                "path": str(config.path),
            }
        else:
            payload = configure_project_root(args.project_root_command, args.path)
    except ConfigError as exc:
        print(f"lifeos: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        _emit(payload, True)
        return
    if args.project_root_command == "list":
        if not payload["roots"]:
            print("没有已配置的项目发现根。")
        for root in payload["roots"]:
            print(root)
        return
    status = "已更新" if payload["changed"] else "未变化"
    print(f"项目发现根{status}：{payload['root']}")


def register_config_parser(domains: argparse._SubParsersAction) -> None:
    config = domains.add_parser("config", help="初始化并校验 Git 外私人配置")
    commands = config.add_subparsers(dest="command", required=True)
    command = commands.add_parser("init", help="创建默认私人配置，不覆盖现有文件")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_config_init)
    command = commands.add_parser("validate", help="校验私人配置")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_config_validate)
    project_root = commands.add_parser("project-root", help="维护项目动态发现根")
    project_commands = project_root.add_subparsers(
        dest="project_root_command", required=True
    )
    for name, help_text in (
        ("add", "增加项目发现根"),
        ("remove", "移除项目发现根"),
    ):
        command = project_commands.add_parser(name, help=help_text)
        command.add_argument("path")
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=command_project_root)
    command = project_commands.add_parser("list", help="列出项目发现根")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_project_root)


def register_capabilities_parser(domains: argparse._SubParsersAction) -> None:
    command = domains.add_parser("capabilities", help="查看内置能力的当前状态")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_capabilities)


__all__ = ["register_capabilities_parser", "register_config_parser"]
