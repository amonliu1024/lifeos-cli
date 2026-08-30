"""``lifeos dchat`` public command composition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

from lifeos_config.core import ConfigError, configure_dchat, load_config
from lifeos_projects.manifest import ProjectManifestError
from lifeos_projects.registry import dchat_project_rows, project_map_payload
from lifeos_sessions.projects import ProjectMap

from .client import DwsDChatAdapter
from .core import DChatError, DChatService, TimeWindow
from .evidence import build_index, build_pack
from .store import DChatStore, DChatStoreError


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def _emit(args: Any, payload: Dict[str, Any], lines: Sequence[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in lines:
        print(line)


def _store(args: Any) -> DChatStore:
    return DChatStore(args.dchat_root)


def _project_map(args: Any) -> ProjectMap:
    return ProjectMap.from_dict(project_map_payload(args.data_dir))


def _project_rows(args: Any) -> list[Dict[str, Any]]:
    return dchat_project_rows(args.data_dir)


def _known_projects(args: Any) -> set[str]:
    return {str(item["key"]) for item in _project_map(args).projects}


def _window(args: Any) -> TimeWindow:
    return TimeWindow.from_values(args.from_value, args.to_value)


def command_configure(args: Any) -> None:
    try:
        payload = configure_dchat(args.dws_wrapper)
    except ConfigError as exc:
        _fail(str(exc))
    _emit(args, payload, ["DChat 已配置。" if payload["changed"] else "DChat 配置未变化。"])


def command_scan(args: Any) -> None:
    try:
        store = _store(args)
        config = load_config(allow_missing=False)
        if not config.dchat.enabled:
            raise ConfigError("DChat 未启用；先运行 lifeos dchat configure")
        project_rows = _project_rows(args)
        service = DChatService(
            DwsDChatAdapter(str(config.dchat.dws_wrapper)),
            {row["conversation_id"] for row in project_rows},
            limit=args.limit,
        )
        payload = store.write_scan(service.scan(_window(args)))
    except (ConfigError, DChatError, DChatStoreError, ValueError) as exc:
        _fail(str(exc))
    summary = payload["summary"]
    _emit(args, payload, [
        f"{payload['scan_id']} · {payload['status']} · 会话 {summary['conversations_total']} · 消息 {summary['messages']}"
    ])
    if payload["status"] != "complete":
        raise SystemExit(1)


def command_scans(args: Any) -> None:
    try:
        if args.from_value or args.to_value:
            values = _window(args).to_dict()
            rows = _store(args).list_scans(values["from"], values["to"])
        else:
            rows = _store(args).list_scans()
    except (DChatError, DChatStoreError) as exc:
        _fail(str(exc))
    _emit(args, {"scans": rows, "total": len(rows)}, [
        f"{row['scan_id']} · {row['status']} · {row['from_value']} → {row['to_value']}" for row in rows
    ] or ["没有符合条件的 DChat scan。"])


def command_index(args: Any) -> None:
    try:
        window = _window(args)
        start, end = window.query_bounds()
        display_window = window.to_dict()
        project_map = _project_map(args)
        payload = build_index(
            _store(args), start, end, args.conversation,
            source_window=(display_window["from"], display_window["to"]),
            project_rows=_project_rows(args),
        )
        payload["window"] = display_window
        payload["project_catalog"] = project_map.to_dict()["catalog"]
    except (ConfigError, DChatError, DChatStoreError, ProjectManifestError, ValueError) as exc:
        _fail(str(exc))
    _emit(args, payload, [
        f"DChat supporting evidence · 会话 {payload['summary']['conversations']} · 消息 {payload['summary']['messages']}"
    ])


def command_pack(args: Any) -> None:
    try:
        window = _window(args)
        start, end = window.query_bounds()
        display_window = window.to_dict()
        payload = build_pack(
            _store(args), start, end, args.conversation, args.max_bytes,
            source_window=(display_window["from"], display_window["to"]),
        )
        payload["window"] = display_window
    except (DChatError, DChatStoreError, ValueError) as exc:
        _fail(str(exc))
    _emit(args, payload, [
        f"DChat supporting evidence · 原文 {len(payload['messages'])} · 省略 {payload['budget']['omitted_messages']}"
    ])


def command_projects_list(args: Any) -> None:
    try:
        rows = _project_rows(args)
        if args.conversation:
            rows = [row for row in rows if row["conversation_id"] == args.conversation]
    except (ConfigError, ProjectManifestError, ValueError) as exc:
        _fail(str(exc))
    _emit(args, {"projects": rows, "total": len(rows)}, [
        f"{row['conversation_id']} · {'、'.join(row['projects']) or '空集合'}" for row in rows
    ] or ["项目清单中没有匹配的 DChat 群聊关联。"])

def command_usage(args: Any) -> None:
    payload = _store(args).usage()
    _emit(args, payload, [
        f"DChat Runtime · {payload['bytes']} bytes · {payload['messages']} 条消息 · {payload['revisions']} 个 revision"
    ])


def command_validate(args: Any) -> None:
    findings = []
    try:
        project_map = _project_map(args)
        findings.extend({
            "scope": "project_catalog",
            "problem": item.get("message", "Project Catalog finding"),
        } for item in project_map.catalog_findings if item.get("severity") == "error")
        try:
            config = load_config(allow_missing=False)
            if not config.dchat.enabled:
                findings.append({"scope": "config", "problem": "DChat 未启用"})
        except ConfigError as exc:
            findings.append({"scope": "config", "problem": str(exc)})
        findings.extend(_store(args).validate(None))
    except (ConfigError, DChatStoreError, ProjectManifestError, ValueError) as exc:
        _fail(str(exc))
    payload = {"ok": not findings, "findings": findings}
    _emit(args, payload, ["DChat evidence 校验通过。"] if not findings else [
        f"! {item['scope']}：{item['problem']}" for item in findings
    ])
    if findings:
        raise SystemExit(1)


def _json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")


def _window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="from_value", required=True, help="窗口起点（日期或带时区 ISO）")
    parser.add_argument("--to", dest="to_value", required=True, help="窗口终点（日期或带时区 ISO，不含）")


def register_dchat_parser(domains: Any, data_dir: Path) -> None:
    dchat = domains.add_parser(
        "dchat",
        help="归档 DChat 私聊与项目群聊，提供日报辅助证据",
        description=(
            "结构化类型为 p2p / extp2p 的私聊全部纳入；"
            "群聊只在当前 Project Catalog 的 lifeos-project.json 中声明时读取正文。"
            "原始消息位于 Private Runtime，采集范围与项目关联由同一批项目清单派生。"
        ),
        epilog=(
            "DChat 始终是 supporting evidence，不能单独证明完成、提交、推送、部署或上线。"
            "命令不发送消息、不修改 DChat、不写 Work。"
        ),
    )
    dchat.set_defaults(dchat_root=data_dir / "dchat", data_dir=data_dir)
    commands = dchat.add_subparsers(dest="command", required=True)

    command = commands.add_parser("configure", help="配置批准的 dws wrapper（写入）")
    command.add_argument("--dws-wrapper", required=True)
    _json(command)
    command.set_defaults(handler=command_configure)

    command = commands.add_parser("scan", help="读取显式窗口并归档原始消息 revision（写入）")
    _window_arguments(command)
    command.add_argument("--limit", type=int, default=500, help=argparse.SUPPRESS)
    _json(command)
    command.set_defaults(handler=command_scan)

    command = commands.add_parser("scans", help="列出已有 scan（只读）")
    command.add_argument("--from", dest="from_value")
    command.add_argument("--to", dest="to_value")
    _json(command)
    command.set_defaults(handler=command_scans)

    command = commands.add_parser("index", help="读取有界会话活动索引（只读）")
    _window_arguments(command)
    command.add_argument("--conversation")
    _json(command)
    command.set_defaults(handler=command_index)

    command = commands.add_parser("pack", help="读取有字节预算的原始消息包（只读）")
    _window_arguments(command)
    command.add_argument("--conversation")
    command.add_argument("--max-bytes", type=int, default=100_000)
    _json(command)
    command.set_defaults(handler=command_pack)

    projects = commands.add_parser("projects", help="查看由项目清单派生的会话项目关联")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    command = project_commands.add_parser("list", help="列出项目清单中的群聊关联（只读）")
    command.add_argument("--conversation")
    _json(command)
    command.set_defaults(handler=command_projects_list)
    command = commands.add_parser("usage", help="查看 DChat 私有归档占用（只读）")
    _json(command)
    command.set_defaults(handler=command_usage)

    command = commands.add_parser("validate", help="校验权限、revision 与项目引用（只读）")
    _json(command)
    command.set_defaults(handler=command_validate)
