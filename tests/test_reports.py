import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

from lifeos_reports import store
from lifeos_sessions.activity_ids import activity_id, migrate_legacy_activity_id


REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"
ACTIVITY_1 = "ACT-AAAAAAAAAAAAAAAAAAAAAAAA"
ACTIVITY_2 = "ACT-AAAAAAAAAAAAAAAAAAAAAAAB"
LEGACY_ACTIVITY_1 = "ACT-" + "00" * 32


class ReportsCLITestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        self.reports_root = self.data_dir / "reports"
        self.daily_dir = self.reports_root / "daily"
        self.environment = os.environ.copy()
        self.environment["LIFEOS_HOME"] = str(self.data_dir)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments, check=True, input_text=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "reports", *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
            input=input_text,
        )

    def report(self, day="2026-08-09"):
        return self.daily_dir / f"{day}.md"

    def write_body(self, day="2026-08-09", body="## 概览\n\n今天只推进了一个项目。\n"):
        path = self.report(day)
        meta, _existing = store.read_report(path)
        store.write_report(path, meta, body)
        return path

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)


class ReportsHelpTest(ReportsCLITestCase):
    def test_reports_help_explains_natural_day_and_state_boundaries(self):
        output = self.run_cli("--help").stdout

        self.assertIn("自然日日报", output)
        self.assertIn("Asia/Shanghai", output)
        self.assertIn("begin/write/confirm", output)
        self.assertIn("会写入日报", output)
        self.assertIn("path/list/validate", output)
        self.assertIn("只读", output)

    def test_stateful_reports_help_exposes_confirmation_and_read_only_gates(self):
        begin_help = self.run_cli("begin", "--help").stdout
        self.assertIn("--redo", begin_help)
        self.assertIn("confirmed", begin_help)

        confirm_help = self.run_cli("confirm", "--help").stdout
        self.assertIn("本人", confirm_help)
        self.assertIn("状态写入", confirm_help)

        validate_help = self.run_cli("validate", "--help").stdout
        self.assertIn("不创建", validate_help)
        self.assertIn("不修改", validate_help)
        self.assertIn("不确认", validate_help)

        periodic_help = self.run_cli("periodic", "--help").stdout
        for phrase in ("周、月、季度、半年或年度", "不重新采集", "YYYY-Www"):
            self.assertIn(phrase, periodic_help)
        self.assertNotIn("import", periodic_help)
        self.assertNotIn("read", periodic_help)

        periodic_confirm_help = self.run_cli(
            "periodic", "confirm", "--help"
        ).stdout
        self.assertIn("不重新读取日报", periodic_confirm_help)


class BeginTest(ReportsCLITestCase):
    def test_begin_creates_private_skeleton_for_the_natural_day(self):
        result = self.run_cli("begin", "--day", "2026-08-09", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual("new", payload["state"])
        self.assertEqual("2026-08-09T00:00:00+08:00", payload["window"]["from"])
        self.assertEqual("2026-08-10T00:00:00+08:00", payload["window"]["to"])
        self.assertIsNone(payload["superseded"])

        path = self.report()
        self.assertTrue(path.is_file())
        self.assertEqual(0o700, self.mode(self.reports_root))
        self.assertEqual(0o700, self.mode(self.daily_dir))
        self.assertEqual(0o600, self.mode(path))

        meta, body = store.read_report(path)
        self.assertEqual("draft", meta["status"])
        self.assertEqual("2026-08-09", meta["day"])
        self.assertIsNone(meta["confirmed_at"])
        self.assertEqual(
            "2026-08-09T00:00:00+08:00/2026-08-10T00:00:00+08:00", meta["window"]
        )
        self.assertEqual([], meta["activity_ids"])
        self.assertTrue(body.strip())

    def test_first_line_of_text_output_is_the_path(self):
        result = self.run_cli("begin", "--day", "2026-08-09")
        self.assertEqual(str(self.report()), result.stdout.splitlines()[0])

    def test_begin_overwrites_a_draft_without_leaving_a_snapshot(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body(body="## 概览\n\n草稿正文。\n")
        result = self.run_cli("begin", "--day", "2026-08-09", "--json")

        self.assertEqual("overwrite", json.loads(result.stdout)["state"])
        self.assertEqual([], store.superseded_paths(self.reports_root, date(2026, 8, 9)))
        _meta, body = store.read_report(self.report())
        self.assertNotIn("草稿正文", body)

    def test_begin_refuses_to_replace_a_confirmed_day(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body()
        self.run_cli("confirm", "--day", "2026-08-09")
        before = self.report().read_bytes()

        result = self.run_cli("begin", "--day", "2026-08-09", check=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("--redo", result.stderr)
        self.assertEqual(before, self.report().read_bytes())

    def test_redo_keeps_the_confirmed_report_byte_for_byte(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body()
        self.run_cli("confirm", "--day", "2026-08-09")
        before = self.report().read_bytes()

        payload = json.loads(
            self.run_cli("begin", "--day", "2026-08-09", "--redo", "--json").stdout
        )

        self.assertEqual("redo", payload["state"])
        snapshot = Path(payload["superseded"])
        self.assertTrue(snapshot.is_file())
        self.assertEqual(before, snapshot.read_bytes())
        self.assertEqual(0o600, self.mode(snapshot))
        meta, _body = store.read_report(self.report())
        self.assertEqual("draft", meta["status"])

    def test_invalid_day_is_an_argument_error(self):
        result = self.run_cli("begin", "--day", "2026-8-9", check=False)
        self.assertEqual(2, result.returncode)


class ConfirmTest(ReportsCLITestCase):
    def test_confirm_records_the_moment_and_is_idempotent(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body()

        first = json.loads(self.run_cli("confirm", "--day", "2026-08-09", "--json").stdout)
        self.assertTrue(first["changed"])
        self.assertEqual("confirmed", first["status"])
        self.assertTrue(first["confirmed_at"])
        after_first = self.report().read_bytes()

        second = json.loads(self.run_cli("confirm", "--day", "2026-08-09", "--json").stdout)
        self.assertFalse(second["changed"])
        self.assertEqual(first["confirmed_at"], second["confirmed_at"])
        self.assertEqual(after_first, self.report().read_bytes())

    def test_confirm_refuses_an_invalid_report_and_leaves_it_draft(self):
        self.run_cli("begin", "--day", "2026-08-09")
        path = self.report()
        meta, _body = store.read_report(path)
        store.write_report(path, meta, "\n")

        result = self.run_cli("confirm", "--day", "2026-08-09", check=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("正文为空", result.stderr)
        meta, _body = store.read_report(path)
        self.assertEqual("draft", meta["status"])

    def test_confirm_reports_a_missing_day(self):
        result = self.run_cli("confirm", "--day", "2026-08-09", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("还没有日报", result.stderr)


class WriteTest(ReportsCLITestCase):
    def body_file(self, body="## 概览\n\n今天推进了两个项目。\n"):
        path = self.data_dir / "body.md"
        path.write_text(body, encoding="utf-8")
        return path

    def test_write_atomically_updates_draft_and_derives_id_counts(self):
        self.run_cli("begin", "--day", "2026-08-09")
        before, _body = store.read_report(self.report())
        payload = json.loads(
            self.run_cli(
                "write",
                "--day",
                "2026-08-09",
                "--body-file",
                str(self.body_file()),
                "--sessions-partial",
                "3",
                "--sessions-interrupted",
                "2",
                "--sessions-omitted",
                "1",
                "--user-notes",
                "1",
                "--activity-id",
                ACTIVITY_1,
                "--activity-id",
                ACTIVITY_2,
                "--work-event-id",
                "EVT-1",
                "--json",
            ).stdout
        )

        meta, body = store.read_report(self.report())
        self.assertEqual("draft", payload["status"])
        self.assertEqual(2, payload["sessions_activities"])
        self.assertEqual(1, payload["work_events"])
        self.assertEqual(before["generated_at"], meta["generated_at"])
        self.assertEqual(before["window"], meta["window"])
        self.assertEqual("draft", meta["status"])
        self.assertEqual("2", meta["sessions_activities"])
        self.assertEqual("1", meta["work_events"])
        self.assertEqual([ACTIVITY_1, ACTIVITY_2], meta["activity_ids"])
        self.assertEqual(["EVT-1"], meta["work_event_ids"])
        self.assertIn("今天推进了两个项目", body)
        self.assertEqual(0o600, self.mode(self.report()))
        self.assertEqual(0o600, self.mode(self.data_dir / ".lifeos-reports.lock"))
        self.assertEqual([], store.check_report(self.report()))

    def test_write_accepts_optional_git_scan_and_commit_evidence(self):
        self.run_cli("begin", "--day", "2026-08-09")
        sha = "a" * 40
        scan_id = "GITSCAN-20260809T120000+0800-deadbeef"
        payload = json.loads(
            self.run_cli(
                "write",
                "--day",
                "2026-08-09",
                "--body-file",
                str(self.body_file()),
                "--git-scan-id",
                scan_id,
                "--git-commit-id",
                f"lifeos-cli@{sha}",
                "--json",
            ).stdout
        )

        meta, _body = store.read_report(self.report())
        self.assertEqual(scan_id, meta["git_scan_id"])
        self.assertEqual("1", meta["git_commits"])
        self.assertEqual([f"lifeos-cli@{sha}"], meta["git_commit_ids"])
        self.assertEqual(scan_id, payload["git_scan_id"])
        self.assertEqual(1, payload["git_commits"])
        self.assertEqual([], store.check_report(self.report()))

    def test_write_rejects_invalid_git_evidence_without_changing_report(self):
        self.run_cli("begin", "--day", "2026-08-09")
        before = self.report().read_bytes()
        result = self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            str(self.body_file()),
            "--git-scan-id",
            "not-a-scan",
            check=False,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("git-scan-id", result.stderr)
        self.assertEqual(before, self.report().read_bytes())

    def test_write_requires_begin_and_never_overwrites_confirmed(self):
        body_file = self.body_file()
        missing = self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            str(body_file),
            check=False,
        )
        self.assertEqual(1, missing.returncode)
        self.assertIn("先运行 reports begin", missing.stderr)

        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body()
        self.run_cli("confirm", "--day", "2026-08-09")
        before = self.report().read_bytes()
        confirmed = self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            str(body_file),
            check=False,
        )
        self.assertEqual(1, confirmed.returncode)
        self.assertIn("已确认", confirmed.stderr)
        self.assertEqual(before, self.report().read_bytes())

    def test_write_accepts_body_from_stdin(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            "-",
            input_text="## 概览\n\n来自标准输入。\n",
        )
        _meta, body = store.read_report(self.report())
        self.assertIn("来自标准输入", body)

    def test_write_rejects_frontmatter_duplicate_ids_and_negative_counts(self):
        self.run_cli("begin", "--day", "2026-08-09")
        before = self.report().read_bytes()
        frontmatter = self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            str(self.body_file("---\nstatus: confirmed\n---\n正文\n")),
            check=False,
        )
        self.assertEqual(1, frontmatter.returncode)
        self.assertIn("不能包含 frontmatter", frontmatter.stderr)

        duplicate = self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            str(self.body_file()),
            "--activity-id",
            ACTIVITY_1,
            "--activity-id",
            ACTIVITY_1,
            check=False,
        )
        self.assertEqual(1, duplicate.returncode)
        self.assertIn("不能重复", duplicate.stderr)

        negative = self.run_cli(
            "write",
            "--day",
            "2026-08-09",
            "--body-file",
            str(self.body_file()),
            "--sessions-partial",
            "-1",
            check=False,
        )
        self.assertEqual(2, negative.returncode)
        self.assertEqual(before, self.report().read_bytes())


class PathAndListTest(ReportsCLITestCase):
    def test_path_is_read_only_for_a_day_that_has_no_report(self):
        payload = json.loads(self.run_cli("path", "--day", "2026-08-09", "--json").stdout)
        self.assertFalse(payload["exists"])
        self.assertIsNone(payload["status"])
        self.assertEqual(
            {
                "from": "2026-08-09T00:00:00+08:00",
                "to": "2026-08-10T00:00:00+08:00",
            },
            payload["window"],
        )
        self.assertFalse(self.daily_dir.exists())

    def test_list_reports_missing_days_inside_the_window(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body()
        payload = json.loads(
            self.run_cli(
                "list", "--from", "2026-08-07", "--to", "2026-08-10", "--json"
            ).stdout
        )
        self.assertEqual(["2026-08-07", "2026-08-08"], payload["missing_days"])
        self.assertEqual(1, payload["total"])
        self.assertEqual("draft", payload["reports"][0]["status"])

    def test_list_window_is_half_open(self):
        for day in ("2026-08-09", "2026-08-10"):
            self.run_cli("begin", "--day", day)
            self.write_body(day=day)
        payload = json.loads(
            self.run_cli(
                "list", "--from", "2026-08-09", "--to", "2026-08-10", "--json"
            ).stdout
        )
        self.assertEqual(["2026-08-09"], [row["day"] for row in payload["reports"]])


class ValidateTest(ReportsCLITestCase):
    def prepare(self):
        self.run_cli("begin", "--day", "2026-08-09")
        self.write_body()
        return self.report()

    def problems(self):
        result = self.run_cli("validate", "--json", check=False)
        return result.returncode, json.loads(result.stdout)["problems"]

    def test_clean_store_passes(self):
        self.prepare()
        code, problems = self.problems()
        self.assertEqual(0, code)
        self.assertEqual([], problems)

    def test_status_day_and_window_are_checked(self):
        path = self.prepare()
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("status: draft", "status: 完成")
            .replace("day: 2026-08-09", "day: 2026-08-08")
            .replace(
                "window: 2026-08-09T00:00:00+08:00/2026-08-10T00:00:00+08:00",
                "window: 2026-08-09/2026-08-10",
            ),
            encoding="utf-8",
        )
        code, problems = self.problems()
        self.assertEqual(1, code)
        joined = " ".join(item["problem"] for item in problems)
        self.assertIn("status 非法", joined)
        self.assertIn("day 与文件名不一致", joined)
        self.assertIn("window 与自然日不一致", joined)

    def test_widened_permissions_are_reported(self):
        path = self.prepare()
        os.chmod(path, 0o644)
        code, problems = self.problems()
        self.assertEqual(1, code)
        self.assertIn("文件权限", " ".join(item["problem"] for item in problems))

    def test_stray_file_is_reported(self):
        self.prepare()
        (self.daily_dir / "notes.md").write_text("x\n", encoding="utf-8")
        code, problems = self.problems()
        self.assertEqual(1, code)
        self.assertIn("既不是日报", " ".join(item["problem"] for item in problems))

    def test_superseded_snapshot_is_not_judged_as_a_current_report(self):
        self.prepare()
        self.run_cli("confirm", "--day", "2026-08-09")
        self.run_cli("begin", "--day", "2026-08-09", "--redo")
        self.write_body()
        code, problems = self.problems()
        self.assertEqual(0, code)
        self.assertEqual([], problems)


class FrontmatterTest(unittest.TestCase):
    def test_report_without_git_evidence_fields_remains_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            directory = store.ensure_daily_dir(reports_root)
            path = directory / "2026-08-09.md"
            meta = store.skeleton(date(2026, 8, 9), "2026-08-10T02:00:00+08:00")
            for key in ("git_scan_id", "git_commits", "git_commit_ids"):
                meta.pop(key)
            store.write_report(path, meta, "## 概览\n\n无 Git 证据的日报正文。\n")

            self.assertEqual([], store.check_report(path))

    def test_roundtrip_keeps_lists_and_empty_scalars(self):
        meta = store.skeleton(date(2026, 8, 9), "2026-08-10T02:00:00+08:00")
        meta["activity_ids"] = [ACTIVITY_1, ACTIVITY_2]
        meta["sessions_activities"] = 2
        text = store.render_report(meta, "## 概览\n\n正文。\n")

        parsed, body = store.parse_frontmatter(text)

        self.assertEqual([ACTIVITY_1, ACTIVITY_2], parsed["activity_ids"])
        self.assertEqual([], parsed["work_event_ids"])
        self.assertIsNone(parsed["confirmed_at"])
        self.assertEqual("2", parsed["sessions_activities"])
        self.assertEqual("## 概览\n\n正文。\n", body)

    def test_unclosed_frontmatter_fails_loudly(self):
        with self.assertRaises(store.ReportError):
            store.parse_frontmatter("---\nday: 2026-08-09\n")

    def test_unsupported_shape_fails_loudly(self):
        with self.assertRaises(store.ReportError):
            store.parse_frontmatter("---\nnested:\n  key: value\n---\n\nbody\n")

    def test_day_window_is_the_local_natural_day(self):
        start, end = store.day_window(date(2026, 8, 9))
        self.assertEqual("2026-08-09T00:00:00+08:00", start.isoformat())
        self.assertEqual("2026-08-10T00:00:00+08:00", end.isoformat())

    def test_session_counts_close_over_unique_prefixed_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            directory = store.ensure_daily_dir(reports_root)
            path = directory / "2026-08-09.md"
            meta = store.skeleton(date(2026, 8, 9), "2026-08-10T02:00:00+08:00")
            meta.update(
                sessions_activities=2,
                sessions_partial=1,
                sessions_interrupted=1,
                sessions_omitted=3,
                activity_ids=[ACTIVITY_1, ACTIVITY_2],
                work_events=1,
                work_event_ids=["EVT-1"],
            )
            store.write_report(path, meta, "## 概览\n\n正文。\n")
            self.assertEqual([], store.check_report(path))

    def test_session_cleaning_counts_reject_negative_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            directory = store.ensure_daily_dir(reports_root)
            path = directory / "2026-08-09.md"
            meta = store.skeleton(date(2026, 8, 9), "2026-08-10T02:00:00+08:00")
            for key in ("sessions_partial", "sessions_interrupted", "sessions_omitted"):
                meta[key] = "-1"
            store.write_report(path, meta, "## 概览\n\n正文。\n")
            problems = " ".join(store.check_report(path))
            self.assertIn("sessions_partial 必须是非负整数", problems)
            self.assertIn("sessions_interrupted 必须是非负整数", problems)
            self.assertIn("sessions_omitted 必须是非负整数", problems)

    def test_session_count_list_mismatch_duplicate_and_prefix_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_root = Path(temporary) / "reports"
            directory = store.ensure_daily_dir(reports_root)
            path = directory / "2026-08-09.md"
            meta = store.skeleton(date(2026, 8, 9), "2026-08-10T02:00:00+08:00")
            meta.update(
                sessions_activities=1,
                activity_ids=[ACTIVITY_1, ACTIVITY_1, "BAD-2"],
                work_events=1,
                work_event_ids=["EVT-1", "NOPE-2"],
            )
            store.write_report(path, meta, "## 概览\n\n正文。\n")
            problems = " ".join(store.check_report(path))
            self.assertIn("activity_ids 不能重复", problems)
            self.assertIn("activity_ids 格式非法", problems)
            self.assertIn("work_event_ids 前缀非法", problems)
            self.assertIn("sessions_activities 必须等于唯一 activity_ids 数量", problems)
            self.assertIn("work_events 必须等于唯一 work_event_ids 数量", problems)


class ActivityIdMigrationTest(ReportsCLITestCase):
    def prepare_legacy_reports(self):
        self.run_cli("begin", "--day", "2026-08-09")
        path = self.report()
        meta, _body = store.read_report(path)
        meta.update(
            sessions_activities=1,
            activity_ids=[LEGACY_ACTIVITY_1],
            status="confirmed",
            confirmed_at="2026-08-10T02:05:00+08:00",
        )
        body = "## 概览\n\n历史正文保持不变。\n"
        store.write_report(path, meta, body)
        snapshot = self.daily_dir / "2026-08-09.superseded-20260810T020600+0800.md"
        store.write_report(snapshot, meta, body)
        return path, snapshot

    def test_dry_run_reads_current_and_superseded_without_writing(self):
        path, snapshot = self.prepare_legacy_reports()
        before = {item: item.read_bytes() for item in (path, snapshot)}

        payload = json.loads(self.run_cli("migrate-activity-ids", "--json").stdout)

        self.assertEqual("dry-run", payload["mode"])
        self.assertEqual(2, payload["checked_files"])
        self.assertEqual(2, payload["changed_files"])
        self.assertEqual(2, payload["changed_ids"])
        self.assertIsNone(payload["backup"])
        self.assertEqual(before, {item: item.read_bytes() for item in (path, snapshot)})

    def test_apply_only_changes_ids_and_is_idempotent(self):
        path, snapshot = self.prepare_legacy_reports()
        before = {item: store.read_report(item) for item in (path, snapshot)}

        payload = json.loads(
            self.run_cli("migrate-activity-ids", "--apply", "--json").stdout
        )

        compact = migrate_legacy_activity_id(LEGACY_ACTIVITY_1)
        self.assertEqual(2, payload["changed_files"])
        self.assertEqual(2, payload["changed_ids"])
        self.assertTrue(Path(payload["backup"]).is_dir())
        for item in (path, snapshot):
            meta, body = store.read_report(item)
            old_meta, old_body = before[item]
            self.assertEqual([compact], meta["activity_ids"])
            old_meta["activity_ids"] = [compact]
            self.assertEqual(old_meta, meta)
            self.assertEqual(old_body, body)

        again = json.loads(
            self.run_cli("migrate-activity-ids", "--apply", "--json").stdout
        )
        self.assertEqual(0, again["changed_files"])
        self.assertEqual(0, again["changed_ids"])
        self.assertIsNone(again["backup"])
        self.assertEqual(0, self.run_cli("validate", check=False).returncode)

    def test_collision_fails_before_any_file_changes(self):
        path, _snapshot = self.prepare_legacy_reports()
        meta, body = store.read_report(path)
        collision = "ACT-" + "00" * 15 + "11" * 17
        meta.update(sessions_activities=2, activity_ids=[LEGACY_ACTIVITY_1, collision])
        store.write_report(path, meta, body)
        before = path.read_bytes()

        result = self.run_cli("migrate-activity-ids", "--apply", check=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("碰撞", result.stderr)
        self.assertEqual(before, path.read_bytes())

    def test_interruption_after_old_tree_moves_is_recovered_before_validate(self):
        path, _snapshot = self.prepare_legacy_reports()
        planned = store.plan_activity_id_migration(self.reports_root)
        original_replace = os.replace

        def interrupt_after_old_tree_moves(source, target):
            result = original_replace(source, target)
            if Path(source) == self.reports_root:
                raise KeyboardInterrupt("synthetic process termination")
            return result

        with mock.patch(
            "lifeos_reports.store.os.replace", side_effect=interrupt_after_old_tree_moves
        ):
            with self.assertRaises(KeyboardInterrupt):
                store.apply_activity_id_migration(self.reports_root, planned)

        self.assertFalse(path.exists())
        marker = self.data_dir / store.ACTIVITY_ID_MIGRATION_MARKER
        self.assertTrue(marker.is_file())
        validated = self.run_cli("validate", check=False)
        self.assertEqual(1, validated.returncode)
        self.assertIn("activity_ids 格式非法", validated.stderr)
        self.assertTrue(path.exists())
        self.assertFalse(marker.exists())

    def test_interruption_after_new_tree_moves_keeps_new_tree_on_recovery(self):
        path, _snapshot = self.prepare_legacy_reports()
        planned = store.plan_activity_id_migration(self.reports_root)
        original_replace = os.replace

        def interrupt_after_new_tree_moves(source, target):
            result = original_replace(source, target)
            source_path = Path(source)
            if (
                source_path.name == self.reports_root.name
                and source_path.parent.name.startswith(
                    store.ACTIVITY_ID_MIGRATION_STAGING_PREFIX
                )
            ):
                raise KeyboardInterrupt("synthetic process termination")
            return result

        with mock.patch(
            "lifeos_reports.store.os.replace", side_effect=interrupt_after_new_tree_moves
        ):
            with self.assertRaises(KeyboardInterrupt):
                store.apply_activity_id_migration(self.reports_root, planned)

        marker = self.data_dir / store.ACTIVITY_ID_MIGRATION_MARKER
        self.assertTrue(path.exists())
        self.assertTrue(marker.is_file())
        validated = self.run_cli("validate", check=False)
        self.assertEqual(0, validated.returncode)
        self.assertFalse(marker.exists())
        meta, _body = store.read_report(path)
        self.assertEqual(
            [migrate_legacy_activity_id(LEGACY_ACTIVITY_1)], meta["activity_ids"]
        )

    def test_superseded_invalid_shape_body_or_permissions_blocks_migration(self):
        _path, snapshot = self.prepare_legacy_reports()

        meta, _body = store.read_report(snapshot)
        meta["activity_ids"] = ["ACT-invalid"]
        store.write_report(snapshot, meta, "## 概览\n\n正文。\n")
        with self.assertRaisesRegex(store.ReportError, "activity_ids 格式非法"):
            store.plan_activity_id_migration(self.reports_root)

        meta["activity_ids"] = [LEGACY_ACTIVITY_1]
        store.write_report(snapshot, meta, "")
        with self.assertRaisesRegex(store.ReportError, "正文为空"):
            store.plan_activity_id_migration(self.reports_root)

        store.write_report(snapshot, meta, "## 概览\n\n正文。\n")
        os.chmod(snapshot, 0o644)
        with self.assertRaisesRegex(store.ReportError, "文件权限"):
            store.plan_activity_id_migration(self.reports_root)


class ActivityIdContractTest(unittest.TestCase):
    def test_activity_id_is_stable_compact_base32(self):
        first = activity_id("codex", "conversation-1", "slice-1", "slice-2")
        second = activity_id("codex", "conversation-1", "slice-1", "slice-2")

        self.assertEqual(first, second)
        self.assertRegex(first, r"^ACT-[A-Z2-7]{24}$")

    def test_legacy_conversion_uses_the_same_digest_prefix(self):
        import hashlib

        parts = ("codex", "conversation-1", "slice-1")
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

        self.assertEqual(activity_id(*parts), migrate_legacy_activity_id(f"ACT-{digest}"))

    def test_real_eighty_bit_collision_remains_distinct_at_120_bits(self):
        first = "ACT-a750d0ea924464af5f2446d022c8d39b41cc17f0c5c7c63b63897c275275fbe0"
        second = "ACT-a750d0ea924464af5f2446d022c8c39b41cc17f0c5c7c63b63897c275275fbe0"

        self.assertNotEqual(
            migrate_legacy_activity_id(first), migrate_legacy_activity_id(second)
        )


class PeriodicReportsTest(ReportsCLITestCase):
    def periodic_report(self, period="2026-W32"):
        return self.reports_root / "periodic" / f"{period}.md"

    def prepare_daily(self, day, *, confirmed=True, body=None):
        self.run_cli("begin", "--day", day)
        path = self.report(day)
        meta, _existing = store.read_report(path)
        store.write_report(
            path,
            meta,
            body or f"# {day}\n\n当天形成了可复用的工作结论。\n",
        )
        if confirmed:
            self.run_cli("confirm", "--day", day)
        return path

    def write_periodic(self, period="2026-W32"):
        body = self.data_dir / "periodic-body.md"
        body.write_text("# 周期总结\n\n这一周期形成了一条跨日变化主线。\n", encoding="utf-8")
        return self.run_cli(
            "periodic",
            "write",
            "--period",
            period,
            "--body-file",
            str(body),
        )

    def test_period_windows_cover_supported_calendar_kinds(self):
        expected = {
            "2026-W01": ("week", "2025-12-29", "2026-01-05"),
            "2026-02": ("month", "2026-02-01", "2026-03-01"),
            "2026-Q3": ("quarter", "2026-07-01", "2026-10-01"),
            "2026-H2": ("half", "2026-07-01", "2027-01-01"),
            "2026": ("year", "2026-01-01", "2027-01-01"),
        }
        for period, result in expected.items():
            kind, start, end = store.period_window(period)
            self.assertEqual(result, (kind, start.isoformat(), end.isoformat()))
        for invalid in ("2026-Q5", "0000-Q1", "0000-H1", "9999-W52"):
            with self.assertRaises(store.ReportError):
                store.period_window(invalid)

    def test_sources_only_returns_confirmed_daily_bodies_and_reports_gaps(self):
        self.prepare_daily("2026-08-03", body="# 周一\n\n确认正文。\n")
        self.prepare_daily("2026-08-04", confirmed=False)

        payload = json.loads(
            self.run_cli(
                "periodic", "sources", "--period", "2026-W32", "--json"
            ).stdout
        )

        self.assertEqual(1, payload["source_total"])
        self.assertIsNone(payload["next_offset"])
        self.assertEqual(["2026-08-03"], payload["coverage"]["source_days"])
        self.assertEqual(["2026-08-04"], payload["coverage"]["draft_days"])
        self.assertEqual(
            ["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09"],
            payload["coverage"]["missing_days"],
        )
        self.assertEqual("# 周一\n\n确认正文。\n", payload["reports"][0]["body"])

    def test_sources_paginate_long_period_without_repeating_coverage(self):
        for day in ("2026-08-03", "2026-08-04", "2026-08-05"):
            self.prepare_daily(day)

        first = json.loads(
            self.run_cli(
                "periodic",
                "sources",
                "--period",
                "2026-W32",
                "--limit",
                "2",
                "--json",
            ).stdout
        )
        second = json.loads(
            self.run_cli(
                "periodic",
                "sources",
                "--period",
                "2026-W32",
                "--offset",
                str(first["next_offset"]),
                "--limit",
                "2",
                "--json",
            ).stdout
        )

        self.assertEqual(["2026-08-03", "2026-08-04"], [row["day"] for row in first["reports"]])
        self.assertEqual(2, first["next_offset"])
        self.assertIn("coverage", first)
        self.assertEqual(["2026-08-05"], [row["day"] for row in second["reports"]])
        self.assertIsNone(second["next_offset"])
        self.assertNotIn("coverage", second)

    def test_begin_write_confirm_and_list_keep_compact_periodic_state(self):
        self.prepare_daily("2026-08-03")
        self.prepare_daily("2026-08-04")
        begun = json.loads(
            self.run_cli(
                "periodic", "begin", "--period", "2026-W32", "--json"
            ).stdout
        )
        self.assertEqual("new", begun["state"])
        self.assertEqual(["2026-08-03", "2026-08-04"], begun["source_days"])
        self.assertEqual(0o700, self.mode(self.reports_root / "periodic"))
        self.assertEqual(0o600, self.mode(self.periodic_report()))

        self.write_periodic()
        confirmed = json.loads(
            self.run_cli(
                "periodic", "confirm", "--period", "2026-W32", "--json"
            ).stdout
        )
        self.assertTrue(confirmed["changed"])
        meta, body = store.read_report(self.periodic_report())
        self.assertEqual("confirmed", meta["status"])
        self.assertEqual("week", meta["period_type"])
        self.assertEqual(
            {
                "period",
                "period_type",
                "status",
                "generated_at",
                "confirmed_at",
                "window",
            },
            set(meta),
        )
        self.assertIn("跨日变化主线", body)

        rows = json.loads(self.run_cli("periodic", "list", "--json").stdout)
        self.assertEqual(1, rows["total"])
        self.assertNotIn("source_days", rows["reports"][0])
        self.assertEqual(0, self.run_cli("validate", check=False).returncode)

    def test_confirm_rejects_an_unwritten_skeleton(self):
        self.prepare_daily("2026-08-03")
        self.run_cli("periodic", "begin", "--period", "2026-W32")

        result = self.run_cli(
            "periodic", "confirm", "--period", "2026-W32", check=False
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("正文为空", result.stderr)

    def test_begin_requires_at_least_one_confirmed_daily(self):
        self.prepare_daily("2026-08-03", confirmed=False)
        result = self.run_cli(
            "periodic", "begin", "--period", "2026-W32", check=False
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("没有已确认日报", result.stderr)
        self.assertFalse(self.periodic_report().exists())

    def test_confirm_accepts_the_reviewed_draft_without_rechecking_daily_sources(self):
        self.prepare_daily("2026-08-03")
        self.run_cli("periodic", "begin", "--period", "2026-W32")
        self.write_periodic()
        self.prepare_daily("2026-08-04")

        result = json.loads(
            self.run_cli(
                "periodic", "confirm", "--period", "2026-W32", "--json"
            ).stdout
        )

        self.assertTrue(result["changed"])
        meta, _body = store.read_report(self.periodic_report())
        self.assertEqual("confirmed", meta["status"])

    def test_redo_preserves_confirmed_periodic_report(self):
        self.prepare_daily("2026-08-03")
        self.run_cli("periodic", "begin", "--period", "2026-W32")
        self.write_periodic()
        self.run_cli("periodic", "confirm", "--period", "2026-W32")
        before = self.periodic_report().read_bytes()

        payload = json.loads(
            self.run_cli(
                "periodic",
                "begin",
                "--period",
                "2026-W32",
                "--redo",
                "--json",
            ).stdout
        )

        snapshot = Path(payload["superseded"])
        self.assertEqual(before, snapshot.read_bytes())
        meta, _body = store.read_report(self.periodic_report())
        self.assertEqual("draft", meta["status"])


if __name__ == "__main__":
    unittest.main()
