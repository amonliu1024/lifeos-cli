import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lifeos_sessions.core import SourceScanRequest, TimeWindow
from lifeos_sessions.pi import PiAdapter


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def request(*, checkpoint=None):
    return SourceScanRequest(
        window=TimeWindow(
            from_ms=epoch_ms("2026-08-31T00:00:00Z"),
            to_ms=epoch_ms("2026-09-01T00:00:00Z"),
            from_iso="2026-08-31T00:00:00Z",
            to_iso="2026-09-01T00:00:00Z",
        ),
        includes=[],
        checkpoint=dict(checkpoint or {}),
        temp_dir=None,
    )


def write_session(path: Path, records, *, partial_tail: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    path.write_text(body + partial_tail, encoding="utf-8")


def as_dict(item):
    if not hasattr(item, "to_dict"):
        return item
    try:
        return item.to_dict(include_mutable=False)
    except TypeError:
        return item.to_dict()


def header(session_id="pi-session-1"):
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-08-31T01:00:00.000Z",
        "cwd": "/synthetic/pi-workspace",
    }


class PiAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sessions"

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, *, checkpoint=None):
        return PiAdapter(root=self.root).scan(request(checkpoint=checkpoint))

    def test_projects_tree_turn_with_latest_branch_result_and_bounded_tools(self):
        write_session(
            self.root / "--synthetic--" / "session.jsonl",
            [
                header(),
                {
                    "type": "session_info",
                    "id": "info-1",
                    "parentId": None,
                    "timestamp": "2026-08-31T01:00:00.500Z",
                    "name": "Pi 采集验证",
                },
                {
                    "type": "message",
                    "id": "user-1",
                    "parentId": "info-1",
                    "timestamp": "2026-08-31T01:00:01.000Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "请验证 Pi 采集"}],
                        "timestamp": 1788138001000,
                    },
                },
                {
                    "type": "message",
                    "id": "assistant-tools",
                    "parentId": "user-1",
                    "timestamp": "2026-08-31T01:00:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "不应进入正文"},
                            {
                                "type": "toolCall",
                                "id": "call-1",
                                "name": "bash",
                                "arguments": {"command": "pytest -q"},
                            },
                            {
                                "type": "toolCall",
                                "id": "call-2",
                                "name": "write",
                                "arguments": {"path": "/synthetic/file.md", "content": "private body"},
                            },
                        ],
                        "stopReason": "toolUse",
                        "timestamp": 1788138002000,
                    },
                },
                {
                    "type": "message",
                    "id": "tool-result",
                    "parentId": "assistant-tools",
                    "timestamp": "2026-08-31T01:00:03.000Z",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "call-1",
                        "toolName": "bash",
                        "content": [{"type": "text", "text": "secret stdout must not persist"}],
                        "details": {"exitCode": 0},
                        "isError": False,
                        "timestamp": 1788138003000,
                    },
                },
                {
                    "type": "message",
                    "id": "tool-result-write",
                    "parentId": "tool-result",
                    "timestamp": "2026-08-31T01:00:04.000Z",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "call-2",
                        "toolName": "write",
                        "content": [{"type": "text", "text": "wrote private body"}],
                        "isError": False,
                        "timestamp": 1788138004000,
                    },
                },
                {
                    "type": "message",
                    "id": "assistant-first",
                    "parentId": "tool-result-write",
                    "timestamp": "2026-08-31T01:00:04.500Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "第一分支完成。"}],
                        "stopReason": "stop",
                        "timestamp": 1788138004000,
                    },
                },
                {
                    "type": "branch_summary",
                    "id": "branch-summary",
                    "parentId": "user-1",
                    "timestamp": "2026-08-31T01:00:05.000Z",
                    "summary": "模型生成的分支摘要，不应进入正文",
                    "fromId": "assistant-first",
                },
                {
                    "type": "message",
                    "id": "assistant-latest",
                    "parentId": "branch-summary",
                    "timestamp": "2026-08-31T01:00:06.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "最新分支完成。"}],
                        "stopReason": "stop",
                        "timestamp": 1788138006000,
                    },
                },
            ],
        )
        result = self.scan()
        self.assertEqual("complete", result.status)
        self.assertEqual(1, len(result.slices))
        item = as_dict(result.slices[0])
        self.assertEqual("pi", item["source"])
        self.assertEqual("pi-session-1", item["conversation"]["id"])
        self.assertEqual("Pi 采集验证", item["conversation"]["title"])
        self.assertEqual("/synthetic/pi-workspace", item["workspace"])
        self.assertEqual("user-1", item["native_unit"]["id"])
        self.assertEqual("请验证 Pi 采集", item["blocks"][0]["text"])
        self.assertEqual("最新分支完成。", item["blocks"][-1]["text"])
        encoded = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("不应进入正文", encoded)
        self.assertNotIn("secret stdout", encoded)
        self.assertNotIn("private body", encoded)
        checks = item["execution_evidence"]["verifications"]
        self.assertTrue(any(value.startswith("pytest -q") for value in checks), checks)
        self.assertIn("/synthetic/file.md", item["execution_evidence"]["changed_targets"])

    def test_structured_abort_without_work_becomes_omission(self):
        write_session(
            self.root / "--synthetic--" / "aborted.jsonl",
            [
                header("pi-aborted"),
                {
                    "type": "message",
                    "id": "user-abort",
                    "parentId": None,
                    "timestamp": "2026-08-31T02:00:01.000Z",
                    "message": {"role": "user", "content": "先看看", "timestamp": 1788141601000},
                },
                {
                    "type": "message",
                    "id": "assistant-abort",
                    "parentId": "user-abort",
                    "timestamp": "2026-08-31T02:00:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "未产生结果"}],
                        "stopReason": "aborted",
                        "timestamp": 1788141602000,
                    },
                },
            ],
        )
        result = self.scan()
        self.assertEqual([], list(result.slices))
        self.assertEqual(1, len(result.omissions))
        omission = as_dict(result.omissions[0])
        self.assertEqual("pi", omission["source"])
        self.assertEqual("user-abort", omission["native_unit"]["id"])

    def test_pre_turn_shell_is_not_materialized_and_recovered_error_is_complete(self):
        write_session(
            self.root / "--synthetic--" / "recovered.jsonl",
            [
                header("pi-recovered"),
                {
                    "type": "message",
                    "id": "shell-before-turn",
                    "parentId": None,
                    "timestamp": "2026-08-31T02:30:00.000Z",
                    "message": {
                        "role": "bashExecution",
                        "command": "pwd",
                        "output": "/synthetic/pi-workspace",
                        "exitCode": 0,
                        "cancelled": False,
                        "timestamp": 1788143400000,
                    },
                },
                {
                    "type": "message",
                    "id": "user-recovered",
                    "parentId": "shell-before-turn",
                    "timestamp": "2026-08-31T02:30:01.000Z",
                    "message": {"role": "user", "content": "失败后继续", "timestamp": 1788143401000},
                },
                {
                    "type": "message",
                    "id": "assistant-error",
                    "parentId": "user-recovered",
                    "timestamp": "2026-08-31T02:30:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "第一次失败"}],
                        "stopReason": "error",
                        "errorMessage": "provider failed",
                        "timestamp": 1788143402000,
                    },
                },
                {
                    "type": "message",
                    "id": "assistant-recovered",
                    "parentId": "assistant-error",
                    "timestamp": "2026-08-31T02:30:03.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "恢复完成"}],
                        "stopReason": "stop",
                        "timestamp": 1788143403000,
                    },
                },
            ],
        )
        result = self.scan()
        self.assertEqual("complete", result.status)
        self.assertEqual(1, len(result.slices))
        item = as_dict(result.slices[0])
        self.assertEqual("user-recovered", item["native_unit"]["id"])
        self.assertEqual("complete", item["content_completeness"])
        self.assertEqual("completed", item["turn_completion"])
        self.assertIn("provider failed", item["execution_evidence"]["failures"])

    def test_incomplete_tail_is_partial_and_not_cacheable(self):
        write_session(
            self.root / "--synthetic--" / "partial.jsonl",
            [
                header("pi-partial"),
                {
                    "type": "message",
                    "id": "user-partial",
                    "parentId": None,
                    "timestamp": "2026-08-31T03:00:01.000Z",
                    "message": {"role": "user", "content": "保留完整行", "timestamp": 1788145201000},
                },
                {
                    "type": "message",
                    "id": "assistant-partial",
                    "parentId": "user-partial",
                    "timestamp": "2026-08-31T03:00:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "完整答复"}],
                        "stopReason": "stop",
                        "timestamp": 1788145202000,
                    },
                },
            ],
            partial_tail='{"type":"message"',
        )
        result = self.scan()
        self.assertEqual("partial", result.status)
        self.assertFalse(result.checkpoint["cacheable"])
        self.assertEqual("partial", as_dict(result.slices[0])["content_completeness"])

    def test_v2_hook_message_is_noise_and_unparsed_user_image_is_partial(self):
        old_header = header("pi-v2-image")
        old_header["version"] = 2
        write_session(
            self.root / "--synthetic--" / "v2-image.jsonl",
            [
                old_header,
                {
                    "type": "message",
                    "id": "user-image",
                    "parentId": None,
                    "timestamp": "2026-08-31T03:30:01.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看看这张图"},
                            {"type": "image", "data": "not-persisted", "mimeType": "image/png"},
                        ],
                        "timestamp": 1788147001000,
                    },
                },
                {
                    "type": "message",
                    "id": "hook-message",
                    "parentId": "user-image",
                    "timestamp": "2026-08-31T03:30:01.500Z",
                    "message": {
                        "role": "hookMessage",
                        "content": "extension context must not persist",
                        "timestamp": 1788147001500,
                    },
                },
                {
                    "type": "message",
                    "id": "assistant-image",
                    "parentId": "hook-message",
                    "timestamp": "2026-08-31T03:30:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "只能确认文字部分"}],
                        "stopReason": "stop",
                        "timestamp": 1788147002000,
                    },
                },
            ],
        )
        result = self.scan()
        self.assertEqual("partial", result.status)
        self.assertEqual(1, len(result.slices))
        item = as_dict(result.slices[0])
        self.assertEqual("partial", item["content_completeness"])
        self.assertIn("pi_image_content_unparsed", item["warnings"])
        encoded = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("not-persisted", encoded)
        self.assertNotIn("extension context", encoded)

    def test_checkpoint_reuses_unchanged_files(self):
        write_session(
            self.root / "--synthetic--" / "stable.jsonl",
            [
                header("pi-stable"),
                {
                    "type": "message",
                    "id": "user-stable",
                    "parentId": None,
                    "timestamp": "2026-08-31T04:00:01.000Z",
                    "message": {"role": "user", "content": "稳定扫描", "timestamp": 1788148801000},
                },
                {
                    "type": "message",
                    "id": "assistant-stable",
                    "parentId": "user-stable",
                    "timestamp": "2026-08-31T04:00:02.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "稳定结果"}],
                        "stopReason": "stop",
                        "timestamp": 1788148802000,
                    },
                },
            ],
        )
        first = self.scan()
        second = self.scan(checkpoint=first.checkpoint)
        self.assertEqual(1, first.stats["files_read"])
        self.assertEqual(0, second.stats["files_read"])
        self.assertEqual(1, second.stats["reused_from_checkpoint"])

    def test_missing_directory_fails_without_creating_it(self):
        missing = self.root / "missing"
        result = PiAdapter(root=missing).scan(request())
        self.assertEqual("failed", result.status)
        self.assertEqual("source_unavailable", result.error["code"])
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
