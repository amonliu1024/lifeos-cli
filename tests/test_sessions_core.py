import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lifeos_sessions.core import (
    AdapterResult,
    ConversationSlice,
    SessionValidationError,
    SessionsService,
    SourceScanRequest,
    TimeWindow,
    adapter_cache_generation,
    canonical_revision,
    stable_slice_id,
    validate_slice,
)
from lifeos_sessions.store import SessionsStore, StoreError


def make_slice(source="codex", conversation="thread-1", native_id="turn-1", *, text="hello", at="2026-08-08T00:00:00Z", ended="2026-08-08T00:01:00Z", **extra):
    payload = {
        "schema_version": 1,
        "source": source,
        "conversation": {"id": conversation, "title": "Synthetic"},
        "native_unit": {"kind": "turn", "id": native_id},
        "started_at": at,
        "ended_at": ended,
        "workspace": "/synthetic",
        "blocks": [{
            "kind": "message",
            "author_role": "self",
            "origin": "user",
            "at": at,
            "text": text,
            "context": False,
            "source_refs": [f"{source}:{native_id}"],
        }],
        "execution_evidence": {
            "changed_targets": [], "other_targets": [], "tool_calls": [],
            "verifications": [], "failures": [], "user_interrupts": [], "omitted_count": 0,
        },
        "delegations": [],
        "content_completeness": "complete",
        "provenance_trimmed": False,
        "warnings": [],
        "source_refs": [f"{source}:{native_id}"],
        "source_meta": {},
        "adapter": {"name": source, "version": "test"},
        **extra,
    }
    return ConversationSlice.from_dict(payload)


class CoreContractTest(unittest.TestCase):
    def test_adapter_cache_generation_covers_source_shared_and_slice_revisions(self):
        baseline = adapter_cache_generation("codex", "1")
        self.assertEqual(baseline, adapter_cache_generation("codex", "1"))
        self.assertNotEqual(baseline, adapter_cache_generation("codex", "2"))
        self.assertNotEqual(
            baseline,
            adapter_cache_generation("codex", "1", shared_revision=2),
        )
        self.assertNotEqual(
            baseline,
            adapter_cache_generation("codex", "1", slice_schema_version=2),
        )

    def test_request_normalises_window_and_rejects_invalid_bounds_or_include(self):
        window = TimeWindow.from_values("2026-08-08T08:00:00+08:00", "2026-08-08T09:00:00+08:00")
        self.assertEqual("2026-08-08T00:00:00Z", window.from_iso)
        self.assertTrue(window.contains(window.from_ms))
        self.assertFalse(window.contains(window.to_ms))
        with self.assertRaises(SessionValidationError):
            TimeWindow.from_values("2026-08-08T08:00:00", "2026-08-08T09:00:00+08:00")
        with self.assertRaises(SessionValidationError):
            TimeWindow.from_values("2026-08-08T09:00:00Z", "2026-08-08T08:00:00Z")
        with self.assertRaisesRegex(SessionValidationError, "source:conversation-id"):
            SourceScanRequest(window, includes=("thread-1",))

    def test_slice_identity_validation_and_adapter_contract_are_source_neutral(self):
        first = make_slice().finalize(materialized_at="2026-08-08T01:00:00Z")
        second = make_slice().finalize(materialized_at="2026-08-08T02:00:00Z")
        self.assertEqual(first.slice_id, second.slice_id)
        self.assertEqual(first.revision, second.revision)
        self.assertEqual(
            first.slice_id,
            stable_slice_id("codex", "thread-1", "turn", "turn-1"),
        )
        self.assertEqual(first.revision, canonical_revision(first))
        item = make_slice(source="smartwork", native_id="turn-1", ended="2026-08-08T00:00:00Z").finalize()
        self.assertEqual([], validate_slice(item, require_identity=True))
        non_turn = make_slice()
        non_turn.native_unit = {"kind": "message", "id": "message-1"}
        self.assertIn("native_unit.kind must be turn", validate_slice(non_turn, require_identity=True))
        bad = make_slice(ended="2026-08-07T23:59:00Z")
        self.assertTrue(any("after ended_at" in error for error in validate_slice(bad)))
        quality = make_slice(
            quality_flags=["unconfirmed_outcome"],
            omissions=["source_noise:context_compacted"],
        ).finalize()
        self.assertEqual(["unconfirmed_outcome"], quality.quality_flags)
        self.assertEqual(["source_noise:context_compacted"], quality.omissions)
        self.assertEqual([], validate_slice(quality, require_identity=True))
        invalid_delegations = make_slice(delegations=[{
            "agent_id": None,
            "status": "partial",
            "result": None,
            "unknown_ref": "unsupported",
        }])
        delegation_errors = validate_slice(invalid_delegations)
        self.assertTrue(any("unsupported keys: unknown_ref" in error for error in delegation_errors))
        self.assertTrue(any("missing required keys: task" in error for error in delegation_errors))
        window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-08T00:01:00Z")
        request = SourceScanRequest(window, includes=("codex:thread-1",), checkpoint={"offset": 1})
        result = AdapterResult("codex", slices=[make_slice()], checkpoint={"offset": 2}, stats={"matched": 1})
        self.assertEqual(["codex:thread-1"], list(request.includes))
        self.assertEqual("codex", result.to_dict()["source"])
        self.assertNotIn("tool_call", request.to_dict())
        self.assertNotIn("cwd", request.to_dict())

    def test_slice_requires_an_explicit_schema1(self):
        payload = make_slice().to_dict()
        payload.pop("schema_version")
        self.assertIn("schema_version must be 1", validate_slice(payload))
        with self.assertRaisesRegex(SessionValidationError, "schema_version"):
            ConversationSlice.from_dict(payload)

        payload["schema_version"] = 2
        self.assertIn("schema_version must be 1", validate_slice(payload))


class StoreAndServiceTest(unittest.TestCase):
    def test_store_is_private_idempotent_and_fts_searchable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice(text="已检查并完成合成测试").finalize(materialized_at="2026-08-08T01:00:00Z")
            result = AdapterResult("codex", slices=[item], checkpoint={"offset": 4})
            first = store.commit_scan("SCN-synthetic-1", [result], window)
            second = store.commit_scan("SCN-synthetic-2", [result], window)
            self.assertEqual(1, first["sources"][0]["stats"]["created"])
            self.assertEqual(1, second["sources"][0]["stats"]["reused"])
            self.assertEqual(item.slice_id, store.list_slices({"query": "已检查"})[0]["slice_id"])
            self.assertEqual(item.slice_id, store.list_slices({"query": "检查"})[0]["slice_id"])
            self.assertEqual(item.revision, store.show(item.slice_id)["revision"])
            self.assertEqual([], store.validate())
            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((root / "index.sqlite3").stat().st_mode))
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (root / "slices").rglob("*") if path.is_dir()))
            self.assertEqual({"offset": 4}, store.load_checkpoint("codex"))

            cached = store.commit_scan(
                "SCN-synthetic-3",
                [AdapterResult("codex", reused_slice_ids=[item.slice_id], checkpoint={"offset": 4}, stats={"examined": 0, "matched": 1})],
                window,
            )
            self.assertEqual(1, cached["sources"][0]["stats"]["reused"])
            self.assertEqual(item.slice_id, store.list_slices({"scan": "SCN-synthetic-3"})[0]["slice_id"])
            duplicate = make_slice(conversation="thread-2", native_id="turn-2").finalize()
            manifest = store.commit_scan(
                "SCN-duplicate-candidates",
                [AdapterResult("codex", slices=[duplicate, duplicate])],
                window,
            )
            stats = manifest["sources"][0]["stats"]
            self.assertEqual(2, stats["candidate_count"])
            self.assertEqual(1, stats["matched"])
            self.assertEqual(1, stats["created"])
            self.assertEqual(1, stats["duplicate_candidates"])
            self.assertEqual(1, len(store.list_slices({"scan": "SCN-duplicate-candidates"})))

    def test_conflicting_candidate_error_identifies_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            first = make_slice(text="第一个候选").finalize()
            second = make_slice(text="冲突候选").finalize()
            self.assertEqual(first.slice_id, second.slice_id)
            with self.assertRaisesRegex(StoreError, rf"source codex.*{first.slice_id}"):
                store.commit_scan(
                    "SCN-conflicting-candidates",
                    [AdapterResult("codex", slices=[first, second])],
                    window,
                )

    def test_validate_reports_integrity_and_corrupt_json_without_raising(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice(text="只应出现在真实索引中").finalize()
            store.commit_scan("SCN-integrity", [AdapterResult("codex", slices=[item])], window)
            source_dir = root / "slices" / "codex"
            os.chmod(source_dir, 0o755)
            connection = sqlite3.connect(root / "index.sqlite3")
            try:
                connection.execute("UPDATE slice_fts SET text = ? WHERE slice_id = ?", ("被篡改的索引", item.slice_id))
                connection.commit()
            finally:
                connection.close()
            errors = store.validate()
            self.assertTrue(any("expected 0700" in error and str(source_dir) in error for error in errors))
            self.assertTrue(any("FTS content mismatch" in error for error in errors))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice().finalize()
            store.commit_scan("SCN-invalid-schema", [AdapterResult("codex", slices=[item])], window)
            revision_path = next((root / "slices").rglob("*.json"))
            payload = json.loads(revision_path.read_text(encoding="utf-8"))
            payload.pop("blocks")
            revision_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            errors = store.validate()
            self.assertTrue(any(str(revision_path) in error and "blocks" in error for error in errors))
            self.assertTrue(any("cannot derive FTS payload" in error for error in errors))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice().finalize()
            store.commit_scan("SCN-non-object", [AdapterResult("codex", slices=[item])], window)
            revision_path = next((root / "slices").rglob("*.json"))
            manifest_path = root / "scans" / "SCN-non-object.json"
            revision_path.write_text("[]\n", encoding="utf-8")
            manifest_path.write_text("[]\n", encoding="utf-8")
            errors = store.validate()
            self.assertTrue(any(str(revision_path) in error and "expected object" in error for error in errors))
            self.assertTrue(any(str(manifest_path) in error and "expected object" in error for error in errors))
            with self.assertRaisesRegex(StoreError, "must contain an object"):
                store.show(item.slice_id)

    def test_manifest_write_failure_returns_and_persists_partial_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice().finalize()
            original = store._atomic_json

            def fail_manifest(path, value):
                if path.parent == store.scans_dir:
                    raise OSError("synthetic manifest failure")
                return original(path, value)

            with mock.patch.object(store, "_atomic_json", side_effect=fail_manifest):
                manifest = store.commit_scan(
                    "SCN-manifest-failure",
                    [AdapterResult("codex", slices=[item])],
                    window,
                )
            self.assertEqual("partial", manifest["status"])
            self.assertIn("synthetic manifest failure", manifest["manifest_error"])
            self.assertTrue(any("missing scan manifest" in error for error in store.validate("SCN-manifest-failure")))
            connection = sqlite3.connect(root / "index.sqlite3")
            try:
                persisted = connection.execute(
                    "SELECT status FROM scans WHERE scan_id = ?",
                    ("SCN-manifest-failure",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("partial", persisted)

    def test_checkpoint_reuse_refuses_missing_revision_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice().finalize()
            store.commit_scan("SCN-reuse-source", [AdapterResult("codex", slices=[item])], window)
            next((root / "slices").rglob("*.json")).unlink()
            with self.assertRaisesRegex(StoreError, "reused slice content is unavailable"):
                store.commit_scan(
                    "SCN-reuse-missing",
                    [AdapterResult("codex", reused_slice_ids=[item.slice_id])],
                    window,
                )

    def test_service_scans_in_order_and_isolates_adapter_errors(self):
        window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
        calls = []

        class Good:
            name = "good"

            def scan(self, request):
                calls.append(self.name)
                return AdapterResult(self.name, slices=[make_slice(source=self.name)])

        class Bad:
            name = "bad"

            def scan(self, request):
                calls.append(self.name)
                raise RuntimeError("synthetic adapter failure")

        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            manifest = SessionsService([Good(), Bad()], store).scan(window, sources=["good", "bad"])
            self.assertEqual(["good", "bad"], calls)
            self.assertEqual("partial", manifest["status"])
            self.assertEqual("failed", next(item for item in manifest["sources"] if item["source"] == "bad")["status"])
            self.assertEqual(1, len(store.list_slices()))

    def test_read_only_commands_do_not_create_a_never_used_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root)
            self.assertTrue(store.validate())
            self.assertFalse(root.exists())
            self.assertEqual([], store.list_slices())
            with self.assertRaises(KeyError):
                store.show("SLC-missing")
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
