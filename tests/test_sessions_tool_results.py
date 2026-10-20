import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifeos_config.core import default_payload
from lifeos_sessions.claude import ClaudeAdapter
from lifeos_sessions.codex import CodexAdapter
from lifeos_sessions.deepseek import DeepseekAdapter
from lifeos_sessions.pi import PiAdapter
from lifeos_sessions.smartwork import SmartworkAdapter
from lifeos_sessions.core import SessionError, SourceScanRequest, TimeWindow
from lifeos_sessions.evidence import read_evidence


WINDOW = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
SCRIPT = Path(__file__).resolve().parents[1] / "lifeos.py"


def event(kind, payload, second):
    return {"timestamp": f"2026-08-08T00:00:{second:02d}Z", "type": kind, "payload": payload}


def codex_records(tools):
    return [
        event("session_meta", {"id": "synthetic"}, 0),
        event("turn_context", {"turn_id": "turn-1"}, 1),
        event("event_msg", {"type": "user_message", "message": "运行检查"}, 2),
        *[event("response_item", payload, index + 3) for index, payload in enumerate(tools)],
        event("response_item", {"type": "message", "role": "assistant", "phase": "final",
                               "content": [{"type": "output_text", "text": "已结束本轮操作"}]}, 50),
    ]


class ToolResultEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def scan_codex(self, tools):
        path = self.root / "sessions" / "rollout-test.jsonl"
        path.parent.mkdir(exist_ok=True)
        # Blank lines must not displace a saved physical record.
        path.write_text("\n\n" + "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                       for r in codex_records(tools)), encoding="utf-8")
        result = CodexAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
        self.assertEqual(1, len(result.slices))
        item = result.slices[0]
        return item.to_dict() if hasattr(item, "to_dict") else item, path

    def test_concurrent_results_match_ids_not_order_and_missing_stays_pending(self):
        calls = [{"type": "function_call", "call_id": key, "name": "exec_command",
                  "arguments": json.dumps({"cmd": command})}
                 for key, command in (("a", "pytest a"), ("b", "pytest b"), ("c", "pytest c"))]
        item, _ = self.scan_codex(calls + [
            {"type": "function_call_output", "call_id": "b", "output": '{"exit_code": 1, "output": "b failed"}'},
            {"type": "function_call_output", "call_id": "a", "output": "Process exited with code 0\n10 passed"},
        ])
        evidence = item["execution_evidence"]
        self.assertEqual(["pytest a: passed", "pytest b: failed", "pytest c: pending"], evidence["verifications"])
        self.assertEqual(1, len(evidence["failures"]))
        self.assertIn("pytest b", evidence["failures"][0])

    def test_running_unknown_interrupted_and_failure_cannot_become_success(self):
        for output, status in [
            ('{"session_id": 123, "output": "still running"}', "pending"),
            ('{"output": "agent says tests passed"}', "unknown"),
            ('{"interrupted": true}', "interrupted"),
            ('{"exit_code": 1, "status": "success"}', "failed"),
        ]:
            with self.subTest(output=output):
                item, _ = self.scan_codex([
                    {"type": "function_call", "call_id": "a", "name": "exec_command",
                     "arguments": '{"cmd":"pytest"}'},
                    {"type": "function_call_output", "call_id": "a", "output": output},
                ])
                self.assertEqual([f"pytest: {status}"], item["execution_evidence"]["verifications"])

    def test_failed_edit_does_not_claim_changed_target(self):
        item, _ = self.scan_codex([
            {"type": "custom_tool_call", "call_id": "a", "name": "apply_patch",
             "input": "*** Begin Patch\n*** Add File: x.py\n+x\n*** End Patch"},
            {"type": "custom_tool_call_output", "call_id": "a", "output": '{"success": false}'},
        ])
        self.assertEqual([], item["execution_evidence"]["changed_targets"])
        self.assertTrue(item["execution_evidence"]["failures"])

    def test_claude_native_error_and_missing_result_are_distinct(self):
        for status in ("passed", "failed", "pending", "interrupted"):
            with self.subTest(status=status):
                records = [
                    {"type": "user", "uuid": "u", "sessionId": "s", "timestamp": "2026-08-08T00:00:00Z",
                     "message": {"role": "user", "content": "请检查"}},
                    {"type": "assistant", "uuid": "a", "parentUuid": "u", "sessionId": "s",
                     "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": "call", "name": "Bash", "input": {"command": "pytest"}}]}},
                ]
                if status != "pending":
                    records.append({"type": "user", "uuid": "r", "parentUuid": "a", "sessionId": "s",
                                    "timestamp": "2026-08-08T00:00:02Z",
                                    "toolUseResult": {"interrupted": status == "interrupted"},
                                    "message": {"role": "user", "content": [
                                        {"type": "tool_result", "tool_use_id": "call", "is_error": status == "failed",
                                         "content": "Exit code 1\n1 failed" if status == "failed" else "output"}]}})
                records.append({"type": "assistant", "uuid": "f", "parentUuid": "r" if status != "pending" else "a",
                                "sessionId": "s", "timestamp": "2026-08-08T00:00:03Z",
                                "message": {"role": "assistant", "content": "已结束", "stop_reason": "end_turn"}})
                path = self.root / "-project" / "s.jsonl"
                path.parent.mkdir(exist_ok=True)
                path.write_text("".join(json.dumps(r) + "\n" for r in records))
                result = ClaudeAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
                item = result.slices[0]
                item = item.to_dict() if hasattr(item, "to_dict") else item
                self.assertEqual([f"pytest: {status}"], item["execution_evidence"]["verifications"])
                self.assertEqual(status in {"failed", "interrupted"}, bool(item["execution_evidence"]["failures"]))
                if status != "pending":
                    page = read_evidence(item, "claude:s:r", self.root)
                    self.assertIn("tool_result", page["text"])
        # Native text blocks may carry a shell state below a successful
        # transport envelope.
        for text, expected in [
            ("Exit code: 1\n1 failed", "failed"),
            ('{"exit_code":1}', "failed"),
            ('{"session_id":123,"output":"still running"}', "pending"),
            ("Exit code: 0\n10 passed", "passed"),
        ]:
            with self.subTest(native_text=text):
                records[2]["toolUseResult"] = {}
                records[2]["message"]["content"][0].update(
                    is_error=False, content=[{"type": "text", "text": text}, {"type": "text", "text": "extra diagnostic"}])
                path.write_text("".join(json.dumps(r) + "\n" for r in records))
                result = ClaudeAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
                item = result.slices[0]
                item = item.to_dict() if hasattr(item, "to_dict") else item
                self.assertEqual([f"pytest: {expected}"], item["execution_evidence"]["verifications"])

    def test_exact_read_pagination_append_and_changed_record(self):
        item, path = self.scan_codex([
            {"type": "function_call_output", "call_id": "a", "output": "结果" * 5000},
        ])
        ref = item["source_meta"]["evidence_records"][0]["ref"]
        first = read_evidence(item, ref, self.root, limit=1000)
        self.assertTrue(first["truncated"])
        pieces = [first["text"]]
        position = first["next_offset"]
        while position is not None:
            page = read_evidence(item, ref, self.root, offset=position, limit=1000)
            pieces.append(page["text"])
            position = page["next_offset"]
        self.assertEqual("结果" * 5000, json.loads("".join(pieces))["payload"]["output"])
        with path.open("a") as handle:
            handle.write("{}\n")
        self.assertEqual(first, read_evidence(item, ref, self.root, limit=1000))
        path.write_text(path.read_text().replace("结果", "变更"))
        with self.assertRaisesRegex(SessionError, "changed"):
            read_evidence(item, ref, self.root)

    def test_read_ref_rejects_unregistered_escape_symlink_missing_and_oversized(self):
        item, path = self.scan_codex([{"type": "function_call_output", "call_id": "a", "output": "x"}])
        entry = item["source_meta"]["evidence_records"][0]
        ref = entry["ref"]
        with self.assertRaises(SessionError):
            read_evidence(item, "made-up-ref", self.root)
        with self.assertRaises(SessionError):
            read_evidence(item, ref, self.root, limit=32001)
        for locator in ("../outside.jsonl", "/tmp/outside.jsonl", "auth.json"):
            original = entry["locator"]
            entry["locator"] = locator
            with self.assertRaises(SessionError):
                read_evidence(item, ref, self.root)
            entry["locator"] = original
        outside = self.root.parent / (self.root.name + "-outside.jsonl")
        try:
            outside.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(SessionError):
                read_evidence(item, ref, self.root)
            path.unlink()
            with self.assertRaises(SessionError):
                read_evidence(item, ref, self.root)
        finally:
            outside.unlink(missing_ok=True)

    def test_cli_scan_show_and_exact_read_use_only_temporary_home(self):
        home = self.root / "home"
        source = home / ".codex" / "sessions" / "rollout-test.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text("".join(json.dumps(r) + "\n" for r in codex_records([
            {"type": "function_call_output", "call_id": "a", "output": "exact evidence"},
        ])))
        config = default_payload()
        config["modules"]["projects"]["roots"] = [str(self.root)]
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config))
        env = {**os.environ, "HOME": str(home), "LIFEOS_HOME": str(self.root / "runtime"),
               "LIFEOS_CONFIG": str(config_path)}

        def cli(*args):
            result = subprocess.run([sys.executable, str(SCRIPT), "sessions", *args],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout)

        cli("scan", "--source", "codex", "--from", WINDOW.from_iso, "--to", WINDOW.to_iso, "--json")
        slices = cli("list", "--json")
        item = cli("show", slices[0]["slice_id"], "--json")
        ref = item["source_meta"]["evidence_records"][0]["ref"]
        page = cli("show", item["slice_id"], "--evidence-ref", ref, "--json")
        self.assertTrue(page["source_verified"])
        self.assertIn("exact evidence", page["text"])

    def test_smartwork_refs_resolve_relative_to_sessions_directory(self):
        source_root = self.root / "sessions"
        source_root.mkdir()
        path = source_root / "rollout-test.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in codex_records([
            {"type": "function_call", "call_id": "a", "name": "exec_command", "arguments": '{"cmd":"pytest"}'},
            {"type": "function_call_output", "call_id": "a", "output": '{"exit_code":0}'},
        ])))
        result = SmartworkAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
        item = result.slices[0]
        item = item.to_dict() if hasattr(item, "to_dict") else item
        self.assertEqual(["pytest: passed"], item["execution_evidence"]["verifications"])
        for entry in item["source_meta"]["evidence_records"]:
            self.assertTrue(read_evidence(item, entry["ref"], source_root)["source_verified"])

    def test_pi_projected_tool_records_read_original_messages(self):
        path = self.root / "s.jsonl"
        messages = [
            {"role": "user", "content": "请检查"},
            {"role": "assistant", "stopReason": "toolUse", "content": [
                {"type": "toolCall", "id": "a", "name": "bash", "arguments": {"command": "pytest"}}]},
            {"role": "toolResult", "toolCallId": "a", "toolName": "bash", "isError": True,
             "content": [{"type": "text", "text": "pi original failure"}]},
            {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "已结束"}]},
        ]
        records = [{"type": "session", "version": 3, "id": "s", "timestamp": "2026-08-08T00:00:00Z"}]
        for index, message in enumerate(messages):
            records.append({"type": "message", "id": f"m{index}", "parentId": f"m{index-1}" if index else None,
                            "timestamp": f"2026-08-08T00:00:0{index+1}Z", "message": message})
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        result = PiAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
        item = result.slices[0]
        item = item.to_dict() if hasattr(item, "to_dict") else item
        self.assertEqual(["pytest: failed"], item["execution_evidence"]["verifications"])
        texts = [read_evidence(item, entry["ref"], self.root)["text"]
                 for entry in item["source_meta"]["evidence_records"]]
        self.assertTrue(any("pi original failure" in text for text in texts))
        self.assertTrue(all('"type": "message"' in text for text in texts))
        for text, expected in [
            ("Exit code: 1\n1 failed", "failed"),
            ('{"exit_code":1}', "failed"),
            ('{"session_id":123,"output":"still running"}', "pending"),
            ("Exit code: 0\n10 passed", "passed"),
        ]:
            messages[2].update(isError=False, content=[{"type": "text", "text": text}, {"type": "text", "text": "extra diagnostic"}])
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            result = PiAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
            item = result.slices[0]
            item = item.to_dict() if hasattr(item, "to_dict") else item
            self.assertEqual([f"pytest: {expected}"], item["execution_evidence"]["verifications"])

    @unittest.skipUnless(shutil.which("zstd"), "zstd required")
    def test_deepseek_compressed_refs_read_native_record_not_projection(self):
        records = [
            {"type": "session", "id": "s", "createdAt": 1786147200000, "cwd": "/synthetic"},
            {"type": "turn/start", "time": 1786147201000, "data": {"turn": 1}},
            {"type": "user/message", "time": 1786147202000, "data": {"role": "user", "content": [
                {"type": "text", "text": "请检查"}]}},
            {"type": "tool/call", "time": 1786147203000, "data": {"turn": 1, "callId": "a", "name": "bash",
                "arguments": '{"command":"pytest"}'}},
            {"type": "tool/result", "time": 1786147204000, "data": {"turn": 1, "message": {
                "source": {"callId": "a"}, "content": [{"type": "tool-result", "isError": False,
                    "content": [{"type": "text", "text": "deepseek original success"}]}]}}},
            {"type": "assistant/message", "time": 1786147205000, "data": {"turn": 1, "message": {
                "role": "assistant", "content": [{"type": "text", "text": "已结束"}]}}},
        ]
        path = self.root / "session.jsonl.zstd"
        body = "\n\n" + "".join(json.dumps(r) + "\n" for r in records)
        subprocess.run(["zstd", "-q", "-o", str(path), "-f", "-"], input=body.encode(), check=True)
        result = DeepseekAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
        self.assertEqual(1, len(result.slices))
        item = result.slices[0]
        item = item.to_dict() if hasattr(item, "to_dict") else item
        self.assertEqual(["pytest: passed"], item["execution_evidence"]["verifications"])
        texts = [read_evidence(item, entry["ref"], self.root)["text"]
                 for entry in item["source_meta"]["evidence_records"]]
        self.assertTrue(any("deepseek original success" in text for text in texts))
        self.assertTrue(any('"type": "tool/result"' in text for text in texts))
        for text, expected in [
            ("Exit code: 1\n1 failed", "failed"),
            ('{"exit_code":1}', "failed"),
            ('{"session_id":123,"output":"still running"}', "pending"),
            ("Exit code: 0\n10 passed", "passed"),
        ]:
            records[4]["data"]["message"]["content"][0]["content"] = [{"type": "text", "text": text}, {"type": "text", "text": "extra diagnostic"}]
            body = "".join(json.dumps(r) + "\n" for r in records)
            subprocess.run(["zstd", "-q", "-o", str(path), "-f", "-"], input=body.encode(), check=True)
            result = DeepseekAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
            item = result.slices[0]
            item = item.to_dict() if hasattr(item, "to_dict") else item
            self.assertEqual([f"pytest: {expected}"], item["execution_evidence"]["verifications"])

    def test_source_location_does_not_change_event_or_generated_turn_identity(self):
        records = [
            event("session_meta", {"id": "synthetic"}, 0),
            event("event_msg", {"type": "user_message", "message": "no native turn id"}, 1),
            event("response_item", {"type": "function_call", "name": "exec_command",
                                    "arguments": '{"cmd":"pytest"}'}, 2),
        ]
        path = self.root / "sessions" / "rollout-one.jsonl"
        path.parent.mkdir()
        body = "".join(json.dumps(r) + "\n" for r in records)
        path.write_text(body)
        first = CodexAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
        old_ids = [s["slice_id"] if isinstance(s, dict) else s.slice_id for s in first.slices]
        archive = self.root / "archived_sessions" / "rollout-copy.jsonl"
        archive.parent.mkdir()
        archive.write_text("\n" + body)
        repeated = CodexAdapter(self.root).scan(SourceScanRequest(window=WINDOW))
        self.assertEqual(3, repeated.stats["records_deduplicated"])
        self.assertEqual(old_ids, [s["slice_id"] if isinstance(s, dict) else s.slice_id for s in repeated.slices])
