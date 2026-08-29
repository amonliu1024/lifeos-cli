"""Declared retention policy for the private sessions store.

Growth is not a bug to be optimised away once; it is a property of keeping
every turn of every Agent session forever.  The mechanism therefore has to be
a *declared bound* the person chooses, plus honest reporting of what that
bound costs and what it has already removed.

Two separate things can be kept for different lengths of time:

``fts_days``
    How long full-text search covers.  Trigram indexing costs roughly 3.5x
    the text it indexes, so a shorter search window is the cheapest large
    saving that loses no evidence at all.

``keep_slices_days``
    How long the immutable revision JSON -- the evidence itself -- is kept.
    Dropping it is the only thing that frees the bulk of the disk, and it is
    irreversible once the source application has rotated its own logs.

What a prune never removes is the knowledge that something was there: each
pruned source-day leaves a tombstone, so a later query over that window
reports it as pruned instead of quiet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

CONFIG_NAME = "retention.json"
SCHEMA_VERSION = 1

# Defaults keep everything.  A bound has to be chosen deliberately, because
# pruning destroys evidence that the source application may also have rotated.
DEFAULT_POLICY: Dict[str, Optional[int]] = {
    "keep_slices_days": None,
    "fts_days": None,
}

_FIELDS = tuple(DEFAULT_POLICY)


@dataclass(frozen=True)
class RetentionPolicy:
    """A declared bound.  ``None`` means "keep indefinitely"."""

    keep_slices_days: Optional[int] = None
    fts_days: Optional[int] = None
    source_path: Optional[Path] = None

    def __post_init__(self) -> None:
        for name in _FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer number of days or null")
        # Searching text that has already been deleted is not possible, so a
        # search window wider than the evidence window is a contradiction.
        if self.fts_days is not None and self.keep_slices_days is not None:
            if self.fts_days > self.keep_slices_days:
                raise ValueError("fts_days must not exceed keep_slices_days")

    @property
    def bounded(self) -> bool:
        return any(getattr(self, name) is not None for name in _FIELDS)

    @classmethod
    def load(cls, root: os.PathLike[str] | str) -> "RetentionPolicy":
        path = Path(root).expanduser() / CONFIG_NAME
        if not path.exists():
            return cls(source_path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"retention policy is unreadable: {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"retention policy must be an object: {path}")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"retention policy schema_version must be {SCHEMA_VERSION}: {path}")
        unknown = sorted(set(payload) - set(_FIELDS) - {"schema_version"})
        if unknown:
            raise ValueError(f"retention policy has unknown fields: {', '.join(unknown)}")
        values = {name: payload.get(name, DEFAULT_POLICY[name]) for name in _FIELDS}
        return cls(source_path=path, **values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            **{name: getattr(self, name) for name in _FIELDS},
        }

    def cutoff_ms(self, name: str, now_ms: int) -> Optional[int]:
        """Return the epoch millisecond boundary for one retention field."""

        days = getattr(self, name)
        if days is None:
            return None
        return int(now_ms) - int(days) * 86_400_000
