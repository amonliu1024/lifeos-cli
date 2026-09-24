"""``lifeos calendar`` public command composition."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

from lifeos_config.core import ConfigError, configure_calendar, load_config

from .client import DwsCalendarAdapter
from .core import CalendarError, CalendarService, TimeWindow
from .evidence import build_index
from .store import SERIES_RULES, CalendarStore, CalendarStoreError


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def _emit(args: Any, payload: Dict[str, Any], lines: Sequence[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in lines:
        print(line)


def _store(args: Any) -> CalendarStore:
    return CalendarStore(args.calendar_root)


def _window(args: Any) -> TimeWindow:
    return TimeWindow.from_values(args.from_value, args.to_value)


def _enabled_config():
    config = load_config(allow_missing=False)
    if not config.calendar.enabled:
        raise ConfigError("日历未启用；先运行 lifeos calendar configure")
    if not config.dchat.dws_wrapper:
        raise ConfigError("日历复用 DChat 的 dws wrapper；先运行 lifeos dchat configure")
    return config


def command_configure(args: Any) -> None:
    try:
        payload = configure_calendar(enabled=not args.disable)
    except ConfigError as exc:
        _fail(str(exc))
    state = "已禁用" if args.disable else "已启用"
    _emit(args, payload, [f"日历{state}。" if payload["changed"] else "日历配置未变化。"])


def command_scan(args: Any) -> None:
    try:
        config = _enabled_config()
        service = CalendarService(DwsCalendarAdapter(str(config.dchat.dws_wrapper)))
        payload = _store(args).write_scan(service.scan(_window(args)))
    except (ConfigError, CalendarError, CalendarStoreError, ValueError) as exc:
        _fail(str(exc))
    _emit(args, payload, [
        f"{payload['scan_id']} · {payload['status']} · 日程 {payload['summary']['events']}"
        + (f" · 告警 {'、'.join(payload['warnings'])}" if payload["warnings"] else "")
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
    except (CalendarError, CalendarStoreError) as exc:
        _fail(str(exc))
    _emit(args, {"scans": rows, "total": len(rows)}, [
        f"{row['scan_id']} · {row['status']} · {row['from_value']} → {row['to_value']} · 日程 {row['events']}"
        for row in rows
    ] or ["没有符合条件的日历 scan。"])


def command_index(args: Any) -> None:
    try:
        values = _window(args).to_dict()
        payload = build_index(_store(args), values["from"], values["to"])
    except (CalendarError, CalendarStoreError, ValueError) as exc:
        _fail(str(exc))
    lines = [
        f"日历 supporting evidence · {payload['source_status']} · 日程 {payload['summary']['events']}"
    ]
    for event in payload["events"]:
        rule = f" · {event['series_rule']}" if event.get("series_rule") else ""
        overlap = " · 时间重叠" if event.get("overlaps") else ""
        lines.append(
            f"  {event['instance_id']} {event['dtstart'][11:16]}-{event['dtend'][11:16]} "
            f"{event['summary']} · {event['type']}{rule}{overlap}"
        )
    _emit(args, payload, lines)


def command_series_list(args: Any) -> None:
    try:
        rules = _store(args).series_rules()
    except CalendarStoreError as exc:
        _fail(str(exc))
    rows = [{"series_key": key, **value} for key, value in sorted(rules.items())]
    _emit(args, {"series": rows, "total": len(rows)}, [
        f"{row['rule']} · {row['title'] or row['series_key']}" for row in rows
    ] or ["还没有循环会议的出席名单。"])


def command_series_set(args: Any) -> None:
    try:
        entry = _store(args).set_series(args.series_key, None if args.clear else args.rule, args.title or "")
    except CalendarStoreError as exc:
        _fail(str(exc))
    action = "已清除" if args.clear else f"已记为 {args.rule}"
    _emit(args, entry, [f"{entry['title'] or entry['series_key']} {action}。"])


def command_usage(args: Any) -> None:
    payload = _store(args).usage()
    _emit(args, payload, [
        f"日历 Runtime · {payload['bytes']} bytes · {payload['scans']} 个 scan · {payload['events']} 份日程快照"
    ])


def command_validate(args: Any) -> None:
    findings = []
    try:
        try:
            _enabled_config()
        except ConfigError as exc:
            findings.append({"scope": "config", "problem": str(exc)})
        findings.extend(_store(args).validate())
    except CalendarStoreError as exc:
        _fail(str(exc))
    payload = {"ok": not findings, "findings": findings}
    _emit(args, payload, ["日历 evidence 校验通过。"] if not findings else [
        f"! {item['scope']}：{item['problem']}" for item in findings
    ])
    if findings:
        raise SystemExit(1)


def _json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")


def _window_arguments(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument("--from", dest="from_value", required=required, help="窗口起点（日期或带时区 ISO）")
    parser.add_argument("--to", dest="to_value", required=required, help="窗口终点（日期或带时区 ISO，不含）")


def register_calendar_parser(domains: Any, data_dir: Path) -> None:
    calendar = domains.add_parser(
        "calendar",
        help="归档 D-Chat 日历日程，提供日报的会议辅助证据",
        description=(
            "按显式窗口只读 D-Chat 日历，把与窗口有交集的日程存成不可变快照；"
            "每条只保留标题、起止时间、类型和参会人姓名。"
            "去没去、算不算工作由 Daily 提议并经本人确认，CLI 不判断。"
        ),
        epilog=(
            "日历始终是 supporting evidence。命令不创建、修改或回复任何日程，"
            "不读取回复状态，不写 Work，也不写日报正文。"
        ),
    )
    calendar.set_defaults(calendar_root=data_dir / "calendar", data_dir=data_dir)
    commands = calendar.add_subparsers(dest="command", required=True)

    command = commands.add_parser("configure", help="启用或禁用日历来源（写入私有配置）")
    command.add_argument("--disable", action="store_true", help="禁用；省略时启用")
    _json(command)
    command.set_defaults(handler=command_configure)

    command = commands.add_parser("scan", help="读取显式窗口并归档日程快照（写入）")
    _window_arguments(command)
    _json(command)
    command.set_defaults(handler=command_scan)

    command = commands.add_parser("scans", help="列出已有 scan（只读）")
    _window_arguments(command, required=False)
    _json(command)
    command.set_defaults(handler=command_scans)

    command = commands.add_parser("index", help="读取某窗口最新 scan 的日程索引（只读）")
    _window_arguments(command)
    _json(command)
    command.set_defaults(handler=command_index)

    series = commands.add_parser("series", help="循环会议的出席名单：本人说过默认去或不去的系列")
    series_commands = series.add_subparsers(dest="series_command", required=True)
    command = series_commands.add_parser("list", help="列出名单（只读）")
    _json(command)
    command.set_defaults(handler=command_series_list)
    command = series_commands.add_parser("set", help="写入或清除一个系列的默认出席（写入，留审计记录）")
    command.add_argument("--series", dest="series_key", required=True, help="系列身份（index 输出的 series_key）")
    group = command.add_mutually_exclusive_group(required=True)
    group.add_argument("--rule", choices=SERIES_RULES, help="attend 默认去，skip 默认不去")
    group.add_argument("--clear", action="store_true", help="清除名单，恢复为每次列出")
    command.add_argument("--title", help="便于阅读的系列标题（可选）")
    _json(command)
    command.set_defaults(handler=command_series_set)

    command = commands.add_parser("usage", help="查看日历私有归档占用（只读）")
    _json(command)
    command.set_defaults(handler=command_usage)

    command = commands.add_parser("validate", help="校验配置、权限与快照一致性（只读）")
    _json(command)
    command.set_defaults(handler=command_validate)
