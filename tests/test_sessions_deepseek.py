import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lifeos_sessions.deepseek import DeepseekAdapter
from lifeos_sessions.core import SourceScanRequest, TimeWindow


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


def write_session(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    ).encode("utf-8")
    subprocess.run(
        ["zstd", "-q", "-o", str(path), "-f", "-"],
        input=body,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def fixture_records():
    return [
        {
            "type": "session",
            "version": 0,
            "id": "session-fix-1",
            "createdAt": 1786960325686,
            "cwd": "/synthetic/workspace",
        },
        {"type": "permission/preset", "seq": 0, "time": 1786960325691, "data": {"preset": "workspace-write"}},
        {"type": "session/title", "seq": 1, "time": 1786960354821, "data": {"title": "采集接入验证"}},
        {"type": "turn/start", "seq": 4, "time": 1786960354806, "data": {"turn": 1}},
        {
            "type": "user/message",
            "seq": 7,
            "time": 1786960354821,
            "data": {
                "content": [{"type": "text", "text": "请验证 deepseek 采集"}],
                "role": "user",
                "id": "user-msg-1",
            },
        },
        {
            "type": "assistant/message",
            "seq": 8,
            "time": 1786960359442,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "内部推理"},
                        {"type": "tool-call", "id": "call-1", "name": "bash", "arguments": "{\"command\":\"pytest -q\"}"},
                        {"type": "text", "text": "先执行测试再答复。"},
                    ],
                    "id": "assistant-msg-1",
                },
            },
        },
        {
            "type": "tool/call",
            "seq": 8,
            "time": 1786960359443,
            "data": {"turn": 1, "step": 1, "callId": "call-1", "name": "bash", "arguments": "{\"command\":\"pytest -q\"}"},
        },
        {
            "type": "tool/result",
            "seq": 9,
            "time": 1786960366821,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "source": {"kind": "tool", "callId": "call-1"},
                    "content": [{"type": "tool-result", "toolCallId": "call-1", "content": [{"type": "text", "text": "3 passed"}], "isError": False}],
                },
            },
        },
        {
            "type": "assistant/message",
            "seq": 10,
            "time": 1786960367000,
            "data": {
                "turn": 1,
                "step": 2,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "验证完成。"}], "id": "assistant-msg-2"},
            },
        },
        {"type": "turn/end", "seq": 11, "time": 1786960367100, "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]


@unittest.skipIf(shutil.which("zstd") is None, "zstd CLI is required")
class DeepseekAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sessions"

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, **kwargs):
        return DeepseekAdapter(root=self.root).scan(
            request("2026-08-17T00:00:00Z", "2026-08-18T00:00:00Z", **kwargs)
        )

    def test_projects_turns_with_bounded_tool_evidence(self):
        write_session(
            self.root / "--workspace--" / "session-fix-1" / "session.jsonl.zstd",
            fixture_records(),
        )
        result = self.scan()
        self.assertEqual("deepseek", result.source)
        self.assertEqual(1, len(result.slices))
        item = as_dict(result.slices[0])
        self.assertEqual("session-fix-1", item["conversation"]["id"])
        self.assertEqual("采集接入验证", item["conversation"]["title"])
        self.assertEqual("/synthetic/workspace", item["workspace"])
        self.assertEqual("turn-1", item["native_unit"]["id"])
        self.assertEqual("请验证 deepseek 采集", item["blocks"][0]["text"])
        # The final spoken message is the turn outcome; intermediate steps
        # whose only content was tool calls are summarised, not quoted.
        self.assertEqual("验证完成。", item["blocks"][-1]["text"])
        # Reasoning and streaming internals never become quotable blocks.
        self.assertNotIn("内部推理", json.dumps(item, ensure_ascii=False))
        evidence = item["execution_evidence"]
        self.assertTrue(
            any(value.startswith("pytest -q") for value in evidence["tool_calls"] + evidence["verifications"]),
            evidence["tool_calls"] + evidence["verifications"],
        )
        self.assertNotIn("3 passed", json.dumps(item, ensure_ascii=False))

    def test_missing_directory_fails_without_touching_home(self):
        result = DeepseekAdapter(root=self.root / "absent").scan(
            request("2026-08-17T00:00:00Z", "2026-08-18T00:00:00Z")
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("source_unavailable", result.error["code"])

    def test_checkpoint_reuses_unchanged_files(self):
        locator = self.root / "--workspace--" / "session-fix-1" / "session.jsonl.zstd"
        write_session(locator, fixture_records())
        first = self.scan()
        self.assertIn("cache_generation", first.checkpoint)
        self.assertEqual(1, first.stats["files_read"])
        second = DeepseekAdapter(root=self.root).scan(
            request(
                "2026-08-17T00:00:00Z",
                "2026-08-18T00:00:00Z",
                checkpoint=first.checkpoint,
            )
        )
        self.assertEqual(0, second.stats["files_read"])
        self.assertEqual(1, second.stats["files_skipped"])
        self.assertEqual(1, second.stats["slices"])
        self.assertEqual(1, second.stats["reused_from_checkpoint"])
        self.assertEqual(1, first.stats["slices"])
        incomplete_checkpoint = dict(first.checkpoint)
        incomplete_checkpoint.pop("cache_generation")
        rescanned = DeepseekAdapter(root=self.root).scan(
            request(
                "2026-08-17T00:00:00Z",
                "2026-08-18T00:00:00Z",
                checkpoint=incomplete_checkpoint,
            )
        )
        self.assertGreater(rescanned.stats["files_read"], 0)


if __name__ == "__main__":
    unittest.main()
