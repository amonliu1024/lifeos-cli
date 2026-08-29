import json
import tempfile
import unittest
from pathlib import Path

from lifeos_sessions.claude import ClaudeAdapter, _is_delegation_tool
from lifeos_sessions.core import SourceScanRequest, TimeWindow


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "sessions" / "claude" / "projects"


def _slice_dict(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _write_events(root: Path, filename: str, events):
    path = root / "projects" / "-synthetic" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        encoding="utf-8",
    )
    return path


class ClaudeAdapterFixtureTest(unittest.TestCase):
    def request(self, start="2026-08-08T00:00:00Z", end="2026-08-08T00:02:00Z", checkpoint=None):
        return SourceScanRequest(
            window=TimeWindow.from_values(start, end),
            checkpoint=checkpoint,
        )

    def test_projects_turns_delegations_and_only_retained_user_terminal_text(self):
        result = ClaudeAdapter(FIXTURE_ROOT).scan(self.request())
        self.assertEqual("partial", result.status)
        self.assertEqual(2, len(result.slices))

        first, second = [_slice_dict(value) for value in result.slices]
        self.assertEqual("claude", first["source"])
        self.assertEqual("user-1", first["native_unit"]["id"])
        self.assertEqual("user-2", second["native_unit"]["id"])
        self.assertEqual("complete", first["content_completeness"])
        self.assertEqual("partial", second["content_completeness"])

        text = "\n".join(block.get("text", "") for item in (first, second) for block in item["blocks"])
        self.assertIn("请检查项目状态", text)
        self.assertIn("项目状态已检查完成。", text)
        self.assertIn("选择环境: 测试环境", text)
        self.assertIn("第二个问题仍在处理中。", text)
        for noise in ("internal reasoning", "final internal thought", "tool stdout", "compact summary", "后台任务已完成"):
            self.assertNotIn(noise, text)

        outcome = [block for block in first["blocks"] if block["kind"] == "agent_message"]
        self.assertEqual(["项目状态已检查完成。"], [block["text"] for block in outcome])
        user_block = next(block for block in first["blocks"] if block["kind"] == "message")
        self.assertTrue(user_block["context"])
        self.assertFalse(outcome[0]["context"])
        self.assertEqual(1, len(second["delegations"]))
        delegation = second["delegations"][0]
        self.assertEqual("agent-42", delegation["agent_id"])
        # ``agent_path`` says which agent ran; the prompt is the task and does
        # not get to stand in for an identity.
        self.assertEqual("general-purpose", delegation["agent_path"])
        self.assertEqual("检查第二个问题", delegation["task"])
        self.assertEqual("claude-side-42", delegation["thread_id"])
        self.assertEqual("claude-side-42", delegation["child_thread_id"])
        self.assertIsNone(delegation["turn_id"])
        self.assertTrue(delegation["source_refs"])
        self.assertEqual("complete", delegation["status"])
        self.assertIn("子 Agent 已完成检查", delegation["result"])
        self.assertEqual(
            {"agent_id", "agent_path", "thread_id", "child_thread_id", "turn_id", "task", "result", "status", "source_refs"},
            set(delegation),
        )

        self.assertEqual("人工命名的会话", first["conversation"]["title"])
        self.assertEqual(1, len([block for block in second["blocks"] if block["kind"] == "delegation"]))

    def test_a_todo_tool_is_not_a_delegation(self):
        # ``TaskCreate`` carries a description and hands work to no one, so a
        # description must not be what makes something a delegation.
        self.assertFalse(_is_delegation_tool("TaskCreate", {"description": "写周报"}))
        self.assertFalse(_is_delegation_tool("TaskUpdate", {"description": "改状态"}))
        self.assertTrue(_is_delegation_tool("Agent", {"subagent_type": "Explore"}))
        self.assertTrue(_is_delegation_tool("run_thing", {"agentId": "agent-42"}))

    def test_shell_tool_calls_keep_nonverification_command_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_events(
                root,
                "shell-command.jsonl",
                [
                    {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "读取说明"}},
                    {"type": "assistant", "sessionId": "target", "uuid": "shell-use", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "shell-1", "name": "Bash", "input": {"command": "cat README.md"}}], "stop_reason": None}},
                    {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "shell-use", "timestamp": "2026-08-08T00:00:02Z", "message": {"role": "assistant", "content": "已读取", "stop_reason": "end_turn"}},
                ],
            )
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T00:01:00Z"))
            item = _slice_dict(result.slices[0])

        self.assertEqual(["cat README.md"], item["execution_evidence"]["tool_calls"])
        self.assertNotIn("Bash", item["execution_evidence"]["tool_calls"])

    def test_checkpoint_reuses_stable_slices_and_append_invalidates_cache(self):
        adapter = ClaudeAdapter(FIXTURE_ROOT)
        first_result = adapter.scan(self.request())
        self.assertIn("cache_generation", first_result.checkpoint)
        first = [_slice_dict(value) for value in first_result.slices]
        checkpoint_text = json.dumps(first_result.checkpoint, ensure_ascii=False)
        self.assertNotIn("请检查项目状态", checkpoint_text)
        self.assertNotIn("项目状态已检查完成", checkpoint_text)
        second_result = adapter.scan(self.request(checkpoint=first_result.checkpoint))
        self.assertEqual(
            [item["slice_id"] for item in first],
            list(second_result.reused_slice_ids),
        )
        self.assertEqual(0, second_result.stats["files_read"])
        self.assertEqual(second_result.stats["examined"], second_result.stats["files_skipped"])
        incomplete_checkpoint = dict(first_result.checkpoint)
        incomplete_checkpoint.pop("cache_generation")
        rescanned = adapter.scan(self.request(checkpoint=incomplete_checkpoint))
        self.assertGreater(rescanned.stats["files_read"], 0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _write_events(
                root,
                "append.jsonl",
                [
                    {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "检查"}},
                    {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
                ],
            )
            adapter = ClaudeAdapter(root)
            first = adapter.scan(self.request(end="2026-08-08T00:01:00Z"))
            cached = adapter.scan(self.request(end="2026-08-08T00:01:00Z", checkpoint=first.checkpoint))
            self.assertEqual(0, cached.stats["files_read"])
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "user", "sessionId": "target", "uuid": "second-user", "timestamp": "2026-08-08T00:00:10Z", "message": {"role": "user", "content": "再次检查"}}, ensure_ascii=False) + "\n")
                handle.write(json.dumps({"type": "assistant", "sessionId": "target", "uuid": "second-final", "parentUuid": "second-user", "timestamp": "2026-08-08T00:00:11Z", "message": {"role": "assistant", "content": "再次完成", "stop_reason": "end_turn"}}, ensure_ascii=False) + "\n")
            refreshed = adapter.scan(self.request(end="2026-08-08T00:01:00Z", checkpoint=first.checkpoint))
            self.assertEqual(1, refreshed.stats["files_read"])
            self.assertIn("second-user", {_slice_dict(item)["native_unit"]["id"] for item in refreshed.slices})

    def test_ai_title_is_used_but_random_slug_is_never_a_title(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_events(
                root,
                "ai-title.jsonl",
                [
                    {"type": "ai-title", "sessionId": "ai-session", "timestamp": "2026-08-08T00:00:00Z", "aiTitle": "应用生成标题"},
                    {"type": "user", "sessionId": "ai-session", "uuid": "ai-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "user", "content": "检查标题"}},
                    {"type": "assistant", "sessionId": "ai-session", "uuid": "ai-final", "parentUuid": "ai-user", "timestamp": "2026-08-08T00:00:02Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
                ],
            )
            _write_events(
                root,
                "slug-only.jsonl",
                [
                    {"type": "user", "sessionId": "slug-session", "slug": "indexed-kindling-dolphin", "uuid": "slug-user", "timestamp": "2026-08-08T00:00:03Z", "message": {"role": "user", "content": "只有随机 slug"}},
                    {"type": "assistant", "sessionId": "slug-session", "uuid": "slug-final", "parentUuid": "slug-user", "timestamp": "2026-08-08T00:00:04Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
                ],
            )
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T00:01:00Z"))
            by_session = {
                _slice_dict(item)["conversation"]["id"]: _slice_dict(item)
                for item in result.slices
            }
            self.assertEqual("应用生成标题", by_session["ai-session"]["conversation"]["title"])
            self.assertIsNone(by_session["slug-session"]["conversation"]["title"])

    def test_incomplete_tail_is_checkpointed_without_reading_real_claude(self):
        with tempfile.TemporaryDirectory() as temp:
            projects = Path(temp) / "projects" / "-synthetic"
            projects.mkdir(parents=True)
            path = projects / "tail.jsonl"
            complete = {
                "type": "user",
                "sessionId": "tail-session",
                "uuid": "tail-user",
                "timestamp": "2026-08-08T00:00:00Z",
                "message": {"role": "user", "content": "尾行测试"},
            }
            with path.open("wb") as handle:
                handle.write((json.dumps(complete, ensure_ascii=False) + "\n").encode())
                handle.write(b'{"type":"assistant","sessionId":"tail-session"')

            result = ClaudeAdapter(Path(temp)).scan(self.request(end="2026-08-08T00:01:00Z"))
            self.assertEqual("partial", result.status)
            self.assertTrue(any("incomplete_tail" in warning for warning in result.warnings))
            self.assertTrue(result.checkpoint["files"][str(path)]["incomplete_tail"])

    def test_orphan_branch_scope_downgrade_and_warning_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_events(
                root,
                "current.jsonl",
                [
                    {"type": "user", "sessionId": "current", "uuid": "current-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "当前问题"}},
                    {"type": "assistant", "sessionId": "current", "uuid": "current-final", "parentUuid": "current-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": "已完成", "stop_reason": "end_turn"}},
                ],
            )
            _write_events(
                root,
                "history.jsonl",
                [
                    {"type": "user", "sessionId": "history", "uuid": "history-user", "timestamp": "2026-08-01T00:00:00Z", "message": {"role": "user", "content": "历史问题"}},
                    {"type": "assistant", "sessionId": "history", "uuid": "history-orphan", "parentUuid": "missing-parent", "timestamp": "2026-08-01T00:00:01Z", "message": {"role": "assistant", "content": "历史分支"}},
                ],
            )
            result = ClaudeAdapter(root).scan(
                self.request(end="2026-08-08T00:01:00Z")
            )
            self.assertEqual("complete", result.status)
            self.assertEqual(1, len(result.slices))
            self.assertFalse(any("orphan_branch" in warning for warning in result.warnings))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_events(
                root,
                "target.jsonl",
                [
                    {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "检查"}},
                    {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
                    {"type": "assistant", "sessionId": "target", "uuid": "target-orphan", "parentUuid": "missing-parent", "timestamp": "2026-08-08T00:00:02Z", "message": {"role": "assistant", "content": "分支"}},
                ],
            )
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T00:01:00Z"))
            self.assertEqual("partial", result.status)
            item = _slice_dict(result.slices[0])
            self.assertEqual("partial", item["content_completeness"])
            warnings = list(result.warnings) + list(item["warnings"])
            self.assertTrue(any("orphan_branch" in warning and "session=target" in warning and "turn=target-user" in warning for warning in warnings))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "检查"}},
                {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
            ]
            for index in range(200):
                events.append({"type": "assistant", "sessionId": "target", "uuid": f"orphan-{index}", "parentUuid": f"missing-{index}", "timestamp": "2026-08-08T00:00:02Z", "message": {"role": "assistant", "content": "分支"}})
            _write_events(root, "target.jsonl", events)
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T01:00:00Z"))
            self.assertEqual("partial", result.status)
            item = _slice_dict(result.slices[0])
            self.assertLessEqual(len(result.warnings), 32)
            self.assertLessEqual(len(item["warnings"]), 32)
            self.assertTrue(any("orphan_branch" in warning and "omitted_count=" in warning for warning in result.warnings))

    def test_unknown_record_in_window_is_not_silently_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_events(
                root,
                "unknown.jsonl",
                [
                    {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "检查"}},
                    {"type": "future_record", "sessionId": "target", "uuid": "future-1", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "payload": {"opaque": True}},
                    {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "future-1", "timestamp": "2026-08-08T00:00:02Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
                ],
            )
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T00:01:00Z"))
            self.assertEqual("partial", result.status)
            item = _slice_dict(result.slices[0])
            self.assertEqual("partial", item["content_completeness"])
            self.assertTrue(any("unsupported_format" in warning for warning in item["warnings"]))

    def test_known_metadata_and_process_noise_does_not_degrade_or_add_refs(self):
        noise_cases = [
            ("ai-title", {}),
            ("custom-title", {}),
            ("agent-name", {}),
            ("permission-mode", {}),
            ("mode", {}),
            ("last-prompt", {}),
            ("file-history-snapshot", {}),
            ("file-history-delta", {}),
            ("queue-operation", {}),
            ("attachment", {}),
            ("system", {"subtype": "turn_duration", "durationMs": 1000}),
            ("system", {"subtype": "away_summary"}),
            ("system", {"subtype": "compact_boundary"}),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "检查"}},
                {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": "完成", "stop_reason": "end_turn"}},
            ]
            for index, (record_type, extra) in enumerate(noise_cases):
                event = {
                    "type": record_type,
                    "sessionId": "target",
                    "uuid": f"noise-{index}",
                    "parentUuid": "target-final",
                }
                if record_type not in {"last-prompt", "file-history-snapshot"}:
                    event["timestamp"] = "2026-08-08T00:00:02Z"
                event.update(extra)
                events.append(event)
            _write_events(root, "metadata-noise.jsonl", events)
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T00:01:00Z"))
            self.assertEqual("complete", result.status)
            item = _slice_dict(result.slices[0])
            self.assertEqual("complete", item["content_completeness"])
            self.assertEqual([], item["warnings"])
            self.assertEqual(
                {"target-user", "target-final"},
                {ref.rsplit(":", 1)[-1] for ref in item["source_refs"]},
            )

    def test_source_refs_follow_retained_blocks_and_structured_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_events(
                root,
                "refs.jsonl",
                [
                    {"type": "user", "sessionId": "target", "uuid": "target-user", "timestamp": "2026-08-08T00:00:00Z", "message": {"role": "user", "content": "修改文件"}},
                    {"type": "assistant", "sessionId": "target", "uuid": "edit-use", "parentUuid": "target-user", "timestamp": "2026-08-08T00:00:01Z", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "edit-1", "name": "Edit", "input": {"file_path": "src/app.py"}}], "stop_reason": None}},
                    {"type": "assistant", "sessionId": "target", "uuid": "target-final", "parentUuid": "edit-use", "timestamp": "2026-08-08T00:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "已修改"}], "stop_reason": "end_turn"}},
                ],
            )
            result = ClaudeAdapter(root).scan(self.request(end="2026-08-08T00:01:00Z"))
            item = _slice_dict(result.slices[0])
            self.assertEqual(["src/app.py"], item["execution_evidence"]["changed_targets"])
            self.assertEqual(3, len(item["source_refs"]))
            self.assertEqual({"target-user", "edit-use", "target-final"}, {ref.rsplit(":", 1)[-1] for ref in item["source_refs"]})


if __name__ == "__main__":
    unittest.main()
