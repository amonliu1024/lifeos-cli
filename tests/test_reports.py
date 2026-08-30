import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from lifeos_reports import store


REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"


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
                "ACT-1",
                "--activity-id",
                "ACT-2",
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
        self.assertEqual(["ACT-1", "ACT-2"], meta["activity_ids"])
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
            "ACT-1",
            "--activity-id",
            "ACT-1",
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
        meta["activity_ids"] = ["ACT-1", "ACT-2"]
        meta["sessions_activities"] = 2
        text = store.render_report(meta, "## 概览\n\n正文。\n")

        parsed, body = store.parse_frontmatter(text)

        self.assertEqual(["ACT-1", "ACT-2"], parsed["activity_ids"])
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
                activity_ids=["ACT-1", "ACT-2"],
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
                activity_ids=["ACT-1", "ACT-1", "BAD-2"],
                work_events=1,
                work_event_ids=["EVT-1", "NOPE-2"],
            )
            store.write_report(path, meta, "## 概览\n\n正文。\n")
            problems = " ".join(store.check_report(path))
            self.assertIn("activity_ids 不能重复", problems)
            self.assertIn("activity_ids 前缀非法", problems)
            self.assertIn("work_event_ids 前缀非法", problems)
            self.assertIn("sessions_activities 必须等于唯一 activity_ids 数量", problems)
            self.assertIn("work_events 必须等于唯一 work_event_ids 数量", problems)


if __name__ == "__main__":
    unittest.main()
