"""``lifeos reports`` -- structure and state for daily and periodic reports.

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


def _validate_period(value: str) -> str:
    try:
        store.period_window(value)
    except ReportError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    return value


def _validate_nonnegative(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是非负整数")
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _validate_page_size(value: str) -> int:
    parsed = _validate_nonnegative(value)
    if not 1 <= parsed <= 31:
        raise argparse.ArgumentTypeError("必须是 1 到 31 之间的整数")
    return parsed


def _emit(args: Any, payload: Dict[str, Any], lines: List[str]) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in lines:
        print(line)


def _read_body_file(body_file: Path) -> str:
    try:
        body = (
            sys.stdin.read()
            if str(body_file) == "-"
            else body_file.read_text(encoding="utf-8")
        )
    except OSError as exc:
        _fail(f"无法读取正文：{body_file}（{exc}）")
    if not body.strip():
        _fail("正文不能为空")
    if body.lstrip().startswith("---"):
        _fail("正文不能包含 frontmatter；结构字段由 CLI 管理")
    return body


def _with_migration_recovery(handler: Any) -> Any:
    """Recover a terminated Reports migration before serving any command."""

    def recovered(args: Any) -> Any:
        try:
            store.recover_activity_id_migration(args.reports_root)
        except (OSError, ReportError) as exc:
            _fail(str(exc))
        return handler(args)

    return recovered


def command_begin(args: Any) -> None:
    reports_root: Path = args.reports_root
    day: date = args.day
    path = store.report_path(reports_root, day)
    with store.locked(reports_root):
        meta = store.skeleton(day, store.now_text())
        try:
            state, superseded_path = store.prepare_report_draft(
                path,
                meta,
                store.SKELETON_BODY,
                redo=args.redo,
                superseded_filename=store.superseded_name(day),
                label=f"{day.isoformat()} 的日报",
                ensure_directory=lambda: store.ensure_daily_dir(reports_root),
            )
        except ReportError as exc:
            _fail(str(exc))
        superseded = str(superseded_path) if superseded_path else None

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
        try:
            meta, changed = store.confirm_report_draft(
                path,
                validate=store.check_report,
                label=f"{day.isoformat()} 的日报",
                missing_message=f"{day.isoformat()} 还没有日报：{path}",
            )
        except ReportError as exc:
            _fail(str(exc))
    confirmed_at = meta.get("confirmed_at")
    _emit(
        args,
        {
            "day": day.isoformat(),
            "path": str(path),
            "status": "confirmed",
            "confirmed_at": confirmed_at,
            "changed": changed,
        },
        [
            f"{day.isoformat()} 的日报已确认 · {confirmed_at}"
            if changed
            else f"{day.isoformat()} 的日报已是 confirmed，未写入。"
        ],
    )


def command_write(args: Any) -> None:
    reports_root: Path = args.reports_root
    day: date = args.day
    path = store.report_path(reports_root, day)
    body = _read_body_file(args.body_file)
    for name, values, prefix in (
        ("--activity-id", args.activity_ids, "ACT-"),
        ("--work-event-id", args.work_event_ids, "EVT-"),
    ):
        if len(values) != len(set(values)):
            _fail(f"{name} 不能重复")
        invalid = next((value for value in values if not value.startswith(prefix)), None)
        if invalid:
            _fail(f"{name} 前缀非法：{invalid}")
    invalid_activity = next(
        (value for value in args.activity_ids if not store.ACTIVITY_ID.fullmatch(value)),
        None,
    )
    if invalid_activity:
        _fail(f"--activity-id 格式非法：{invalid_activity}")
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
        try:
            store.write_report_draft(
                path,
                body,
                validate=store.check_report,
                begin_hint="reports begin",
                label=f"{day.isoformat()} 的日报",
                meta_updates={
                    "sessions_activities": len(args.activity_ids),
                    "sessions_partial": args.sessions_partial,
                    "sessions_interrupted": args.sessions_interrupted,
                    "sessions_omitted": args.sessions_omitted,
                    "work_events": len(args.work_event_ids),
                    "user_notes": args.user_notes,
                    "unresolved": args.unresolved,
                    "git_scan_id": args.git_scan_id,
                    "git_commits": len(args.git_commit_ids),
                    "activity_ids": args.activity_ids,
                    "work_event_ids": args.work_event_ids,
                    "git_commit_ids": args.git_commit_ids,
                },
            )
        except ReportError as exc:
            _fail(str(exc))

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
        "origin": None,
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


def command_periodic_sources(args: Any) -> None:
    try:
        payload = store.periodic_sources(
            args.reports_root, args.period, offset=args.offset, limit=args.limit
        )
    except ReportError as exc:
        _fail(str(exc))
    lines = [
        f"{payload['period']} · confirmed 日报 {payload['source_total']} 份"
        f" · 本页 {len(payload['reports'])} 份 · offset {payload['offset']}"
    ]
    if payload.get("coverage"):
        lines.append(
            f"缺失 {len(payload['coverage']['missing_days'])} 天"
            f" · draft {len(payload['coverage']['draft_days'])} 天"
        )
    for report in payload["reports"]:
        lines.extend((f"\n--- {report['day']} ---", report["body"].rstrip()))
    _emit(args, payload, lines)


def command_periodic_begin(args: Any) -> None:
    reports_root: Path = args.reports_root
    period: str = args.period
    path = store.periodic_report_path(reports_root, period)
    with store.locked(reports_root):
        try:
            meta = store.periodic_skeleton(reports_root, period, store.now_text())
        except ReportError as exc:
            _fail(str(exc))
        if not meta["source_days"]:
            _fail(f"{period} 没有已确认日报，无法建立周期报草稿")
        try:
            state, superseded_path = store.prepare_report_draft(
                path,
                meta,
                store.PERIODIC_SKELETON_BODY,
                redo=args.redo,
                superseded_filename=store.periodic_superseded_name(period),
                label=f"{period} 的周期报",
                ensure_directory=lambda: store.ensure_periodic_dir(reports_root),
            )
        except ReportError as exc:
            _fail(str(exc))
        superseded = str(superseded_path) if superseded_path else None

    _kind, start, end = store.period_window(period)
    label = {"new": "新建草稿", "overwrite": "覆盖既有草稿", "redo": "重做已确认周期报"}[state]
    _emit(
        args,
        {
            "period": period,
            "period_type": meta["period_type"],
            "path": str(path),
            "state": state,
            "window": {
                "from": store.day_window(start)[0].isoformat(),
                "to": store.day_window(end)[0].isoformat(),
            },
            "source_days": meta["source_days"],
            "missing_days": meta["missing_days"],
            "draft_days": meta["draft_days"],
            "superseded": superseded,
        },
        [
            str(path),
            f"{label} · 来源 {len(meta['source_days'])} 份 confirmed 日报"
            f" · 缺失 {len(meta['missing_days'])} 天 · draft {len(meta['draft_days'])} 天",
        ],
    )


def command_periodic_write(args: Any) -> None:
    path = store.periodic_report_path(args.reports_root, args.period)
    body = _read_body_file(args.body_file)

    with store.locked(args.reports_root):
        try:
            store.write_report_draft(
                path,
                body,
                validate=lambda item: store.check_periodic_report(
                    item, allow_skeleton=True
                ),
                begin_hint="reports periodic begin",
                label=f"{args.period} 的周期报",
            )
        except ReportError as exc:
            _fail(str(exc))
    _emit(
        args,
        {"period": args.period, "path": str(path), "status": "draft"},
        [f"{args.period} 的周期报草稿已写入 · 状态仍为 draft"],
    )


def command_periodic_confirm(args: Any) -> None:
    path = store.periodic_report_path(args.reports_root, args.period)
    with store.locked(args.reports_root):
        try:
            meta, changed = store.confirm_report_draft(
                path,
                validate=store.check_periodic_report,
                label=f"{args.period} 的周期报",
                missing_message=f"{args.period} 还没有周期报：{path}",
                validate_current=lambda item: store.check_periodic_sources_current(
                    args.reports_root, item
                ),
            )
        except ReportError as exc:
            _fail(str(exc))
    confirmed_at = meta.get("confirmed_at")
    _emit(
        args,
        {
            "period": args.period,
            "path": str(path),
            "status": "confirmed",
            "confirmed_at": confirmed_at,
            "changed": changed,
        },
        [
            f"{args.period} 的周期报已确认 · {confirmed_at}"
            if changed
            else f"{args.period} 的周期报已是 confirmed，未写入。"
        ],
    )


def command_periodic_path(args: Any) -> None:
    path = store.periodic_report_path(args.reports_root, args.period)
    kind, start, end = store.period_window(args.period)
    payload: Dict[str, Any] = {
        "period": args.period,
        "period_type": kind,
        "path": str(path),
        "window": {
            "from": store.day_window(start)[0].isoformat(),
            "to": store.day_window(end)[0].isoformat(),
        },
        "exists": path.exists(),
        "status": None,
        "generated_at": None,
        "confirmed_at": None,
        "superseded": [
            str(item)
            for item in store.periodic_superseded_paths(args.reports_root, args.period)
        ],
    }
    lines = [str(path)]
    if path.exists():
        try:
            meta, _body = store.read_report(path)
        except ReportError as exc:
            lines.append(f"存在但无法解析：{exc}")
        else:
            for key in ("status", "generated_at", "confirmed_at"):
                payload[key] = meta.get(key)
            lines.append(
                f"{meta.get('status')} · 来源 {len(meta.get('source_days', []))} 份 confirmed 日报"
            )
    else:
        lines.append("尚未生成")
    if payload["superseded"]:
        lines.append(f"另有 {len(payload['superseded'])} 份 superseded 快照")
    _emit(args, payload, lines)


def command_periodic_list(args: Any) -> None:
    rows = store.list_periodic_reports(args.reports_root)
    if args.status:
        rows = [row for row in rows if row.get("status") == args.status]
    payload = {"total": len(rows), "reports": rows}
    lines = []
    for row in rows:
        if row.get("error"):
            lines.append(f"{row['period']} · 无法解析：{row['error']}")
            continue
        lines.append(
            f"{row['period']} · {row.get('status')} · 来源 {row.get('source_days')} 份"
            f" · 缺失 {len(row.get('missing_days', []))} 天"
            f" · draft {len(row.get('draft_days', []))} 天"
        )
    if not rows:
        lines.append("没有符合条件的周期报。")
    _emit(args, payload, lines)


def command_validate(args: Any) -> None:
    reports_root: Path = args.reports_root
    findings = store.check_directory(reports_root)
    daily_rows = store.list_reports(reports_root)
    periodic_rows = store.list_periodic_reports(reports_root)
    payload = {
        "root": str(reports_root),
        "checked": len(daily_rows) + len(periodic_rows),
        "daily_checked": len(daily_rows),
        "periodic_checked": len(periodic_rows),
        "problems": [{"path": str(path), "problem": problem} for path, problem in findings],
        "ok": not findings,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for path, problem in findings:
            print(f"! {Path(path).name}：{problem}", file=sys.stderr)
        if findings:
            print(
                f"{len(findings)} 项问题，检查了 {len(daily_rows)} 份日报"
                f"和 {len(periodic_rows)} 份周期报。",
                file=sys.stderr,
            )
        else:
            print(
                f"Reports 校验通过，共 {len(daily_rows)} 份日报"
                f"和 {len(periodic_rows)} 份周期报。"
            )
    if findings:
        raise SystemExit(1)


def command_migrate_activity_ids(args: Any) -> None:
    """Preview or apply the one-time breaking Activity ID migration."""

    reports_root: Path = args.reports_root
    try:
        if args.apply:
            with store.locked(reports_root):
                planned = store.plan_activity_id_migration(reports_root)
                backup = store.apply_activity_id_migration(reports_root, planned)
        else:
            planned = store.plan_activity_id_migration(reports_root)
            backup = None
    except (OSError, ReportError) as exc:
        _fail(str(exc))

    changed_ids = sum(item[3] for item in planned)
    payload = {
        "mode": "apply" if args.apply else "dry-run",
        "checked_files": len(store.activity_id_migration_paths(reports_root)),
        "changed_files": len(planned),
        "changed_ids": changed_ids,
        "backup": str(backup) if backup else None,
    }
    action = "已迁移" if args.apply else "将迁移"
    _emit(
        args,
        payload,
        [
            f"{action} {len(planned)} 份日报中的 {changed_ids} 个 Activity ID。"
            + (f" 备份：{backup}" if backup else "")
        ],
    )


def register_reports_parser(domains: Any, data_dir: Path) -> None:
    reports = domains.add_parser(
        "reports",
        help="日报与周期报的结构和状态（正文由 Skill 写入）",
        description=(
            "管理由 lifeos Skill 生成、本人确认的自然日日报和周期报。"
            "CLI 只拥有落点、命名、权限、frontmatter 与状态；不生成正文、不调用模型。"
        ),
        epilog=(
            "自然日按 Asia/Shanghai 的 [00:00, 24:00) 计算。"
            "begin/write/confirm 会写入日报，周期报使用 periodic 子命令；"
            "path/list/validate 与 periodic sources/path/list 只读；"
            "所有正文都由 lifeos Skill 生成。"
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
    command.set_defaults(handler=_with_migration_recovery(command_begin))

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
    command.set_defaults(handler=_with_migration_recovery(command_write))

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
    command.set_defaults(handler=_with_migration_recovery(command_confirm))

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
    command.set_defaults(handler=_with_migration_recovery(command_path))

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
    command.set_defaults(handler=_with_migration_recovery(command_list))

    periodic = commands.add_parser(
        "periodic",
        help="基于 confirmed 日报管理周、月、季度、半年或年度报告",
        description=(
            "周、月、季度、半年或年度报告只消费目标窗口内的 confirmed 日报，"
            "不重新采集 Sessions、Git、DChat 或 Work。"
            "CLI 负责规范周期、来源清单、草稿与确认状态；正文由 lifeos Skill 的 Periodic 分支生成。"
        ),
        epilog=(
            "周期使用 YYYY-Www、YYYY-MM、YYYY-Qn、YYYY-Hn 或 YYYY。"
            "周按 ISO 周一至周日计算，其他周期按自然日历计算。"
        ),
    )
    periodic_commands = periodic.add_subparsers(dest="periodic_command", required=True)

    def add_period_argument(parser: Any) -> None:
        parser.add_argument(
            "--period",
            type=_validate_period,
            required=True,
            metavar="PERIOD",
            help="规范周期：YYYY-Www、YYYY-MM、YYYY-Qn、YYYY-Hn 或 YYYY",
        )

    command = periodic_commands.add_parser(
        "sources",
        help="分页只读返回周期窗口和 confirmed 日报正文",
        description=(
            "按周期逐日检查 Reports Runtime，只分页返回 confirmed 日报正文。"
            "第一页同时返回完整来源、缺失日和 draft 日清单；"
            "按 next_offset 继续可读完长周期且不截断正文。"
        ),
    )
    add_period_argument(command)
    command.add_argument(
        "--offset",
        type=_validate_nonnegative,
        default=0,
        metavar="N",
        help="从第 N 份 confirmed 日报开始（默认 0）",
    )
    command.add_argument(
        "--limit",
        type=_validate_page_size,
        default=7,
        metavar="N",
        help="本页最多返回的日报数，1 到 31（默认 7）",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出来源正文与窗口")
    command.set_defaults(handler=_with_migration_recovery(command_periodic_sources))

    command = periodic_commands.add_parser(
        "begin",
        help="按当前 confirmed 日报来源建立周期报草稿",
        description=(
            "快照目标周期内的 confirmed、缺失和 draft 日报清单并建立草稿。"
            "没有 confirmed 日报时拒绝建立；confirmed 周期报只有 --redo 才能重做。"
        ),
    )
    add_period_argument(command)
    command.add_argument(
        "--redo",
        action="store_true",
        help="确认状态时先保留 superseded 快照再重建草稿",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出路径、来源和状态")
    command.set_defaults(handler=_with_migration_recovery(command_periodic_begin))

    command = periodic_commands.add_parser(
        "write",
        help="原子写入周期报 draft 正文（不确认）",
        description=(
            "把 lifeos Skill 的 Periodic 分支生成的 Markdown 正文写入既有 draft；"
            "来源清单与状态由 CLI 保留。"
        ),
    )
    add_period_argument(command)
    command.add_argument(
        "--body-file",
        type=Path,
        required=True,
        metavar="PATH|-",
        help="Markdown 正文文件；使用 - 从标准输入读取",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出写入后的状态")
    command.set_defaults(handler=_with_migration_recovery(command_periodic_write))

    command = periodic_commands.add_parser(
        "confirm",
        help="校验来源未漂移后确认周期报（需要本人确认）",
        description=(
            "确认 draft 前核对当前 confirmed、缺失和 draft 日报清单仍与生成时一致；"
            "已确认周期报幂等返回。"
        ),
        epilog="确认是状态写入；只有本人明确确认后才应执行。",
    )
    add_period_argument(command)
    command.add_argument("--json", action="store_true", help="以 JSON 输出校验与状态变化")
    command.set_defaults(handler=_with_migration_recovery(command_periodic_confirm))

    command = periodic_commands.add_parser(
        "path",
        help="只读查询周期报落点、状态、窗口与快照",
    )
    add_period_argument(command)
    command.add_argument("--json", action="store_true", help="以 JSON 输出路径和状态")
    command.set_defaults(handler=_with_migration_recovery(command_periodic_path))

    command = periodic_commands.add_parser(
        "list",
        help="只读列出周期报及其来源覆盖",
    )
    command.add_argument(
        "--status",
        choices=store.STATUSES,
        metavar="{draft,confirmed}",
        help="只列出指定状态",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出周期报列表")
    command.set_defaults(handler=_with_migration_recovery(command_periodic_list))

    command = commands.add_parser(
        "validate",
        help="只读校验全部日报和周期报的命名、frontmatter、状态与权限",
        description=(
            "检查全部当前日报和周期报的文件名、必填 frontmatter、状态与 confirmed_at、"
            "正文、目录/文件权限及 stray 文件。任何问题都以失败退出并指出路径。"
        ),
        epilog="validate 不创建、不修改、不确认任何报告。",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出检查范围、问题与 ok 状态")
    command.set_defaults(handler=_with_migration_recovery(command_validate))

    command = commands.add_parser(
        "migrate-activity-ids",
        help="把历史日报中的旧长 Activity ID 一次性迁移为短 ID",
        description=(
            "默认只读预演 current 与 superseded 日报的 Activity ID 迁移。"
            "--apply 会先备份完整 Reports 目录，再只改 frontmatter 的 activity_ids；"
            "不读取或重扫 Sessions 来源。"
        ),
        epilog=(
            "这是破坏式一次性迁移：新版本不兼容旧长 ID。"
            "应用前会解析全部目标并检查短 ID 碰撞，正文和其他字段不变。"
        ),
    )
    command.add_argument(
        "--apply",
        action="store_true",
        help="应用迁移；省略时只报告将变化的文件和 ID 数",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出迁移计划或结果")
    command.set_defaults(handler=_with_migration_recovery(command_migrate_activity_ids))
