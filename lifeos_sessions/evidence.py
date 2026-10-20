"""Source-bound, hash-checked reads of records retained by session adapters."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from .core import SessionError


MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_PAGE_CHARS = 32_000


def record_locator(locator: str, raw: bytes, offset: int) -> dict[str, Any]:
    return {"locator": locator, "offset": offset, "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def read_evidence(
    item: Mapping[str, Any], ref: str, root: Path, *, offset: int = 0, limit: int = 8_000,
) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= MAX_PAGE_CHARS:
        raise SessionError(f"invalid evidence page: offset >= 0, limit 1..{MAX_PAGE_CHARS}")
    entries = (item.get("source_meta") or {}).get("evidence_records") or []
    matches = [entry for entry in entries if isinstance(entry, Mapping) and entry.get("ref") == ref]
    if len(matches) != 1:
        raise SessionError("evidence reference unavailable or ambiguous; rescan this source window")
    entry = matches[0]
    relative = Path(str(entry.get("locator") or ""))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SessionError("invalid evidence source path")
    if item.get("source") == "codex" and relative.parts[0] not in {"sessions", "archived_sessions"}:
        raise SessionError("evidence path is outside Codex session directories")
    if not (relative.name.endswith(".jsonl") or relative.name == "session.jsonl.zstd"):
        raise SessionError("unsupported evidence source file")
    try:
        root = root.resolve(strict=True)
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
        start, length = entry["offset"], entry["length"]
        if type(start) is not int or type(length) is not int or start < 0 or not 0 < length <= MAX_RECORD_BYTES:
            raise ValueError("invalid record range")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("source is not a regular file")
            if path.name.endswith(".zstd"):
                # Same installed decompressor as the DeepSeek adapter; stream
                # only as far as this bounded record, never materialize the file.
                import subprocess
                process = subprocess.Popen(["zstd", "-dc"], stdin=handle, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
                try:
                    assert process.stdout is not None
                    remaining = start
                    while remaining:
                        chunk = process.stdout.read(min(remaining, 64 * 1024))
                        if not chunk:
                            raise ValueError("source ended before reference")
                        remaining -= len(chunk)
                    raw = process.stdout.read(length)
                finally:
                    if process.stdout:
                        process.stdout.close()
                    if process.poll() is None:
                        process.terminate()
                    process.wait()
            else:
                handle.seek(start)
                raw = handle.read(length)
        if len(raw) != length or hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise SessionError("evidence source changed since scan; rescan before using it")
        value = json.loads(raw)
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except SessionError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SessionError(f"evidence source unavailable: {exc}") from exc
    end = min(len(text), offset + limit)
    return {"slice_id": item["slice_id"], "revision": item.get("revision"), "ref": ref,
            "source_verified": True, "offset": offset, "total_chars": len(text),
            "truncated": end < len(text), "next_offset": end if end < len(text) else None,
            "text": text[offset:end]}
