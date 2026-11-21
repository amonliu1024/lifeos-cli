"""Public command for writing the personal company time ledger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .store import CompanyTimeError, set_record


def command_set(args: argparse.Namespace) -> None:
    try:
        record = set_record(args.data_dir, args.day, start=args.start, end=args.end)
    except (CompanyTimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    start = record["started_at"]
    end = record["ended_at"]
    print(f"{args.day}  上班 {start[:16].replace('T', ' ') if start else '未记录'}"
          f"  下班 {end[:16].replace('T', ' ') if end else '未记录'}")


def register_company_time_parser(domains: argparse._SubParsersAction, data_dir: Path) -> None:
    domain = domains.add_parser("company-time", help="记录本人的上下班时间")
    commands = domain.add_subparsers(dest="command", required=True)
    write = commands.add_parser("set", help="新增、补记或修改一天的上下班时间")
    write.add_argument("day", help="上班当天的日期（YYYY-MM-DD，北京时间）")
    write.add_argument("--start", help="上班时间（HH:MM 或 YYYY-MM-DD HH:MM）")
    write.add_argument("--end", help="下班时间；跨天时填写实际日期和时间")
    write.set_defaults(handler=command_set, data_dir=data_dir)
