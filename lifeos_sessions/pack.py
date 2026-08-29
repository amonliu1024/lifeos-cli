"""Build bounded, deterministic analysis inputs from stored session slices.

Two views are produced from the same clustering, because a report needs both
and one object cannot be both:

``index``
    Every activity in the window, one lean row each.  This is the view that
    must never silently lose a project or a day, so its budget degrades by
    shedding optional fields before it sheds rows, and any row it does shed
    is accounted per project.

``pack`` (detail)
    Quoted content for a chosen subset.  Content is expensive, so this view
    is small on purpose and is expected to be requested repeatedly with a
    narrower filter (``--project``, ``--conversation``, ``--query``).

Neither view calls a model, attributes work to a LifeOS project fact, or
writes anything.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .core import TimeWindow, canonical_json
from .projects import ProjectMap
from .semantics import (
    CONTENT_LOSS_CODES,
    PROSE,
    has_readable_content,
    injected_block_count,
    is_approval_verdict,
    is_injected_text,
    prose_excerpt,
    prose_lines,
    readable_blocks,
    text_shape,
    warning_code,
)


MAX_OMITTED_ACTIVITIES = 40
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
# Sized so one ``--project`` pack covers the heaviest project of a heavy day
# rather than a fraction of it: on 2026-08-09 (150 activities across seven
# projects) the largest project needed 273 KB for all 35 of its activities.
# The total is bounded because each activity is capped at MAX_ACTIVITY_BYTES,
# so raising this further buys nothing -- that project saturates at 300 KB.
DEFAULT_MAX_BYTES = 320_000
DEFAULT_INDEX_MAX_BYTES = 400_000
MIN_MAX_BYTES = 8_000
MAX_ACTIVITY_BLOCKS = 8
MAX_BLOCK_TEXT = 800
# A block that says nothing in words still deserves a glimpse, not a slot's
# worth of pasted log.
MAX_MACHINE_BLOCK_TEXT = 240
# One line of an index row: enough to recognise the request and the outcome,
# not enough to read instead of the pack.
INDEX_EXCERPT_LIMIT = 120
# A line has to open this many separate activities before repetition means it
# is scaffolding, and the window has to hold enough activities for the count
# to mean anything.
_BOILERPLATE_REPEATS = 3
_BOILERPLATE_MIN_ACTIVITIES = 6
MAX_ACTIVITY_SUMMARY_ITEMS = 8
MAX_SUMMARY_TEXT = 400
MAX_ACTIVITY_DELEGATIONS = 3
MAX_DELEGATION_TASK = 300
MAX_DELEGATION_RESULT = 800
MAX_ACTIVITY_SLICE_REFS = 24
_TURN_COMPLETION_VALUES = ("completed", "incomplete", "interrupted_with_result")
_TURN_COMPLETION_SET = set(_TURN_COMPLETION_VALUES)
# The default ceiling, and what ``_activity_byte_cap`` returns for the default
# block budget.  A caller that asks for more detail gets a cap that can hold
# it; one that asks for less frees the budget for more activities.
MAX_ACTIVITY_BYTES = 16_000
DEFAULT_GAP_MS = 90 * 60 * 1000
INDEX_TITLE_LIMIT = 90


def _activity_byte_cap(blocks: int, block_chars: int) -> int:
    """Scale the per-activity ceiling with the detail that was asked for."""

    return 4_000 + blocks * (block_chars * 3 // 2 + 300)

# Blocks that state a result get half the detail slots.  An agent's newest
# visible reply is the closest thing a turn has to a stated outcome, but it is
# not itself evidence of one -- ``signal_score`` deliberately does not reward
# it.
_RESULT_KINDS = {"agent_message", "delegation"}


def _epoch_ms(value: str) -> int:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)


def _local_iso(value: Any) -> Any:
    """Render a stored instant in the local timezone.

    Slices are stored in canonical UTC so that ordering and windows are
    unambiguous, but these two views are read by one person in one place and
    are grouped by day.  Handing them UTC means every day silently starts at
    08:00 local: a morning's work lands on the previous day and no error is
    raised anywhere.  So storage stays UTC and the analysis views are local.
    """

    text = str(value)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return value
    return parsed.astimezone(LOCAL_TIMEZONE).isoformat(timespec="seconds")


def _omitted_stub(activity: Mapping[str, Any]) -> Dict[str, Any]:
    """Name an activity the budget could not carry.

    A count is not enough to recover from.  The greedy fill drops whatever
    does not fit, and what does not fit is by definition the largest activity
    left -- often the day's main thread.  Naming it, with the conversation it
    belongs to, turns a silent loss into a work list the caller can fetch one
    at a time via ``--conversation``.
    """

    conversation = activity.get("conversation") or {}
    return {
        "activity_id": activity["activity_id"],
        "project_key": activity["project_key"],
        "conversation_id": conversation.get("id"),
        "started_at": activity["started_at"],
        "ended_at": activity["ended_at"],
        "signal_score": activity["signal_score"],
        "slice_ref_count": activity["slice_ref_count"],
    }


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _dedupe(values: Iterable[str], limit: Optional[int] = None) -> Tuple[List[str], int]:
    unique = list(dict.fromkeys(str(value) for value in values if str(value)))
    if limit is None or len(unique) <= limit:
        return unique, 0
    return unique[:limit], len(unique) - limit


def _clip(text: Any, limit: int = MAX_BLOCK_TEXT) -> Tuple[str, bool]:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value, False
    return value[: limit - 1] + "…", True


def _one_line(candidates: Iterable[Any], drop: Any, limit: int = INDEX_EXCERPT_LIMIT) -> str:
    """Return the first candidate that says something, as a single line.

    Blocks are tried in order because the first thing a person types is often
    a pasted stack trace, and the request follows it.
    """

    for candidate in candidates:
        spoken, _ = prose_excerpt(candidate, drop)
        if spoken:
            return _clip(" ".join(spoken.split()), limit)[0]
    return ""


def _boilerplate(all_facts: Sequence[Mapping[str, Any]]) -> frozenset:
    """Find the lines that open too many activities to be about any of them.

    An Agent application pastes the workspace's rule files, and a skill pastes
    its own preamble, into the person's first turn -- with the person's
    transport role, so nothing downstream can tell them apart.  Repetition
    across independent activities can: on one real day 64 of 113 activities
    appeared to open with the same 3,000-word rule file.

    This is a statement about the window, not about who wrote the text.
    ``origin`` still owns provenance, and correcting it is an ingest change.
    """

    if len(all_facts) < _BOILERPLATE_MIN_ACTIVITIES:
        return frozenset()
    counts: Dict[str, int] = {}
    for facts in all_facts:
        for line in dict.fromkeys(
            line for text in facts["asked"] for line in prose_lines(text)
        ):
            counts[line] = counts.get(line, 0) + 1
    return frozenset(
        line for line, count in counts.items() if count >= _BOILERPLATE_REPEATS
    )


def _assign_excerpts(all_facts: Sequence[Dict[str, Any]]) -> frozenset:
    """Give every activity a recognisable one-line opening and outcome."""

    drop = _boilerplate(all_facts)
    for facts in all_facts:
        facts["opening"] = _one_line(facts["asked"], drop)
        if not facts["opening"]:
            # Window-level suppression is allowed to remove scaffolding, not
            # the only human sentence that identifies an activity.  The
            # unfiltered line is still preferable to an empty row.
            facts["opening"] = _one_line(facts["asked"], frozenset())
        if not facts["opening"]:
            agent_opening = _one_line(facts["stated"], frozenset())
            if agent_opening:
                facts["opening"] = f"agent: {agent_opening}"
        facts["outcome"] = _one_line(reversed(facts["stated"][-3:]), drop)
        # A delegated activity carries no turn of the person's own, so the
        # fallback above and the outcome below reach for the same agent
        # sentence and the row spends half its width saying it twice.  The
        # request that started it lives in the parent conversation, not here.
        if facts["opening"] == f"agent: {facts['outcome']}":
            facts["opening"] = ""
    return drop


SUPPRESSION_REASONS = ("mechanism_only", "no_quotable_text")


def _is_mechanism_delegation(value: Mapping[str, Any]) -> bool:
    """True when a delegation record is the approval reviewer, not work.

    An approval reviewer runs as a sub-agent, so it is recorded with the same
    vocabulary as a real delegation: its ``task`` is the injected prompt it was
    handed and its ``result`` is the verdict.  Counted as delegations these
    raise ``signal_score`` and spend the activity's delegation budget -- on
    real data 1,178 of 2,519 results were verdicts, and 94.5% of all task
    characters were injected scaffolding truncated at the 500-character cap.

    Both halves have to be mechanism text.  A real brief that happens to end
    in an approval, or an injected prompt that produced a real report, still
    says something a report can use.
    """

    return (
        is_injected_text(value.get("task"))
        and (is_approval_verdict(value.get("result")) or not str(value.get("result") or "").strip())
    )


def _carries_evidence(facts: Mapping[str, Any]) -> bool:
    return bool(
        any(facts["evidence"].get(key) for key in facts["evidence"])
        or facts["delegations"]
    )


def _suppression_reason(facts: Mapping[str, Any]) -> Optional[str]:
    """Name why an activity has nothing to hand a report, or return ``None``.

    An Agent application's approval reviewer opens a conversation per assessed
    action, and on a real day 37 of 150 activities were nothing but its
    verdicts -- holding 855 of the window's 1,359 slices while carrying no
    command, no changed file and no check.  Listing them costs a report the
    rows and the budget it needs for the work they were approving.

    The gate is the general one: no line anybody spoke, and no evidence that
    anything ran.  The reason only labels what was suppressed, so a day of
    machine chatter and a day of failed extraction stay tellable apart.
    """

    if facts["opening"] or facts["outcome"] or _carries_evidence(facts):
        return None
    for item in facts["readable"]:
        for block in readable_blocks(item):
            if is_approval_verdict(block.get("text")):
                return "mechanism_only"
    return "no_quotable_text"


def _partition_suppressed(
    all_facts: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Split the window into activities worth a row and a tally of the rest."""

    kept: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {reason: 0 for reason in SUPPRESSION_REASONS}
    counts["slices"] = 0
    for facts in all_facts:
        reason = _suppression_reason(facts)
        if reason is None:
            kept.append(facts)
            continue
        counts[reason] += 1
        counts["slices"] += len(facts["cluster"])
    return kept, counts


def _group_key(slice_value: Mapping[str, Any]) -> Tuple[str, str]:
    conversation = slice_value.get("conversation") or {}
    return str(slice_value.get("source") or "unknown"), str(conversation.get("id") or "unknown")


def _cluster_slices(
    slices: Sequence[Mapping[str, Any]],
    *,
    gap_ms: int,
) -> List[List[Mapping[str, Any]]]:
    grouped: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for item in slices:
        grouped.setdefault(_group_key(item), []).append(item)
    clusters: List[List[Mapping[str, Any]]] = []
    for key in sorted(grouped):
        ordered = sorted(
            grouped[key],
            key=lambda value: (
                _epoch_ms(str(value["started_at"])),
                str(value.get("slice_id") or ""),
            ),
        )
        current: List[Mapping[str, Any]] = []
        current_end: Optional[int] = None
        for item in ordered:
            started = _epoch_ms(str(item["started_at"]))
            ended = _epoch_ms(str(item["ended_at"]))
            if current and current_end is not None and started - current_end > gap_ms:
                clusters.append(current)
                current = []
                current_end = None
            current.append(item)
            current_end = max(current_end or ended, ended)
        if current:
            clusters.append(current)
    return clusters


def _signal_score(evidence: Mapping[str, Any], delegations: int, user_messages: int) -> int:
    """Rank by evidence that work happened, not by how much was said.

    The previous formula weighted every visible agent reply, which made a
    chatty conversation outrank one that changed files and ran tests.
    """

    return (
        len(evidence["changed_targets"]) * 5
        + len(evidence["verifications"]) * 4
        + len(evidence["failures"]) * 6
        + delegations * 2
        + min(user_messages, 5)
    )


def _normalized_turn_completion(value: Any) -> str:
    """Return the lifecycle value used by both index and pack aggregation.

    Store validation rejects unknown Slice Schema values. Keeping the view layer
    conservative as well means a malformed or unknown-shape fixture cannot be read as
    a completed turn, and no lifecycle is inferred from ``user_interrupts`` or
    other evidence fields.
    """

    value = str(value or "")
    return value if value in _TURN_COMPLETION_SET else "incomplete"


def _cluster_facts(
    cluster: Sequence[Mapping[str, Any]],
    project_map: ProjectMap,
) -> Dict[str, Any]:
    """Derive everything both views need from one cluster, exactly once."""

    first = min(cluster, key=lambda item: str(item["started_at"]))
    last = max(cluster, key=lambda item: str(item["ended_at"]))
    conversation = first.get("conversation") or {}
    source = str(first.get("source") or "unknown")
    conversation_id = str(conversation.get("id") or "unknown")
    slice_ids = sorted(str(item.get("slice_id") or "") for item in cluster)

    workspace = next((item.get("workspace") for item in cluster if item.get("workspace")), None)
    resolution = project_map.resolve(workspace)
    title = next(
        ((item.get("conversation") or {}).get("title")
         for item in cluster if (item.get("conversation") or {}).get("title")),
        None,
    )

    changed: List[str] = []
    other_targets: List[str] = []
    tool_calls: List[str] = []
    verifications: List[str] = []
    failures: List[str] = []
    interrupts: List[str] = []
    omitted_count = 0
    completeness_counts = {"complete": 0, "partial": 0, "truncated": 0}
    turn_completion_by_slice = {value: 0 for value in _TURN_COMPLETION_VALUES}
    provenance_trimmed = 0
    content_loss_reasons: List[str] = []
    quality_flags: List[str] = []
    omissions: List[str] = []
    warning_codes: List[str] = []
    delegations: List[Mapping[str, Any]] = []
    injected = 0

    for item in cluster:
        # A schema 2 slice already separates these; nothing is reinterpreted
        # here, only summed across the cluster.
        evidence = item.get("execution_evidence") or {}
        changed.extend(evidence.get("changed_targets") or [])
        other_targets.extend(evidence.get("other_targets") or [])
        tool_calls.extend(evidence.get("tool_calls") or [])
        verifications.extend(evidence.get("verifications") or [])
        failures.extend(evidence.get("failures") or [])
        interrupts.extend(evidence.get("user_interrupts") or [])
        omitted_count += int(evidence.get("omitted_count") or 0)

        completeness = str(item.get("content_completeness") or "partial")
        completeness_counts[completeness if completeness in completeness_counts else "partial"] += 1
        turn_completion_by_slice[_normalized_turn_completion(item.get("turn_completion"))] += 1
        provenance_trimmed += 1 if item.get("provenance_trimmed") else 0
        content_loss_reasons.extend(
            code for code in (warning_code(value) for value in item.get("warnings") or [])
            if code in CONTENT_LOSS_CODES
        )

        quality_flags.extend(str(value) for value in item.get("quality_flags") or [])
        omissions.extend(str(value) for value in item.get("omissions") or [])
        warning_codes.extend(warning_code(value) for value in item.get("warnings") or [])
        delegations.extend(
            value for value in item.get("delegations") or []
            if isinstance(value, Mapping) and not _is_mechanism_delegation(value)
        )
        injected += injected_block_count(item)

    readable = [item for item in cluster if has_readable_content(item)]
    user_messages = 0
    asked: List[Any] = []
    stated: List[Any] = []
    for item in readable:
        for block in readable_blocks(item):
            if block["origin"] == "user":
                user_messages += 1
                if len(asked) < 3:
                    asked.append(block.get("text"))
            if str(block.get("kind") or "") in _RESULT_KINDS:
                stated.append(block.get("text"))

    evidence_totals = {
        "changed_targets": _dedupe(
            (_clip(value, MAX_SUMMARY_TEXT)[0] for value in changed), MAX_ACTIVITY_SUMMARY_ITEMS),
        "other_targets": _dedupe(
            (_clip(value, MAX_SUMMARY_TEXT)[0] for value in other_targets), MAX_ACTIVITY_SUMMARY_ITEMS),
        "tool_calls": _dedupe(
            (_clip(value, MAX_SUMMARY_TEXT)[0] for value in tool_calls), MAX_ACTIVITY_SUMMARY_ITEMS),
        "verifications": _dedupe(
            (_clip(value, MAX_SUMMARY_TEXT)[0] for value in verifications), MAX_ACTIVITY_SUMMARY_ITEMS),
        "failures": _dedupe(
            (_clip(value, MAX_SUMMARY_TEXT)[0] for value in failures), MAX_ACTIVITY_SUMMARY_ITEMS),
        "user_interrupts": _dedupe(
            (_clip(value, 80)[0] for value in interrupts), MAX_ACTIVITY_SUMMARY_ITEMS),
    }
    trimmed_evidence = {key: value for key, (value, _) in evidence_totals.items()}
    omitted_count += sum(dropped for _, dropped in evidence_totals.values())

    if completeness_counts["truncated"]:
        completeness = "truncated"
    elif completeness_counts["partial"]:
        completeness = "partial"
    else:
        completeness = "complete"

    if turn_completion_by_slice["interrupted_with_result"]:
        turn_completion = "interrupted_with_result"
    elif turn_completion_by_slice["incomplete"]:
        turn_completion = "incomplete"
    else:
        turn_completion = "completed"

    return {
        "activity_id": _stable_id("ACT", source, conversation_id, *slice_ids),
        "source": source,
        "conversation_id": conversation_id,
        "title": title,
        "workspace": workspace,
        "resolution": resolution,
        "started_at": first["started_at"],
        "ended_at": last["ended_at"],
        "cluster": cluster,
        "readable": readable,
        "evidence": trimmed_evidence,
        "evidence_omitted_count": omitted_count,
        "delegations": delegations,
        "completeness": completeness,
        "completeness_counts": completeness_counts,
        "turn_completion": turn_completion,
        "turn_completion_by_slice": turn_completion_by_slice,
        "provenance_trimmed_slices": provenance_trimmed,
        "content_loss_reasons": sorted(set(content_loss_reasons)),
        "quality_flags": quality_flags,
        "omissions": omissions,
        "warning_codes": warning_codes,
        "injected_blocks": injected,
        "signal_score": _signal_score(trimmed_evidence, len(delegations), user_messages),
        "low_signal_slices": len(cluster) - len(readable),
        # Filled in by ``_assign_excerpts`` once the whole window is known.
        "asked": asked,
        "stated": stated,
        "opening": "",
        "outcome": "",
    }


# -- index view -------------------------------------------------------------


def _index_row(facts: Mapping[str, Any], *, with_detail: bool) -> Dict[str, Any]:
    resolution = facts["resolution"]
    evidence = facts["evidence"]
    row: Dict[str, Any] = {
        "activity_id": facts["activity_id"],
        "project_key": resolution.project_key,
        "project_kind": resolution.kind,
        "source": facts["source"],
        "started_at": _local_iso(facts["started_at"]),
        "ended_at": _local_iso(facts["ended_at"]),
        "signal_score": facts["signal_score"],
        "slice_count": len(facts["cluster"]),
        "content_completeness": facts["completeness"],
        "turn_completion": facts["turn_completion"],
        "turn_completion_by_slice": dict(facts["turn_completion_by_slice"]),
        "changed_targets": len(evidence["changed_targets"]),
        "verifications": len(evidence["verifications"]),
        "failures": len(evidence["failures"]),
    }
    if facts["provenance_trimmed_slices"]:
        row["provenance_trimmed"] = True
    if with_detail:
        # Counts alone cannot tell two activities apart: on one real day 86 of
        # 150 activities had no file change, no verification and no failure,
        # 50 of them scored identically, and only 5 carried a session title.
        # The request and the reply are what make a row recognisable.
        if facts["title"]:
            row["title"] = _clip(facts["title"], INDEX_TITLE_LIMIT)[0]
        if facts["opening"]:
            row["opening"] = facts["opening"]
        if facts["outcome"]:
            row["outcome"] = facts["outcome"]
    return row


def _summaries(rows: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Roll rows up by project and by day, across every source alike.

    Callers pass every activity in the window, not the rows that survived a
    budget, so the totals stay true even when individual rows are dropped.
    """

    by_project: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        project = by_project.setdefault(str(row["project_key"]), {
            "project_key": row["project_key"],
            "project_kind": row["project_kind"],
            "activities": 0,
            "slices": 0,
            "changed_targets": 0,
            "failures": 0,
            "days": set(),
        })
        project["activities"] += 1
        project["slices"] += int(row["slice_count"])
        project["changed_targets"] += int(row["changed_targets"])
        project["failures"] += int(row["failures"])
        day = str(row["started_at"])[:10]
        project["days"].add(day)
        bucket = by_day.setdefault(day, {"day": day, "activities": 0, "projects": set()})
        bucket["activities"] += 1
        bucket["projects"].add(str(row["project_key"]))

    project_summary = []
    for value in sorted(by_project.values(), key=lambda item: (-item["activities"], item["project_key"])):
        value = dict(value)
        value["active_days"] = len(value.pop("days"))
        project_summary.append(value)
    day_summary = []
    for value in sorted(by_day.values(), key=lambda item: item["day"]):
        value = dict(value)
        value["projects"] = len(value["projects"])
        day_summary.append(value)
    return project_summary, day_summary


def build_activity_index(
    store: Any,
    window: TimeWindow,
    *,
    sources: Sequence[str] = (),
    conversation: Optional[str] = None,
    workspace: Optional[str] = None,
    project: Optional[str] = None,
    query: Optional[str] = None,
    max_bytes: int = DEFAULT_INDEX_MAX_BYTES,
    gap_ms: int = DEFAULT_GAP_MS,
    project_map: Optional[ProjectMap] = None,
) -> Dict[str, Any]:
    """Return one lean row per activity for the whole window.

    Unlike the detail pack, this view is meant to be complete.  When it cannot
    be, it says so per project rather than letting a project vanish.
    """

    if max_bytes < MIN_MAX_BYTES:
        raise ValueError(f"max_bytes must be at least {MIN_MAX_BYTES}")
    if gap_ms <= 0:
        raise ValueError("gap_ms must be greater than zero")
    resolved_map = project_map if project_map is not None else ProjectMap.default()

    slices, coverage_counts, source_counts = _load_slices(
        store, window, sources=sources, conversation=conversation,
        workspace=workspace, query=query,
    )
    cleaning_summary = {
        "explicit_abort_without_work": 0,
        "interrupted_with_result": sum(
            1 for item in slices if item.get("turn_completion") == "interrupted_with_result"
        ),
    }
    if hasattr(store, "list_omissions"):
        omission_filters = {"from_ms": window.from_ms, "to_ms": window.to_ms}
        if sources:
            # Source filtering is applied by the store so an unrelated source
            # cannot inflate the report's cleaning summary.
            cleaning_summary["explicit_abort_without_work"] = sum(
                len(store.list_omissions({**omission_filters, "source": source}))
                for source in sorted(set(sources))
            )
        else:
            cleaning_summary["explicit_abort_without_work"] = len(store.list_omissions(omission_filters))
    all_facts = [
        _cluster_facts(cluster, resolved_map)
        for cluster in _cluster_slices(slices, gap_ms=gap_ms)
    ]
    all_facts = [facts for facts in all_facts if facts["readable"]]
    _assign_excerpts(all_facts)
    all_facts, suppressed = _partition_suppressed(all_facts)
    if project:
        all_facts = [facts for facts in all_facts if facts["resolution"].project_key == project]
    all_facts.sort(key=lambda item: (str(item["started_at"]), str(item["activity_id"])))

    # Retention may already have removed evidence inside this window.  A
    # report that cannot see that would present a pruned week as a quiet one.
    pruned = []
    if hasattr(store, "pruned_overlap"):
        pruned = store.pruned_overlap(window.from_ms, window.to_ms)

    # The summaries are the part a report leans on hardest, and they cost only
    # one row per project and per day.  So they are always computed over every
    # activity in the window: rows may be dropped for budget, the totals above
    # them never are.
    project_summary, day_summary = _summaries(
        [_index_row(facts, with_detail=False) for facts in all_facts]
    )

    def assemble(rows: Sequence[Dict[str, Any]], dropped: Mapping[str, int], detail: bool) -> Dict[str, Any]:
        result = {
            "schema_version": 1,
            "view": "index",
            "window": window.to_dict(),
            "filters": {
                "sources": sorted(set(sources)),
                "conversation": conversation,
                "workspace": workspace,
                "project": project,
                "query": query,
                "gap_ms": gap_ms,
                "max_bytes": max_bytes,
            },
            "source_slice_counts": source_counts,
            "coverage_summary": coverage_counts,
            "cleaning_summary": cleaning_summary,
            "activity_total": len(all_facts),
            "suppressed_activities": dict(suppressed),
            "excerpts_included": detail,
            "budget_exceeded": False,
            "retention_pruned": {
                "days": len({item["day"] for item in pruned}),
                "slices": sum(int(item["slice_count"]) for item in pruned if item["scope"] == "content"),
                "entries": pruned,
            },
            "project_summary": project_summary,
            "day_summary": day_summary,
            "dropped_by_project": [
                {"project_key": key, "activities": count}
                for key, count in sorted(dropped.items()) if count
            ],
            "activities": list(rows),
        }
        basis = dict(result)
        basis.pop("index_id", None)
        result["index_id"] = _stable_id("IDX", canonical_json(basis))
        result["byte_size"] = 0
        while True:
            size = len(canonical_json(result).encode("utf-8"))
            if size == result["byte_size"]:
                return result
            result["byte_size"] = size

    # Degrade in the order that costs a report the least: first the title and
    # excerpt, and only then whole rows -- lowest signal first, and never the
    # last remaining row of a project.
    for detail in (True, False):
        rows = [_index_row(facts, with_detail=detail) for facts in all_facts]
        result = assemble(rows, {}, detail)
        if result["byte_size"] <= max_bytes:
            return result

    rows_by_id = {facts["activity_id"]: _index_row(facts, with_detail=False) for facts in all_facts}
    order = sorted(
        all_facts,
        key=lambda item: (int(item["signal_score"]), str(item["ended_at"]), str(item["activity_id"])),
    )
    per_project: Dict[str, int] = {}
    for facts in all_facts:
        key = facts["resolution"].project_key
        per_project[key] = per_project.get(key, 0) + 1
    dropped: Dict[str, int] = {}
    kept_ids = set(rows_by_id)
    for facts in order:
        key = facts["resolution"].project_key
        if per_project[key] <= 1:
            continue
        kept_ids.discard(facts["activity_id"])
        per_project[key] -= 1
        dropped[key] = dropped.get(key, 0) + 1
        rows = [rows_by_id[facts_id] for facts_id in
                (item["activity_id"] for item in all_facts) if facts_id in kept_ids]
        result = assemble(rows, dropped, False)
        if result["byte_size"] <= max_bytes:
            return result
    # Every project is down to its last row and the budget still is not met.
    # Emptying a project would defeat the point of this view, so the budget
    # gives way instead -- and says so rather than surprising the caller.
    rows = [rows_by_id[facts_id] for facts_id in
            (item["activity_id"] for item in all_facts) if facts_id in kept_ids]
    result = assemble(rows, dropped, False)
    if result["byte_size"] > max_bytes:
        result["budget_exceeded"] = True
        result["index_id"] = _stable_id("IDX", canonical_json(
            {key: value for key, value in result.items()
             if key not in {"index_id", "byte_size"}}))
        result["byte_size"] = 0
        while True:
            size = len(canonical_json(result).encode("utf-8"))
            if size == result["byte_size"]:
                break
            result["byte_size"] = size
    return result


# -- detail view ------------------------------------------------------------


def _even_sample(items: Sequence[Any], count: int) -> List[Any]:
    """Take ``count`` items spread across the sequence, keeping its order.

    Sampling from the end -- which is what sorting by recency amounts to --
    reads the last twenty minutes of a nine-hour session and calls it the day.
    """

    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[0]]
    step = (len(items) - 1) / (count - 1)
    picked: List[Any] = []
    seen: set = set()
    for position in range(count):
        index = round(position * step)
        if index not in seen:
            seen.add(index)
            picked.append(items[index])
    return picked


def _select_blocks(
    candidates: Sequence[Tuple[Any, ...]], limit: int
) -> Tuple[List[Tuple[Any, ...]], set]:
    """Choose which blocks a bounded activity gets to show.

    Two blocks are worth more than any other: what was asked for, and what was
    said back at the end.  Those are returned as protected, so a later byte
    ceiling takes the filling rather than the frame.  The rest are drawn from
    the blocks that are actually words, spread over the activity so the middle
    of a long session is represented at all.
    """

    chosen: List[Tuple[Any, ...]] = []
    protected: set = set()

    def take(candidate: Optional[Tuple[Any, ...]], *, keep: bool = False) -> None:
        if candidate is None or candidate in chosen or len(chosen) >= limit:
            return
        chosen.append(candidate)
        if keep:
            protected.add(candidate[0])

    spoken = [item for item in candidates if item[4]]
    is_user = lambda item: item[1]["origin"] == "user"
    is_result = lambda item: item[1]["kind"] in _RESULT_KINDS
    first = lambda items, test: next((item for item in items if test(item)), None)
    last = lambda items, test: next((item for item in reversed(items) if test(item)), None)
    # Prefer a spoken ask and a spoken result, but a machine-shaped one still
    # frames the activity better than nothing.
    take(first(spoken, is_user) or first(candidates, is_user), keep=True)
    take(last(spoken, is_result) or last(candidates, is_result), keep=True)
    for tier in (spoken, candidates):
        rest = [item for item in tier if item not in chosen]
        for candidate in _even_sample(rest, limit - len(chosen)):
            take(candidate)
    chosen.sort(key=lambda item: (str(item[1].get("at") or ""), item[0]))
    return chosen, protected


def _activity(
    facts: Mapping[str, Any],
    *,
    blocks_limit: int = MAX_ACTIVITY_BLOCKS,
    block_chars: int = MAX_BLOCK_TEXT,
    drop: Any = frozenset(),
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
    stats = {
        "low_signal_slices": int(facts["low_signal_slices"]),
        "omitted_blocks": 0,
        "truncated_texts": 0,
        "reduced_texts": 0,
        "omitted_slice_refs": 0,
        "omitted_delegations": 0,
        "injected_blocks": int(facts["injected_blocks"]),
    }
    readable = facts["readable"]
    if not readable:
        return None, stats

    cluster = facts["cluster"]
    block_candidates: List[Tuple[int, Dict[str, Any], bool, int, bool]] = []
    block_keys: set[Tuple[str, str, str]] = set()
    for item in readable:
        for block in readable_blocks(item):
            raw = str(block.get("text") or "")
            # A machine-shaped block is normally still worth showing -- it
            # frames the activity better than nothing.  A verdict is the one
            # kind that is not: it reports on the approval mechanism, not on
            # the work.  It stays in ``readable_blocks`` so the suppression
            # counts can still see it; it just never becomes content.
            if is_approval_verdict(raw):
                continue
            shape = text_shape(raw)
            # Pasted logs, diffs and file dumps arrive on the same channel as
            # the sentence that asked for the work.  Keeping the spoken lines
            # instead of the first N characters is what makes the difference
            # between quoting a request and quoting a stack trace.
            body, reduced = prose_excerpt(raw, drop)
            if shape == PROSE and not reduced:
                body = raw
            text, truncated = _clip(body or raw, block_chars if body else MAX_MACHINE_BLOCK_TEXT)
            if not text:
                continue
            key = (
                str(block.get("kind") or "message"),
                str(block.get("author_role") or "unknown"),
                " ".join(text.split()),
            )
            if key in block_keys:
                continue
            block_keys.add(key)
            refs, _ = _dedupe((_clip(value, 300)[0] for value in block.get("source_refs") or []), 4)
            block_candidates.append((len(block_candidates), {
                "kind": key[0],
                "author_role": key[1],
                "origin": block.get("origin"),
                "at": _local_iso(block.get("at")) if block.get("at") else block.get("at"),
                "text": text,
                "shape": shape,
                "text_chars": len(raw),
                "source_refs": refs,
            }, truncated, reduced, bool(body)))

    chosen, protected = _select_blocks(block_candidates, blocks_limit)
    blocks = [item[1] for item in chosen]
    keep_flags = [item[0] in protected for item in chosen]
    stats["omitted_blocks"] = max(0, len(block_candidates) - len(blocks))
    stats["truncated_texts"] = sum(1 for item in chosen if item[2])
    stats["reduced_texts"] = sum(1 for item in chosen if item[3])

    quality_flags, _ = _dedupe((_clip(v, 200)[0] for v in facts["quality_flags"]), MAX_ACTIVITY_SUMMARY_ITEMS)
    omissions, _ = _dedupe((_clip(v, 200)[0] for v in facts["omissions"]), MAX_ACTIVITY_SUMMARY_ITEMS)
    warning_codes, _ = _dedupe((_clip(v, 80)[0] for v in facts["warning_codes"]), MAX_ACTIVITY_SUMMARY_ITEMS)
    if stats["low_signal_slices"]:
        omissions.append(f"low_signal_slices:{stats['low_signal_slices']}")
    if stats["omitted_blocks"]:
        omissions.append(f"activity_blocks_omitted:{stats['omitted_blocks']}")
    if stats["truncated_texts"]:
        omissions.append(f"activity_texts_truncated:{stats['truncated_texts']}")
    if stats["reduced_texts"]:
        omissions.append(f"activity_texts_reduced_to_prose:{stats['reduced_texts']}")
    if stats["injected_blocks"]:
        omissions.append(f"system_injected_blocks_excluded:{stats['injected_blocks']}")

    normalized_delegations: List[Dict[str, Any]] = []
    for value in facts["delegations"][:MAX_ACTIVITY_DELEGATIONS]:
        source_refs = value.get("source_refs") or []
        first_ref = source_refs[0] if isinstance(source_refs, (list, tuple)) and source_refs else None
        # The delegation still happened, but neither an injected prompt nor an
        # approval verdict says anything about it -- the same mechanism text
        # suppressed everywhere else, arriving here in two more containers.
        # Records where both halves are mechanism text are already gone.
        task = value.get("task")
        result = value.get("result")
        normalized_delegations.append({
            "task": "" if is_injected_text(task) else _clip(task, MAX_DELEGATION_TASK)[0],
            "result": "" if is_approval_verdict(result) else _clip(result, MAX_DELEGATION_RESULT)[0],
            "status": _clip(value.get("status"), 80)[0],
            "agent": _clip(value.get("agent_path") or value.get("agent_id"), 80)[0],
            "ref": _clip(first_ref, 300)[0],
        })
    stats["omitted_delegations"] = max(0, len(facts["delegations"]) - len(normalized_delegations))
    if stats["omitted_delegations"]:
        omissions.append(f"delegations_omitted:{stats['omitted_delegations']}")

    slice_refs = [
        {
            "slice_id": item.get("slice_id"),
            "revision": item.get("revision"),
            "turn_completion": _normalized_turn_completion(item.get("turn_completion")),
        }
        for item in cluster
    ]
    if len(slice_refs) > MAX_ACTIVITY_SLICE_REFS:
        half = MAX_ACTIVITY_SLICE_REFS // 2
        stats["omitted_slice_refs"] = len(slice_refs) - MAX_ACTIVITY_SLICE_REFS
        slice_refs = [*slice_refs[:half], *slice_refs[-half:]]
        omissions.append(f"slice_refs_omitted:{stats['omitted_slice_refs']}")

    resolution = facts["resolution"]
    activity = {
        "activity_id": facts["activity_id"],
        "source": facts["source"],
        "conversation": {"id": facts["conversation_id"], "title": facts["title"]},
        "workspace": facts["workspace"],
        "project_key": resolution.project_key,
        "project_kind": resolution.kind,
        "project_title": resolution.title,
        "started_at": _local_iso(facts["started_at"]),
        "ended_at": _local_iso(facts["ended_at"]),
        "content": blocks,
        "execution_evidence": {
            **facts["evidence"],
            "omitted_count": facts["evidence_omitted_count"],
        },
        "delegations": normalized_delegations,
        "content_completeness": facts["completeness"],
        "completeness_by_slice": facts["completeness_counts"],
        "turn_completion": facts["turn_completion"],
        "turn_completion_by_slice": dict(facts["turn_completion_by_slice"]),
        "provenance_trimmed_slices": facts["provenance_trimmed_slices"],
        "content_loss_reasons": facts["content_loss_reasons"],
        "quality_flags": quality_flags,
        "omissions": omissions,
        "warning_codes": warning_codes,
        "slice_ref_count": len(cluster),
        "slice_refs": slice_refs,
        "signal_score": facts["signal_score"],
    }
    # Drop the filling before the frame.  Popping the tail, as this used to,
    # threw away the reply that stated the outcome -- the one block a report
    # can least do without.
    byte_cap = _activity_byte_cap(blocks_limit, block_chars)
    if (
        len(canonical_json(activity).encode("utf-8")) > byte_cap
        and activity["execution_evidence"].get("other_targets")
    ):
        # These targets are useful when there is room, but weaker than the
        # readable blocks.  Give the blocks the first chance to survive the
        # per-activity ceiling.
        activity["execution_evidence"]["other_targets"] = []
    while blocks and len(canonical_json(activity).encode("utf-8")) > byte_cap:
        droppable = [position for position, keep in enumerate(keep_flags) if not keep]
        position = (
            max(droppable, key=lambda index: len(blocks[index]["text"]))
            if droppable else len(blocks) - 1
        )
        blocks.pop(position)
        keep_flags.pop(position)
        stats["omitted_blocks"] += 1
    if stats["omitted_blocks"]:
        marker = f"activity_blocks_omitted:{stats['omitted_blocks']}"
        activity["omissions"] = [
            value for value in activity["omissions"]
            if not value.startswith("activity_blocks_omitted:")
        ]
        activity["omissions"].append(marker)
    return activity, stats


def _load_slices(
    store: Any,
    window: TimeWindow,
    *,
    sources: Sequence[str] = (),
    conversation: Optional[str] = None,
    workspace: Optional[str] = None,
    query: Optional[str] = None,
) -> Tuple[List[Mapping[str, Any]], Dict[str, int], Dict[str, int]]:
    filters: Dict[str, Any] = {"from_ms": window.from_ms, "to_ms": window.to_ms}
    if conversation:
        filters["conversation"] = conversation
    if workspace:
        filters["workspace"] = workspace
    if query:
        filters["query"] = query
    metadata = store.list_slices(filters)
    allowed_sources = set(sources)
    if allowed_sources:
        metadata = [item for item in metadata if item.get("source") in allowed_sources]
    slices = [store.show(item["slice_id"], item["revision"]) for item in metadata]

    source_counts: Dict[str, int] = {}
    coverage_counts = {"complete": 0, "partial": 0, "truncated": 0}
    for item in slices:
        source = str(item.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        completeness = str(item.get("content_completeness") or "partial")
        coverage_counts[completeness if completeness in coverage_counts else "partial"] += 1
    return slices, coverage_counts, source_counts


def build_analysis_pack(
    store: Any,
    window: TimeWindow,
    *,
    sources: Sequence[str] = (),
    conversation: Optional[str] = None,
    workspace: Optional[str] = None,
    project: Optional[str] = None,
    query: Optional[str] = None,
    activities_wanted: Sequence[str] = (),
    blocks: int = MAX_ACTIVITY_BLOCKS,
    block_chars: int = MAX_BLOCK_TEXT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    gap_ms: int = DEFAULT_GAP_MS,
    project_map: Optional[ProjectMap] = None,
) -> Dict[str, Any]:
    """Return a bounded detail pack from the immutable local store.

    ``activities_wanted`` is the drill-down: given ids from the index, it
    returns those activities and nothing else, which is what makes ``blocks``
    and ``block_chars`` affordable.  Sampling and reading are different
    requests and should not be served at the same depth.
    """

    if max_bytes < MIN_MAX_BYTES:
        raise ValueError(f"max_bytes must be at least {MIN_MAX_BYTES}")
    if gap_ms <= 0:
        raise ValueError("gap_ms must be greater than zero")
    if blocks < 1:
        raise ValueError("blocks must be at least 1")
    if block_chars < 80:
        raise ValueError("block_chars must be at least 80")
    resolved_map = project_map if project_map is not None else ProjectMap.default()
    wanted = {str(value) for value in activities_wanted}

    slices, coverage_counts, source_counts = _load_slices(
        store, window, sources=sources, conversation=conversation,
        workspace=workspace, query=query,
    )
    cleaning_summary = {
        "explicit_abort_without_work": 0,
        "interrupted_with_result": sum(
            1 for item in slices if item.get("turn_completion") == "interrupted_with_result"
        ),
    }
    if hasattr(store, "list_omissions"):
        omission_filters = {"from_ms": window.from_ms, "to_ms": window.to_ms}
        if sources:
            cleaning_summary["explicit_abort_without_work"] = sum(
                len(store.list_omissions({**omission_filters, "source": source}))
                for source in sorted(set(sources))
            )
        else:
            cleaning_summary["explicit_abort_without_work"] = len(store.list_omissions(omission_filters))

    activities: List[Dict[str, Any]] = []
    excluded = {
        "low_signal_slices": 0,
        "omitted_blocks": 0,
        "truncated_texts": 0,
        "reduced_texts": 0,
        "omitted_slice_refs": 0,
        "omitted_delegations": 0,
        "injected_blocks": 0,
        "budget_activities": 0,
        "mechanism_only": 0,
        "no_quotable_text": 0,
    }
    # The whole window is clustered before anything is selected: repeated
    # scaffolding can only be recognised against every activity in it, and a
    # ``--project`` pack has to see the same scaffolding as a full one.
    all_facts = [
        _cluster_facts(cluster, resolved_map)
        for cluster in _cluster_slices(slices, gap_ms=gap_ms)
    ]
    drop = _assign_excerpts([facts for facts in all_facts if facts["readable"]])
    for facts in all_facts:
        if project and facts["resolution"].project_key != project:
            continue
        if wanted and str(facts["activity_id"]) not in wanted:
            continue
        reason = _suppression_reason(facts)
        # An id names one activity, so asking for it is a decision already
        # made; the tally only stands in for rows nobody asked for.
        if reason is not None and str(facts["activity_id"]) not in wanted:
            excluded[reason] += 1
            continue
        activity, stats = _activity(
            facts, blocks_limit=blocks, block_chars=block_chars, drop=drop)
        for key in ("low_signal_slices", "omitted_blocks", "truncated_texts", "reduced_texts",
                    "omitted_slice_refs", "omitted_delegations", "injected_blocks"):
            excluded[key] += stats[key]
        if activity is not None:
            activities.append(activity)
    activities.sort(key=lambda item: (-int(item["signal_score"]), str(item["ended_at"]), str(item["activity_id"])))

    base = {
        "schema_version": 1,
        "view": "detail",
        "window": window.to_dict(),
        "filters": {
            "sources": sorted(set(sources)),
            "conversation": conversation,
            "workspace": workspace,
            "project": project,
            "query": query,
            "activities": sorted(wanted),
            "blocks": blocks,
            "block_chars": block_chars,
            "gap_ms": gap_ms,
            "max_bytes": max_bytes,
        },
        "source_slice_counts": source_counts,
        "coverage_summary": coverage_counts,
        "cleaning_summary": cleaning_summary,
        "activities": [],
        "excluded_summary": excluded,
    }
    stubs = {str(item["activity_id"]): _omitted_stub(item) for item in activities}

    def omitted_for(kept: Iterable[str]) -> List[Dict[str, Any]]:
        kept_ids = set(kept)
        return [
            stubs[str(item["activity_id"])]
            for item in activities
            if str(item["activity_id"]) not in kept_ids
        ][:MAX_OMITTED_ACTIVITIES]

    selected: List[Dict[str, Any]] = []
    selected_ids: set = set()
    for activity in activities:
        candidate = dict(base)
        candidate["activities"] = [*selected, activity]
        trial_ids = selected_ids | {str(activity["activity_id"])}
        candidate["omitted_activities"] = omitted_for(trial_ids)
        candidate["excluded_summary"] = {**excluded, "budget_activities": len(activities) - len(trial_ids)}
        if len(canonical_json(candidate).encode("utf-8")) > max_bytes:
            excluded["budget_activities"] += 1
            continue
        selected.append(activity)
        selected_ids.add(str(activity["activity_id"]))

    def finalize_pack() -> Dict[str, Any]:
        result = dict(base)
        result["activities"] = list(selected)
        result["excluded_summary"] = dict(excluded)
        selected_keys = {str(item["project_key"]) for item in selected}
        result["project_buckets"] = [
            {
                "project_key": key,
                "activity_ids": [item["activity_id"] for item in selected if item["project_key"] == key],
            }
            for key in sorted(selected_keys)
        ]
        # A detail pack is a sample.  Name the projects it did not reach so a
        # report never mistakes this list for the whole window.
        selected_ids = {str(item["activity_id"]) for item in selected}
        dropped: Dict[str, int] = {}
        for activity in activities:
            if str(activity["activity_id"]) in selected_ids:
                continue
            key = str(activity["project_key"])
            dropped[key] = dropped.get(key, 0) + 1
        result["omitted_by_project"] = [
            {"project_key": key, "activities": count}
            for key, count in sorted(dropped.items())
        ]
        result["omitted_activities"] = omitted_for(selected_ids)
        # An activity id is derived from the window's clustering, so asking
        # for one under a different ``--from/--to/--gap-minutes`` matches
        # nothing.  Say so instead of returning an empty pack.
        result["missing_activities"] = sorted(
            wanted - {str(item["activity_id"]) for item in activities}
        )
        pack_basis = dict(result)
        pack_basis.pop("pack_id", None)
        pack_basis.pop("byte_size", None)
        result["pack_id"] = _stable_id("PAK", canonical_json(pack_basis))
        result["byte_size"] = 0
        while True:
            exact_size = len(canonical_json(result).encode("utf-8"))
            if exact_size == result["byte_size"]:
                return result
            result["byte_size"] = exact_size

    result = finalize_pack()
    while selected and result["byte_size"] > max_bytes:
        selected_ids.discard(str(selected.pop()["activity_id"]))
        excluded["budget_activities"] += 1
        result = finalize_pack()
    return result
