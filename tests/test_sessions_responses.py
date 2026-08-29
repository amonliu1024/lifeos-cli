import json
import unittest

from lifeos_sessions.core import TimeWindow
from lifeos_sessions.responses import (
    SourceProfile,
    build_session_document,
    normalize_session,
    normalize_sessions,
)


WINDOW = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")


def normalize_tool(payload, *, name="synthetic-tool", source="synthetic"):
    records = [
        {
            "timestamp": "2026-08-08T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "synthetic-thread", "session_id": "synthetic-session"},
        },
        {
            "timestamp": "2026-08-08T00:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "synthetic-turn"},
        },
        {
            "timestamp": "2026-08-08T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "合成证据请求"},
        },
        {
            "timestamp": "2026-08-08T00:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": name, **payload},
            "id": "synthetic-tool-event",
        },
        {
            "timestamp": "2026-08-08T00:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final",
                "content": [{"type": "output_text", "text": "合成证据结果"}],
            },
        },
    ]
    document = build_session_document(
        source=source,
        locator="synthetic/rollout.jsonl",
        records=records,
    )
    result = normalize_session(document, WINDOW, SourceProfile(source, "test"))
    assert len(result.slices) == 1, result
    return result.slices[0]


def normalize_tools(payloads, *, source="synthetic"):
    records = [
        {
            "timestamp": "2026-08-08T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "synthetic-thread", "session_id": "synthetic-session"},
        },
        {
            "timestamp": "2026-08-08T00:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "synthetic-turn"},
        },
        {
            "timestamp": "2026-08-08T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "合成证据请求"},
        },
    ]
    for index, (name, payload) in enumerate(payloads):
        records.append({
            "timestamp": f"2026-08-08T00:00:{3 + index:02d}Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": name, **payload},
            "id": f"synthetic-tool-event-{index}",
        })
    records.append({
        "timestamp": "2026-08-08T00:10:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final",
            "content": [{"type": "output_text", "text": "合成证据结果"}],
        },
    })
    document = build_session_document(
        source=source,
        locator="synthetic/rollout-many-tools.jsonl",
        records=records,
    )
    result = normalize_session(document, WINDOW, SourceProfile(source, "test"))
    assert len(result.slices) == 1, result
    return result.slices[0]


class ResponseContainerPipelineTest(unittest.TestCase):
    def evidence(self, payload, **kwargs):
        return normalize_tool(payload, **kwargs)["execution_evidence"]

    def test_acceptance_arguments_json_command_is_a_verification_and_not_a_bare_name(self):
        evidence = self.evidence(
            {"arguments": json.dumps({"cmd": "./scripts/test.sh"})},
            name="function_call",
        )
        self.assertEqual(["./scripts/test.sh: passed"], evidence["verifications"])
        self.assertNotIn("function_call: passed", evidence["tool_calls"])

    def test_acceptance_arguments_json_nonverification_keeps_the_command_in_tool_calls(self):
        evidence = self.evidence(
            {"arguments": json.dumps({"cmd": "cat README.md"})},
            name="exec_command",
        )
        self.assertEqual(["cat README.md: passed"], evidence["tool_calls"])
        self.assertNotIn("exec_command: passed", evidence["tool_calls"])

    def test_arguments_json_with_an_escaped_newline_is_normalized_once(self):
        evidence = self.evidence(
            {"arguments": json.dumps({"cmd": "printf 'first\\nsecond'"})},
            name="unrelated-tool",
        )
        self.assertEqual(["printf 'first second': passed"], evidence["tool_calls"])

    def test_acceptance_js_source_with_real_newlines_is_not_gated_by_tool_name(self):
        evidence = self.evidence(
            {
                "input": 'const r = await tools.exec_command({\n  cmd: "./scripts/test.sh"\n});',
            },
            name="unrelated-tool",
        )
        self.assertEqual(["./scripts/test.sh: passed"], evidence["verifications"])

    def test_js_source_with_escaped_newlines_extracts_commands(self):
        evidence = self.evidence(
            {"input": r'const r = await tools.exec_command({cmd:"./scripts/check.sh"});\nconst done = true;'},
            name="unrelated-tool",
        )
        self.assertEqual(["./scripts/check.sh: passed"], evidence["verifications"])

    def test_js_source_with_real_and_escaped_newlines_extracts_both_commands(self):
        value = (
            'const r = await tools.exec_command({cmd:"printf \'first\\nsecond\'"});\\n'
            'const s = await tools.exec_command({\n  cmd: "pytest -q"\n});'
        )
        evidence = self.evidence({"input": value}, name="unrelated-tool")
        self.assertEqual(["printf 'first second': passed"], evidence["tool_calls"])
        self.assertEqual(["pytest -q: passed"], evidence["verifications"])

    def test_acceptance_raw_patch_with_real_newlines_extracts_both_paths(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n*** Update File: b.py\n*** End Patch\n"
        evidence = self.evidence({"input": patch}, name="unrelated-tool")
        self.assertEqual(["a.py", "b.py"], evidence["changed_targets"])

    def test_raw_patch_with_escaped_newlines_extracts_the_same_paths(self):
        patch = r"*** Begin Patch\n*** Update File: a.py\n*** Update File: b.py\n*** End Patch\n"
        evidence = self.evidence({"input": patch}, name="unrelated-tool")
        self.assertEqual(["a.py", "b.py"], evidence["changed_targets"])

    def test_raw_patch_with_real_and_escaped_newlines_extracts_all_paths(self):
        patch = "*** Begin Patch\n*** Update File: a.py\\n*** Add File: b.py\n*** End Patch\n"
        evidence = self.evidence({"input": patch}, name="unrelated-tool")
        self.assertEqual(["a.py", "b.py"], evidence["changed_targets"])

    def test_acceptance_same_js_container_produces_commands_and_paths_independently(self):
        value = (
            r'const patch = "*** Begin Patch\n*** Add File: added.py\n*** End Patch\n"; '
            r'const r = await tools.exec_command({cmd:"./scripts/test.sh"});'
        )
        evidence = self.evidence({"input": value}, name="unrelated-tool")
        self.assertEqual(["added.py"], evidence["changed_targets"])
        self.assertEqual(["./scripts/test.sh: passed"], evidence["verifications"])
        self.assertEqual([], evidence["tool_calls"])

    def test_acceptance_arguments_dict_with_nested_patch_is_structured_evidence(self):
        evidence = self.evidence(
            {
                "arguments": {
                    "input": "*** Begin Patch\n*** Update File: nested.py\n*** End Patch\n",
                }
            },
            name="unrelated-tool",
        )
        self.assertEqual(["nested.py"], evidence["changed_targets"])

    def test_acceptance_send_message_target_is_other_target_not_changed_target(self):
        evidence = self.evidence(
            {"arguments": json.dumps({"target": "/root", "message": "合成消息"})},
            name="send_message",
        )
        self.assertEqual([], evidence["changed_targets"])
        self.assertEqual(["/root"], evidence["other_targets"])

    def test_acceptance_view_image_path_is_not_a_changed_target(self):
        evidence = self.evidence(
            {"arguments": json.dumps({"path": "/tmp/a.png"})},
            name="view_image",
        )
        self.assertEqual([], evidence["changed_targets"])

    def test_acceptance_changed_files_are_changed_targets(self):
        evidence = self.evidence(
            {"arguments": json.dumps({"changed_files": ["src/a.py"]})},
            name="unrelated-tool",
        )
        self.assertEqual(["src/a.py"], evidence["changed_targets"])
        self.assertEqual([], evidence["other_targets"])

    def test_acceptance_patch_file_marker_is_changed_target_evidence(self):
        patch = "*** Begin Patch\n*** Update File: src/b.py\n*** End Patch\n"
        evidence = self.evidence({"input": patch}, name="unrelated-tool")
        self.assertEqual(["src/b.py"], evidence["changed_targets"])
        self.assertEqual([], evidence["other_targets"])

    def test_acceptance_invalid_json_falls_back_without_raising_or_guessing_a_command(self):
        evidence = self.evidence(
            {"arguments": '{"cmd": "cat README.md"'},
            name="exec_command",
        )
        self.assertEqual(["exec_command: passed"], evidence["tool_calls"])
        self.assertEqual([], evidence["verifications"])

    def test_acceptance_command_and_path_limits_are_independent(self):
        paths = [f"file-{index}.py" for index in range(1, 10)]
        patch = r'const patch = "*** Begin Patch\n'
        patch += "".join(f"*** Update File: {path}\\n" for path in paths)
        patch += r'*** End Patch\n"; '
        patch += " ".join(
            f'await tools.exec_command({{cmd:"./scripts/check-{index}.sh"}});'
            for index in range(1, 10)
        )
        evidence = self.evidence({"input": patch}, name="unrelated-tool")
        self.assertEqual(paths[:8], evidence["changed_targets"])
        self.assertEqual(
            [f"./scripts/check-{index}.sh: passed" for index in range(1, 9)],
            evidence["verifications"],
        )
        self.assertEqual(2, evidence["omitted_count"])

    def test_evidence_budget_prioritizes_verifications_then_builds_then_reads(self):
        reads = [
            "sed -n 1,20p README.md",
            "rg -n synthetic README.md",
            "nl -ba README.md",
            "cat README.md",
            "head -n 5 README.md",
            "tail -n 5 README.md",
            "wc -l README.md",
            "ls tests",
            "find tests -type f",
            "grep -n synthetic README.md",
            "less README.md",
            "file README.md",
            "stat README.md",
            "tree tests",
            "du -sh tests",
            "pwd",
            "echo synthetic",
            "printf synthetic",
            "which python3",
            "type python3",
        ]
        builds = ["git commit synthetic", "npm run build", "python3 scripts/deploy.py"]
        payloads = [
            ("exec_command", {"arguments": json.dumps({"cmd": "./scripts/test.sh"})}),
            *[("exec_command", {"arguments": json.dumps({"cmd": command})}) for command in reads],
            *[("exec_command", {"arguments": json.dumps({"cmd": command})}) for command in builds],
        ]

        evidence = normalize_tools(payloads)["execution_evidence"]

        self.assertEqual(["./scripts/test.sh: passed"], evidence["verifications"])
        self.assertEqual(
            [
                "git commit synthetic: passed",
                "npm run build: passed",
                "python3 scripts/deploy.py: passed",
                "sed -n 1,20p README.md: passed",
                "rg -n synthetic README.md: passed",
                "nl -ba README.md: passed",
                "cat README.md: passed",
            ],
            evidence["tool_calls"],
        )
        self.assertEqual(16, evidence["omitted_count"])

    def test_a_round_with_only_read_commands_keeps_them_in_tool_calls(self):
        payloads = [
            ("exec_command", {"arguments": json.dumps({"cmd": command})})
            for command in (
                "sed -n 1,20p README.md",
                "rg -n synthetic README.md",
                "cat README.md",
            )
        ]

        evidence = normalize_tools(payloads)["execution_evidence"]

        self.assertEqual(
            [
                "sed -n 1,20p README.md: passed",
                "rg -n synthetic README.md: passed",
                "cat README.md: passed",
            ],
            evidence["tool_calls"],
        )
        self.assertEqual([], evidence["verifications"])
        self.assertEqual(0, evidence["omitted_count"])

    def test_read_only_priority_does_not_use_a_fallback_tool_name_as_a_command(self):
        reads = [
            "sed -n 1,20p README.md",
            "rg -n synthetic README.md",
            "nl -ba README.md",
            "head -n 5 README.md",
            "tail -n 5 README.md",
            "wc -l README.md",
            "ls tests",
            "find tests -type f",
        ]
        payloads = [
            *[("exec_command", {"arguments": json.dumps({"cmd": command})}) for command in reads],
            ("cat", {"arguments": json.dumps({"path": "/synthetic/not-a-command"})}),
        ]

        evidence = normalize_tools(payloads)["execution_evidence"]

        self.assertEqual(
            [
                "cat: passed",
                "sed -n 1,20p README.md: passed",
                "rg -n synthetic README.md: passed",
                "nl -ba README.md: passed",
                "head -n 5 README.md: passed",
                "tail -n 5 README.md: passed",
                "wc -l README.md: passed",
                "ls tests: passed",
            ],
            evidence["tool_calls"],
        )
        self.assertEqual(1, evidence["omitted_count"])

    def test_acceptance_payload_without_parameter_fields_preserves_the_existing_evidence_shape(self):
        evidence = self.evidence(
            {"params": {"turnId": "opaque-param-value"}},
            name="tool_without_parameters",
        )
        self.assertEqual(
            {
                "changed_targets": [],
                "other_targets": [],
                "tool_calls": ["tool_without_parameters: passed"],
                "verifications": [],
                "failures": [],
                "user_interrupts": [],
                "omitted_count": 0,
                "source_refs": [
                    "synthetic://synthetic-thread/turn/synthetic-turn/event/synthetic-tool-event"
                ],
            },
            evidence,
        )
        self.assertNotIn("opaque-param-value", json.dumps(evidence, ensure_ascii=False))

    def test_fork_lineage_requires_exact_structured_metadata_not_near_miss_or_user_text(self):
        def document(thread_id, session_id, *, session_extra=None, user_text="普通请求"):
            records = [
                {
                    "timestamp": "2026-08-08T01:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": thread_id, "session_id": session_id, **(session_extra or {})},
                },
                {
                    "timestamp": "2026-08-08T01:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": f"turn-{thread_id}"},
                },
                {
                    "timestamp": "2026-08-08T01:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": user_text},
                },
                {
                    "timestamp": "2026-08-08T01:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final",
                        "content": [{"type": "output_text", "text": "合成结果"}],
                    },
                },
            ]
            return build_session_document(
                source="synthetic",
                locator=f"synthetic/{thread_id}.jsonl",
                records=records,
            )

        near_miss = document(
            "near-miss-thread",
            "near-miss-session",
            session_extra={"forked_from": "parent-thread"},
        )
        user_text = document(
            "user-text-thread",
            "user-text-session",
            user_text="forked_from_id=parent-thread 只是普通文本",
        )
        result = normalize_sessions(
            [near_miss, user_text],
            WINDOW,
            SourceProfile("synthetic", "test"),
        )
        self.assertEqual(2, len(result.slices))
        for item in result.slices:
            self.assertNotIn("forked_from_id", item["source_meta"])
            self.assertIsNone(item["source_meta"]["parent_thread_id"])


if __name__ == "__main__":
    unittest.main()
