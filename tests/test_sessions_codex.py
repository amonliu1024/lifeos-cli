import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lifeos_sessions.codex import CodexAdapter
from lifeos_sessions.core import SessionsService, SourceScanRequest, TimeWindow
from lifeos_sessions.store import SessionsStore


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "sessions" / "codex"


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def request(from_time: str, to_time: str, *, includes=None, checkpoint=None):
    return SourceScanRequest(
        window=TimeWindow(
            from_ms=epoch_ms(from_time),
            to_ms=epoch_ms(to_time),
            from_iso=from_time,
            to_iso=to_time,
        ),
        includes=list(includes or []),
        checkpoint=dict(checkpoint or {}),
        temp_dir=None,
    )


def as_dict(item):
    return item.to_dict(include_mutable=False) if hasattr(item, "to_dict") else item


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".codex"
        shutil.copytree(FIXTURE_DIR / "sessions", self.root / "sessions")
        shutil.copytree(FIXTURE_DIR / "archived_sessions", self.root / "archived_sessions")

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, from_time="2026-08-08T00:00:00Z", to_time="2026-08-09T00:00:00Z", **kwargs):
        return CodexAdapter(root=self.root).scan(request(from_time, to_time, **kwargs))

    def test_projects_active_archived_turns_and_bounded_execution_content(self):
        result = self.scan()
        self.assertEqual("codex", result.source)
        self.assertEqual("partial", result.status)  # incomplete synthetic source is in the same scan
        current = [as_dict(item) for item in result.slices if as_dict(item)["source_meta"]["thread_id"] == "thread-codex-1"]
        self.assertEqual(["turn-new"], [item["native_unit"]["id"] for item in current])
        item = current[0]
        self.assertEqual("session-root-1", item["conversation"]["id"])
        self.assertEqual("/synthetic/workspace", item["workspace"])
        self.assertEqual("请检查本次续写", item["blocks"][0]["text"])
        self.assertEqual("已检查并完成本次续写。", item["blocks"][-1]["text"])
        self.assertNotIn("中间投影，不应重复", [block["text"] for block in item["blocks"]])
        self.assertEqual(4, result.stats["records_deduplicated"])  # archived copy has four records
        summary = item["execution_evidence"]
        self.assertIn("src/example.py", summary["changed_targets"])
        self.assertNotIn("PRIVATE_TOOL_OUTPUT_SHOULD_NOT_APPEAR", json.dumps(item, ensure_ascii=False))
        # Tool activity is only ever summarised into execution_evidence; it
        # never becomes a quotable block.
        self.assertEqual(
            set(),
            {block["kind"] for block in item["blocks"]} - {"message", "agent_message", "delegation"},
        )
        self.assertTrue(all(
            block["origin"] in {"user", "agent", "system_injected"} for block in item["blocks"]
        ))

    def test_command_arguments_feed_only_real_verifications_and_titles_use_rename_events(self):
        long_command = "ruff check " + "x" * 220
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-command-evidence.jsonl",
            [
                {"timestamp": "2026-08-08T05:00:00Z", "type": "session_meta", "payload": {"id": "thread-command", "session_id": "session-command", "title": "ignored metadata title"}},
                {"timestamp": "2026-08-08T05:00:01Z", "type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "未命名会话"}},
                {"timestamp": "2026-08-08T05:00:02Z", "type": "event_msg", "payload": {"type": "thread_name_updated", "thread_name": "验证证据整理"}},
                {"timestamp": "2026-08-08T05:00:03Z", "type": "turn_context", "payload": {"turn_id": "turn-command"}},
                {"timestamp": "2026-08-08T05:00:04Z", "type": "event_msg", "payload": {"type": "user_message", "message": "执行检查", "id": "command-user"}},
                {"timestamp": "2026-08-08T05:00:05Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": ["/bin/zsh", "-lc", "cd /x && python3 -m unittest discover"]}), "id": "verify-unittest"}},
                {"timestamp": "2026-08-08T05:00:06Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"script": "npm test"}), "id": "verify-npm"}},
                {"timestamp": "2026-08-08T05:00:07Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"command": long_command}), "id": "verify-ruff"}},
                {"timestamp": "2026-08-08T05:00:08Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "git status"}), "id": "not-verification"}},
                {"timestamp": "2026-08-08T05:00:09Z", "type": "event_msg", "payload": {"type": "exec_command_end", "exit_code": 0}},
                {"timestamp": "2026-08-08T05:00:10Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "检查完成"}], "id": "command-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T04:59:00Z", to_time="2026-08-08T05:01:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-command")
        evidence = item["execution_evidence"]
        self.assertEqual("验证证据整理", item["conversation"]["title"])
        self.assertIn('/bin/zsh -lc cd /x && python3 -m unittest discover: passed', evidence["verifications"])
        self.assertIn("npm test: passed", evidence["verifications"])
        ruff = next(value for value in evidence["verifications"] if value.startswith("ruff check "))
        self.assertEqual(208, len(ruff))
        self.assertTrue(ruff.endswith(": passed"))
        self.assertIn("git status: passed", evidence["tool_calls"])
        self.assertIn("exec_command_end: passed", evidence["tool_calls"])
        self.assertNotIn("git status: passed", evidence["verifications"])

    def test_exec_string_input_extracts_all_commands_and_routes_them_to_semantics(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-exec.jsonl",
            [
                {"timestamp": "2026-08-08T05:20:00Z", "type": "session_meta", "payload": {"id": "thread-string-exec", "session_id": "session-string-exec"}},
                {"timestamp": "2026-08-08T05:20:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-exec"}},
                {"timestamp": "2026-08-08T05:20:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "执行合成检查", "id": "string-exec-user"}},
                {
                    "timestamp": "2026-08-08T05:20:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "input": (
                            'await tools.exec_command({cmd:"./scripts/test.sh"}) '
                            "await tools.exec_command({ 'cmd' : 'groovy scripts/local/verify-x.groovy' }) "
                            "await tools.exec_command({cmd: `python3 scripts/test_cases.py`})"
                        ),
                        "id": "string-exec-call",
                    },
                },
                {"timestamp": "2026-08-08T05:20:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "合成检查完成"}], "id": "string-exec-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:19:00Z", to_time="2026-08-08T05:21:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-exec")
        evidence = item["execution_evidence"]
        self.assertEqual(
            [
                "./scripts/test.sh: passed",
                "groovy scripts/local/verify-x.groovy: passed",
                "python3 scripts/test_cases.py: passed",
            ],
            evidence["verifications"],
        )
        self.assertEqual([], evidence["tool_calls"])
        self.assertNotIn("exec: passed", evidence["tool_calls"])

    def test_exec_string_input_caps_one_fragment_at_eight_and_counts_the_rest(self):
        commands = [r'./scripts/check-0.sh --label \"quoted\"'] + [
            f"./scripts/check-{index}.sh" for index in range(1, 9)
        ]
        fragment = " ".join(
            f'await tools.exec_command({{cmd:"{command}"}})' for command in commands
        )
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-exec-limit.jsonl",
            [
                {"timestamp": "2026-08-08T05:26:00Z", "type": "session_meta", "payload": {"id": "thread-string-limit", "session_id": "session-string-limit"}},
                {"timestamp": "2026-08-08T05:26:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-limit"}},
                {"timestamp": "2026-08-08T05:26:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "限制合成命令数量", "id": "string-limit-user"}},
                {"timestamp": "2026-08-08T05:26:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": fragment, "id": "string-limit-call"}},
                {"timestamp": "2026-08-08T05:26:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "已限制"}], "id": "string-limit-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:25:00Z", to_time="2026-08-08T05:27:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-limit")
        evidence = item["execution_evidence"]
        expected_commands = [commands[0].replace(r'\"', '"')] + commands[1:8]
        self.assertEqual([f"{command}: passed" for command in expected_commands], evidence["verifications"])
        self.assertEqual(1, evidence["omitted_count"])

    def test_exec_string_input_without_a_complete_command_keeps_the_bare_tool_fallback(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-exec-invalid.jsonl",
            [
                {"timestamp": "2026-08-08T05:22:00Z", "type": "session_meta", "payload": {"id": "thread-string-invalid", "session_id": "session-string-invalid"}},
                {"timestamp": "2026-08-08T05:22:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-invalid"}},
                {"timestamp": "2026-08-08T05:22:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "保留兼容行为", "id": "string-invalid-user"}},
                {"timestamp": "2026-08-08T05:22:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "await tools.other({cmd:'not-a-command'})", "id": "string-invalid-no-marker"}},
                {"timestamp": "2026-08-08T05:22:04Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "await tools.exec_command({cmd:\"./scripts/test.sh})", "id": "string-invalid-unpaired"}},
                {"timestamp": "2026-08-08T05:22:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "兼容行为保留"}], "id": "string-invalid-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:21:00Z", to_time="2026-08-08T05:23:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-invalid")
        evidence = item["execution_evidence"]
        self.assertEqual(["exec: passed"], evidence["tool_calls"])
        self.assertEqual([], evidence["verifications"])

    def test_exec_string_input_keeps_a_bare_fallback_for_each_malformed_occurrence(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-exec-mixed.jsonl",
            [
                {"timestamp": "2026-08-08T05:28:00Z", "type": "session_meta", "payload": {"id": "thread-string-mixed", "session_id": "session-string-mixed"}},
                {"timestamp": "2026-08-08T05:28:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-mixed"}},
                {"timestamp": "2026-08-08T05:28:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "混合命令格式", "id": "string-mixed-user"}},
                {"timestamp": "2026-08-08T05:28:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": 'await tools.exec_command({cmd:"./scripts/test.sh"}) await tools.exec_command({cmd:"./scripts/check.sh})', "id": "string-mixed-call"}},
                {"timestamp": "2026-08-08T05:28:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "混合格式已处理"}], "id": "string-mixed-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:27:00Z", to_time="2026-08-08T05:29:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-mixed")
        evidence = item["execution_evidence"]
        self.assertEqual(["./scripts/test.sh: passed"], evidence["verifications"])
        self.assertEqual(["exec: passed"], evidence["tool_calls"])

    def test_exec_string_input_extracts_patch_update_paths_in_order(self):
        patch_input = (
            r'const patch = "*** Begin Patch\n'
            r'*** Update File: /synthetic/first.py\n'
            r'@@ ...\n'
            r'*** Update File: /synthetic/second.py\n'
            r'*** End Patch\n";'
        )
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-patch-paths.jsonl",
            [
                {"timestamp": "2026-08-08T05:30:00Z", "type": "session_meta", "payload": {"id": "thread-string-patch-paths", "session_id": "session-string-patch-paths"}},
                {"timestamp": "2026-08-08T05:30:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-patch-paths"}},
                {"timestamp": "2026-08-08T05:30:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "提取合成改动路径", "id": "string-patch-paths-user"}},
                {"timestamp": "2026-08-08T05:30:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": patch_input, "id": "string-patch-paths-call"}},
                {"timestamp": "2026-08-08T05:30:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "路径已提取"}], "id": "string-patch-paths-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:29:00Z", to_time="2026-08-08T05:31:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-patch-paths")
        self.assertEqual(
            ["/synthetic/first.py", "/synthetic/second.py"],
            item["execution_evidence"]["changed_targets"],
        )

    def test_exec_string_input_keeps_patch_paths_and_commands_from_the_same_fragment(self):
        mixed_input = (
            r'const patch = "*** Begin Patch\n'
            r'*** Add File: /synthetic/added.py\n'
            r'*** End Patch\n"; '
            r'const r = await tools.exec_command({cmd:"./scripts/test.sh"});'
        )
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-patch-mixed.jsonl",
            [
                {"timestamp": "2026-08-08T05:32:00Z", "type": "session_meta", "payload": {"id": "thread-string-patch-mixed", "session_id": "session-string-patch-mixed"}},
                {"timestamp": "2026-08-08T05:32:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-patch-mixed"}},
                {"timestamp": "2026-08-08T05:32:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "提取混合片段", "id": "string-patch-mixed-user"}},
                {"timestamp": "2026-08-08T05:32:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": mixed_input, "id": "string-patch-mixed-call"}},
                {"timestamp": "2026-08-08T05:32:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "混合片段已提取"}], "id": "string-patch-mixed-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:31:00Z", to_time="2026-08-08T05:33:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-patch-mixed")
        evidence = item["execution_evidence"]
        self.assertEqual(["/synthetic/added.py"], evidence["changed_targets"])
        self.assertEqual(["./scripts/test.sh: passed"], evidence["verifications"])
        self.assertEqual([], evidence["tool_calls"])

    def test_exec_string_input_ignores_patch_without_file_paths_or_with_empty_path(self):
        no_file_input = r'const patch = "*** Begin Patch\n*** End Patch\n";'
        empty_path_input = r'const patch = "*** Begin Patch\n*** Update File: \n*** End Patch\n";'
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-patch-invalid.jsonl",
            [
                {"timestamp": "2026-08-08T05:34:00Z", "type": "session_meta", "payload": {"id": "thread-string-patch-invalid", "session_id": "session-string-patch-invalid"}},
                {"timestamp": "2026-08-08T05:34:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-patch-invalid"}},
                {"timestamp": "2026-08-08T05:34:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "保留无效补丁兼容行为", "id": "string-patch-invalid-user"}},
                {"timestamp": "2026-08-08T05:34:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": no_file_input, "id": "string-patch-invalid-no-file"}},
                {"timestamp": "2026-08-08T05:34:04Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": empty_path_input, "id": "string-patch-invalid-empty-path"}},
                {"timestamp": "2026-08-08T05:34:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "无效补丁已处理"}], "id": "string-patch-invalid-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:33:00Z", to_time="2026-08-08T05:35:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-patch-invalid")
        evidence = item["execution_evidence"]
        self.assertEqual([], evidence["changed_targets"])
        self.assertNotIn("", evidence["changed_targets"])
        self.assertEqual(["exec: passed"], evidence["tool_calls"])

    def test_exec_string_input_caps_patch_paths_at_eight_and_counts_only_the_ninth(self):
        paths = [f"/synthetic/file-{index}.py" for index in range(1, 10)]
        patch_input = (
            r'const patch = "*** Begin Patch\n'
            + "".join(f"*** Update File: {path}\\n" for path in paths)
            + r'*** End Patch\n";'
        )
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-patch-limit.jsonl",
            [
                {"timestamp": "2026-08-08T05:36:00Z", "type": "session_meta", "payload": {"id": "thread-string-patch-limit", "session_id": "session-string-patch-limit"}},
                {"timestamp": "2026-08-08T05:36:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-patch-limit"}},
                {"timestamp": "2026-08-08T05:36:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "限制合成补丁路径数量", "id": "string-patch-limit-user"}},
                {"timestamp": "2026-08-08T05:36:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": patch_input, "id": "string-patch-limit-call"}},
                {"timestamp": "2026-08-08T05:36:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "补丁路径已限制"}], "id": "string-patch-limit-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:35:00Z", to_time="2026-08-08T05:37:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-patch-limit")
        evidence = item["execution_evidence"]
        self.assertEqual(paths[:8], evidence["changed_targets"])
        self.assertEqual(1, evidence["omitted_count"])

    def test_exec_string_input_with_only_cmd_keeps_the_previous_evidence_bytes(self):
        cmd_only_input = r'const r = await tools.exec_command({cmd:"./scripts/test.sh"});'
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-string-cmd-only.jsonl",
            [
                {"timestamp": "2026-08-08T05:38:00Z", "type": "session_meta", "payload": {"id": "thread-string-cmd-only", "session_id": "session-string-cmd-only"}},
                {"timestamp": "2026-08-08T05:38:01Z", "type": "turn_context", "payload": {"turn_id": "turn-string-cmd-only"}},
                {"timestamp": "2026-08-08T05:38:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "保持命令兼容输出", "id": "string-cmd-only-user"}},
                {"timestamp": "2026-08-08T05:38:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": cmd_only_input, "id": "string-cmd-only-call"}},
                {"timestamp": "2026-08-08T05:38:04Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "兼容输出已保持"}], "id": "string-cmd-only-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:37:00Z", to_time="2026-08-08T05:39:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-string-cmd-only")
        self.assertEqual(
            '{"changed_targets":[],"other_targets":[],"tool_calls":[],"verifications":["./scripts/test.sh: passed"],"failures":[],"user_interrupts":[],"omitted_count":0,"source_refs":["codex://thread-string-cmd-only/turn/turn-string-cmd-only/event/string-cmd-only-call"]}',
            json.dumps(item["execution_evidence"], ensure_ascii=False, separators=(",", ":")),
        )

    def test_history_header_is_not_a_readable_user_block_when_collected(self):
        history = "The following is the Codex agent history added by the application"
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-injected-history.jsonl",
            [
                {"timestamp": "2026-08-08T05:24:00Z", "type": "session_meta", "payload": {"id": "thread-injected-history", "session_id": "session-injected-history"}},
                {"timestamp": "2026-08-08T05:24:01Z", "type": "turn_context", "payload": {"turn_id": "turn-injected-history"}},
                {"timestamp": "2026-08-08T05:24:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": history, "id": "injected-history-user"}},
                {"timestamp": "2026-08-08T05:24:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "合成历史已识别"}], "id": "injected-history-final"}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:23:00Z", to_time="2026-08-08T05:25:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-injected-history")
        self.assertEqual("system_injected", item["blocks"][0]["origin"])
        self.assertNotIn(history, [block["text"] for block in item["blocks"] if block["origin"] == "user"])

    def test_external_import_turns_are_context_only_not_timestamped_work(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-external-import.jsonl",
            [
                {"timestamp": "2026-08-08T06:00:00Z", "type": "session_meta", "payload": {"id": "thread-external-import", "session_id": "session-external-import"}},
                {"timestamp": "2026-08-08T06:00:01Z", "type": "turn_context", "payload": {"turn_id": "external-import-turn-1"}},
                {"timestamp": "2026-08-08T06:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "历史任务里的用户原话", "id": "external-import-user"}},
                {"timestamp": "2026-08-08T06:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "历史任务里的最终回复"}], "id": "external-import-final"}},
                {"timestamp": "2026-08-08T06:00:04Z", "type": "turn_context", "payload": {"turn_id": "external-import-turn-2"}},
                {"timestamp": "2026-08-08T06:00:05Z", "type": "event_msg", "payload": {"type": "user_message", "message": "另一段被导入的历史", "id": "external-import-user-2"}},
                {"timestamp": "2026-08-08T06:00:06Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "未确认的历史回复"}], "id": "external-import-agent-2"}},
                {"timestamp": "2026-08-08T06:00:07Z", "type": "turn_context", "payload": {"turn_id": "turn-live"}},
                {"timestamp": "2026-08-08T06:00:08Z", "type": "event_msg", "payload": {"type": "user_message", "message": "这是当前真正发出的请求", "id": "live-user"}},
                {"timestamp": "2026-08-08T06:00:09Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "当前请求已完成"}], "id": "live-final"}},
            ],
        )

        result = self.scan(from_time="2026-08-08T05:59:00Z", to_time="2026-08-08T06:01:00Z")
        items = [as_dict(value) for value in result.slices]

        self.assertEqual(["turn-live"], [item["native_unit"]["id"] for item in items])
        self.assertEqual("这是当前真正发出的请求", items[0]["blocks"][0]["text"])
        self.assertEqual("completed", items[0]["turn_completion"])
        self.assertEqual(2, result.stats["context_only_turns_excluded"])

    def test_external_import_prefix_is_exact(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-external-import-near-miss.jsonl",
            [
                {"timestamp": "2026-08-08T06:02:00Z", "type": "session_meta", "payload": {"id": "thread-external-import-near-miss", "session_id": "session-external-import-near-miss"}},
                {"timestamp": "2026-08-08T06:02:01Z", "type": "turn_context", "payload": {"turn_id": "external-imported-turn-1"}},
                {"timestamp": "2026-08-08T06:02:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "名字相近但属于真实输入", "id": "near-miss-user"}},
                {"timestamp": "2026-08-08T06:02:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "近似名字仍保留"}], "id": "near-miss-final"}},
            ],
        )

        result = self.scan(from_time="2026-08-08T06:01:00Z", to_time="2026-08-08T06:03:00Z")
        items = [as_dict(value) for value in result.slices]

        self.assertEqual(["external-imported-turn-1"], [item["native_unit"]["id"] for item in items])
        self.assertEqual(0, result.stats["context_only_turns_excluded"])

    def test_failed_tools_prefer_json_error_message_then_parsed_command_then_type(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-failure-detail.jsonl",
            [
                {"timestamp": "2026-08-08T05:10:00Z", "type": "session_meta", "payload": {"id": "thread-failure-detail", "session_id": "session-failure-detail"}},
                {"timestamp": "2026-08-08T05:10:01Z", "type": "turn_context", "payload": {"turn_id": "turn-failure-detail"}},
                {"timestamp": "2026-08-08T05:10:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "检查失败摘要", "id": "failure-user"}},
                {"timestamp": "2026-08-08T05:10:03Z", "type": "event_msg", "payload": {"type": "exec_command_end", "exit_code": 1, "error": "{\"message\":\"lint failed\",\"details\":\"private\"}"}},
                {"timestamp": "2026-08-08T05:10:04Z", "type": "event_msg", "payload": {"type": "exec_command_end", "exit_code": 2, "arguments": "{\"cmd\":\"pytest -q\"}"}},
                {"timestamp": "2026-08-08T05:10:05Z", "type": "event_msg", "payload": {"type": "patch_apply_end", "success": False}},
            ],
        )
        result = self.scan(from_time="2026-08-08T05:09:00Z", to_time="2026-08-08T05:11:00Z")
        item = next(as_dict(value) for value in result.slices if as_dict(value)["native_unit"]["id"] == "turn-failure-detail")
        self.assertEqual(
            ["lint failed", "pytest -q (exit 2)"],
            item["execution_evidence"]["failures"],
        )
        self.assertEqual(1, item["execution_evidence"]["omitted_count"])

    def test_missing_final_and_incomplete_tail_emit_checkpoint_and_partial_slice(self):
        result = self.scan(from_time="2026-08-08T02:59:00Z", to_time="2026-08-08T04:00:00Z")
        item = next(as_dict(item) for item in result.slices if as_dict(item)["native_unit"]["id"] == "turn-incomplete")
        self.assertEqual("partial", item["content_completeness"])
        self.assertTrue(any("incomplete" in warning for warning in item["warnings"]))
        entries = [value for value in result.checkpoint["files"].values() if value["incomplete_tail"]]
        self.assertEqual(1, len(entries))
        self.assertIsNotNone(entries[0]["incomplete_tail_offset"])
        self.assertIn("last_complete_offset", entries[0])
        self.assertIn("last_complete_record_hash", entries[0])

    def test_child_fork_history_is_not_replayed_and_final_is_projected_as_delegation(self):
        result = self.scan(from_time="2026-08-08T01:59:00Z", to_time="2026-08-08T05:00:00Z")
        parent = next(as_dict(item) for item in result.slices if as_dict(item)["source_meta"]["thread_id"] == "thread-codex-1")
        child = next(as_dict(item) for item in result.slices if as_dict(item)["source_meta"]["thread_id"] == "thread-child")
        child_messages = [block["text"] for block in child["blocks"] if block["kind"] == "message"]
        self.assertEqual(["子任务：核对文件"], child_messages)
        self.assertEqual("子任务已完成", child["blocks"][-1]["text"])
        self.assertEqual(1, len(parent["delegations"]))
        self.assertEqual("子任务已完成", parent["delegations"][0]["result"])

    def test_fork_family_threads_can_reuse_native_turn_id_without_store_conflict(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-collision-child.jsonl",
            [
                {
                    "timestamp": "2026-08-08T04:30:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "thread-collision-child",
                        "session_id": "session-root-1",
                        "parent_thread_id": "thread-codex-1",
                        "parent_turn_id": "turn-new",
                        "thread_source": "subagent",
                        "cwd": "/synthetic/workspace",
                    },
                },
                {
                    "timestamp": "2026-08-08T04:30:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-new"},
                },
                {
                    "timestamp": "2026-08-08T04:30:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final",
                        "content": [{"type": "output_text", "text": "子分支独立结果"}],
                        "id": "collision-child-final",
                    },
                },
            ],
        )
        result = self.scan(from_time="2026-08-08T01:59:00Z", to_time="2026-08-08T05:00:00Z")
        reused_turn = [
            as_dict(item)
            for item in result.slices
            if as_dict(item)["native_unit"]["id"] == "turn-new"
        ]
        self.assertEqual(
            {"session-root-1", "thread-collision-child"},
            {item["conversation"]["id"] for item in reused_turn},
        )
        self.assertEqual(2, len({item["slice_id"] for item in reused_turn}))
        self.assertEqual(
            {"session-root-1"},
            {item["source_meta"]["session_id"] for item in reused_turn},
        )

        store = SessionsStore(Path(self.temp.name) / "runtime" / "sessions")
        service = SessionsService([CodexAdapter(root=self.root)], store)
        manifest = service.scan(
            TimeWindow.from_values(
                "2026-08-08T01:59:00Z",
                "2026-08-08T05:00:00Z",
            ),
            sources=["codex"],
        )
        self.assertIn(manifest["status"], {"complete", "partial"})
        self.assertEqual(
            2,
            len(
                [
                    item
                    for item in store.list_slices({"source": "codex"})
                    if item["conversation_id"]
                    in {"session-root-1", "thread-collision-child"}
                ]
            ),
        )

    def test_structured_fork_replay_is_context_only_and_new_fork_turn_is_independent(self):
        parent_thread = "thread-structured-fork-parent"
        fork_thread = "thread-structured-fork-child"
        parent_records = [
            {"timestamp": "2026-08-08T05:40:00Z", "type": "session_meta", "payload": {"id": parent_thread, "session_id": "session-structured-fork"}},
            {"timestamp": "2026-08-08T05:40:01Z", "type": "turn_context", "payload": {"turn_id": "turn-inherited"}},
            {"timestamp": "2026-08-08T05:40:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "继承的父请求", "id": "parent-inherited-user"}},
            {"timestamp": "2026-08-08T05:40:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "父请求结果"}], "id": "parent-inherited-final"}},
        ]
        fork_records = [
            {"timestamp": "2026-08-08T05:41:00Z", "type": "session_meta", "payload": {"id": fork_thread, "session_id": "session-structured-fork", "forked_from_id": parent_thread}},
            {"timestamp": "2026-08-08T05:41:01Z", "type": "turn_context", "payload": {"turn_id": "turn-inherited"}},
            {"timestamp": "2026-08-08T05:41:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "继承的父请求", "id": "fork-inherited-user"}},
            {"timestamp": "2026-08-08T05:41:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "父请求结果"}], "id": "fork-inherited-final"}},
            {"timestamp": "2026-08-08T05:41:04Z", "type": "turn_context", "payload": {"turn_id": "turn-fork-new"}},
            {"timestamp": "2026-08-08T05:41:05Z", "type": "event_msg", "payload": {"type": "user_message", "message": "分支的新请求", "id": "fork-new-user"}},
            {"timestamp": "2026-08-08T05:41:06Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "分支的新结果"}], "id": "fork-new-final"}},
        ]
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-structured-fork-parent.jsonl",
            parent_records,
        )
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-structured-fork-child.jsonl",
            fork_records,
        )

        result = self.scan(from_time="2026-08-08T05:39:00Z", to_time="2026-08-08T05:42:00Z")
        items = [
            as_dict(item)
            for item in result.slices
            if as_dict(item)["source_meta"].get("thread_id") in {parent_thread, fork_thread}
        ]
        parent = [item for item in items if item["source_meta"]["thread_id"] == parent_thread]
        fork = [item for item in items if item["source_meta"]["thread_id"] == fork_thread]
        self.assertEqual(["turn-inherited"], [item["native_unit"]["id"] for item in parent])
        self.assertEqual(["turn-fork-new"], [item["native_unit"]["id"] for item in fork])
        self.assertEqual(1, sum(item["native_unit"]["id"] == "turn-inherited" for item in items))
        self.assertEqual("session-structured-fork", parent[0]["conversation"]["id"])
        self.assertEqual(fork_thread, fork[0]["conversation"]["id"])
        self.assertEqual(parent_thread, fork[0]["source_meta"]["parent_thread_id"])
        self.assertEqual(parent_thread, fork[0]["source_meta"]["forked_from_id"])
        self.assertEqual([], parent[0]["delegations"])

        store = SessionsStore(Path(self.temp.name) / "runtime" / "sessions", create=True)
        manifest = SessionsService([CodexAdapter(root=self.root)], store).scan(
            TimeWindow.from_values("2026-08-08T05:39:00Z", "2026-08-08T05:42:00Z"),
            sources=["codex"],
        )
        self.assertNotEqual("failed", manifest["status"])
        self.assertEqual([], store.validate())

    def test_turn_aborted_is_a_bounded_failure_not_an_unknown_format(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-aborted.jsonl",
            [
                {
                    "timestamp": "2026-08-08T04:40:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "thread-aborted",
                        "session_id": "session-aborted",
                        "cwd": "/synthetic/aborted",
                    },
                },
                {
                    "timestamp": "2026-08-08T04:40:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-aborted"},
                },
                {
                    "timestamp": "2026-08-08T04:40:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "触发合成中止",
                        "id": "aborted-user",
                    },
                },
                {
                    "timestamp": "2026-08-08T04:40:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_aborted",
                        "reason": "合成取消原因",
                        "id": "aborted-event",
                    },
                },
            ],
        )
        result = self.scan(
            from_time="2026-08-08T04:39:00Z",
            to_time="2026-08-08T04:41:00Z",
        )
        self.assertFalse(
            any(
                as_dict(value)["native_unit"]["id"] == "turn-aborted"
                for value in result.slices
            )
        )
        self.assertEqual(1, len(result.omissions))
        omission = result.omissions[0]
        omission = omission.to_dict() if hasattr(omission, "to_dict") else omission
        self.assertEqual("explicit_abort_without_work", omission["reason"])
        self.assertFalse(
            any("unknown_record_type" in warning for warning in result.warnings)
        )
        self.assertFalse(
            any("unsupported_format" in warning for warning in result.warnings)
        )

    def test_include_limits_to_thread_and_source_refs_are_stable(self):
        first = self.scan(includes=["codex:thread-codex-1"])
        second = self.scan(includes=["codex:thread-codex-1"])
        self.assertTrue(first.slices)
        self.assertTrue(all(as_dict(item)["source_meta"]["thread_id"] == "thread-codex-1" for item in first.slices))
        by_turn_first = {as_dict(item)["native_unit"]["id"]: as_dict(item) for item in first.slices}
        by_turn_second = {as_dict(item)["native_unit"]["id"]: as_dict(item) for item in second.slices}
        self.assertEqual(by_turn_first.keys(), by_turn_second.keys())
        self.assertEqual(by_turn_first["turn-new"]["source_refs"], by_turn_second["turn-new"]["source_refs"])

    def test_exact_checkpoint_is_content_private_and_reuses_all_slices(self):
        first = self.scan()
        self.assertIn("cache_generation", first.checkpoint)
        checkpoint_text = json.dumps(first.checkpoint, ensure_ascii=False)
        self.assertNotIn("请检查本次续写", checkpoint_text)
        self.assertNotIn("PRIVATE_TOOL_OUTPUT_SHOULD_NOT_APPEAR", checkpoint_text)
        second = self.scan(checkpoint=first.checkpoint)
        self.assertEqual(0, second.stats["files_read"])
        self.assertEqual(first.stats["files_examined"], second.stats["files_skipped"])
        self.assertEqual(
            {as_dict(item)["slice_id"] for item in first.slices},
            set(second.reused_slice_ids),
        )
        incomplete_checkpoint = dict(first.checkpoint)
        incomplete_checkpoint.pop("cache_generation")
        rescanned = self.scan(checkpoint=incomplete_checkpoint)
        self.assertGreater(rescanned.stats["files_read"], 0)

    def test_append_to_old_locator_is_reread_and_new_turn_is_returned(self):
        first = self.scan()
        archived = self.root / "archived_sessions" / "rollout-archived.jsonl"
        with archived.open("a", encoding="utf-8") as handle:
            for record in (
                {
                    "timestamp": "2026-08-08T06:00:00Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-appended", "session_id": "session-root-1"},
                },
                {
                    "timestamp": "2026-08-08T06:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "追加内容", "id": "append-user"},
                },
                {
                    "timestamp": "2026-08-08T06:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final",
                        "content": [{"type": "output_text", "text": "追加完成"}],
                        "id": "append-final",
                    },
                },
            ):
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        second = self.scan(checkpoint=first.checkpoint)
        turns = {as_dict(item)["native_unit"]["id"] for item in second.slices}
        self.assertIn("turn-appended", turns)
        self.assertGreater(second.stats["records_examined"], 0)

    def test_same_size_and_restored_mtime_rewrite_invalidates_checkpoint(self):
        first = self.scan()
        archived = self.root / "archived_sessions" / "rollout-archived.jsonl"
        before = archived.stat()
        original = archived.read_bytes()
        rewritten = original.replace("历史问题".encode("utf-8"), "历史疑问".encode("utf-8"), 1)
        self.assertEqual(len(original), len(rewritten))
        archived.write_bytes(rewritten)
        os.utime(archived, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = archived.stat()
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_ino, after.st_ino)
        second = self.scan(checkpoint=first.checkpoint)
        self.assertEqual(first.stats["files_examined"], second.stats["files_read"])
        self.assertEqual(0, second.stats["files_skipped"])

    def test_known_noise_is_silent_but_unknown_records_make_coverage_partial(self):
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-noise.jsonl",
            [
                {"timestamp": "2026-08-08T06:10:00Z", "type": "session_meta", "payload": {"id": "thread-noise", "session_id": "session-noise"}},
                {"timestamp": "2026-08-08T06:10:01Z", "type": "turn_context", "payload": {"turn_id": "turn-noise"}},
                {"timestamp": "2026-08-08T06:10:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "噪声测试", "id": "noise-user"}},
                {"timestamp": "2026-08-08T06:10:03Z", "type": "event_msg", "payload": {"type": "task_started"}},
                {"timestamp": "2026-08-08T06:10:04Z", "type": "response_item", "payload": {"type": "reasoning", "text": "internal"}},
                {"timestamp": "2026-08-08T06:10:05Z", "type": "event_msg", "payload": {"type": "token_count", "count": 10}},
                {"timestamp": "2026-08-08T06:10:06Z", "type": "event_msg", "payload": {"type": "thread_settings_applied"}},
                {"timestamp": "2026-08-08T06:10:07Z", "type": "event_msg", "payload": {"type": "task_complete"}},
                {"timestamp": "2026-08-08T06:10:08Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "噪声已折叠"}], "id": "noise-final"}},
            ],
        )
        result = self.scan()
        item = next(as_dict(item) for item in result.slices if as_dict(item)["source_meta"]["thread_id"] == "thread-noise")
        self.assertEqual("complete", item["content_completeness"])
        self.assertFalse(any("unknown_record_type" in warning for warning in item["warnings"]))
        self.assertGreaterEqual(result.stats["known_noise_records"], 5)
        write_jsonl(
            self.root / "sessions" / "2026" / "08" / "08" / "rollout-unknown-only.jsonl",
            [
                {"timestamp": "2026-08-08T06:20:00Z", "type": "session_meta", "payload": {"id": "thread-unknown-only", "session_id": "session-unknown-only"}},
                {"timestamp": "2026-08-08T06:20:01Z", "type": "future_unknown", "payload": {"type": "future_unknown"}},
            ],
        )
        result = self.scan(includes=["codex:thread-unknown-only"])
        self.assertEqual("partial", result.status)
        self.assertFalse(result.slices)
        self.assertTrue(any(warning.startswith("unsupported_format:thread-unknown-only:") for warning in result.warnings))

    def test_delegations_and_source_refs_are_bounded(self):
        parent_file = self.root / "sessions" / "2026" / "08" / "08" / "rollout-large-parent.jsonl"
        write_jsonl(
            parent_file,
            [
                {"timestamp": "2026-08-08T07:00:00Z", "type": "session_meta", "payload": {"id": "thread-large-parent", "session_id": "session-large", "cwd": "/synthetic/large"}},
                {"timestamp": "2026-08-08T07:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-large-parent"}},
                {"timestamp": "2026-08-08T07:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "批量子任务", "id": "large-parent-user"}},
                {"timestamp": "2026-08-08T07:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "已安排"}], "id": "large-parent-final"}},
            ],
        )
        long_task = "任务" * 400
        long_result = "结果" * 1_500
        for index in range(20):
            write_jsonl(
                self.root / "sessions" / "2026" / "08" / "08" / f"rollout-large-child-{index:02d}.jsonl",
                [
                    {"timestamp": f"2026-08-08T07:{10 + index:02d}:00Z", "type": "session_meta", "payload": {"id": f"thread-large-child-{index:02d}", "session_id": "session-large", "parent_thread_id": "thread-large-parent", "parent_turn_id": "turn-large-parent", "agent_path": ["root", f"child-{index:02d}"]}},
                    {"timestamp": f"2026-08-08T07:{10 + index:02d}:01Z", "type": "turn_context", "payload": {"turn_id": f"turn-large-child-{index:02d}"}},
                    {"timestamp": f"2026-08-08T07:{10 + index:02d}:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": long_task, "id": f"large-child-user-{index:02d}"}},
                    {"timestamp": f"2026-08-08T07:{10 + index:02d}:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": long_result}], "id": f"large-child-final-{index:02d}"}},
                ],
            )
        result = self.scan(from_time="2026-08-08T07:00:00Z")
        parent = next(as_dict(item) for item in result.slices if as_dict(item)["source_meta"]["thread_id"] == "thread-large-parent")
        self.assertEqual(12, len(parent["delegations"]))
        self.assertTrue(any("delegations_omitted" in warning for warning in parent["warnings"]))
        self.assertTrue(all(len(item.get("task") or "") <= 500 for item in parent["delegations"]))
        self.assertTrue(all(len(item.get("result") or "") <= 2_000 for item in parent["delegations"]))
        self.assertLessEqual(len(parent["source_refs"]), 32)
        self.assertLess(len(json.dumps(parent, ensure_ascii=False)), 100_000)


if __name__ == "__main__":
    unittest.main()
