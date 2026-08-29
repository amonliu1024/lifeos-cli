"""The public, source-neutral sessions data model.

The package deliberately does not import any source adapter.  Adapters can
depend on :mod:`lifeos_sessions.core` without creating an import cycle and
the command layer can choose which adapters to install at runtime.
"""

from .core import (
    AdapterResult,
    ConversationSlice,
    SessionError,
    SessionValidationError,
    SessionsService,
    SourceScanRequest,
    TimeWindow,
    TurnOmission,
    canonical_json,
    canonical_revision,
    parse_iso_datetime,
    stable_slice_id,
    stable_omission_id,
    validate_slice,
)
from .store import SessionNotFound, SessionsStore, StoreError
from .pack import build_analysis_pack

__all__ = [
    "AdapterResult",
    "ConversationSlice",
    "SessionError",
    "SessionNotFound",
    "SessionValidationError",
    "SessionsService",
    "SessionsStore",
    "SourceScanRequest",
    "StoreError",
    "TimeWindow",
    "TurnOmission",
    "canonical_json",
    "canonical_revision",
    "parse_iso_datetime",
    "stable_slice_id",
    "stable_omission_id",
    "validate_slice",
    "build_analysis_pack",
]
