"""Source-neutral models and the deterministic sessions scan service.

Only stable, public session concepts live here.  A source adapter is allowed
to know how to read its own records, but it returns a ``ConversationSlice``
through the small :class:`AdapterResult` seam defined in this module.  The
module intentionally contains no filesystem discovery and never reads the
user's real runtime.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Protocol, Sequence


UTC = timezone.utc
SCHEMA_VERSION = 1
# Shared extraction and judgment changes invalidate every source checkpoint.
# Source-specific parser changes continue to use each adapter's own revision.
SHARED_EXTRACTION_REVISION = 1
# What a slice may claim about itself.  These names describe what the value
# proves, not where it came from; see ``semantics`` for the classification
# rules the adapters apply when producing them.
ALLOWED_COMPLETENESS = frozenset({"complete", "partial", "truncated"})
ALLOWED_TURN_COMPLETION = frozenset({"completed", "incomplete", "interrupted_with_result"})
ALLOWED_BLOCK_KINDS = frozenset({"message", "agent_message", "delegation"})
ALLOWED_BLOCK_ORIGINS = frozenset({"user", "agent", "system_injected"})
EXECUTION_EVIDENCE_LISTS = (
    "changed_targets",
    "other_targets",
    "tool_calls",
    "verifications",
    "failures",
    "user_interrupts",
)
ALLOWED_DELEGATION_KEYS = frozenset({
    "agent_id", "agent_path", "thread_id", "child_thread_id", "turn_id",
    "task", "result", "status", "source_refs", "warnings",
})
REQUIRED_DELEGATION_KEYS = frozenset({"agent_id", "status", "task", "result"})
ALLOWED_STATUSES = frozenset({"complete", "partial", "failed"})
MAX_BLOCKS = 500
MAX_BLOCK_TEXT = 200_000
MAX_WARNING_LENGTH = 2_000
MAX_SUMMARY_ITEMS = 100
MAX_SUMMARY_ITEM_LENGTH = 4_096
MAX_DELEGATIONS = 100
OMISSION_REASON_EXPLICIT_ABORT = "explicit_abort_without_work"


def adapter_cache_generation(
    adapter_name: str,
    adapter_revision: str,
    *,
    shared_revision: int = SHARED_EXTRACTION_REVISION,
    slice_schema_version: int = SCHEMA_VERSION,
) -> str:
    """Return the checkpoint generation for one source adapter."""

    payload = {
        "adapter": str(adapter_name),
        "adapter_revision": str(adapter_revision),
        "shared_extraction_revision": int(shared_revision),
        "slice_schema_version": int(slice_schema_version),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class SessionError(Exception):
    """Base error for the source-neutral sessions layer."""


class SessionValidationError(SessionError, ValueError):
    """Raised when an object cannot satisfy the public sessions schema."""

    def __init__(self, errors: Sequence[str] | str):
        if isinstance(errors, str):
            self.errors = (errors,)
        else:
            self.errors = tuple(str(error) for error in errors)
        super().__init__("; ".join(self.errors) or "invalid sessions object")


def parse_iso_datetime(value: str, *, field_name: str = "time") -> datetime:
    """Parse an ISO-8601 timestamp and require an explicit timezone.

    ``datetime.fromisoformat`` accepts both ``+00:00`` and (on newer Python
    versions) ``Z``.  Normalising ``Z`` ourselves keeps this code compatible
    with the Python 3.9 runtime used by the CLI.
    """

    if not isinstance(value, str) or not value.strip():
        raise SessionValidationError(f"{field_name} must be a non-empty ISO timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise SessionValidationError(f"{field_name} is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SessionValidationError(f"{field_name} must include a timezone offset")
    return parsed.astimezone(UTC)


def _iso_millis(value: datetime) -> str:
    value = value.astimezone(UTC)
    # Indexing is epoch milliseconds.  A canonical representation avoids a
    # new revision merely because a source used +00:00 rather than Z.
    text = value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if text.endswith(".000Z"):
        text = text[:-5] + "Z"
    return text


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _epoch_from_iso(value: str) -> int:
    return _epoch_ms(parse_iso_datetime(value, field_name="slice time"))


def _canonicalise(value: Any) -> Any:
    """Return JSON-compatible values with deterministic dictionary ordering."""

    if isinstance(value, Mapping):
        return {str(key): _canonicalise(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_canonicalise(item) for item in value]
    if isinstance(value, datetime):
        return _iso_millis(value)
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    """Serialise a public object using the revision canonicalisation rules."""

    return json.dumps(
        _canonicalise(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_slice_id(
    source: str,
    conversation_id: str,
    native_unit_kind: str,
    native_unit_id: str,
) -> str:
    """Build the source-independent stable identity for one native unit."""

    pieces = (source, conversation_id, native_unit_kind, native_unit_id)
    if any(not isinstance(piece, str) or not piece for piece in pieces):
        raise SessionValidationError("source, conversation id, native unit kind and id are required")
    digest = hashlib.sha256("".join(pieces).encode("utf-8")).hexdigest()
    return f"SLC-{digest}"


def _without_revision_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("revision", None)
    payload.pop("materialized_at", None)
    return payload


def canonical_revision(value: Mapping[str, Any] | "ConversationSlice") -> str:
    """Hash a slice's canonical JSON, excluding mutable materialisation fields."""

    if isinstance(value, ConversationSlice):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("canonical_revision expects a mapping or ConversationSlice")
    digest = hashlib.sha256(canonical_json(_without_revision_fields(payload)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _copy_json(value: Any) -> Any:
    # Round-tripping through JSON catches accidental mutable source objects and
    # gives dataclass instances ordinary dictionaries/lists.
    return json.loads(json.dumps(value, ensure_ascii=False))


@dataclass(frozen=True)
class TimeWindow:
    """A timezone-normalised half-open ``[from, to)`` window."""

    from_ms: int
    to_ms: int
    from_iso: str
    to_iso: str

    def __post_init__(self) -> None:
        try:
            from_ms = int(self.from_ms)
            to_ms = int(self.to_ms)
        except (TypeError, ValueError) as exc:
            raise SessionValidationError("window epoch values must be integers") from exc
        if from_ms != self.from_ms or to_ms != self.to_ms:
            raise SessionValidationError("window epoch values must be integer milliseconds")
        from_dt = parse_iso_datetime(self.from_iso, field_name="from")
        to_dt = parse_iso_datetime(self.to_iso, field_name="to")
        derived_from = _epoch_ms(from_dt)
        derived_to = _epoch_ms(to_dt)
        if from_ms != derived_from or to_ms != derived_to:
            raise SessionValidationError("window ISO and epoch values disagree")
        if from_ms >= to_ms:
            raise SessionValidationError("window requires from < to")
        object.__setattr__(self, "from_iso", _iso_millis(from_dt))
        object.__setattr__(self, "to_iso", _iso_millis(to_dt))

    @classmethod
    def from_values(cls, from_value: str | datetime, to_value: str | datetime) -> "TimeWindow":
        from_dt = from_value if isinstance(from_value, datetime) else parse_iso_datetime(str(from_value), field_name="from")
        to_dt = to_value if isinstance(to_value, datetime) else parse_iso_datetime(str(to_value), field_name="to")
        if from_dt.tzinfo is None or from_dt.utcoffset() is None:
            raise SessionValidationError("from must include a timezone offset")
        if to_dt.tzinfo is None or to_dt.utcoffset() is None:
            raise SessionValidationError("to must include a timezone offset")
        from_dt = from_dt.astimezone(UTC)
        to_dt = to_dt.astimezone(UTC)
        return cls(_epoch_ms(from_dt), _epoch_ms(to_dt), _iso_millis(from_dt), _iso_millis(to_dt))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TimeWindow":
        if {"from_ms", "to_ms", "from_iso", "to_iso"}.issubset(value):
            return cls(int(value["from_ms"]), int(value["to_ms"]), str(value["from_iso"]), str(value["to_iso"]))
        if "from" in value and "to" in value:
            return cls.from_values(value["from"], value["to"])
        raise SessionValidationError("window requires from/to ISO values or all four canonical fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_ms": self.from_ms,
            "to_ms": self.to_ms,
            "from_iso": self.from_iso,
            "to_iso": self.to_iso,
        }

    def contains(self, timestamp_ms: int) -> bool:
        return self.from_ms <= int(timestamp_ms) < self.to_ms

    def overlaps(self, started_ms: int, ended_ms: int) -> bool:
        # A slice's end is inclusive for query overlap.  The scan window is
        # half-open and therefore uses the strict comparison on its upper end.
        return int(started_ms) < self.to_ms and int(ended_ms) >= self.from_ms


@dataclass(frozen=True)
class SourceScanRequest:
    """Input shared by all source adapters."""

    window: TimeWindow
    includes: tuple[str, ...] = field(default_factory=tuple)
    checkpoint: Optional[Mapping[str, Any]] = None
    temp_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if not isinstance(self.window, TimeWindow):
            if isinstance(self.window, Mapping):
                object.__setattr__(self, "window", TimeWindow.from_mapping(self.window))
            else:
                raise SessionValidationError("request.window must be TimeWindow")
        includes = tuple(str(value) for value in (self.includes or ()) if str(value))
        for value in includes:
            source, separator, conversation_id = value.partition(":")
            if not separator or not source or not conversation_id:
                raise SessionValidationError("request.includes must use source:conversation-id")
        object.__setattr__(self, "includes", includes)
        if self.checkpoint is not None and not isinstance(self.checkpoint, Mapping):
            raise SessionValidationError("request.checkpoint must be an object or null")
        if self.checkpoint is not None:
            object.__setattr__(self, "checkpoint", _copy_json(self.checkpoint))
        if self.temp_dir is not None:
            object.__setattr__(self, "temp_dir", Path(self.temp_dir))

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.to_dict(),
            "includes": list(self.includes),
            "checkpoint": _copy_json(self.checkpoint) if self.checkpoint is not None else None,
            "temp_dir": str(self.temp_dir) if self.temp_dir is not None else None,
        }


@dataclass
class ConversationSlice:
    """The source-neutral immutable revision payload.

    ``slice_id`` and ``revision`` are intentionally optional while an adapter
    is assembling a candidate.  :meth:`finalize` fills and verifies them
    before the candidate enters the private store.
    """

    source: str
    conversation: Mapping[str, Any]
    native_unit: Mapping[str, Any]
    started_at: str
    ended_at: str
    blocks: list[Mapping[str, Any]]
    workspace: Optional[str] = None
    execution_evidence: Mapping[str, Any] = field(default_factory=dict)
    delegations: list[Mapping[str, Any]] = field(default_factory=list)
    content_completeness: str = "complete"
    # Lifecycle is independent from content completeness.  A turn may have
    # complete captured content and still be an interrupted attempt, while a
    # source with a missing terminal record remains incomplete.
    turn_completion: str = "completed"
    provenance_trimmed: bool = False
    quality_flags: list[str] = field(default_factory=list)
    omissions: list[str] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    source_meta: Mapping[str, Any] = field(default_factory=dict)
    adapter: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    slice_id: Optional[str] = None
    revision: Optional[str] = None
    materialized_at: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.conversation, Mapping):
            self.conversation = _copy_json(self.conversation)
        if isinstance(self.native_unit, Mapping):
            self.native_unit = _copy_json(self.native_unit)
        self.blocks = [_copy_json(block) for block in (self.blocks or [])]
        self.execution_evidence = _copy_json(self.execution_evidence or {})
        self.provenance_trimmed = bool(self.provenance_trimmed)
        self.delegations = [_copy_json(item) for item in (self.delegations or [])]
        self.quality_flags = [str(item) for item in (self.quality_flags or [])]
        self.omissions = [str(item) for item in (self.omissions or [])]
        self.warnings = [_copy_json(item) for item in (self.warnings or [])]
        self.source_refs = [str(item) for item in (self.source_refs or [])]
        self.source_meta = _copy_json(self.source_meta or {})
        self.adapter = _copy_json(self.adapter or {})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "ConversationSlice") -> "ConversationSlice":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SessionValidationError("slice must be an object")
        known = {
            "source", "conversation", "native_unit", "started_at", "ended_at", "workspace",
            "blocks", "execution_evidence", "delegations", "content_completeness",
            "turn_completion",
            "provenance_trimmed", "quality_flags", "omissions",
            "warnings", "source_refs",
            "source_meta", "adapter", "schema_version", "slice_id", "revision", "materialized_at",
        }
        missing = sorted({
            "schema_version", "source", "conversation", "native_unit",
            "started_at", "ended_at", "blocks",
        } - set(value))
        if missing:
            raise SessionValidationError(f"slice missing required fields: {', '.join(missing)}")
        payload = {key: value[key] for key in known if key in value}
        return cls(**payload)

    @property
    def conversation_id(self) -> Optional[str]:
        value = self.conversation.get("id") if isinstance(self.conversation, Mapping) else None
        return str(value) if value is not None else None

    @property
    def native_unit_kind(self) -> Optional[str]:
        value = self.native_unit.get("kind") if isinstance(self.native_unit, Mapping) else None
        return str(value) if value is not None else None

    @property
    def native_unit_id(self) -> Optional[str]:
        value = self.native_unit.get("id") if isinstance(self.native_unit, Mapping) else None
        return str(value) if value is not None else None

    def to_dict(self, *, include_mutable: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "slice_id": self.slice_id,
            "revision": self.revision,
            "source": self.source,
            "conversation": _copy_json(self.conversation),
            "native_unit": _copy_json(self.native_unit),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "workspace": self.workspace,
            "blocks": _copy_json(self.blocks),
            "execution_evidence": _copy_json(self.execution_evidence),
            "delegations": _copy_json(self.delegations),
            "content_completeness": self.content_completeness,
            "turn_completion": self.turn_completion,
            "provenance_trimmed": self.provenance_trimmed,
            "quality_flags": list(self.quality_flags),
            "omissions": list(self.omissions),
            "warnings": _copy_json(self.warnings),
            "source_refs": list(self.source_refs),
            "source_meta": _copy_json(self.source_meta),
            "adapter": _copy_json(self.adapter),
            "materialized_at": self.materialized_at,
        }
        if not include_mutable:
            payload.pop("revision", None)
            payload.pop("materialized_at", None)
        return payload

    def validate(self, *, require_identity: bool = False) -> list[str]:
        return validate_slice(self, require_identity=require_identity)

    def finalize(self, *, materialized_at: Optional[str] = None) -> "ConversationSlice":
        errors = validate_slice(self, require_identity=False)
        if errors:
            raise SessionValidationError(errors)
        computed_id = stable_slice_id(
            self.source,
            self.conversation_id or "",
            self.native_unit_kind or "",
            self.native_unit_id or "",
        )
        if self.slice_id is not None and self.slice_id != computed_id:
            raise SessionValidationError("slice_id does not match stable native identity")
        candidate = ConversationSlice.from_dict(self.to_dict())
        candidate.slice_id = computed_id
        if materialized_at is not None:
            candidate.materialized_at = _iso_millis(parse_iso_datetime(materialized_at, field_name="materialized_at"))
        elif candidate.materialized_at is None:
            candidate.materialized_at = _iso_millis(datetime.now(UTC))
        computed_revision = canonical_revision(candidate)
        if self.revision is not None and self.revision != computed_revision:
            raise SessionValidationError("revision does not match canonical slice content")
        candidate.revision = computed_revision
        return candidate


def _validate_mapping(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{label} must be an object")


def _validate_list(value: Any, label: str, errors: list[str]) -> bool:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return False
    return True


def _validate_bounded_text(value: Any, label: str, errors: list[str], limit: int = MAX_SUMMARY_ITEM_LENGTH) -> None:
    if not isinstance(value, str):
        errors.append(f"{label} must be text")
    elif len(value) > limit:
        errors.append(f"{label} exceeds {limit} characters")


def validate_slice(
    value: Mapping[str, Any] | ConversationSlice,
    *,
    require_identity: bool = False,
) -> list[str]:
    """Return schema and bounds errors without mutating the candidate."""

    if isinstance(value, ConversationSlice):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        return ["slice must be an object"]

    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        errors.append("source is required")
    conversation = payload.get("conversation")
    native_unit = payload.get("native_unit")
    _validate_mapping(conversation, "conversation", errors)
    _validate_mapping(native_unit, "native_unit", errors)
    conversation_id = conversation.get("id") if isinstance(conversation, Mapping) else None
    native_kind = native_unit.get("kind") if isinstance(native_unit, Mapping) else None
    native_id = native_unit.get("id") if isinstance(native_unit, Mapping) else None
    if not isinstance(conversation_id, str) or not conversation_id:
        errors.append("conversation.id is required")
    if not isinstance(native_kind, str) or not native_kind:
        errors.append("native_unit.kind is required")
    elif native_kind != "turn":
        errors.append("native_unit.kind must be turn")
    if not isinstance(native_id, str) or not native_id:
        errors.append("native_unit.id is required")

    started_dt: Optional[datetime] = None
    ended_dt: Optional[datetime] = None
    try:
        started_dt = parse_iso_datetime(str(payload.get("started_at", "")), field_name="started_at")
    except SessionValidationError as exc:
        errors.extend(exc.errors)
    try:
        ended_dt = parse_iso_datetime(str(payload.get("ended_at", "")), field_name="ended_at")
    except SessionValidationError as exc:
        errors.extend(exc.errors)
    # A turn may have only one retained source timestamp, so a zero-duration
    # ``started_at == ended_at`` interval is valid. Only inverted bounds fail.
    if started_dt is not None and ended_dt is not None and started_dt > ended_dt:
        errors.append("started_at must not be after ended_at")

    blocks = payload.get("blocks")
    if _validate_list(blocks, "blocks", errors):
        if len(blocks) > MAX_BLOCKS:
            errors.append(f"blocks exceeds {MAX_BLOCKS} items")
        for index, block in enumerate(blocks):
            label = f"blocks[{index}]"
            if not isinstance(block, Mapping):
                errors.append(f"{label} must be an object")
                continue
            if block.get("kind") not in ALLOWED_BLOCK_KINDS:
                errors.append(f"{label}.kind must be one of {', '.join(sorted(ALLOWED_BLOCK_KINDS))}")
            if not isinstance(block.get("author_role"), str) or not block.get("author_role"):
                errors.append(f"{label}.author_role is required")
            # ``author_role`` is the transport role.  ``origin`` is who really
            # produced the text, which is the only one a reader may quote.
            if block.get("origin") not in ALLOWED_BLOCK_ORIGINS:
                errors.append(f"{label}.origin must be one of {', '.join(sorted(ALLOWED_BLOCK_ORIGINS))}")
            try:
                block_time = parse_iso_datetime(str(block.get("at", "")), field_name=f"{label}.at")
            except SessionValidationError as exc:
                errors.extend(exc.errors)
                block_time = None
            if block_time is not None and started_dt is not None and ended_dt is not None:
                if not bool(block.get("context")) and not (started_dt <= block_time <= ended_dt):
                    errors.append(f"{label}.at is outside slice bounds")
            text = block.get("text")
            if text is not None:
                _validate_bounded_text(text, f"{label}.text", errors, MAX_BLOCK_TEXT)
            if "source_refs" in block:
                refs = block.get("source_refs")
                if _validate_list(refs, f"{label}.source_refs", errors):
                    if any(not isinstance(ref, str) for ref in refs):
                        errors.append(f"{label}.source_refs must contain text")

    evidence = payload.get("execution_evidence", {})
    if not isinstance(evidence, Mapping):
        errors.append("execution_evidence must be an object")
    else:
        for key in EXECUTION_EVIDENCE_LISTS:
            values = evidence.get(key, [])
            if _validate_list(values, f"execution_evidence.{key}", errors):
                if len(values) > MAX_SUMMARY_ITEMS:
                    errors.append(f"execution_evidence.{key} exceeds {MAX_SUMMARY_ITEMS} items")
                for index, item in enumerate(values):
                    _validate_bounded_text(item, f"execution_evidence.{key}[{index}]", errors)
        omitted_count = evidence.get("omitted_count", 0)
        if not isinstance(omitted_count, int) or omitted_count < 0:
            errors.append("execution_evidence.omitted_count must be a non-negative integer")

    delegations = payload.get("delegations", [])
    if _validate_list(delegations, "delegations", errors):
        if len(delegations) > MAX_DELEGATIONS:
            errors.append(f"delegations exceeds {MAX_DELEGATIONS} items")
        for index, delegation in enumerate(delegations):
            if not isinstance(delegation, Mapping):
                errors.append(f"delegations[{index}] must be an object")
                continue
            extra = set(delegation) - ALLOWED_DELEGATION_KEYS
            if extra:
                errors.append(
                    f"delegations[{index}] has unsupported keys: {', '.join(sorted(extra))}"
                )
            missing = REQUIRED_DELEGATION_KEYS - set(delegation)
            if missing:
                errors.append(
                    f"delegations[{index}] is missing required keys: {', '.join(sorted(missing))}"
                )

    completeness = payload.get("content_completeness", "complete")
    if completeness not in ALLOWED_COMPLETENESS:
        errors.append("content_completeness must be complete, partial or truncated")
    turn_completion = payload.get("turn_completion")
    if turn_completion not in ALLOWED_TURN_COMPLETION:
        errors.append("turn_completion must be completed, incomplete or interrupted_with_result")
    if not isinstance(payload.get("provenance_trimmed", False), bool):
        errors.append("provenance_trimmed must be a boolean")
    for field_name in ("quality_flags", "omissions"):
        values = payload.get(field_name, [])
        if _validate_list(values, field_name, errors):
            if len(values) > MAX_SUMMARY_ITEMS:
                errors.append(f"{field_name} exceeds {MAX_SUMMARY_ITEMS} items")
            for index, item in enumerate(values):
                _validate_bounded_text(item, f"{field_name}[{index}]", errors, MAX_WARNING_LENGTH)
    warnings = payload.get("warnings", [])
    if _validate_list(warnings, "warnings", errors):
        for index, warning in enumerate(warnings):
            if isinstance(warning, str):
                _validate_bounded_text(warning, f"warnings[{index}]", errors, MAX_WARNING_LENGTH)
            elif not isinstance(warning, Mapping):
                errors.append(f"warnings[{index}] must be text or object")
    refs = payload.get("source_refs", [])
    if _validate_list(refs, "source_refs", errors) and any(not isinstance(ref, str) for ref in refs):
        errors.append("source_refs must contain text")
    _validate_mapping(payload.get("source_meta", {}), "source_meta", errors)
    _validate_mapping(payload.get("adapter", {}), "adapter", errors)

    if require_identity or payload.get("slice_id") is not None:
        if not isinstance(payload.get("slice_id"), str) or not payload.get("slice_id"):
            errors.append("slice_id is required")
        elif isinstance(source, str) and isinstance(conversation_id, str) and isinstance(native_kind, str) and isinstance(native_id, str):
            expected = stable_slice_id(source, conversation_id, native_kind, native_id)
            if payload["slice_id"] != expected:
                errors.append("slice_id does not match stable native identity")
    if require_identity or payload.get("revision") is not None:
        if not isinstance(payload.get("revision"), str) or not payload.get("revision"):
            errors.append("revision is required")
        else:
            expected_revision = canonical_revision(payload)
            if payload["revision"] != expected_revision:
                errors.append("revision does not match canonical content")
    return errors


class SourceAdapter(Protocol):
    name: str
    adapter_version: str

    def scan(self, request: SourceScanRequest) -> AdapterResult:
        ...


@dataclass
class TurnOmission:
    """A stable, contentless record for an explicitly aborted empty turn.

    Omission rows intentionally contain no blocks, workspace or title.  They
    are useful for audit/rebuild accounting but are never inserted into FTS.
    """

    source: str
    conversation: Mapping[str, Any]
    native_unit: Mapping[str, Any]
    at: str
    reason: str = OMISSION_REASON_EXPLICIT_ABORT
    source_ref: Optional[str] = None
    adapter: Mapping[str, Any] = field(default_factory=dict)
    omission_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise SessionValidationError("omission source is required")
        self.conversation = _copy_json(self.conversation or {})
        self.native_unit = _copy_json(self.native_unit or {})
        self.adapter = _copy_json(self.adapter or {})
        self.source_ref = str(self.source_ref) if self.source_ref is not None else None

    @property
    def conversation_id(self) -> Optional[str]:
        value = self.conversation.get("id") if isinstance(self.conversation, Mapping) else None
        return str(value) if value is not None else None

    @property
    def native_unit_id(self) -> Optional[str]:
        value = self.native_unit.get("id") if isinstance(self.native_unit, Mapping) else None
        return str(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "omission_id": self.omission_id,
            "source": self.source,
            "conversation": _copy_json(self.conversation),
            "native_unit": _copy_json(self.native_unit),
            "at": self.at,
            "reason": self.reason,
            "source_ref": self.source_ref,
            "adapter": _copy_json(self.adapter),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | "TurnOmission") -> "TurnOmission":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SessionValidationError("omission must be an object")
        required = {"source", "conversation", "native_unit", "at", "reason", "adapter"}
        missing = sorted(required - set(value))
        if missing:
            raise SessionValidationError(f"omission missing required fields: {', '.join(missing)}")
        return cls(**{key: value[key] for key in {
            "source", "conversation", "native_unit", "at", "reason", "source_ref", "adapter", "omission_id"
        } if key in value})

    def validate(self, *, require_identity: bool = False) -> list[str]:
        errors: list[str] = []
        if not isinstance(self.conversation, Mapping) or not self.conversation.get("id"):
            errors.append("omission.conversation.id is required")
        if not isinstance(self.native_unit, Mapping) or self.native_unit.get("kind") != "turn":
            errors.append("omission.native_unit.kind must be turn")
        if not isinstance(self.native_unit, Mapping) or not self.native_unit.get("id"):
            errors.append("omission.native_unit.id is required")
        try:
            parse_iso_datetime(self.at, field_name="omission.at")
        except SessionValidationError as exc:
            errors.extend(exc.errors)
        if self.reason != OMISSION_REASON_EXPLICIT_ABORT:
            errors.append(f"omission.reason must be {OMISSION_REASON_EXPLICIT_ABORT}")
        if not isinstance(self.adapter, Mapping) or not self.adapter.get("name"):
            errors.append("omission.adapter.name is required")
        expected = stable_omission_id(
            self.source,
            self.conversation_id or "",
            str(self.native_unit.get("kind") if isinstance(self.native_unit, Mapping) else ""),
            self.native_unit_id or "",
            self.reason,
        )
        if require_identity or self.omission_id is not None:
            if self.omission_id != expected:
                errors.append("omission_id does not match stable identity")
        return errors

    def finalize(self) -> "TurnOmission":
        errors = self.validate()
        if errors:
            raise SessionValidationError(errors)
        candidate = TurnOmission.from_dict(self.to_dict())
        candidate.at = _iso_millis(parse_iso_datetime(candidate.at, field_name="omission.at"))
        candidate.omission_id = stable_omission_id(
            candidate.source,
            candidate.conversation_id or "",
            str(candidate.native_unit.get("kind")),
            candidate.native_unit_id or "",
            candidate.reason,
        )
        if self.omission_id is not None and self.omission_id != candidate.omission_id:
            raise SessionValidationError("omission_id does not match stable identity")
        return candidate


def stable_omission_id(
    source: str,
    conversation_id: str,
    native_unit_kind: str,
    native_unit_id: str,
    reason: str = OMISSION_REASON_EXPLICIT_ABORT,
) -> str:
    pieces = (source, conversation_id, native_unit_kind, native_unit_id, reason)
    if any(not isinstance(piece, str) or not piece for piece in pieces):
        raise SessionValidationError("omission identity fields are required")
    digest = hashlib.sha256("\0".join(pieces).encode("utf-8")).hexdigest()
    return f"OMN-{digest}"


@dataclass
class AdapterResult:
    source: str
    status: str = "complete"
    slices: Sequence[ConversationSlice | Mapping[str, Any]] = field(default_factory=tuple)
    omissions: Sequence[TurnOmission | Mapping[str, Any]] = field(default_factory=tuple)
    reused_slice_ids: Sequence[str] = field(default_factory=tuple)
    reused_omission_ids: Sequence[str] = field(default_factory=tuple)
    checkpoint: Optional[Mapping[str, Any]] = None
    warnings: Sequence[Any] = field(default_factory=tuple)
    error: Optional[Any] = None
    stats: MutableMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise SessionValidationError("adapter result source is required")
        if self.status not in ALLOWED_STATUSES:
            raise SessionValidationError("adapter result status must be complete, partial or failed")
        self.slices = tuple(ConversationSlice.from_dict(item) for item in (self.slices or ()))
        self.omissions = tuple(TurnOmission.from_dict(item) for item in (self.omissions or ()))
        self.reused_slice_ids = tuple(dict.fromkeys(str(item) for item in (self.reused_slice_ids or ()) if str(item)))
        self.reused_omission_ids = tuple(dict.fromkeys(str(item) for item in (self.reused_omission_ids or ()) if str(item)))
        if any(not item.startswith("SLC-") for item in self.reused_slice_ids):
            raise SessionValidationError("reused slice ids must use stable SLC- identities")
        if any(not item.startswith("OMN-") for item in self.reused_omission_ids):
            raise SessionValidationError("reused omission ids must use stable OMN- identities")
        self.warnings = tuple(_copy_json(item) for item in (self.warnings or ()))
        if self.checkpoint is not None:
            if not isinstance(self.checkpoint, Mapping):
                raise SessionValidationError("adapter result checkpoint must be an object or null")
            self.checkpoint = _copy_json(self.checkpoint)
        self.stats = dict(self.stats or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "slices": [item.to_dict() for item in self.slices],
            "omissions": [item.to_dict() for item in self.omissions],
            "reused_slice_ids": list(self.reused_slice_ids),
            "reused_omission_ids": list(self.reused_omission_ids),
            "checkpoint": _copy_json(self.checkpoint) if self.checkpoint is not None else None,
            "warnings": _copy_json(list(self.warnings)),
            "error": _copy_json(self.error) if self.error is not None else None,
            "stats": _copy_json(self.stats),
        }


def _scan_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = hashlib.sha256(f"{stamp}:{id(object())}".encode("ascii")).hexdigest()[:10]
    return f"SCN-{stamp}-{suffix}"


class SessionsService:
    """Sequentially scan selected adapters and commit isolated results."""

    def __init__(self, adapters: Mapping[str, SourceAdapter] | Iterable[SourceAdapter], store: Any = None):
        if isinstance(adapters, Mapping):
            self.adapters = dict(adapters)
        else:
            self.adapters = {}
            for adapter in adapters:
                name = getattr(adapter, "name", None)
                if not name:
                    raise SessionValidationError("adapter.name is required")
                self.adapters[str(name)] = adapter
        self.store = store

    def scan(
        self,
        window: TimeWindow,
        sources: Optional[Sequence[str]] = None,
        includes: Sequence[str] = (),
        temp_dir: Optional[Path] = None,
        checkpoints: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        if not isinstance(window, TimeWindow):
            window = TimeWindow.from_mapping(window) if isinstance(window, Mapping) else TimeWindow.from_values(*window)
        if isinstance(sources, str):
            selected = [sources]
        else:
            selected = list(sources) if sources is not None else list(self.adapters)
        if "all" in selected:
            selected = list(self.adapters)
        selected = list(dict.fromkeys(selected))
        scan_id = _scan_id()
        results: list[AdapterResult] = []
        own_temp = None
        try:
            if temp_dir is None:
                own_temp = tempfile.TemporaryDirectory(prefix="lifeos-sessions-")
                temp_path = Path(own_temp.name)
            else:
                temp_path = Path(temp_dir)
                temp_path.mkdir(parents=True, exist_ok=True)
            for source in selected:
                adapter = self.adapters.get(source)
                if adapter is None:
                    results.append(AdapterResult(
                        source=source,
                        status="failed",
                        error={"code": "source_unavailable", "message": "adapter is not installed"},
                        stats={"examined": 0, "matched": 0},
                    ))
                    continue
                checkpoint = None
                if checkpoints and source in checkpoints:
                    checkpoint = checkpoints[source]
                elif self.store is not None and hasattr(self.store, "load_checkpoint"):
                    checkpoint = self.store.load_checkpoint(source)
                request = SourceScanRequest(window, tuple(includes), checkpoint, temp_path)
                try:
                    raw_result = adapter.scan(request)
                    result = raw_result if isinstance(raw_result, AdapterResult) else AdapterResult(**raw_result)
                    if result.source != source:
                        result = AdapterResult(
                            source=source,
                            status="failed",
                            error={"code": "source_mismatch", "message": "adapter returned a different source name"},
                            stats={"examined": 0, "matched": 0},
                        )
                except Exception as exc:  # one source must not poison other sources
                    result = AdapterResult(
                        source=source,
                        status="failed",
                        error={"code": "adapter_exception", "message": str(exc)[:MAX_WARNING_LENGTH]},
                        stats={"examined": 0, "matched": 0},
                    )
                valid_slices: list[ConversationSlice] = []
                invalid_errors: list[str] = []
                for candidate in result.slices:
                    try:
                        finalized = candidate.finalize()
                        started_ms = _epoch_from_iso(finalized.started_at)
                        ended_ms = _epoch_from_iso(finalized.ended_at)
                        if not window.overlaps(started_ms, ended_ms):
                            raise SessionValidationError("slice is outside requested time window")
                    except SessionValidationError as exc:
                        invalid_errors.extend(exc.errors)
                        continue
                    valid_slices.append(finalized)
                if invalid_errors:
                    warnings = list(result.warnings)
                    warnings.append({"code": "invalid_record", "count": len(invalid_errors), "details": invalid_errors[:10]})
                    status = "partial" if result.status == "complete" else result.status
                    result = AdapterResult(
                        source=result.source,
                        status=status,
                        slices=valid_slices,
                        omissions=result.omissions,
                        reused_slice_ids=result.reused_slice_ids,
                        reused_omission_ids=result.reused_omission_ids,
                        checkpoint=result.checkpoint,
                        warnings=warnings,
                        error=result.error,
                        stats={**result.stats, "skipped": len(invalid_errors)},
                    )
                else:
                    result.slices = tuple(valid_slices)
                valid_omissions: list[TurnOmission] = []
                invalid_omissions: list[str] = []
                for candidate in result.omissions:
                    try:
                        finalized = candidate.finalize()
                        at_ms = _epoch_from_iso(finalized.at)
                        if not window.contains(at_ms):
                            raise SessionValidationError("omission is outside requested time window")
                    except SessionValidationError as exc:
                        invalid_omissions.extend(exc.errors)
                        continue
                    valid_omissions.append(finalized)
                if invalid_omissions:
                    warnings = list(result.warnings)
                    warnings.append({"code": "invalid_omission", "count": len(invalid_omissions), "details": invalid_omissions[:10]})
                    status = "partial" if result.status == "complete" else result.status
                    result = AdapterResult(
                        source=result.source,
                        status=status,
                        slices=result.slices,
                        omissions=valid_omissions,
                        reused_slice_ids=result.reused_slice_ids,
                        reused_omission_ids=result.reused_omission_ids,
                        checkpoint=result.checkpoint,
                        warnings=warnings,
                        error=result.error,
                        stats={**result.stats, "skipped_omissions": len(invalid_omissions)},
                    )
                else:
                    result.omissions = tuple(valid_omissions)
                results.append(result)
            overall = "complete" if results and all(item.status == "complete" for item in results) else "partial"
            if results and all(item.status == "failed" for item in results):
                overall = "failed"
            manifest = {
                "scan_id": scan_id,
                "window": window.to_dict(),
                "status": overall,
                "sources": [item.to_dict() for item in results],
            }
            if self.store is not None:
                persisted = self.store.commit_scan(scan_id, results, window=window, status=overall)
                if isinstance(persisted, Mapping):
                    manifest.update(dict(persisted))
                    manifest.setdefault("scan_id", scan_id)
                    manifest.setdefault("window", window.to_dict())
                    manifest.setdefault("status", overall)
                    manifest.setdefault("sources", [item.to_dict() for item in results])
            return manifest
        finally:
            if own_temp is not None:
                own_temp.cleanup()

    def rebuild(
        self,
        window: TimeWindow,
        *,
        sources: Optional[Sequence[str]] = None,
        includes: Sequence[str] = (),
        apply: bool = False,
    ) -> dict[str, Any]:
        """Full-rescan into a staging Store, optionally activating it.

        No current rows are reconciled in place: the staging tree is built
        from source files alone, validated, and then switched as one
        directory.  A failed scan or validation leaves the active tree
        untouched.
        """

        if self.store is None:
            raise SessionError("rebuild requires a SessionsStore")
        from .store import SessionsStore

        target = self.store
        selected_sources = list(sources) if sources is not None else list(self.adapters)
        selected_sources = list(dict.fromkeys(selected_sources))
        if apply and (
            set(selected_sources) != set(self.adapters)
            or set(selected_sources) != {"codex", "claude", "smartwork", "deepseek"}
            or includes
        ):
            raise SessionError("rebuild --apply requires all registered sources and no --include")
        staging_parent = target.root.parent
        stage_directory = Path(tempfile.mkdtemp(prefix=f".{target.root.name}.rebuild-", dir=str(staging_parent)))
        staging_store = SessionsStore(stage_directory, create=True)
        staged_service = SessionsService(self.adapters, staging_store)
        try:
            result = staged_service.scan(
                window,
                sources=sources,
                includes=includes,
            )
            validation_errors = staging_store.validate()
            result = dict(result)
            result["rebuild"] = {
                "apply_requested": bool(apply),
                "staging": str(stage_directory),
                "validation_errors": list(validation_errors),
                "active_unchanged": True,
                "applied": False,
            }
            failed_sources = [
                str(item.get("source"))
                for item in result.get("sources") or []
                if item.get("status") == "failed"
            ]
            result["rebuild"]["failed_sources"] = failed_sources
            if validation_errors or failed_sources or result.get("status") == "failed":
                result["rebuild"]["reason"] = "staging_validation_failed" if validation_errors else "scan_failed"
                return result
            if not apply:
                result["rebuild"]["reason"] = "preview_only"
                return result
            activation = target.activate_staging(stage_directory)
            result["rebuild"].update(activation)
            result["rebuild"].update({"active_unchanged": False, "applied": True})
            return result
        finally:
            if stage_directory.exists():
                import shutil

                shutil.rmtree(stage_directory)
