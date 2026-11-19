import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lifeos_sessions.antigravity import AntigravityAdapter
from lifeos_sessions.core import SourceScanRequest, TimeWindow
from lifeos_sessions.evidence import read_evidence


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def request(*, checkpoint=None, includes=()):
    return SourceScanRequest(
        window=TimeWindow(
            from_ms=epoch_ms("2026-11-20T00:00:00Z"),
            to_ms=epoch_ms("2026-11-21T00:00:00Z"),
            from_iso="2026-11-20T00:00:00Z",
            to_iso="2026-11-21T00:00:00Z",
        ),
        includes=includes,
        checkpoint=dict(checkpoint or {}),
        temp_dir=None,
    )


def write_transcript(root: Path, conv_id: str, records, *, filename: str = "transcript_full.jsonl", partial_tail: str = "") -> Path:
    path = root / "brain" / conv_id / ".system_generated" / "logs" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    path.write_text(body + partial_tail, encoding="utf-8")
    return path


def write_summaries_db(root: Path, rows, parents=()):
    db_path = root / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT "",
                workspace_uris TEXT NOT NULL DEFAULT "[]",
                parent_conversation_id TEXT NOT NULL DEFAULT "",
                last_modified_time TEXT NOT NULL DEFAULT ""
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_parents (
                conversation_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL
            )
            """
        )
        conn.execute("DELETE FROM conversation_summaries")
        conn.execute("DELETE FROM conversation_parents")
        for row in rows:
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, title, workspace_uris, parent_conversation_id, last_modified_time) VALUES (?, ?, ?, ?, ?)",
                row,
            )
        for parent in parents:
            conn.execute(
                "INSERT INTO conversation_parents (conversation_id, parent_id) VALUES (?, ?)",
                parent,
            )
        conn.commit()
    finally:
        conn.close()


def as_dict(item):
    if not hasattr(item, "to_dict"):
        return item
    try:
        return item.to_dict(include_mutable=False)
    except TypeError:
        return item.to_dict()


class AntigravityAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".gemini" / "antigravity-cli"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, *, checkpoint=None, includes=()):
        return AntigravityAdapter(root=self.root).scan(
            request(checkpoint=checkpoint, includes=includes)
        )

    def test_projects_transcript_with_db_enrichment_bounded_tools_and_evidence_readback(self):
        conv_id = "conv-main-1"
        write_summaries_db(
            self.root,
            [
                (
                    conv_id,
                    "AntiGravity 验证",
                    json.dumps(["file:///synthetic/my%20repo"]),
                    "",
                    "2026-11-20T01:05:00Z",
                ),
            ],
        )
        write_transcript(
            self.root,
            conv_id,
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:00Z",
                    "content": (
                        "<USER_REQUEST>\n请实现 AntiGravity 采集\n</USER_REQUEST>\n"
                        "<ADDITIONAL_METADATA>\nMETADATA_MUST_NOT_APPEAR\n</ADDITIONAL_METADATA>\n"
                        "<USER_SETTINGS_CHANGE>\nSETTINGS_MUST_NOT_APPEAR\n</USER_SETTINGS_CHANGE>"
                    ),
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:05Z",
                    "model": "gemini-4-hydrogen-high",
                    "thinking": "PRIVATE_THINKING_MUST_NOT_APPEAR",
                    "tool_calls": [
                        {
                            "name": "read_file",
                            "args": {"Path": "/synthetic/my repo/README.md"},
                        },
                        {
                            "name": "write_file",
                            "args": {
                                "Path": "/synthetic/my repo/adapter.py",
                                "Content": "PRIVATE_FILE_BODY_MUST_NOT_APPEAR",
                            },
                        },
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "pytest -q"},
                        },
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "python3 -m compileall ."},
                        },
                    ],
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:06Z",
                    "content": "PRIVATE_READ_STDOUT_MUST_NOT_APPEAR",
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:07Z",
                    "content": "Created file.",
                },
                {
                    "step_index": 4,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "ERROR",
                    "created_at": "2026-11-20T01:00:08Z",
                    "error": "pytest failed once",
                    "content": "Command exited with code 1.\nPRIVATE_PYTEST_FAIL_STDOUT",
                },
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:09Z",
                    "content": "Command exited with code 0.",
                },
                {
                    "step_index": 6,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:10Z",
                    "content": "SYSTEM_REMINDER_MUST_NOT_APPEAR",
                },
                {
                    "step_index": 7,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T01:00:12Z",
                    "thinking": "FINAL_THINKING_MUST_NOT_APPEAR",
                    "content": "已完成 AntiGravity 采集适配器。",
                },
            ],
        )

        result = self.scan()
        self.assertEqual("complete", result.status)
        self.assertEqual(1, len(result.slices))
        item = as_dict(result.slices[0])
        self.assertEqual("antigravity", item["source"])
        self.assertEqual(conv_id, item["conversation"]["id"])
        self.assertEqual("AntiGravity 验证", item["conversation"]["title"])
        self.assertEqual("/synthetic/my repo", item["workspace"])
        self.assertEqual("step-0", item["native_unit"]["id"])
        self.assertEqual("请实现 AntiGravity 采集", item["blocks"][0]["text"])
        self.assertEqual("已完成 AntiGravity 采集适配器。", item["blocks"][-1]["text"])

        encoded = json.dumps(item, ensure_ascii=False)
        for forbidden in (
            "METADATA_MUST_NOT_APPEAR",
            "SETTINGS_MUST_NOT_APPEAR",
            "PRIVATE_THINKING_MUST_NOT_APPEAR",
            "FINAL_THINKING_MUST_NOT_APPEAR",
            "PRIVATE_FILE_BODY_MUST_NOT_APPEAR",
            "PRIVATE_READ_STDOUT_MUST_NOT_APPEAR",
            "PRIVATE_PYTEST_FAIL_STDOUT",
            "SYSTEM_REMINDER_MUST_NOT_APPEAR",
        ):
            self.assertNotIn(forbidden, encoded)

        ev = item["execution_evidence"]
        self.assertIn("/synthetic/my repo/adapter.py", ev["changed_targets"])
        self.assertIn("/synthetic/my repo/README.md", ev["other_targets"])
        self.assertTrue(any(v == "pytest -q: failed" for v in ev["verifications"]), ev["verifications"])
        self.assertTrue(any("compileall" in v and v.endswith(": passed") for v in ev["verifications"]), ev["verifications"])
        self.assertTrue(any("pytest failed once" in f for f in ev["failures"]), ev["failures"])

        records = item["source_meta"]["evidence_records"]
        self.assertTrue(records)
        page = read_evidence(item, records[0]["ref"], self.root)
        self.assertTrue(page["source_verified"])
        self.assertIn("read_file", page["text"])

    def test_links_child_conversation_delegation_via_db_parents(self):
        write_summaries_db(
            self.root,
            [
                ("parent-1", "主会话", '["file:///synthetic/ws"]', "", "2026-11-20T02:01:00Z"),
                ("child-1", "子会话", '["file:///synthetic/ws"]', "", "2026-11-20T02:00:50Z"),
            ],
            parents=[("child-1", "parent-1")],
        )
        write_transcript(
            self.root,
            "parent-1",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T02:00:00Z",
                    "content": "<USER_REQUEST>\n主任务\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T02:01:00Z",
                    "content": "主任务完成。",
                },
            ],
        )
        write_transcript(
            self.root,
            "child-1",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T02:00:10Z",
                    "content": "<USER_REQUEST>\n子任务调研\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T02:00:50Z",
                    "content": "子任务结论已返回。",
                },
            ],
        )
        result = self.scan()
        self.assertEqual("complete", result.status)
        self.assertEqual(2, len(result.slices))
        parent = next(as_dict(s) for s in result.slices if as_dict(s)["conversation"]["id"] == "parent-1")
        self.assertEqual(1, len(parent["delegations"]))
        self.assertEqual("child-1", parent["delegations"][0]["child_thread_id"])
        self.assertEqual("子任务调研", parent["delegations"][0]["task"])
        self.assertEqual("子任务结论已返回。", parent["delegations"][0]["result"])

    def test_cancelled_turn_without_work_becomes_omission(self):
        write_transcript(
            self.root,
            "conv-abort",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T03:00:00Z",
                    "content": "<USER_REQUEST>\n先查一下\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "CANCELLED",
                    "created_at": "2026-11-20T03:00:02Z",
                    "thinking": "尚未执行任何实质动作",
                },
            ],
        )
        result = self.scan()
        self.assertEqual([], list(result.slices))
        self.assertEqual(1, len(result.omissions))
        omission = as_dict(result.omissions[0])
        self.assertEqual("antigravity", omission["source"])
        self.assertEqual("step-0", omission["native_unit"]["id"])

    def test_compact_fallback_with_truncated_fields_and_unknown_step_are_partial(self):
        write_transcript(
            self.root,
            "conv-compact",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T04:00:00Z",
                    "content": "<USER_REQUEST>\n回退压缩日志\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "FUTURE_UNKNOWN_STEP",
                    "status": "DONE",
                    "created_at": "2026-11-20T04:00:01Z",
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T04:00:02Z",
                    "content": "部分结果",
                    "truncated_fields": ["thinking"],
                },
            ],
            filename="transcript.jsonl",
        )
        result = self.scan()
        self.assertEqual("partial", result.status)
        self.assertEqual(1, len(result.slices))
        item = as_dict(result.slices[0])
        self.assertEqual("partial", item["content_completeness"])
        self.assertIn("antigravity_compact_transcript_truncated", item["warnings"])
        self.assertTrue(any(w.startswith("unknown_record_type:") for w in item["warnings"]))

    def test_incomplete_tail_is_partial_and_not_cacheable(self):
        write_transcript(
            self.root,
            "conv-tail",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T05:00:00Z",
                    "content": "<USER_REQUEST>\n尾部未写完\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T05:00:02Z",
                    "content": "完成",
                },
            ],
            partial_tail='{"step_index":2,"broken',
        )
        first = self.scan()
        self.assertEqual("partial", first.status)
        self.assertFalse(first.checkpoint["cacheable"])
        self.assertTrue(any(w.startswith("incomplete_tail:") for w in first.warnings))
        second = self.scan(checkpoint=first.checkpoint)
        self.assertEqual(1, second.stats["files_read"])

    def test_checkpoint_reuses_unchanged_and_invalidates_on_db_title_change(self):
        write_summaries_db(
            self.root,
            [("conv-cache", "原标题", '["file:///synthetic/ws"]', "", "2026-11-20T06:00:01Z")],
        )
        write_transcript(
            self.root,
            "conv-cache",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-20T06:00:00Z",
                    "content": "<USER_REQUEST>\n缓存测试\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-20T06:00:01Z",
                    "content": "完成。",
                },
            ],
        )
        first = self.scan()
        self.assertEqual("complete", first.status)
        self.assertEqual(1, first.stats["files_read"])

        second = self.scan(checkpoint=first.checkpoint)
        self.assertEqual("complete", second.status)
        self.assertEqual(0, second.stats["files_read"])
        self.assertEqual(
            [as_dict(s)["slice_id"] for s in first.slices],
            list(second.reused_slice_ids),
        )

        # Updating only SQLite title must invalidate checkpoint and re-read.
        write_summaries_db(
            self.root,
            [("conv-cache", "新标题", '["file:///synthetic/ws"]', "", "2026-11-20T06:00:01Z")],
        )
        third = self.scan(checkpoint=second.checkpoint)
        self.assertEqual("complete", third.status)
        self.assertEqual(1, third.stats["files_read"])
        self.assertEqual("新标题", as_dict(third.slices[0])["conversation"]["title"])


if __name__ == "__main__":
    unittest.main()
