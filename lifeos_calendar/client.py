"""External D-Chat calendar read adapter using the approved local dws wrapper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Protocol, Sequence


class CalendarClientError(RuntimeError):
    """A classified external calendar read failure."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class CalendarClient(Protocol):
    def search(self, start_ms: int, end_ms: int) -> Sequence[Mapping[str, Any]]: ...

    def info(self, event_id: str) -> Mapping[str, Any]: ...


class DwsCalendarAdapter:
    """Run ``dws calendar`` read commands and parse their JSON envelope from stdout."""

    def __init__(self, wrapper: str, *, timeout: int = 120):
        path = Path(wrapper).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise CalendarClientError("client_unavailable", "dws wrapper 必须是存在的绝对文件")
        self.wrapper = str(path)
        self.timeout = timeout

    def _run(self, arguments: Sequence[str]) -> Any:
        # Same argv discipline as the DChat adapter: bash receives an array,
        # nothing is interpolated into a shell string.
        command = ["bash", self.wrapper, "--output", "json", *arguments]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as exc:
            raise CalendarClientError("client_unavailable", str(exc)) from exc
        text = (completed.stdout or "").strip()
        if completed.returncode != 0 and not text:
            detail = (completed.stderr or "dws 读取失败").strip()[:2000]
            lowered = detail.lower()
            if "workspace-server" in lowered and ("permission denied" in lowered or "operation not permitted" in lowered):
                raise CalendarClientError(
                    "client_ipc_forbidden",
                    "当前执行环境无权访问 DChat workspace-server 的本地 IPC socket；"
                    "请在允许访问本机 socket 的环境中重跑同一条 lifeos/dws 命令。",
                )
            raise CalendarClientError("temporary_dependency_failure", detail)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CalendarClientError("unsupported_payload", f"dws 输出不是 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise CalendarClientError("unsupported_payload", "dws 输出不是对象")
        if payload.get("ok") is not True:
            error = payload.get("error") or {}
            message = str(error.get("message") or "dws 读取失败")[:2000]
            kind = "temporary_dependency_failure"
            if "timeout" in message.lower():
                kind = "temporary_dependency_failure"
            elif "无法连接" in message or "client" in message.lower():
                kind = "client_unavailable"
            raise CalendarClientError(kind, message)
        return payload.get("data")

    def search(self, start_ms: int, end_ms: int) -> Sequence[Mapping[str, Any]]:
        data = self._run(["calendar", "search", "--start-time", str(start_ms), "--end-time", str(end_ms)])
        if isinstance(data, str):
            # dws answers an empty window with a sentence instead of an empty array.
            return []
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise CalendarClientError("unsupported_payload", "dws 日程搜索结果不是数组")
        return data

    def info(self, event_id: str) -> Mapping[str, Any]:
        data = self._run(["calendar", "info", event_id])
        if not isinstance(data, dict):
            raise CalendarClientError("unsupported_payload", "dws 日程详情不是对象")
        return data


def attendee_names(detail: Mapping[str, Any]) -> List[str]:
    names: List[str] = []
    for item in detail.get("attendees") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("displayName") or item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names
