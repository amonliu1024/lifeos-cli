import tempfile
import unittest
from pathlib import Path

from lifeos_sessions.core import AdapterResult, TimeWindow, canonical_json
from lifeos_sessions.pack import build_activity_index, build_analysis_pack
from lifeos_sessions.store import SessionsStore
from tests.test_sessions_core import make_slice


class AnalysisPackTest(unittest.TestCase):
    def test_activity_lifecycle_fields_cover_each_single_slice_state(self):
        states = ("completed", "incomplete", "interrupted_with_result")
        for state in states:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                store = SessionsStore(Path(temporary) / "sessions", create=True)
                window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
                item = make_slice(
                    conversation=f"thread-{state}",
                    native_id=f"turn-{state}",
                    text="这是一条可读的生命周期测试请求",
                    turn_completion=state,
                ).finalize()
                store.commit_scan(
                    f"SCN-lifecycle-{state}",
                    [AdapterResult("codex", slices=[item])],
                    window,
                )

                index = build_activity_index(store, window)
                pack = build_analysis_pack(store, window)
                index_activity = index["activities"][0]
                pack_activity = pack["activities"][0]
                expected_counts = {
                    "completed": int(state == "completed"),
                    "incomplete": int(state == "incomplete"),
                    "interrupted_with_result": int(state == "interrupted_with_result"),
                }

                self.assertEqual(state, index_activity["turn_completion"])
                self.assertEqual(expected_counts, index_activity["turn_completion_by_slice"])
                self.assertEqual(state, pack_activity["turn_completion"])
                self.assertEqual(expected_counts, pack_activity["turn_completion_by_slice"])
                self.assertEqual(state, pack_activity["slice_refs"][0]["turn_completion"])

    def test_activity_lifecycle_uses_interrupted_then_incomplete_priority_and_counts_slices(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            states = ("completed", "incomplete", "interrupted_with_result")
            slices = [
                make_slice(
                    conversation="thread-mixed-lifecycle",
                    native_id=f"turn-{index}",
                    text=f"混合生命周期测试第 {index} 轮请求",
                    at=f"2026-08-08T00:{index * 5:02d}:00Z",
                    ended=f"2026-08-08T00:{index * 5:02d}:30Z",
                    turn_completion=state,
                ).finalize()
                for index, state in enumerate(states)
            ]
            store.commit_scan("SCN-mixed-lifecycle", [AdapterResult("codex", slices=slices)], window)

            index = build_activity_index(store, window)
            pack = build_analysis_pack(store, window)
            expected_counts = {
                "completed": 1,
                "incomplete": 1,
                "interrupted_with_result": 1,
            }
            self.assertEqual("interrupted_with_result", index["activities"][0]["turn_completion"])
            self.assertEqual(expected_counts, index["activities"][0]["turn_completion_by_slice"])
            activity = pack["activities"][0]
            self.assertEqual("interrupted_with_result", activity["turn_completion"])
            self.assertEqual(expected_counts, activity["turn_completion_by_slice"])
            self.assertEqual(list(states), [ref["turn_completion"] for ref in activity["slice_refs"]])

            # An incomplete slice outranks completed, but not an interrupted
            # slice.  This is the activity-level summary, not a new lifecycle
            # inference from content or user_interrupts.
            incomplete_only = [slices[0], slices[1]]
            store = SessionsStore(Path(temporary) / "sessions-incomplete", create=True)
            store.commit_scan(
                "SCN-incomplete-priority",
                [AdapterResult("codex", slices=incomplete_only)],
                window,
            )
            index = build_activity_index(store, window)
            self.assertEqual("incomplete", index["activities"][0]["turn_completion"])
            self.assertEqual(
                {"completed": 1, "incomplete": 1, "interrupted_with_result": 0},
                index["activities"][0]["turn_completion_by_slice"],
            )

    def test_index_and_pack_use_schema_without_redundant_output_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice(
                text="用于验证生命周期输出版本的请求",
                turn_completion="interrupted_with_result",
            ).finalize()
            store.commit_scan("SCN-lifecycle-version", [AdapterResult("codex", slices=[item])], window)

            index = build_activity_index(store, window)
            pack = build_analysis_pack(store, window)
            self.assertEqual(1, index["schema_version"])
            self.assertEqual(1, pack["schema_version"])
            self.assertNotIn("index_version", index)
            self.assertNotIn("pack_version", pack)

    def test_groups_consecutive_slices_and_excludes_low_signal_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            first = make_slice(native_id="turn-1", text="检查输入").finalize()
            second = make_slice(
                native_id="turn-2",
                text="已完成目标",
                at="2026-08-08T00:20:00Z",
                ended="2026-08-08T00:21:00Z",
                blocks=[{
                    "kind": "agent_message",
                    "author_role": "agent",
                    "origin": "agent",
                    "at": "2026-08-08T00:20:00Z",
                    "text": "已完成目标",
                    "context": False,
                    "source_refs": ["codex:turn-2"],
                }],
                execution_evidence={
                    "changed_targets": ["src/example.py"],
                    "other_targets": [],
                    "tool_calls": [],
                    "verifications": ["pytest: passed"],
                    "failures": [],
                    "user_interrupts": [],
                    "omitted_count": 0,
                },
                delegations=[{
                    "agent_id": "agent-42",
                    "agent_path": "root/reviewer",
                    "thread_id": "thread-42",
                    "child_thread_id": "thread-42",
                    "turn_id": "turn-42",
                    "task": "核对结果",
                    "result": "核对完成",
                    "status": "complete",
                    "source_refs": ["codex://thread-42/turn/turn-42/event/final"],
                }, {
                    "agent_id": "agent-fallback",
                    "agent_path": None,
                    "thread_id": None,
                    "child_thread_id": None,
                    "turn_id": None,
                    "task": None,
                    "result": None,
                    "status": "partial",
                    "source_refs": [],
                }],
            ).finalize()
            low = make_slice(
                native_id="turn-low",
                at="2026-08-08T00:30:00Z",
                ended="2026-08-08T00:31:00Z",
                blocks=[],
                quality_flags=["no_readable_outcome"],
            ).finalize()
            store.commit_scan("SCN-pack", [AdapterResult("codex", slices=[first, second, low])], window)

            pack = build_analysis_pack(store, window, max_bytes=20_000)
            self.assertEqual(1, len(pack["activities"]))
            activity = pack["activities"][0]
            self.assertEqual(3, len(activity["slice_refs"]))
            self.assertEqual(3, activity["slice_ref_count"])
            self.assertEqual(["src/example.py"], activity["execution_evidence"]["changed_targets"])
            self.assertEqual([{
                "task": "核对结果",
                "result": "核对完成",
                "status": "complete",
                "agent": "root/reviewer",
                "ref": "codex://thread-42/turn/turn-42/event/final",
            }, {
                "task": "",
                "result": "",
                "status": "partial",
                "agent": "agent-fallback",
                "ref": "",
            }], activity["delegations"])
            self.assertEqual(
                {"kind", "author_role", "origin", "at", "text", "shape",
                 "text_chars", "source_refs"},
                set(activity["content"][-1]),
            )
            self.assertEqual(1, pack["excluded_summary"]["low_signal_slices"])
            self.assertLessEqual(pack["byte_size"], 20_000)
            self.assertEqual(pack["byte_size"], len(canonical_json(pack).encode("utf-8")))

    def test_pack_is_stable_and_budgeted_without_source_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            slices = [
                make_slice(
                    conversation=f"conversation-{index}",
                    native_id=f"turn-{index}",
                    # The spoken line matters: an activity with nothing but a
                    # paste is suppressed before the budget ever sees it, so a
                    # fixture without one would stop testing the budget.
                    text=f"第 {index} 条请求，先看这一段。\n" + "x" * 3_500,
                    at=f"2026-08-08T{index:02d}:00:00Z",
                    ended=f"2026-08-08T{index:02d}:01:00Z",
                ).finalize()
                for index in range(10)
            ]
            store.commit_scan("SCN-budget", [AdapterResult("codex", slices=slices)], window)
            first = build_analysis_pack(store, window, max_bytes=12_000)
            second = build_analysis_pack(store, window, max_bytes=12_000)
            self.assertEqual(first["pack_id"], second["pack_id"])
            self.assertEqual(canonical_json(first), canonical_json(second))
            self.assertLessEqual(first["byte_size"], 12_000)
            self.assertEqual(first["byte_size"], len(canonical_json(first).encode("utf-8")))
            self.assertGreater(first["excluded_summary"]["budget_activities"], 0)

            # A count cannot be acted on.  Everything the budget dropped is
            # named, with the conversation needed to go fetch it.
            omitted = first["omitted_activities"]
            self.assertEqual(first["excluded_summary"]["budget_activities"], len(omitted))
            kept = {item["activity_id"] for item in first["activities"]}
            for item in omitted:
                self.assertNotIn(item["activity_id"], kept)
                self.assertTrue(item["conversation_id"])
                self.assertGreaterEqual(item["slice_ref_count"], 1)
            self.assertLessEqual(first["byte_size"], 12_000)

    def test_large_activity_bounds_content_and_refs_without_losing_total_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            slices = []
            for index in range(40):
                at = f"2026-08-08T00:{index:02d}:00Z"
                text = ("优先保留的结果" if index == 20 else f"消息 {index}") + "\n" + "x" * 2_000
                slices.append(make_slice(
                    native_id=f"turn-{index}",
                    text=text,
                    at=at,
                    ended=f"2026-08-08T00:{index:02d}:30Z",
                    blocks=[{
                        "kind": "agent_message" if index == 20 else "message",
                        "author_role": "agent" if index == 20 else "self",
                        "origin": "agent" if index == 20 else "user",
                        "at": at,
                        "text": text,
                        "context": False,
                        "source_refs": [f"codex:turn-{index}"],
                    }],
                ).finalize())
            store.commit_scan("SCN-large", [AdapterResult("codex", slices=slices)], window)
            pack = build_analysis_pack(store, window, max_bytes=30_000)
            activity = pack["activities"][0]
            self.assertEqual(40, activity["slice_ref_count"])
            self.assertLessEqual(len(activity["slice_refs"]), 24)
            self.assertLessEqual(len(activity["content"]), 8)
            self.assertTrue(any("优先保留的结果" in block["text"] for block in activity["content"]))
            self.assertTrue(any(value.startswith("slice_refs_omitted:") for value in activity["omissions"]))
            self.assertGreater(pack["excluded_summary"]["omitted_slice_refs"], 0)

            with self.assertRaisesRegex(ValueError, "gap_ms"):
                build_analysis_pack(store, window, max_bytes=30_000, gap_ms=0)

    def test_other_targets_remain_when_the_activity_byte_budget_has_room(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            item = make_slice(
                text="预算充裕时保留弱信号目标",
                execution_evidence={
                    "changed_targets": [],
                    "other_targets": ["/synthetic/other-target.txt"],
                    "tool_calls": [],
                    "verifications": [],
                    "failures": [],
                    "user_interrupts": [],
                    "omitted_count": 0,
                },
            ).finalize()
            store.commit_scan("SCN-other-targets-room", [AdapterResult("codex", slices=[item])], window)

            pack = build_analysis_pack(store, window, max_bytes=20_000)

            self.assertEqual(
                ["/synthetic/other-target.txt"],
                pack["activities"][0]["execution_evidence"]["other_targets"],
            )

    def test_other_targets_leave_before_any_block_when_activity_byte_budget_is_exceeded(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
            at = "2026-08-08T00:00:00Z"
            blocks = [{
                "kind": "message",
                "author_role": "self",
                "origin": "user",
                "at": at,
                "text": "第 0 轮继续推进这项工作，保留这条可读请求。" * 4,
                "context": False,
                "source_refs": ["codex:target-budget-0"],
            }]
            other_targets = [
                f"/synthetic/other-targets/{index}-" + "x" * 370
                for index in range(8)
            ]
            item = make_slice(
                native_id="turn-other-target-budget",
                blocks=blocks,
                execution_evidence={
                    "changed_targets": [],
                    "other_targets": other_targets,
                    "tool_calls": [],
                    "verifications": [],
                    "failures": [],
                    "user_interrupts": [],
                    "omitted_count": 0,
                },
            ).finalize()
            store.commit_scan("SCN-other-targets-budget", [AdapterResult("codex", slices=[item])], window)

            pack = build_analysis_pack(
                store, window, blocks=1, block_chars=80, max_bytes=20_000,
            )
            activity = pack["activities"][0]

            self.assertEqual([], activity["execution_evidence"]["other_targets"])
            self.assertEqual(1, len(activity["content"]))
            self.assertTrue(any("第 0 轮继续推进" in block["text"] for block in activity["content"]))


class WhatSurvivesTest(unittest.TestCase):
    """A bounded view is a choice about what a day is remembered as."""

    PREAMBLE = "# Agent 工作原则\n本文件是跨项目的系统级工作规则。\n先读规则再动手。"
    LOG = "\n".join(f"  File \"/repo/app/module_{index}.py\", line {index}" for index in range(60))

    def build(self, blocks, *, conversation="thread-1", start=0):
        """One activity whose blocks are given as ``(kind, role, text)``."""

        slices = []
        for offset, (kind, role, text) in enumerate(blocks):
            minute = start + offset
            at = f"2026-08-08T{minute // 60:02d}:{minute % 60:02d}:00Z"
            slices.append(make_slice(
                conversation=conversation,
                native_id=f"{conversation}-turn-{offset}",
                at=at,
                ended=f"2026-08-08T{minute // 60:02d}:{minute % 60:02d}:30Z",
                blocks=[{
                    "kind": kind,
                    "author_role": role,
                    "origin": "user" if role == "self" else "agent",
                    "at": at,
                    "text": text,
                    "context": False,
                    "source_refs": [f"codex:{conversation}-{offset}"],
                }],
            ).finalize())
        return slices

    def pack(self, slices, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SessionsStore(Path(temporary.name) / "sessions", create=True)
        window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
        store.commit_scan("SCN-x", [AdapterResult("codex", slices=slices)], window)
        return store, window, build_analysis_pack(store, window, gap_ms=6 * 3600 * 1000, **kwargs)

    def test_a_pasted_log_does_not_crowd_out_the_sentence_that_asked(self):
        _store, _window, pack = self.pack(self.build([
            ("message", "self", "帮我把报价模板的评价体系重新校准\n\n" + self.LOG),
        ]))

        block = pack["activities"][0]["content"][0]
        self.assertEqual("machine", block["shape"])
        self.assertEqual("帮我把报价模板的评价体系重新校准", block["text"])
        # The reader is told the quote is shorter than what was said.
        self.assertGreater(block["text_chars"], len(block["text"]))
        self.assertIn(
            "activity_texts_reduced_to_prose:1", pack["activities"][0]["omissions"])

    def test_the_ask_and_the_stated_result_survive_a_full_activity(self):
        conversation = [("message", "self", "开始前先确认范围：只改评价模板")]
        conversation += [("message", "self", f"第 {index} 轮讨论，继续往下推进这个点")
                         for index in range(1, 18)]
        conversation += [("agent_message", "agent", "已完成：模板评价体系整体重新校准，共 26 项")]
        _store, _window, pack = self.pack(self.build(conversation))

        activity = pack["activities"][0]
        texts = [block["text"] for block in activity["content"]]
        self.assertEqual(8, len(texts))
        self.assertEqual("开始前先确认范围：只改评价模板", texts[0])
        self.assertEqual("已完成：模板评价体系整体重新校准，共 26 项", texts[-1])
        # Sorting by recency would have taken the last eight turns.  The
        # middle of the activity has to be represented too.
        middle = [text for text in texts[1:-1] if text.startswith("第")]
        self.assertGreaterEqual(len(middle), 5)
        self.assertLess(int(middle[0].split()[1]), 8)

    def test_a_preamble_repeated_across_activities_is_not_what_any_is_about(self):
        slices = []
        for index in range(6):
            slices.extend(self.build(
                [("message", "self", f"{self.PREAMBLE}\n第 {index} 件事：核对会议室排期")],
                conversation=f"thread-{index}",
                start=index * 180,
            ))
        store, window, _pack = self.pack(slices)

        index_view = build_activity_index(store, window, gap_ms=6 * 3600 * 1000)
        openings = [row["opening"] for row in index_view["activities"]]
        self.assertEqual(6, len(set(openings)), openings)
        for opening in openings:
            self.assertTrue(opening.startswith("第"), opening)

    def test_one_activity_can_be_read_in_full_by_id(self):
        conversation = [("message", "self", f"第 {index} 轮讨论，继续往下推进这个点")
                        for index in range(30)]
        store, window, pack = self.pack(self.build(conversation))
        activity_id = pack["activities"][0]["activity_id"]
        self.assertEqual(8, len(pack["activities"][0]["content"]))

        deep = build_analysis_pack(
            store, window, gap_ms=6 * 3600 * 1000,
            activities_wanted=(activity_id,), blocks=30, block_chars=2_000,
        )
        self.assertEqual(1, len(deep["activities"]))
        self.assertEqual(activity_id, deep["activities"][0]["activity_id"])
        self.assertEqual(30, len(deep["activities"][0]["content"]))
        self.assertEqual([], deep["missing_activities"])

    def test_an_id_from_a_different_window_is_reported_not_silently_empty(self):
        store, window, _pack = self.pack(self.build([("message", "self", "一句话")]))

        deep = build_analysis_pack(store, window, activities_wanted=("ACT-nope",))

        self.assertEqual([], deep["activities"])
        self.assertEqual(["ACT-nope"], deep["missing_activities"])


class LocalDayTest(unittest.TestCase):
    """Both views are grouped by day, so they must speak the reader's day."""

    def build(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SessionsStore(Path(temporary.name) / "sessions", create=True)
        # 2026-08-08T16:30Z is 2026-08-09 00:30 in Asia/Shanghai: the same
        # instant belongs to different days depending on which one you use.
        early = make_slice(
            native_id="turn-early",
            text="凌晨继续推进",
            at="2026-08-08T16:30:00Z",
            ended="2026-08-08T16:40:00Z",
        ).finalize()
        later = make_slice(
            conversation="thread-2",
            native_id="turn-later",
            text="下午继续推进",
            at="2026-08-09T06:00:00Z",
            ended="2026-08-09T06:10:00Z",
        ).finalize()
        window = TimeWindow.from_values(
            "2026-08-09T00:00:00+08:00", "2026-08-10T00:00:00+08:00"
        )
        store.commit_scan("SCN-local", [AdapterResult("codex", slices=[early, later])], window)
        return store, window

    def test_index_rows_and_day_summary_use_the_local_day(self):
        store, window = self.build()

        result = build_activity_index(store, window)

        self.assertEqual(
            ["2026-08-09"], [item["day"] for item in result["day_summary"]]
        )
        for row in result["activities"]:
            self.assertTrue(
                row["started_at"].endswith("+08:00"),
                f"index row must be local, got {row['started_at']}",
            )
        self.assertEqual("2026-08-09T00:30:00+08:00", result["activities"][0]["started_at"])

    def test_pack_activities_and_blocks_use_the_local_day(self):
        store, window = self.build()

        pack = build_analysis_pack(store, window)

        activity = pack["activities"][0]
        self.assertEqual("2026-08-09T00:30:00+08:00", activity["started_at"])
        self.assertTrue(activity["ended_at"].endswith("+08:00"))
        for block in activity["content"]:
            self.assertTrue(block["at"].endswith("+08:00"))


class SuppressedActivityTest(unittest.TestCase):
    """An approval reviewer's own conversation is not a day's work."""

    VERDICT = (
        '{"risk_level":"medium","user_authorization":"high","outcome":"allow",'
        '"rationale":"用户明确授权在 test 环境只读验证，不提交报价。"}'
    )

    def verdict_slice(self, conversation, native_id, **extra):
        return make_slice(
            conversation=conversation,
            native_id=native_id,
            blocks=[
                {
                    "kind": "message",
                    "author_role": "self",
                    "origin": "system_injected",
                    "at": "2026-08-08T00:00:00Z",
                    "text": "The following is the Codex agent history whose request action you are assessing.",
                    "context": False,
                    "source_refs": [f"codex:{native_id}-injected"],
                },
                {
                    "kind": "agent_message",
                    "author_role": "agent",
                    "origin": "agent",
                    "at": "2026-08-08T00:00:01Z",
                    "text": self.VERDICT,
                    "context": False,
                    "source_refs": [f"codex:{native_id}-verdict"],
                },
            ],
            **extra,
        ).finalize()

    def store(self, slices):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SessionsStore(Path(temporary.name) / "sessions", create=True)
        window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
        store.commit_scan("SCN-verdict", [AdapterResult("codex", slices=slices)], window)
        return store, window

    def test_acceptance_verdict_only_activities_become_a_count_not_rows(self):
        store, window = self.store([
            self.verdict_slice("thread-verdict", "turn-verdict-1"),
            self.verdict_slice("thread-verdict", "turn-verdict-2"),
            make_slice(conversation="thread-work", native_id="turn-work",
                       text="把报价模板的评价体系重新校准一遍"),
        ])

        result = build_activity_index(store, window)

        self.assertEqual(1, result["activity_total"])
        self.assertEqual(1, len(result["activities"]))
        self.assertIn("重新校准", result["activities"][0]["opening"])
        self.assertEqual(1, result["suppressed_activities"]["mechanism_only"])
        self.assertEqual(2, result["suppressed_activities"]["slices"])
        # The window's totals count what a report can act on, so a day of
        # machine chatter does not read as a day of work.
        self.assertEqual(1, sum(item["activities"] for item in result["project_summary"]))

    def test_acceptance_a_kept_activity_never_delivers_the_verdict_as_content(self):
        # Machine-shaped blocks are normally shown anyway -- better than an
        # empty activity.  A verdict is the exception, so an activity kept for
        # its evidence must not smuggle it back in through ``content``.
        store, window = self.store([
            self.verdict_slice(
                "thread-verdict-evidence",
                "turn-verdict-evidence",
                execution_evidence={
                    "changed_targets": ["lifeos_sessions/pack.py"], "other_targets": [],
                    "tool_calls": [], "verifications": [], "failures": [],
                    "user_interrupts": [], "omitted_count": 0,
                },
            ),
        ])

        activity = build_analysis_pack(store, window)["activities"][0]

        self.assertEqual([], activity["content"])
        self.assertEqual(
            ["lifeos_sessions/pack.py"],
            activity["execution_evidence"]["changed_targets"],
        )

    def test_acceptance_a_verdict_activity_that_ran_something_is_kept(self):
        # 36 of 4,239 verdict-only slices on real data did carry evidence.  The
        # gate is "nothing to quote and nothing ran", never the schema alone.
        store, window = self.store([
            self.verdict_slice(
                "thread-verdict-evidence",
                "turn-verdict-evidence",
                execution_evidence={
                    "changed_targets": ["lifeos_sessions/pack.py"], "other_targets": [],
                    "tool_calls": [], "verifications": [], "failures": [],
                    "user_interrupts": [], "omitted_count": 0,
                },
            ),
        ])

        result = build_activity_index(store, window)

        self.assertEqual(1, result["activity_total"])
        self.assertEqual(0, result["suppressed_activities"]["mechanism_only"])
        self.assertEqual(1, result["activities"][0]["changed_targets"])

    def test_acceptance_a_delegation_survives_but_its_verdict_result_does_not(self):
        store, window = self.store([
            make_slice(
                conversation="thread-delegating",
                native_id="turn-delegating",
                text="并行把四个项目的一致性核对完",
                delegations=[{
                    "task": "核对行政主数据的一致性",
                    "result": self.VERDICT,
                    "status": "completed",
                    "agent_id": "luna-worker",
                    "source_refs": ["codex:turn-delegating-deleg"],
                }],
            ).finalize(),
        ])

        activity = build_analysis_pack(store, window)["activities"][0]

        self.assertEqual(1, len(activity["delegations"]))
        self.assertIn("行政主数据", activity["delegations"][0]["task"])
        self.assertEqual("", activity["delegations"][0]["result"])

    def test_acceptance_an_approval_review_is_not_counted_as_a_delegation(self):
        # The reviewer runs as a sub-agent, so it is recorded with the same
        # vocabulary as real work: injected prompt in, verdict out.  Counting
        # it would raise signal_score and spend the delegation budget.
        def slice_with(delegation):
            return make_slice(
                conversation="thread-mixed",
                native_id=f"turn-{id(delegation)}",
                text="并行把四个项目的一致性核对完",
                delegations=[delegation],
            ).finalize()

        store, window = self.store([
            slice_with({
                "task": "The following is the Codex agent history whose request action you are assessing.",
                "result": self.VERDICT,
                "status": "completed",
                "agent_id": "approval-reviewer",
                "source_refs": ["codex:review"],
            }),
        ])

        activity = build_analysis_pack(store, window)["activities"][0]

        self.assertEqual([], activity["delegations"])

    def test_acceptance_the_pack_reports_the_suppression_rather_than_a_short_read(self):
        # ``--activity <id>`` still returns one of these when asked by name:
        # a report's frontmatter keeps activity ids, so yesterday's id must not
        # start resolving to silence.  That path is deliberately not reachable
        # from the index, which is why only the count is asserted here.
        store, window = self.store([self.verdict_slice("thread-verdict", "turn-verdict-1")])

        pack = build_analysis_pack(store, window)

        self.assertEqual([], pack["activities"])
        self.assertEqual(1, pack["excluded_summary"]["mechanism_only"])
        self.assertEqual(0, pack["excluded_summary"]["budget_activities"])


class OpeningFallbackTest(unittest.TestCase):
    def store(self, slices):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SessionsStore(Path(temporary.name) / "sessions", create=True)
        window = TimeWindow.from_values("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")
        store.commit_scan("SCN-opening", [AdapterResult("codex", slices=slices)], window)
        return store, window

    @staticmethod
    def agent_slice(native_id, texts):
        return make_slice(
            native_id=native_id,
            blocks=[
                {
                    "kind": "message",
                    "author_role": "self",
                    "origin": "system_injected",
                    "at": "2026-08-08T00:00:00Z",
                    "text": "# synthetic injected rules",
                    "context": False,
                    "source_refs": ["codex:injected"],
                },
                *[
                    {
                        "kind": "agent_message",
                        "author_role": "agent",
                        "origin": "agent",
                        "at": f"2026-08-08T00:00:{index + 1:02d}Z",
                        "text": text,
                        "context": False,
                        "source_refs": [f"codex:agent-{index}"],
                    }
                    for index, text in enumerate(texts)
                ],
            ],
        ).finalize()

    def test_acceptance_one_agent_statement_is_carried_by_the_outcome_alone(self):
        # A delegated activity has no turn of the person's own, so the opening
        # falls back to the agent.  With a single statement that fallback and
        # the outcome are the same sentence, and a row that prints it twice is
        # half as wide as it looks.  The row must still say something.
        store, window = self.store([
            self.agent_slice("turn-agent-single", ["已完成合成验证，结果可回读。"]),
        ])

        row = build_activity_index(store, window)["activities"][0]

        self.assertIn("已完成合成验证", row["outcome"])
        self.assertFalse(row.get("opening"))

    def test_acceptance_distinct_agent_statements_keep_a_marked_opening(self):
        store, window = self.store([
            self.agent_slice(
                "turn-agent-pair",
                ["先读取合成数据并核对窗口。", "已完成合成验证，结果可回读。"],
            ),
        ])

        row = build_activity_index(store, window)["activities"][0]

        self.assertIn("agent", row["opening"].lower())
        self.assertIn("先读取合成数据", row["opening"])
        self.assertIn("已完成合成验证", row["outcome"])

    def test_acceptance_boilerplate_suppression_keeps_a_short_user_opening(self):
        slices = [
            make_slice(
                conversation=f"thread-short-{index}",
                native_id=f"turn-short-{index}",
                text="可以",
                at=f"2026-08-08T00:{index:02d}:00Z",
                ended=f"2026-08-08T00:{index:02d}:30Z",
            ).finalize()
            for index in range(6)
        ]
        store, window = self.store(slices)

        result = build_activity_index(store, window, gap_ms=6 * 3600 * 1000)

        self.assertEqual(["可以"] * 6, [item["opening"] for item in result["activities"]])


if __name__ == "__main__":
    unittest.main()
