"""CLI registration for the local LifeOS Web workspace."""

from __future__ import annotations

import argparse
import ipaddress
import webbrowser
from pathlib import Path

from .server import create_server


def _loopback_host(value: str) -> str:
    if value == "localhost":
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host 只能使用 localhost 或回环 IP") from exc
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("Web 工作台只允许监听本机回环地址")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port 必须是整数") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port 必须在 0 到 65535 之间")
    return port


def command_serve(args: argparse.Namespace) -> None:
    server = create_server(args.host, args.port, args.reports_root)
    actual_host, actual_port = server.server_address[:2]
    display_host = "localhost" if actual_host in {"127.0.0.1", "::1"} else actual_host
    url = f"http://{display_host}:{actual_port}/"
    print(f"LifeOS Web · {url}", flush=True)
    print("只读本地视图；按 Ctrl+C 停止。", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def register_web_parser(domains: argparse._SubParsersAction, data_dir: Path) -> None:
    web = domains.add_parser(
        "web",
        help="启动只读本地 Web 工作台",
        description=(
            "在本机回环地址启动只读 Web 工作台，展示工作、日报、闪念与成果。"
            "它不提供 Agent、编辑或状态流转能力。"
        ),
        epilog=(
            "服务不接受局域网或公网绑定；打开原文只作用于 Reports Runtime 中的真实日报。"
        ),
    )
    commands = web.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本地只读服务")
    serve.add_argument(
        "--host",
        type=_loopback_host,
        default="127.0.0.1",
        help="监听地址，默认 127.0.0.1；只允许回环地址",
    )
    serve.add_argument(
        "--port",
        type=_port,
        default=8787,
        help="监听端口，默认 8787；0 表示由系统分配",
    )
    serve.add_argument(
        "--open",
        action="store_true",
        help="服务启动后用默认浏览器打开页面",
    )
    serve.set_defaults(handler=command_serve, reports_root=data_dir / "reports")


__all__ = ["register_web_parser"]
