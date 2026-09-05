"""Composition root and static registry for built-in LifeOS command modules.

The root parser, home screen and ``main`` live here so every command domain
(including Work) registers through the same ``CommandModule`` seam. The static
registry keeps code review, packaging and the security boundary visible:
configuration cannot specify import paths or load arbitrary code.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT_DESCRIPTION = "人生 OS / LifeOS CLI：管理个人工作事实、Agent 会话、本地协作证据与报告状态。"
ROOT_EPILOG = """领域边界：
  project  校验项目工作区的 lifeos-project.json；不写 Private Runtime。
  work     个人工作事实（项目引用、事项、待办、闪念、成果胶囊）；查询与写入都在此域。
  sessions 只读 Agent 应用来源，维护私有派生索引；不写来源日志、Work 或日报。
  git      只读本地 Git 提交，维护日报辅助证据快照；不修改仓库、remote、Work 或日报正文。
  dchat    只读 p2p / extp2p 私聊与项目清单群聊，维护私有原始证据；不发送消息、修改 DChat 或写 Work。
  reports  日报与周期报的结构和状态；正文由 lifeos Skill 的对应分支维护。
  web      只读本地工作台；只监听回环地址，不写 Work、日报或其它 Runtime。

所有时间窗口使用半开区间 [from, to)；具体参数、默认值和写入边界以对应子命令 --help 为准。"""

HOME_LOGO_LINES = (
    "⢀⣠⠀⠀⠀⡀",
    "⣰⢫⢖⣫⣝⡂⠙⣆",
    "⡇⣏⢾⠀⠀⡷⠀⢸",
    "⠹⣜⠦⣄⣠⠴⣣⠏",
    "⠈⠙⠒⠒⠋⠁",
)

HOME_TEMPLATE = """{brand}

以数据，照见人生。
让行动有迹，让经历成知。

常用命令
  lifeos work brief --mode current   查看当前工作简报
  lifeos work tasks                  查看待办
  lifeos work show TASK-ID           查看一条待办的完整记录
  lifeos work task-add --help        新增待办
  lifeos work task-close --help      完成待办并记录完成依据
  lifeos work idea-add --help        记下一条闪念
  lifeos project discover            发现项目工作区
  lifeos capabilities                检查本机可用能力

开始使用
  lifeos --help                      查看所有领域
  lifeos <领域> --help                查看领域内的命令
"""


def render_home(version):
    """Render the compact terminal adaptation of the LifeOS brand mark."""

    brand_lines = (*HOME_LOGO_LINES, "LifeOS", f"v{version}")
    brand_width = max(len(line) for line in brand_lines)
    brand = "\n".join(line.center(brand_width).rstrip() for line in brand_lines)
    return HOME_TEMPLATE.format(brand=brand)


@dataclass(frozen=True)
class ModuleContext:
    data_dir: Path


@dataclass(frozen=True)
class CommandModule:
    name: str
    register: Callable[[Any, ModuleContext], None]


def _work(domains: Any, context: ModuleContext) -> None:
    del context
    from lifeos_work.cli import register_work_parser

    register_work_parser(domains)


def _config(domains: Any, context: ModuleContext) -> None:
    from lifeos_config.cli import register_config_parser

    register_config_parser(domains)


def _capabilities(domains: Any, context: ModuleContext) -> None:
    del context
    from lifeos_config.cli import register_capabilities_parser

    register_capabilities_parser(domains)


def _git(domains: Any, context: ModuleContext) -> None:
    from lifeos_git.cli import register_git_parser

    register_git_parser(domains, context.data_dir)


def _dchat(domains: Any, context: ModuleContext) -> None:
    from lifeos_dchat.cli import register_dchat_parser

    register_dchat_parser(domains, context.data_dir)


def _sessions(domains: Any, context: ModuleContext) -> None:
    from lifeos_sessions.cli import register_sessions_parser

    register_sessions_parser(domains, context.data_dir)


def _reports(domains: Any, context: ModuleContext) -> None:
    from lifeos_reports.cli import register_reports_parser

    register_reports_parser(domains, context.data_dir)


def _project(domains: Any, context: ModuleContext) -> None:
    from lifeos_projects.cli import register_project_parser

    register_project_parser(domains)


def _web(domains: Any, context: ModuleContext) -> None:
    from lifeos_web.cli import register_web_parser

    register_web_parser(domains, context.data_dir)


COMMAND_MODULES = (
    CommandModule("work", _work),
    CommandModule("config", _config),
    CommandModule("capabilities", _capabilities),
    CommandModule("git", _git),
    CommandModule("dchat", _dchat),
    CommandModule("sessions", _sessions),
    CommandModule("reports", _reports),
    CommandModule("project", _project),
    CommandModule("web", _web),
)


def register_command_modules(domains: Any, data_dir: Path) -> None:
    context = ModuleContext(data_dir=data_dir)
    for module in COMMAND_MODULES:
        module.register(domains, context)


def build_parser(version: str) -> argparse.ArgumentParser:
    """Build the root parser with every registered command domain attached."""

    parser = argparse.ArgumentParser(
        prog="lifeos",
        description=ROOT_DESCRIPTION,
        epilog=ROOT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"LifeOS v{version}")
    domains = parser.add_subparsers(dest="domain")
    from lifeos_work.config import DATA_DIR

    register_command_modules(domains, DATA_DIR)
    return parser


def main(version: str) -> None:
    """Parse argv and dispatch to the selected domain handler."""

    parser = build_parser(version)
    args = parser.parse_args()
    if args.domain is None:
        print(render_home(version))
        return
    args.handler(args)


__all__ = [
    "COMMAND_MODULES",
    "CommandModule",
    "ModuleContext",
    "build_parser",
    "main",
    "register_command_modules",
    "render_home",
]
