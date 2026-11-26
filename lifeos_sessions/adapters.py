"""Registry for built-in Agent session source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple


@dataclass(frozen=True)
class SessionSource:
    name: str
    factory: Callable[[], Any]
    root: Callable[[], Path]
    roots: Optional[Callable[[], Tuple[Path, ...]]] = None


def _codex() -> Any:
    from .codex import CodexAdapter

    return CodexAdapter()


def _claude() -> Any:
    from .claude import ClaudeAdapter

    return ClaudeAdapter()


def _smartwork() -> Any:
    from .smartwork import SmartworkAdapter

    return SmartworkAdapter()


def _deepseek() -> Any:
    from .deepseek import DeepseekAdapter

    return DeepseekAdapter()


def _pi() -> Any:
    from .pi import PiAdapter

    return PiAdapter()


def _antigravity() -> Any:
    from .antigravity import AntigravityAdapter

    return AntigravityAdapter()


def _antigravity_roots() -> Tuple[Path, ...]:
    from .antigravity import default_antigravity_roots

    return tuple(default_antigravity_roots())


def _antigravity_root() -> Path:
    roots = _antigravity_roots()
    for path in roots:
        if path.is_dir():
            return path
    return roots[0]


SESSION_SOURCES = (
    SessionSource("codex", _codex, lambda: Path.home() / ".codex"),
    SessionSource("claude", _claude, lambda: Path.home() / ".claude" / "projects"),
    SessionSource("smartwork", _smartwork, lambda: Path.home() / ".SmartWork" / "sessions"),
    SessionSource("deepseek", _deepseek, lambda: Path.home() / ".dsh" / "sessions"),
    SessionSource("pi", _pi, lambda: Path.home() / ".pi" / "agent" / "sessions"),
    SessionSource("antigravity", _antigravity, _antigravity_root, _antigravity_roots),
)
SESSION_SOURCE_NAMES = tuple(source.name for source in SESSION_SOURCES)
_BY_NAME = {source.name: source for source in SESSION_SOURCES}


def build_adapters(names: Iterable[str]) -> list[Any]:
    return [_BY_NAME[name].factory() for name in names]


def source_root(name: str) -> Path:
    return _BY_NAME[name].root()


def source_roots(name: str) -> Tuple[Path, ...]:
    source = _BY_NAME[name]
    if source.roots is not None:
        return source.roots()
    return (source.root(),)


def resolve_selected_sources(
    values: Iterable[str], enabled: Iterable[str]
) -> tuple[str, ...]:
    enabled_names = tuple(enabled)
    selected: list[str] = []
    for value in values:
        current = enabled_names if value == "all" else (value,)
        for source in current:
            if source not in enabled_names:
                raise ValueError(f"Sessions source 未启用：{source}")
            if source not in selected:
                selected.append(source)
    return tuple(selected)


__all__ = [
    "SESSION_SOURCES",
    "SESSION_SOURCE_NAMES",
    "SessionSource",
    "build_adapters",
    "resolve_selected_sources",
    "source_root",
    "source_roots",
]
