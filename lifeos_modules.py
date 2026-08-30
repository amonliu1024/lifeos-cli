"""Static registry for built-in LifeOS command modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ModuleContext:
    data_dir: Path


@dataclass(frozen=True)
class CommandModule:
    name: str
    register: Callable[[Any, ModuleContext], None]


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


__all__ = ["COMMAND_MODULES", "CommandModule", "ModuleContext", "register_command_modules"]
