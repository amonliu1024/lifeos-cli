import json
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from lifeos_sessions.cli import command_rebuild
from lifeos_sessions.core import (
    AdapterResult,
    SessionError,
    SessionsService,
    TimeWindow,
    TurnOmission,
)
from lifeos_sessions.responses import SourceProfile, build_session_document, normalize_sessions
from lifeos_sessions.semantics import build_execution_evidence, has_substantive_result
from lifeos_sessions.store import SessionsStore


WINDOW = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")


def _record(turn_id, kind, seconds, **payload):
    payload.setdefault("type", kind)
    payload.setdefault("turn_id", turn_id)
    return {
        "timestamp": f"2026-08-08T00:00:{seconds:02d}Z",
        "type": kind,
        "payload": payload,
    }


def _normalize(turn_id, records):
    document = build_session_document(
        source="codex",
        locator=f"fixture-{turn_id}",
        records=[
            _record(turn_id, "session_meta", 0, id="conversation-1"),
            *records,
        ],
    )
    return normalize_sessions([document], WINDOW, SourceProfile("codex", "7"))


class AbortSemanticsTest(unittest.TestCase):
    def test_final_unknown_and_explicit_abort_matrix(self):
        final = _normalize(
            "final",
            [
                _record("final", "user_message", 1, text="完成"),
                _record("final", "assistant_message", 2, text="已完成", phase="final"),
            ],
        )
        self.assertEqual(["completed"], [item["turn_completion"] for item in final.slices])
        self.assertEqual((), final.omissions)

        unknown = _normalize(
            "unknown",
            [
                _record("unknown", "user_message", 1, text="继续"),
                _record("unknown", "future_payload", 2, value="near miss"),
            ],
        )
        self.assertEqual(["incomplete"], [item["turn_completion"] for item in unknown.slices])
        self.assertTrue(any("unknown_record_type" in warning for warning in unknown.slices[0]["warnings"]))

        exact_noise = _normalize(
            "exact-noise",
            [
                _record("exact-noise", "user_message", 1, text="继续"),
                _record("exact-noise", "inter_agent_communication_metadata", 2, opaque="bridge"),
            ],
        )
        self.assertFalse(any("unknown_record_type" in warning for warning in exact_noise.slices[0]["warnings"]))
        near_miss = _normalize(
            "near-miss",
            [
                _record("near-miss", "user_message", 1, text="继续"),
                _record("near-miss", "inter_agent_communication_metadata_v2", 2),
            ],
        )
        self.assertTrue(any("unknown_record_type" in warning for warning in near_miss.slices[0]["warnings"]))
        user_text = _normalize(
            "user-text",
            [_record("user-text", "user_message", 1, text="inter_agent_communication_metadata")],
        )
        self.assertFalse(any("unknown_record_type" in warning for warning in user_text.slices[0]["warnings"]))

        for label, tool in (
            ("user", None),
            ("partial", "prose"),
            ("read", {"arguments": {"cmd": "cat file && sed -n 1p file"}}),
        ):
            records = [_record(label, "user_message", 1, text="停止")]
            if tool == "prose":
                records.append(_record(label, "assistant_message", 2, text="半截说明"))
            elif tool:
                records.append(_record(label, "exec_command", 2, **tool))
            records.append(_record(label, "turn_aborted", 3, reason="用户取消"))
            result = _normalize(label, records)
            self.assertEqual(0, len(result.slices), label)
            self.assertEqual(1, len(result.omissions), label)
            self.assertEqual("explicit_abort_without_work", result.omissions[0]["reason"])

        mutation = _normalize(
            "mutation",
            [
                _record("mutation", "user_message", 1, text="写入"),
                _record("mutation", "exec_command", 2, arguments={"cmd": "cat file && touch output"}),
                _record("mutation", "turn_aborted", 3),
            ],
        )
        self.assertEqual(["interrupted_with_result"], [item["turn_completion"] for item in mutation.slices])

        # Read-oriented command names do not make known mutating forms safe to
        # omit: an explicit abort after either command still preserves a slice.
        for label, command in (
            ("sed-in-place", "sed -i x file"),
            ("find-delete", "find . -delete"),
        ):
            result = _normalize(
                label,
                [
                    _record(label, "user_message", 1, text="继续处理"),
                    _record(label, "exec_command", 2, arguments={"cmd": command}),
                    _record(label, "turn_aborted", 3),
                ],
            )
            self.assertEqual(["interrupted_with_result"], [item["turn_completion"] for item in result.slices], label)
            self.assertEqual((), result.omissions, label)

        verification = _normalize(
            "verification",
            [
                _record("verification", "user_message", 1, text="检查"),
                _record("verification", "exec_command", 2, arguments={"cmd": "pytest tests"}),
                _record("verification", "turn_aborted", 3),
            ],
        )
        self.assertEqual(["interrupted_with_result"], [item["turn_completion"] for item in verification.slices])
        self.assertTrue(verification.slices[0]["execution_evidence"]["verifications"])

        failure = _normalize(
            "failure",
            [
                _record("failure", "user_message", 1, text="排查"),
                _record("failure", "exec_command", 2, status="failed", error="pytest: 3 failed"),
                _record("failure", "turn_aborted", 3),
            ],
        )
        self.assertEqual(["interrupted_with_result"], [item["turn_completion"] for item in failure.slices])
        self.assertEqual(["pytest: 3 failed"], failure.slices[0]["execution_evidence"]["failures"])

        final_after_abort = _normalize(
            "final-after-abort",
            [
                _record("final-after-abort", "user_message", 1, text="完成"),
                _record("final-after-abort", "assistant_message", 2, text="最终结果", phase="final"),
                _record("final-after-abort", "turn_aborted", 3),
            ],
        )
        self.assertEqual(["completed"], [item["turn_completion"] for item in final_after_abort.slices])

        marker_text = _normalize(
            "marker-text",
            [_record("marker-text", "user_message", 1, text="用户说 turn_aborted 只是示例")],
        )
        self.assertEqual(["incomplete"], [item["turn_completion"] for item in marker_text.slices])
        self.assertEqual((), marker_text.omissions)

    def test_completed_delegation_is_substantive_and_only_read_only_compounds_are_empty(self):
        evidence = build_execution_evidence()
        self.assertTrue(
            has_substantive_result(
                evidence,
                completed_delegations=[{"status": "complete", "result": "核对完成"}],
            )
        )
        self.assertFalse(has_substantive_result(evidence, successful_commands=["cat a && sed -n 1p b: passed"]))
        self.assertTrue(has_substantive_result(evidence, successful_commands=["cat a && touch b: passed"]))


class _FakeAdapter:
    def __init__(self, source, status="complete"):
        self.name = source
        self.adapter_version = "fixture"
        self.status = status

    def scan(self, _request):
        return AdapterResult(
            source=self.name,
            status=self.status,
            error={"code": "fixture_failed"} if self.status == "failed" else None,
        )


class RebuildAndOmissionTest(unittest.TestCase):
    def test_omission_is_idempotent_and_never_enters_fts(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            omission = TurnOmission(
                source="codex",
                conversation={"id": "conversation-1"},
                native_unit={"kind": "turn", "id": "turn-1"},
                at="2026-08-08T00:00:01Z",
                source_ref="codex://conversation-1/turn/turn-1/event/abort",
                adapter={"name": "codex", "version": "7"},
            ).finalize()
            first = store.commit_scan(
                "SCN-omission-1",
                [AdapterResult("codex", omissions=[omission])],
                WINDOW,
            )
            self.assertEqual(1, len(store.list_omissions()))
            self.assertEqual([], store.list_slices({"query": "explicit_abort_without_work"}))
            second = store.commit_scan(
                "SCN-omission-2",
                [AdapterResult("codex", reused_omission_ids=[omission.omission_id])],
                WINDOW,
            )
            self.assertEqual(1, len(store.list_omissions()))
            self.assertEqual(1, second["sources"][0]["stats"]["reused_omissions"])
            self.assertEqual([], store.validate())

    def test_apply_requires_all_sources_and_failed_staging_keeps_active_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            active_root = Path(temporary) / "sessions"
            active = SessionsStore(active_root, create=True)
            before = {
                str(path.relative_to(active_root)): path.read_bytes()
                for path in active_root.rglob("*")
                if path.is_file()
            }
            failed_adapters = {
                source: _FakeAdapter(source, "failed" if source == "claude" else "complete")
                for source in ("codex", "claude", "smartwork", "deepseek", "pi")
            }
            service = SessionsService(failed_adapters, active)
            result = service.rebuild(WINDOW, sources=list(failed_adapters), apply=True)
            self.assertFalse(result["rebuild"]["applied"])
            self.assertEqual(["claude"], result["rebuild"]["failed_sources"])
            after = {
                str(path.relative_to(active_root)): path.read_bytes()
                for path in active_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            with self.assertRaises(SessionError):
                service.rebuild(WINDOW, sources=["codex"], apply=True)

    def test_successful_apply_switches_whole_tree_and_cli_rejects_failed_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            active_root = Path(temporary) / "sessions"
            active = SessionsStore(active_root, create=True)
            (active_root / "projects.json").write_text(
                '{"schema_version":1,"projects":[{"key":"kept","roots":["/kept"]}]}',
                encoding="utf-8",
            )
            (active_root / "retention.json").write_text(
                '{"schema_version":1,"keep_slices_days":null,"fts_days":null}',
                encoding="utf-8",
            )
            adapters = {source: _FakeAdapter(source) for source in ("codex", "claude", "smartwork", "deepseek", "pi")}
            result = SessionsService(adapters, active).rebuild(
                WINDOW, sources=list(adapters), apply=True
            )
            self.assertTrue(result["rebuild"]["applied"])
            self.assertFalse(result["rebuild"]["active_unchanged"])
            self.assertTrue(result["rebuild"]["backup"])
            self.assertEqual([], active.validate())
            self.assertFalse((active_root / "projects.json").exists())
            self.assertTrue((active_root / "retention.json").is_file())

            failing_service = mock.Mock()
            failing_service.rebuild.return_value = {
                "status": "partial",
                "sources": [{"source": "claude", "status": "failed"}],
                "rebuild": {
                    "apply_requested": True,
                    "applied": False,
                    "failed_sources": ["claude"],
                    "validation_errors": [],
                },
            }
            args = Namespace(
                from_value="2026-08-08T00:00:00Z",
                to_value="2026-08-09T00:00:00Z",
                source=["all"],
                include=[],
                apply=True,
                json=True,
                sessions_root=Path(temporary) / "cli-sessions",
            )
            with mock.patch(
                "lifeos_sessions.cli._sources",
                return_value=["codex", "claude", "smartwork", "deepseek", "pi"],
            ), mock.patch("lifeos_sessions.cli._service", return_value=failing_service):
                with self.assertRaises(SystemExit) as raised:
                    command_rebuild(args)
            self.assertEqual(1, raised.exception.code)

    def test_activation_failure_restores_the_old_active_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            marker = root / "active-marker"
            marker.write_text("old", encoding="utf-8")
            before = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            staging = Path(temporary) / "staging"
            staging.mkdir()
            (staging / "new-marker").write_text("new", encoding="utf-8")
            original_replace = __import__("os").replace
            calls = {"count": 0}

            def replace_with_failure(source, target):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("synthetic activation failure")
                return original_replace(source, target)

            with mock.patch("lifeos_sessions.store.os.replace", side_effect=replace_with_failure):
                with self.assertRaises(OSError):
                    store.activate_staging(staging)
            after = {
                str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertTrue(staging.exists())

    def test_activation_stable_lock_blocks_writer_while_active_tree_is_moved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            store = SessionsStore(root, create=True)
            marker = root / "active-marker"
            marker.write_text("old", encoding="utf-8")

            staging = Path(temporary) / "staging"
            staging.mkdir()
            (staging / "new-marker").write_text("new", encoding="utf-8")

            original_replace = __import__("os").replace
            old_tree_moved = threading.Event()
            release_switch = threading.Event()
            activation_done = threading.Event()
            writer_started = threading.Event()
            writer_done = threading.Event()
            errors = []
            writer_results = []
            calls = {"count": 0}

            def replace_with_pause(source, target):
                calls["count"] += 1
                result = original_replace(source, target)
                if calls["count"] == 1:
                    old_tree_moved.set()
                    if not release_switch.wait(5):
                        raise OSError("timed out waiting to complete activation")
                return result

            def activate():
                try:
                    store.activate_staging(staging)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(("activation", exc))
                finally:
                    activation_done.set()

            def write_after_switch_starts():
                writer_started.set()
                try:
                    result = store.commit_scan(
                        "SCN-concurrent-writer",
                        [AdapterResult("codex")],
                        WINDOW,
                    )
                    writer_results.append(result)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(("writer", exc))
                finally:
                    writer_done.set()

            activation_thread = threading.Thread(target=activate)
            writer_thread = None
            with mock.patch("lifeos_sessions.store.os.replace", side_effect=replace_with_pause):
                activation_thread.start()
                try:
                    self.assertTrue(old_tree_moved.wait(5))
                    self.assertFalse(root.exists())

                    writer_thread = threading.Thread(target=write_after_switch_starts)
                    writer_thread.start()
                    self.assertTrue(writer_started.wait(5))
                    self.assertFalse(writer_done.wait(0.2))
                    # ``ensure_initialized`` is also behind the stable lock,
                    # so a waiting writer cannot recreate the active path in
                    # this gap.
                    self.assertFalse(root.exists())

                    release_switch.set()
                    self.assertTrue(activation_done.wait(5))
                    self.assertTrue(writer_done.wait(5))
                finally:
                    release_switch.set()
                    activation_thread.join(5)
                    if writer_thread is not None:
                        writer_thread.join(5)

            self.assertFalse(activation_thread.is_alive())
            self.assertIsNotNone(writer_thread)
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, len(writer_results))
            self.assertEqual("new", (root / "new-marker").read_text(encoding="utf-8"))
            self.assertTrue(store.switch_lock_path.exists())
            self.assertEqual([], store.validate())


if __name__ == "__main__":
    unittest.main()
