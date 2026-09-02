"""Stable public identity for dynamically aggregated Session activities."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Iterable


ACTIVITY_ID = re.compile(r"^ACT-[A-Z2-7]{24}$")
LEGACY_ACTIVITY_ID = re.compile(r"^ACT-([0-9a-f]{64})$")


def _encode_digest(digest: bytes) -> str:
    """Encode the first 120 digest bits as a compact, padding-free Base32 token."""

    return base64.b32encode(digest[:15]).decode("ascii").rstrip("=")


def activity_id(*parts: str) -> str:
    """Build the stable public ID for one Activity aggregation basis."""

    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).digest()
    return f"ACT-{_encode_digest(digest)}"


def migrate_legacy_activity_id(value: str) -> str:
    """Convert one legacy full-digest Activity ID without reading Session content."""

    matched = LEGACY_ACTIVITY_ID.fullmatch(value)
    if not matched:
        raise ValueError(f"不是旧版 Activity ID：{value}")
    return f"ACT-{_encode_digest(bytes.fromhex(matched.group(1)))}"


def ensure_unique_activity_ids(values: Iterable[str]) -> None:
    """Reject duplicate compact IDs before callers publish an Activity collection."""

    seen = set()
    for value in values:
        if value in seen:
            raise ValueError(f"Activity ID 碰撞：{value}")
        seen.add(value)
