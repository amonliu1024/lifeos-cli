"""``lifeos reports`` -- structure and state for daily reports.

The split with the ``lifeos`` skill's Daily branch is deliberate: the skill authors the
prose, while this domain decides where a report lives, what it is called, who
can read it, whether its frontmatter is legal and whether an already confirmed
day may be replaced.  It transports prose into a draft but never generates it
or calls a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from . import store
from .store import ReportError


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def _validate_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD")


def _validate_nonnegative(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是非负整数")
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _emit(args: Any, payload: Dict[str, Any], lines: List[str]) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in lines:
        print(line)


def command_begin(args: Any) -> None:
    reports_root: Path = args.reports_root
    day: date = args.day
    path = store.report_path(reports_root, day)
    state = "new"
    superseded = None

    with store.locked(reports_root):
        if path.exists():
            try:
                meta, _body = store.read_report(path)
            except ReportError as exc:
                _fail(f"已存在的日报无法解析，请先修复或移走：{path}（{exc}）")
            status = meta.get("status")
            if status == "confirmed" and not args.redo:
                _fail(
                    f"{day.isoformat()} 的日报已确认，不会自动覆盖。"
                    f"确实要重做时加 --redo，旧文件会另存为 superseded 快照。"
                )
            state = "redo" if status == "confirmed" else "overwrite"

        store.ensure_daily_dir(reports_root)
        if state == "redo":
            target = path.parent / store.superseded_name(day)
            path.rename(target)
            superseded = str(target)

        meta = store.skeleton(day, store.now_text())
        store.write_report(path, meta, store.SKELETON_BODY)

    window_from, window_to = store.day_window(day)
    label = {"new": "新建草稿", "overwrite": "覆盖既有草稿", "redo": "重做已确认日报"}[state]
    _emit(
        args,
        {
            "day": day.isoformat(),
            "path": str(path),
            "state": state,
            "window": {"from": window_from.isoformat(), "to": window_to.isoformat()},
            "superseded": superseded,
        },
        [
            str(path),
            f"{label} · 窗口 {window_from.isoformat()} → {window_to.isoformat()}"
            + (f" · 旧文件已另存 {Path(superseded).name}" if superseded else ""),
        ],
    )


def command_confirm(args: Any) -> None:
    reports_root: Path = args.reports_root
    day: date = args.day
    path = store.report_path(reports_root, day)
    with store.locked(reports_root):
        if not path.exists():
            _fail(f"{day.isoformat()} 还没有日报：{path}")

        problems = store.check_report(path)
        if problems:
            for problem in problems:
                print(f"! {problem}", file=sys.stderr)
            _fail(f"日报未通过校验，不予确认：{path}")

        meta, body = store.read_report(path)
        if meta.get("status") == "confirmed":
            _emit(
                args,
                {
                    "day": day.isoformat(),
                    "path": str(path),
                    "status": "confirmed",
                    "confirmed_at": meta.get("confirmed_at"),
                    "changed": False,
                },
                [f"{day.isoformat()} 的日报已是 confirmed，未写入。"],
            )
            return

        confirmed_at = store.now_text()
        meta["status"] = "confirmed"
        meta["confirmed_at"] = confirmed_at
        store.write_report(path, meta, body)
    _emit(
        args,
        {
            "day": day.isoformat(),
            "path": str(path),
            "status": "confirmed",
            "confirmed_at": confirmed_at,
            "changed": True,
        },
        [f"{day.isoformat()} 的日报已确认 · {confirmed_at}"],
    )


def command_write(args: Any) -> None:
    reports_root: Path = args.reports_root
    day: date = args.day
    path = store.report_path(reports_root, day)
    try:
        body = (
            sys.stdin.read()
            if str(args.body_file) == "-"
            else args.body_file.read_text(encoding="utf-8")
        )
    except OSError as exc:
        _fail(f"无法读取正文：{args.body_file}（{exc}）")
    if not body.strip():
        _fail("正文不能为空")
    if body.lstrip().startswith("---"):
        _fail("正文不能包含 frontmatter；结构字段由 CLI 管理")
    for name, values, prefix in (
        ("--activity-id", args.activity_ids, "ACT-"),
        ("--work-event-id", args.work_event_ids, "EVT-"),
    ):
        if len(values) != len(set(values)):
            _fail(f"{name} 不能重复")
        invalid = next((value for value in values if not value.startswith(prefix)), None)
        if invalid:
            _fail(f"{name} 前缀非法：{invalid}")
    if args.git_commit_ids:
        if len(args.git_commit_ids) != len(set(args.git_commit_ids)):
            _fail("--git-commit-id 不能重复")
        invalid_git_id = next(
            (value for value in args.git_commit_ids if not store.GIT_COMMIT_ID.fullmatch(value)),
            None,
        )
        if invalid_git_id:
            _fail(f"--git-commit-id 格式非法：{invalid_git_id}")
    if args.git_scan_id and not store.GIT_SCAN_ID.fullmatch(args.git_scan_id):
        _fail(f"--git-scan-id 格式非法：{args.git_scan_id}")

    with store.locked(reports_root):
        if not path.exists():
            _fail(f"{day.isoformat()} 还没有日报；请先运行 reports begin")
        problems = store.check_report(path)
        if problems:
            for problem in problems:
                print(f"! {problem}", file=sys.stderr)
            _fail(f"既有日报未通过校验，不予写入：{path}")
        meta, _existing = store.read_report(path)
        if meta.get("status") != "draft":
            _fail(f"{day.isoformat()} 的日报已确认；确实要重做时先运行 reports begin --redo")
        meta.update(
            sessions_activities=len(args.activity_ids),
            sessions_partial=args.sessions_partial,
            sessions_interrupted=args.sessions_interrupted,
            sessions_omitted=args.sessions_omitted,
            work_events=len(args.work_event_ids),
            user_notes=args.user_notes,
            unresolved=args.unresolved,
            git_scan_id=args.git_scan_id,
            git_commits=len(args.git_commit_ids),
            activity_ids=args.activity_ids,
            work_event_ids=args.work_event_ids,
            git_commit_ids=args.git_commit_ids,
        )
        store.write_report(path, meta, body)

    _emit(
        args,
        {
            "day": day.isoformat(),
            "path": str(path),
            "status": "draft",
            "sessions_activities": len(args.activity_ids),
            "sessions_partial": args.sessions_partial,
            "sessions_interrupted": args.sessions_interrupted,
            "sessions_omitted": args.sessions_omitted,
            "work_events": len(args.work_event_ids),
            "user_notes": args.user_notes,
            "unresolved": args.unresolved,
            "git_scan_id": args.git_scan_id,
            "git_commits": len(args.git_commit_ids),
        },
        [
            f"{day.isoformat()} 的日报草稿已写入 · 会话 {len(args.activity_ids)}"
            f" · work {len(args.work_event_ids)} · 待确认 {args.unresolved}"
        ],
    )


def command_path(args: Any) -> None:
    reports_root: Path = args.reports_root
    day: date = args.day
    path = store.report_path(reports_root, day)
    window_from, window_to = store.day_window(day)
    payload: Dict[str, Any] = {
        "day": day.isoformat(),
        "path": str(path),
        "window": {"from": window_from.isoformat(), "to": window_to.isoformat()},
        "exists": path.exists(),
        "status": None,
        "generated_at": None,
        "confirmed_at": None,
        "superseded": [str(item) for item in store.superseded_paths(reports_root, day)],
    }
    lines = [str(path)]
    if path.exists():
        try:
            meta, _body = store.read_report(path)
        except ReportError as exc:
            lines.append(f"存在但无法解析：{exc}")
        else:
            payload["status"] = meta.get("status")
            payload["generated_at"] = meta.get("generated_at")
            payload["confirmed_at"] = meta.get("confirmed_at")
            lines.append(
                f"{meta.get('status')} · 生成于 {meta.get('generated_at')}"
                + (f" · 确认于 {meta.get('confirmed_at')}" if meta.get("confirmed_at") else "")
            )
    else:
        lines.append("尚未生成")
    if payload["superseded"]:
        lines.append(f"另有 {len(payload['superseded'])} 份 superseded 快照")
    _emit(args, payload, lines)


def command_list(args: Any) -> None:
    reports_root: Path = args.reports_root
    rows = store.list_reports(reports_root)
    window_from: date = args.from_value
    window_to: date = args.to_value
    if window_from and window_to and window_to <= window_from:
        _fail("--to 必须晚于 --from")
    if window_from:
        rows = [row for row in rows if date.fromisoformat(row["day"]) >= window_from]
    if window_to:
        rows = [row for row in rows if date.fromisoformat(row["day"]) < window_to]
    if args.status:
        rows = [row for row in rows if row.get("status") == args.status]

    missing = (
        store.missing_days(rows, window_from, window_to)
        if window_from and window_to and not args.status
        else []
    )
    payload = {"total": len(rows), "reports": rows, "missing_days": missing}
    lines = []
    for row in rows:
        if row.get("error"):
            lines.append(f"{row['day']} · 无法解析：{row['error']}")
            continue
        detail = (
            f"会话 {row.get('sessions_activities')} · work {row.get('work_events')}"
            f" · 补录 {row.get('user_notes')} · 待确认 {row.get('unresolved')}"
        )
        suffix = f" · superseded {row['superseded']}" if row.get("superseded") else ""
        lines.append(f"{row['day']} · {row.get('status')} · {detail}{suffix}")
    if not rows:
        lines.append("没有符合条件的日报。")
    if missing:
        lines.append(f"缺失日报 {len(missing)} 天：{'、'.join(missing)}")
    _emit(args, payload, lines)


def command_validate(args: Any) -> None:
    reports_root: Path = args.reports_root
    findings = store.check_directory(reports_root)
    rows = store.list_reports(reports_root)
    payload = {
        "root": str(reports_root),
        "checked": len(rows),
        "problems": [{"path": str(path), "problem": problem} for path, problem in findings],
        "ok": not findings,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for path, problem in findings:
            print(f"! {Path(path).name}：{problem}", file=sys.stderr)
        if findings:
            print(f"{len(findings)} 项问题，检查了 {len(rows)} 份日报。", file=sys.stderr)
        else:
            print(f"日报校验通过，共 {len(rows)} 份。")
    if findings:
        raise SystemExit(1)


def register_reports_parser(domains: Any, data_dir: Path) -> None:
    reports = domains.add_parser(
        "reports",
        help="日报结构与状态（正文由 Skill 写入）",
        description=(
            "管理由 lifeos Skill 的 Daily 分支生成、本人确认的自然日日报。"
            "CLI 只拥有落点、命名、权限、frontmatter 与状态；不生成正文、不调用模型。"
        ),
        epilog=(
            "自然日按 Asia/Shanghai 的 [00:00, 24:00) 计算。"
            "begin/write/confirm 会写入日报；path/list/validate 只读。"
        ),
    )
    reports.set_defaults(reports_root=data_dir / "reports")
    commands = reports.add_subparsers(dest="command", required=True)

    command = commands.add_parser(
        "begin",
        help="准备某自然日的日报草稿（会写入；confirmed 需 --redo）",
        description=(
            "建立目标自然日的日报骨架并返回路径与窗口。"
            "已有 draft 会被重建并覆盖；confirmed 只有显式 --redo 才会先保留 superseded 快照再重做。"
            "正文仍由 lifeos Skill 的 Daily 分支写入。"
        ),
        epilog="这是写入握手，不生成正文；重复 begin 会覆盖 draft 正文，--redo 会为 confirmed 留存旧快照。",
    )
    command.add_argument(
        "--day",
        type=_validate_day,
        required=True,
        metavar="YYYY-MM-DD",
        help="目标自然日（Asia/Shanghai；窗口为 [00:00, 24:00)）",
    )
    command.add_argument(
        "--redo",
        action="store_true",
        help="确认状态时先保留 superseded 快照再重建草稿；draft 不需要此选项",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出路径、状态与窗口")
    command.set_defaults(handler=command_begin)

    command = commands.add_parser(
        "write",
        help="原子写入 draft 正文、计数与引用 ID（不确认）",
        description=(
            "把 lifeos Skill 的 Daily 分支已生成的正文和证据清单原子写入既有 draft。"
            "CLI 保留 day/status/generated_at/window，并由唯一 ID 数量计算会话与 Work 计数。"
            "confirmed 日报不会被覆盖。"
        ),
        epilog=(
            "先运行 begin；正文文件只包含 Markdown 正文，不含 frontmatter。"
            "本命令不确认日报，写入后状态仍是 draft；本人明确确认后再运行 confirm。"
        ),
    )
    command.add_argument(
        "--day",
        type=_validate_day,
        required=True,
        metavar="YYYY-MM-DD",
        help="要写入的自然日",
    )
    command.add_argument(
        "--body-file",
        type=Path,
        required=True,
        metavar="PATH|-",
        help="Markdown 正文文件；使用 - 从标准输入读取",
    )
    for option, destination, help_text in (
        ("--sessions-partial", "sessions_partial", "窗口内 partial Slice 数"),
        ("--sessions-interrupted", "sessions_interrupted", "窗口内 interrupted_with_result Slice 数"),
        ("--sessions-omitted", "sessions_omitted", "窗口内 explicit_abort_without_work omission 数"),
        ("--user-notes", "user_notes", "正文采用的本人补录数"),
        ("--unresolved", "unresolved", "正文保留的未决判断数"),
    ):
        command.add_argument(
            option,
            dest=destination,
            type=_validate_nonnegative,
            default=0,
            metavar="N",
            help=help_text + "（默认 0）",
        )
    command.add_argument(
        "--activity-id",
        dest="activity_ids",
        action="append",
        default=[],
        metavar="ACT-ID",
        help="正文引用的 Activity ID；可重复，必须唯一",
    )
    command.add_argument(
        "--work-event-id",
        dest="work_event_ids",
        action="append",
        default=[],
        metavar="EVT-ID",
        help="正文引用的 Work event ID；可重复，必须唯一",
    )
    command.add_argument(
        "--git-scan-id",
        dest="git_scan_id",
        metavar="GITSCAN-ID",
        help="Daily 使用的 Git scan 快照 ID（可选）",
    )
    command.add_argument(
        "--git-commit-id",
        dest="git_commit_ids",
        action="append",
        default=[],
        metavar="REPO@SHA",
        help="正文引用的 Git commit ID（repo_key@完整 SHA；可重复，必须唯一）",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出写入后的状态与计数")
    command.set_defaults(handler=command_write)

    command = commands.add_parser(
        "confirm",
        help="先校验再确认某自然日的日报（需要本人确认）",
        description=(
            "读取并校验目标日报；校验通过后才把 draft 翻为 confirmed。"
            "它不会生成或补写正文；已是 confirmed 时保持不变并幂等返回。"
        ),
        epilog="确认是状态写入，不是交互式询问；只有本人明确确认后才应执行。",
    )
    command.add_argument(
        "--day",
        type=_validate_day,
        required=True,
        metavar="YYYY-MM-DD",
        help="要确认的自然日（Asia/Shanghai）",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出校验与状态变化")
    command.set_defaults(handler=command_confirm)

    command = commands.add_parser(
        "path",
        help="只读查询某自然日的落点、状态与 superseded 快照",
        description=(
            "解析目标日报的预期路径、是否存在、当前状态、生成/确认时间和 superseded 快照。"
            "不存在时也只报告状态，不创建目录或文件。"
        ),
        epilog="path 永远只读；正文解析失败时报告错误，不替日报修复内容。",
    )
    command.add_argument(
        "--day",
        type=_validate_day,
        required=True,
        metavar="YYYY-MM-DD",
        help="要查询的自然日（Asia/Shanghai）",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出路径、状态与快照列表")
    command.set_defaults(handler=command_path)

    command = commands.add_parser(
        "list",
        help="只读列出日报（日期窗口半开，并报告缺失日）",
        description=(
            "列出日报的日期、状态、生成时间和计数。"
            "--from 左闭、--to 右开；同时提供时按自然日窗口报告缺失日期。"
            "--status 只保留该状态，不进行缺失日审计。"
        ),
        epilog="list 永远只读；未提供窗口时列出所有当前日报。",
    )
    command.add_argument(
        "--from",
        dest="from_value",
        type=_validate_day,
        metavar="YYYY-MM-DD",
        help="窗口起点，含该日（半开区间左边界）",
    )
    command.add_argument(
        "--to",
        dest="to_value",
        type=_validate_day,
        metavar="YYYY-MM-DD",
        help="窗口终点，不含该日（半开区间右边界）",
    )
    command.add_argument(
        "--status",
        choices=store.STATUSES,
        metavar="{draft,confirmed}",
        help="只列出指定状态；启用时不报告窗口缺失日",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出日报、总数与缺失日")
    command.set_defaults(handler=command_list)

    command = commands.add_parser(
        "validate",
        help="只读校验全部日报的命名、frontmatter、状态与权限",
        description=(
            "检查全部当前日报的文件名与 day、必填 frontmatter、状态与 confirmed_at、"
            "正文、目录/文件权限及 stray 文件。任何问题都以失败退出并指出路径。"
        ),
        epilog="validate 不创建、不修改、不确认日报；确认动作请使用 confirm。",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出检查范围、问题与 ok 状态")
    command.set_defaults(handler=command_validate)
