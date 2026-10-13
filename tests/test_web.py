import copy
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lifeos_reports import store
from lifeos_web.projection import build_snapshot, report_detail, resolve_openable_report
from lifeos_web.server import create_server
from lifeos_web.cli import _loopback_host


REPO_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "runtime"
SCRIPT = REPO_DIR / "lifeos.py"


def fixture_current_data():
    return tuple(
        json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        for name in ("projects.json", "entries.json", "glossary.json")
    )


def write_report(reports_root: Path, day_text: str = "2026-08-29") -> Path:
    day = date.fromisoformat(day_text)
    store.ensure_daily_dir(reports_root)
    meta = store.skeleton(day, "2026-08-30T09:00:00+08:00")
    meta["sessions_activities"] = 2
    meta["work_events"] = 1
    path = store.report_path(reports_root, day)
    store.write_report(path, meta, "## 概览\n\n今天完成了只读 Web 工作台。\n")
    return path


class WebProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.reports_root = Path(self.temporary_directory.name) / "reports"
        self.report_path = write_report(self.reports_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def entries(self, data):
        return data[1]["entries"]

    def test_snapshot_projects_entries_and_marks_live_records(self):
        data = list(copy.deepcopy(fixture_current_data()))
        done = copy.deepcopy(self.entries(data)[0])
        done.update(
            {
                "id": "TASK-20260829-999",
                "status": "done",
                "note": "做完了",
                "updated_at": "2026-08-29T12:00:00+08:00",
            }
        )
        self.entries(data).append(done)
        waiting = copy.deepcopy(self.entries(data)[1])
        waiting.update({"id": "TASK-20260829-998", "owner": "凯健"})
        self.entries(data).append(waiting)
        before = copy.deepcopy(data)

        snapshot = build_snapshot(tuple(data), self.reports_root, reference_date=date(2026, 8, 30))

        by_id = {item["id"]: item for item in snapshot["entries"]}
        self.assertTrue(by_id["TASK-20260725-001"]["live"])
        self.assertTrue(by_id["TASK-20260725-001"]["current"])
        self.assertTrue(by_id["TASK-20260725-001"]["mine"])
        # 等别人的待办也在「在办」里，只是标成不是本人的
        self.assertEqual((True, False), (by_id["TASK-20260829-998"]["current"], by_id["TASK-20260829-998"]["mine"]))
        self.assertEqual("•", by_id["TASK-20260725-001"]["symbol"])
        self.assertFalse(by_id["TASK-20260829-999"]["live"])
        self.assertIsNone(by_id["TASK-20260829-999"]["rank"])
        self.assertEqual(("做完了", "完成"), (by_id["TASK-20260829-999"]["note"], by_id["TASK-20260829-999"]["status_label"]))
        self.assertEqual("2026-07-25", by_id["TASK-20260725-001"]["logged_on"])
        self.assertEqual("2026-08-30", snapshot["reference_date"])
        self.assertEqual("2026-08-29", snapshot["reports"][0]["day"])
        self.assertNotIn("path", snapshot["reports"][0])
        self.assertEqual(before, data)

    def test_progress_is_dated_and_earlier_versions_come_from_the_audit_log(self):
        data = list(copy.deepcopy(fixture_current_data()))
        tracked, older = copy.deepcopy(self.entries(data)[:2])
        tracked.update({"id": "TASK-20260901-001", "status": "done", "note": "研发排进了 11 月"})
        older.update({"id": "TASK-20260901-002", "note": "迁移前写下的进展"})
        data[1]["entries"] = [tracked, older]

        def event(at, **fields):
            return {"occurred_at": f"2026-09-{at}T10:00:00+08:00", "entry_id": "TASK-20260901-001", **fields}

        events = [
            event("01", kind="entry_created", status_from=None, status_to="open"),
            event("02", kind="task_scheduled", status_from="open", status_to="scheduled", note="先等试运行",
                  field_changes={"note": {"from": None, "to": "先等试运行"}}),
            event("03", kind="entry_updated", status_from="scheduled", status_to="open",
                  field_changes={"note": {"from": "先等试运行", "to": None}}),
            event("04", kind="entry_updated", field_changes={"note": {"from": None, "to": "等对方回复"}}),
            event("05", kind="entry_updated", field_changes={"context": {"from": "a", "to": "b"}}),
            event("06", kind="task_done", status_from="open", status_to="done", note="研发排进了 11 月",
                  field_changes={"note": {"from": "等对方回复", "to": "研发排进了 11 月"}}),
        ]

        snapshot = build_snapshot(tuple(data), self.reports_root, reference_date=date(2026, 9, 30), events=events)

        by_id = {item["id"]: item for item in snapshot["entries"]}
        done = by_id["TASK-20260901-001"]
        self.assertEqual(("完成了什么", "2026-09-06T10:00:00+08:00"), (done["note_label"], done["note_at"]))
        self.assertEqual(
            [
                {"at": "2026-09-04T10:00:00+08:00", "note": "等对方回复", "status_label": None},
                {"at": "2026-09-02T10:00:00+08:00", "note": "先等试运行", "status_label": "排到以后"},
            ],
            done["note_history"],
        )
        # 台账里没记下值的旧进展只显示当前一句，不补日期。
        self.assertEqual(("进展", None, []), tuple(by_id["TASK-20260901-002"][key] for key in ("note_label", "note_at", "note_history")))

    def test_snapshot_exposes_only_browser_fields(self):
        data = list(copy.deepcopy(fixture_current_data()))
        private_path = "/Users/example/.local/share/lifeos/private.md"
        data[0]["projects"].append(
            {
                "id": "PROJECT-PRIVATE",
                "project_key": "private",
                "name": "私有项目",
                "sources": [{"location": private_path}],
            }
        )
        for entry in self.entries(data):
            entry["context"] = None

        snapshot = build_snapshot(tuple(data), self.reports_root)
        encoded = json.dumps(snapshot, ensure_ascii=False)

        self.assertNotIn(private_path, encoded)
        self.assertNotIn("sources", encoded)
        self.assertNotIn("fact_source", encoded)

    def test_snapshot_report_errors_do_not_expose_local_paths(self):
        invalid_path = store.report_path(self.reports_root, date(2026, 8, 28))
        invalid_path.write_text("not a valid report", encoding="utf-8")

        snapshot = build_snapshot(fixture_current_data(), self.reports_root)
        invalid = next(report for report in snapshot["reports"] if report["day"] == "2026-08-28")

        self.assertEqual("日报无法读取", invalid["error"])
        self.assertNotIn(str(invalid_path), json.dumps(snapshot, ensure_ascii=False))

    def test_rank_reuses_the_brief_order_of_importance_then_urgency(self):
        data = list(copy.deepcopy(fixture_current_data()))
        template = copy.deepcopy(self.entries(data)[0])
        tasks = []
        for task_id, starred, due in (
            ("TASK-20260801-005", False, None),
            ("TASK-20260801-004", False, "2026-09-01"),
            ("TASK-20260801-003", True, None),
            ("TASK-20260801-002", True, "2026-08-31"),
        ):
            task = copy.deepcopy(template)
            task.update({"id": task_id, "starred": starred, "due": due})
            tasks.append(task)
        data[1]["entries"] = tasks

        snapshot = build_snapshot(tuple(data), self.reports_root, reference_date=date(2026, 8, 30))

        ranked = sorted(snapshot["entries"], key=lambda item: item["rank"])
        self.assertEqual(
            ["TASK-20260801-002", "TASK-20260801-003", "TASK-20260801-004", "TASK-20260801-005"],
            [item["id"] for item in ranked],
        )
        self.assertEqual([0, 1, 2, 3], [item["quadrant"] for item in ranked])

    def test_report_detail_is_date_derived_and_rejects_paths(self):
        before = self.report_path.read_bytes()
        detail = report_detail(self.reports_root, "2026-08-29")
        self.assertIn("只读 Web 工作台", detail["body"])
        self.assertNotIn("path", detail)
        self.assertEqual(self.report_path, resolve_openable_report(self.reports_root, "2026-08-29"))
        for value in ("../2026-08-29", "2026-8-29", "/tmp/report"):
            with self.assertRaises((ValueError, store.ReportError)):
                resolve_openable_report(self.reports_root, value)
        self.assertEqual(before, self.report_path.read_bytes())


class WebServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.reports_root = Path(self.temporary_directory.name) / "reports"
        self.report_path = write_report(self.reports_root)
        self.opener = Mock()
        self.server = create_server(
            "127.0.0.1",
            0,
            self.reports_root,
            current_data_reader=fixture_current_data,
            events_reader=list,
            opener=self.opener,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request_json(self, path, *, method="GET", headers=None):
        request = Request(
            self.base_url + path,
            method=method,
            headers=headers or {},
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, dict(response.headers), json.loads(response.read())

    def test_snapshot_and_static_page_are_same_origin_read_only_views(self):
        with urlopen(self.base_url + "/", timeout=2) as response:
            html = response.read().decode("utf-8")
            self.assertIn("data-tab=\"work\"", html)
            self.assertIn("data-tab=\"insights\"", html)
            self.assertIn("data-tab=\"daily\"", html)
            self.assertIn("role=\"dialog\"", html)
            self.assertIn("aria-modal=\"true\"", html)
            self.assertEqual("no-store", response.headers["Cache-Control"])
            self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])

        with urlopen(self.base_url + "/assets/lifeos-logo.svg", timeout=2) as response:
            self.assertEqual("image/svg+xml", response.headers["Content-Type"])
            self.assertIn(b"LifeOS", response.read())

        with urlopen(self.base_url + "/assets/fonts/LXGWWenKai-Regular.woff2", timeout=2) as response:
            self.assertEqual("font/woff2", response.headers["Content-Type"])
            self.assertGreater(len(response.read()), 1000)

        status, headers, payload = self.request_json("/api/snapshot")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertIn("测试待办 1", [item["text"] for item in payload["entries"]])

        status, _headers, report = self.request_json("/api/reports/2026-08-29")
        self.assertEqual(200, status)
        self.assertIn("只读 Web 工作台", report["body"])

    def test_open_report_requires_explicit_intent_and_uses_canonical_path(self):
        status, _headers, payload = self.request_json(
            "/api/reports/2026-08-29/open", method="POST"
        )
        self.assertEqual(403, status)
        self.assertIn("显式意图", payload["error"])
        self.opener.assert_not_called()

        status, _headers, payload = self.request_json(
            "/api/reports/2026-08-29/open",
            method="POST",
            headers={"X-LifeOS-Intent": "open-report"},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["opened"])
        self.opener.assert_called_once_with(
            ["open", str(self.report_path)], check=True, timeout=5
        )

        status, _headers, _payload = self.request_json(
            "/api/reports/..%2F2026-08-29/open",
            method="POST",
            headers={"X-LifeOS-Intent": "open-report"},
        )
        self.assertEqual(404, status)

    def test_report_errors_do_not_expose_local_paths(self):
        missing_day = "2026-08-28"
        missing_path = str(store.report_path(self.reports_root, date.fromisoformat(missing_day)))

        status, _headers, payload = self.request_json(f"/api/reports/{missing_day}")

        self.assertEqual(404, status)
        self.assertEqual("日报不存在或无法读取", payload["error"])
        self.assertNotIn(missing_path, json.dumps(payload, ensure_ascii=False))

        self.opener.side_effect = subprocess.CalledProcessError(
            1, ["open", str(self.report_path)]
        )
        status, _headers, payload = self.request_json(
            "/api/reports/2026-08-29/open",
            method="POST",
            headers={"X-LifeOS-Intent": "open-report"},
        )
        self.assertEqual(500, status)
        self.assertEqual("无法使用系统默认应用打开日报", payload["error"])
        self.assertNotIn(str(self.report_path), json.dumps(payload, ensure_ascii=False))

    def test_invalid_host_header_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.putrequest("GET", "/api/snapshot", skip_host=True)
        connection.putheader("Host", "example.test")
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(400, response.status)
        self.assertIn("本机 Host", payload["error"])


class WebCLITest(unittest.TestCase):
    def test_help_is_explicitly_read_only_and_non_loopback_binding_is_refused(self):
        self.assertEqual("::1", _loopback_host("::1"))
        help_result = subprocess.run(
            [sys.executable, str(SCRIPT), "web", "--help"],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("只读", help_result.stdout)
        self.assertIn("不提供 Agent", help_result.stdout)

        invalid = subprocess.run(
            [sys.executable, str(SCRIPT), "web", "serve", "--host", "0.0.0.0"],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(2, invalid.returncode)
        self.assertIn("只允许监听本机回环地址", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
