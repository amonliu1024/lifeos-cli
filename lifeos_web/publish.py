"""Render the read-only workspace as static files for a mirror host.

The published site answers the same ``/api/snapshot`` and ``/api/reports/<day>``
paths as the loopback server, so the unchanged front end runs on any static
file host. Project names are resolved here, where the Project Catalog exists;
the host never needs project manifests or a LifeOS install.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lifeos_work.runtime import read_current_data, read_events

from .projection import build_snapshot, report_detail
from .server import STATIC_FILES, _static_bytes


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_site(destination: Path, reports_root: Path) -> int:
    """Write the static workspace into ``destination``; return published report count."""

    snapshot = build_snapshot(read_current_data(), reports_root, read_events())
    snapshot["published"] = True
    for route, (name, _content_type) in STATIC_FILES.items():
        target = destination / ("index.html" if route == "/" else route.lstrip("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_static_bytes(name))
    _write_json(destination / "api" / "snapshot", snapshot)
    published = 0
    for report in snapshot["reports"]:
        if not report.get("readable"):
            continue
        _write_json(
            destination / "api" / "reports" / report["day"],
            report_detail(reports_root, report["day"]),
        )
        published += 1
    return published


__all__ = ["write_site"]
