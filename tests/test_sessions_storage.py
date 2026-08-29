import tempfile
import unittest
from pathlib import Path

from lifeos_sessions.core import AdapterResult, TimeWindow
from lifeos_sessions.pack import MIN_MAX_BYTES, build_activity_index
from lifeos_sessions.projects import ProjectMap
from lifeos_sessions.store import SessionNotFound, SessionsStore
from tests.test_sessions_core import make_slice


DAY_MS = 86_400_000


def committed_store(root, slices, *, window=None, scan="SCN-storage"):
    store = SessionsStore(Path(root) / "sessions", create=True)
    window = window or TimeWindow.from_values("2026-05-01T00:00:00Z", "2026-09-01T00:00:00Z")
    store.commit_scan(scan, [AdapterResult("codex", slices=slices)], window)
    return store


def dated_slice(day, index=0, **extra):
    at = f"2026-{day}T0{index}:00:00Z"
    return make_slice(
        conversation=f"conversation-{day}",
        native_id=f"turn-{day}-{index}",
        text=f"{day} 的会话内容 {index}",
        at=at,
        ended=f"2026-{day}T0{index}:30:00Z",
        **extra,
    ).finalize()


class UsageTest(unittest.TestCase):
    def test_usage_measures_components_and_reports_an_observed_rate(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = committed_store(temporary, [dated_slice("06-01"), dated_slice("07-01")])
            report = store.usage()
            self.assertGreater(report["components"]["slices_json"], 0)
            self.assertGreater(report["components"]["index_sqlite"], 0)
            self.assertEqual(report["total_bytes"], sum(report["components"].values()))
            self.assertEqual(2, report["slices"]["current"])
            self.assertEqual([{"source": "codex", "slices": 2}], report["by_source"])
            self.assertIsNotNone(report["projection"])
            self.assertGreater(report["projection"]["bytes_per_year_unbounded"], 0)

    def test_usage_on_an_uninitialized_store_reports_zero_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            report = SessionsStore(root).usage()
            self.assertEqual(0, report["total_bytes"])
            self.assertFalse(root.exists())


class CompactTest(unittest.TestCase):
    def test_compact_reclaims_space_and_keeps_every_fact_queryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            slices = [dated_slice("06-01", index) for index in range(6)]
            store = committed_store(temporary, slices)
            before = store.list_slices({})
            hits = store.list_slices({"query": "会话内容"})
            self.assertTrue(hits)

            result = store.compact()
            self.assertGreaterEqual(result["index_bytes_before"], result["index_bytes_after"])

            self.assertEqual(
                [item["slice_id"] for item in before],
                [item["slice_id"] for item in store.list_slices({})],
            )
            self.assertEqual(
                [item["slice_id"] for item in hits],
                [item["slice_id"] for item in store.list_slices({"query": "会话内容"})],
            )
            self.assertEqual([], store.validate())


class PruneTest(unittest.TestCase):
    def _store(self, temporary):
        return committed_store(temporary, [
            dated_slice("06-01", 0),
            dated_slice("06-01", 1),
            dated_slice("08-01", 0),
        ])

    def test_dry_run_reports_the_plan_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            cutoff = TimeWindow.from_values("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z").from_ms
            plan = store.prune(slices_before_ms=cutoff, dry_run=True)
            self.assertEqual(2, plan["slices"])
            self.assertGreater(plan["bytes_freed"], 0)
            self.assertEqual([{"source": "codex", "day": "2026-06-01", "slice_count": 2}], plan["pruned_days"])
            self.assertEqual(3, len(store.list_slices({})))
            self.assertEqual([], store.validate())

    def test_apply_removes_content_records_a_tombstone_and_stays_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            removed = [item for item in store.list_slices({}) if item["started_at"].startswith("2026-06")]
            cutoff = TimeWindow.from_values("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z").from_ms

            store.prune(slices_before_ms=cutoff, dry_run=False)

            remaining = store.list_slices({})
            self.assertEqual(1, len(remaining))
            self.assertTrue(remaining[0]["started_at"].startswith("2026-08"))
            for item in removed:
                with self.assertRaises(SessionNotFound):
                    store.show(item["slice_id"])
                self.assertFalse((store.root / item["json_path"]).exists())
            # The window is not silently empty; it is recorded as pruned.
            tombstones = store.pruned_overlap()
            self.assertEqual(1, len(tombstones))
            self.assertEqual(("codex", "2026-06-01", 2, "content"), (
                tombstones[0]["source"], tombstones[0]["day"],
                tombstones[0]["slice_count"], tombstones[0]["scope"],
            ))
            self.assertEqual([], store.validate())

    def test_pruning_only_the_search_index_keeps_the_evidence_and_stays_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            cutoff = TimeWindow.from_values("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z").from_ms

            plan = store.prune(fts_before_ms=cutoff, dry_run=False)
            self.assertEqual(2, plan["fts_rows"])
            self.assertEqual(0, plan["slices"])

            # Every slice is still listable and readable.
            self.assertEqual(3, len(store.list_slices({})))
            for item in store.list_slices({}):
                self.assertTrue(store.show(item["slice_id"]))
            # Only full-text reach shrank.
            self.assertEqual(1, len(store.list_slices({"query": "会话内容"})))
            # A deliberately un-indexed slice is not an integrity error.
            self.assertEqual([], store.validate())
            self.assertEqual(
                [("codex", "2026-06-01", "fts")],
                [(row["source"], row["day"], row["scope"]) for row in store.pruned_overlap()],
            )

    def test_pruned_overlap_is_limited_to_the_requested_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            cutoff = TimeWindow.from_values("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z").from_ms
            store.prune(slices_before_ms=cutoff, dry_run=False)
            july = TimeWindow.from_values("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z")
            self.assertEqual([], store.pruned_overlap(july.from_ms, july.to_ms))
            june = TimeWindow.from_values("2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z")
            self.assertEqual(1, len(store.pruned_overlap(june.from_ms, june.to_ms)))


class ActivityIndexTest(unittest.TestCase):
    PROJECTS = ("alpha", "beta", "gamma")
    PER_PROJECT = 12
    TOTAL = len(PROJECTS) * PER_PROJECT

    def _many_projects(self, temporary):
        slices = []
        for project in self.PROJECTS:
            for index in range(self.PER_PROJECT):
                day = 1 + index % 4
                hour = 2 + (index // 4) * 6
                slices.append(make_slice(
                    conversation=f"{project}-{index}",
                    native_id=f"turn-{project}-{index}",
                    text=f"{project} 的工作内容 {index} " + "填充" * 200,
                    at=f"2026-08-{day:02d}T{hour:02d}:00:00Z",
                    ended=f"2026-08-{day:02d}T{hour:02d}:30:00Z",
                    workspace=f"/work/{project}",
                ).finalize())
        window = TimeWindow.from_values("2026-08-01T00:00:00Z", "2026-08-09T00:00:00Z")
        return committed_store(temporary, slices, window=window), window

    def test_index_covers_every_activity_project_and_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            index = build_activity_index(store, window)
            self.assertEqual(self.TOTAL, index["activity_total"])
            self.assertEqual(self.TOTAL, len(index["activities"]))
            self.assertEqual([], index["dropped_by_project"])
            self.assertEqual(
                {"/work/alpha", "/work/beta", "/work/gamma"},
                {item["project_key"] for item in index["project_summary"]},
            )
            self.assertEqual(4, len(index["day_summary"]))
            self.assertTrue(index["excerpts_included"])

    def test_index_is_deterministic_and_reports_its_own_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            first = build_activity_index(store, window)
            second = build_activity_index(store, window)
            self.assertEqual(first["index_id"], second["index_id"])
            self.assertEqual(first["byte_size"], second["byte_size"])

    def test_a_tight_budget_drops_excerpts_before_it_drops_any_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            full = build_activity_index(store, window)
            index = build_activity_index(store, window, max_bytes=full["byte_size"] - 200)
            self.assertFalse(index["excerpts_included"])
            self.assertEqual(self.TOTAL, len(index["activities"]))
            self.assertEqual([], index["dropped_by_project"])

    def test_a_budget_too_small_for_every_row_never_empties_a_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            index = build_activity_index(store, window, max_bytes=8_000)
            self.assertLess(len(index["activities"]), self.TOTAL)
            self.assertEqual(self.TOTAL, index["activity_total"])
            self.assertTrue(index["dropped_by_project"])
            # Each project keeps at least one row, and what went missing is named.
            self.assertEqual(
                {"/work/alpha", "/work/beta", "/work/gamma"},
                {item["project_key"] for item in index["activities"]},
            )
            self.assertEqual(
                self.TOTAL - len(index["activities"]),
                sum(item["activities"] for item in index["dropped_by_project"]),
            )

    def test_summaries_stay_whole_when_rows_are_dropped_for_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            full = build_activity_index(store, window)
            tight = build_activity_index(store, window, max_bytes=8_000)
            self.assertLess(len(tight["activities"]), len(full["activities"]))
            # Rows are what a budget takes; the totals above them are not.
            self.assertEqual(full["project_summary"], tight["project_summary"])
            self.assertEqual(full["day_summary"], tight["day_summary"])
            self.assertEqual(
                self.TOTAL,
                sum(item["activities"] for item in tight["project_summary"]),
            )

    def test_summaries_merge_the_same_project_across_agents(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionsStore(Path(temporary) / "sessions", create=True)
            window = TimeWindow.from_values("2026-08-01T00:00:00Z", "2026-08-09T00:00:00Z")
            results = [
                AdapterResult(source, slices=[make_slice(
                    source=source, conversation=f"{source}-1", native_id=f"turn-{source}",
                    text=f"在 {source} 里推进同一个项目",
                    at="2026-08-01T01:00:00Z", ended="2026-08-01T01:10:00Z",
                    workspace="/work/shared",
                ).finalize()])
                for source in ("codex", "claude", "smartwork")
            ]
            store.commit_scan("SCN-multi", results, window)

            index = build_activity_index(store, window)
            self.assertEqual(3, len(index["activities"]))
            self.assertEqual(
                {"codex", "claude", "smartwork"},
                {row["source"] for row in index["activities"]},
            )
            # One project row and one day row, regardless of which agent did it.
            self.assertEqual(1, len(index["project_summary"]))
            self.assertEqual(3, index["project_summary"][0]["activities"])
            self.assertEqual([1], [item["projects"] for item in index["day_summary"]])
            self.assertEqual(3, index["day_summary"][0]["activities"])

    def test_a_budget_that_cannot_hold_one_row_per_project_says_so(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            index = build_activity_index(store, window, max_bytes=MIN_MAX_BYTES)
            # The per-project floor wins over the byte budget, but the caller
            # is told the budget was not met.
            self.assertEqual(
                {"/work/alpha", "/work/beta", "/work/gamma"},
                {item["project_key"] for item in index["activities"]},
            )
            if index["byte_size"] > MIN_MAX_BYTES:
                self.assertTrue(index["budget_exceeded"])
            else:
                self.assertFalse(index["budget_exceeded"])

    def test_index_declares_retention_pruning_inside_the_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, window = self._many_projects(temporary)
            cutoff = TimeWindow.from_values("2026-08-03T00:00:00Z", "2026-08-04T00:00:00Z").from_ms
            store.prune(slices_before_ms=cutoff, dry_run=False)
            index = build_activity_index(store, window)
            self.assertGreater(index["retention_pruned"]["slices"], 0)
            self.assertGreater(index["retention_pruned"]["days"], 0)

    def test_a_confirmed_project_map_merges_paths_the_raw_workspace_would_split(self):
        with tempfile.TemporaryDirectory() as temporary:
            slices = [
                make_slice(conversation="a", native_id="turn-a", text="旧路径下的工作",
                           at="2026-08-01T01:00:00Z", ended="2026-08-01T01:10:00Z",
                           workspace="/work/国内行政/meeting-room").finalize(),
                make_slice(conversation="b", native_id="turn-b", text="新路径下的工作",
                           at="2026-08-02T01:00:00Z", ended="2026-08-02T01:10:00Z",
                           workspace="/work/domestic-admin/meeting-room").finalize(),
            ]
            window = TimeWindow.from_values("2026-08-01T00:00:00Z", "2026-08-09T00:00:00Z")
            store = committed_store(temporary, slices, window=window)

            split = build_activity_index(store, window)
            self.assertEqual(2, len(split["project_summary"]))

            merged = build_activity_index(store, window, project_map=ProjectMap(projects=[{
                "key": "meeting-room", "title": "会议室",
                "roots": ["/work/国内行政/meeting-room", "/work/domestic-admin/meeting-room"],
            }]))
            self.assertEqual(1, len(merged["project_summary"]))
            self.assertEqual("meeting-room", merged["project_summary"][0]["project_key"])
            self.assertEqual(2, merged["project_summary"][0]["activities"])


if __name__ == "__main__":
    unittest.main()
