"""Loopback-only HTTP transport for the LifeOS read-only workspace."""

from __future__ import annotations

import json
import ipaddress
import socket
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from lifeos_reports.store import ReportError
from lifeos_work.runtime import read_current_data, read_events

from .projection import build_snapshot, report_detail, resolve_openable_report


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/lifeos-logo.svg": ("lifeos-logo.svg", "image/svg+xml"),
    "/assets/fonts/Geist-Regular.ttf": ("fonts/Geist-Regular.ttf", "font/ttf"),
    "/assets/fonts/Geist-SemiBold.ttf": ("fonts/Geist-SemiBold.ttf", "font/ttf"),
    "/assets/fonts/GeistMono-Regular.ttf": ("fonts/GeistMono-Regular.ttf", "font/ttf"),
    "/assets/fonts/GeistMono-Medium.ttf": ("fonts/GeistMono-Medium.ttf", "font/ttf"),
}


def _static_bytes(name: str) -> bytes:
    return resources.files("lifeos_web").joinpath("static", *Path(name).parts).read_bytes()


class LifeOSWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        reports_root: Path,
        *,
        current_data_reader: Callable[[], tuple[dict[str, Any], ...]] = read_current_data,
        events_reader: Callable[[], list[dict[str, Any]]] = read_events,
        opener: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.reports_root = reports_root
        self.current_data_reader = current_data_reader
        self.events_reader = events_reader
        self.opener = opener
        super().__init__(server_address, LifeOSRequestHandler)


class LifeOSIPv6WebServer(LifeOSWebServer):
    address_family = socket.AF_INET6


class LifeOSRequestHandler(BaseHTTPRequestHandler):
    server: LifeOSWebServer

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _valid_host(self) -> bool:
        raw = self.headers.get("Host", "").strip().lower()
        if raw.startswith("[") and "]" in raw:
            host = raw[1:raw.index("]")]
        else:
            host = raw.split(":", 1)[0]
        return host in {"localhost", "127.0.0.1", "::1"}

    def do_GET(self) -> None:
        if not self._valid_host():
            self._error(HTTPStatus.BAD_REQUEST, "只接受本机 Host")
            return
        path = urlparse(self.path).path
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            self._send(HTTPStatus.OK, _static_bytes(name), content_type)
            return
        if path == "/api/snapshot":
            try:
                payload = build_snapshot(
                    self.server.current_data_reader(),
                    self.server.reports_root,
                    self.server.events_reader(),
                )
            except SystemExit as exc:
                self._error(HTTPStatus.CONFLICT, f"Work Runtime 无法读取（退出码 {exc.code}）")
                return
            except (OSError, ValueError):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "LifeOS Runtime 无法读取")
                return
            self._json(HTTPStatus.OK, payload)
            return
        prefix = "/api/reports/"
        if path.startswith(prefix):
            day_text = unquote(path[len(prefix):])
            if "/" in day_text or not day_text:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            try:
                payload = report_detail(self.server.reports_root, day_text)
            except (ValueError, ReportError):
                self._error(HTTPStatus.NOT_FOUND, "日报不存在或无法读取")
                return
            self._json(HTTPStatus.OK, payload)
            return
        self._error(HTTPStatus.NOT_FOUND, "接口不存在")

    def do_POST(self) -> None:
        if not self._valid_host():
            self._error(HTTPStatus.BAD_REQUEST, "只接受本机 Host")
            return
        path = urlparse(self.path).path
        prefix = "/api/reports/"
        suffix = "/open"
        if not path.startswith(prefix) or not path.endswith(suffix):
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if self.headers.get("X-LifeOS-Intent") != "open-report":
            self._error(HTTPStatus.FORBIDDEN, "缺少打开日报的显式意图")
            return
        day_text = unquote(path[len(prefix):-len(suffix)]).strip("/")
        try:
            report_path = resolve_openable_report(self.server.reports_root, day_text)
            self.server.opener(
                ["open", str(report_path)],
                check=True,
                timeout=5,
            )
        except (ValueError, ReportError):
            self._error(HTTPStatus.NOT_FOUND, "日报不存在或无法打开")
            return
        except (OSError, subprocess.SubprocessError):
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "无法使用系统默认应用打开日报")
            return
        self._json(HTTPStatus.OK, {"opened": True, "day": day_text})


def create_server(
    host: str,
    port: int,
    reports_root: Path,
    **dependencies: Any,
) -> LifeOSWebServer:
    server_type = LifeOSWebServer
    if host != "localhost" and ipaddress.ip_address(host).version == 6:
        server_type = LifeOSIPv6WebServer
    return server_type((host, port), reports_root, **dependencies)
