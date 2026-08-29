"""External DChat read adapter using the approved local dws wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Protocol, Sequence


class DChatClientError(RuntimeError):
    """A classified external DChat read failure."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class DChatClient(Protocol):
    def list_chats(self) -> Sequence[Mapping[str, Any]]: ...

    def dump_messages(
        self, conversation_id: str, from_value: str, to_value: str, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


class DwsDChatAdapter:
    """Read JSON exports without exposing their temporary files to the store."""

    def __init__(self, wrapper: str, *, timeout: int = 120):
        path = Path(wrapper).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise DChatClientError("client_unavailable", "dws wrapper 必须是存在的绝对文件")
        self.wrapper = str(path)
        self.timeout = timeout

    def _export(self, arguments: Sequence[str]) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="lifeos-dchat-") as directory:
            os.chmod(directory, 0o700)
            output = Path(directory) / "export.json"
            # The distributed wrapper is a non-executable shell script.  Bash
            # receives an argv array directly; no command string or shell
            # interpolation is involved.
            # Keep DWS technical diagnostics enabled so a sandbox IPC denial is
            # distinguishable from a stopped client. stderr remains local and
            # is only surfaced when the command fails.
            command = ["bash", self.wrapper, "--debug", *arguments, str(output)]
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
                raise DChatClientError("client_unavailable", str(exc)) from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "dws 读取失败").strip()[:2000]
                lowered = detail.lower()
                if "workspace-server" in lowered and ("permission denied" in lowered or "operation not permitted" in lowered):
                    raise DChatClientError(
                        "client_ipc_forbidden",
                        "当前执行环境无权访问 DChat workspace-server 的本地 IPC socket；"
                        "请在允许访问本机 socket 的环境中重跑同一条 lifeos/dws 命令，"
                        "不要据此判断客户端未启动，也不要改用桌面操作。",
                    )
                if "无法连接至 d-chat 客户端" in lowered:
                    kind = "client_unavailable"
                elif "permission" in lowered or "权限" in detail:
                    kind = "history_forbidden"
                else:
                    kind = "temporary_dependency_failure"
                raise DChatClientError(kind, detail)
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DChatClientError("unsupported_payload", f"dws 导出不可读：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise DChatClientError("unsupported_payload", "dws 导出缺少 ok=true")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DChatClientError("unsupported_payload", "dws 导出缺少 data")
        return data

    def list_chats(self) -> Sequence[Mapping[str, Any]]:
        data = self._export(["chat", "+dump-chats"])
        chats = data.get("chats")
        if not isinstance(chats, list) or not all(isinstance(item, dict) for item in chats):
            raise DChatClientError("unsupported_payload", "dws 会话目录 chats 非数组")
        return chats

    def dump_messages(
        self, conversation_id: str, from_value: str, to_value: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        data = self._export([
            "message", "+dump-by-chat", "--by-chat-id", conversation_id,
            "--from", from_value, "--to", to_value, "--limit", str(limit),
        ])
        messages = data.get("messages")
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise DChatClientError("unsupported_payload", "dws 消息 messages 非数组")
        return messages
