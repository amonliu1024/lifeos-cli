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
        for name in (
            "projects.json",
            "work-items.json",
            "tasks.json",
            "glossary.json",
            "ideas.json",
            "achievements.json",
        )
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

    def test_snapshot_preserves_relationships_and_marks_terminal_records(self):
        data = list(copy.deepcopy(fixture_current_data()))
        data[1]["work_items"].append(
            {
                "id": "WI-20260829-999",
                "title": "已结束主线",
                "project_id": None,
                "state": "closed",
                "next_gate": None,
                "milestones": [],
                "sources": [],
                "created_at": "2026-08-29T10:00:00+08:00",
                "updated_at": "2026-08-29T12:00:00+08:00",
            }
        )
        completed = copy.deepcopy(data[2]["tasks"][0])
        completed.update(
            {
                "id": "TASK-20260829-999",
                "work_item_id": "WI-20260829-999",
                "status": "completed",
                "closed_at": "2026-08-29T12:00:00+08:00",
            }
        )
        data[2]["tasks"].append(completed)
        before = copy.deepcopy(data)

        snapshot = build_snapshot(tuple(data), self.reports_root)

        active = next(item for item in snapshot["work"]["items"] if item["id"] == "WI-20260725-001")
        closed = next(item for item in snapshot["work"]["items"] if item["id"] == "WI-20260829-999")
        self.assertFalse(active["terminal"])
        self.assertFalse(active["tasks"][0]["terminal"])
        self.assertTrue(closed["terminal"])
        self.assertTrue(closed["tasks"][0]["terminal"])
        self.assertEqual("2026-08-29", snapshot["reports"][0]["day"])
        self.assertNotIn("path", snapshot["reports"][0])
        self.assertEqual(before, data)

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
        data[1]["work_items"][0]["sources"] = [{"location": private_path}]
        data[2]["tasks"][0]["sources"] = [{"location": private_path}]
        data[4]["ideas"].append(
            {
                "id": "IDEA-PRIVATE",
                "text": "私有路径不进入投影",
                "status": "inbox",
                "sources": [{"location": private_path}],
            }
        )
        data[5]["achievements"].append(
            {
                "id": "ACH-PRIVATE",
                "title": "私有路径不进入投影",
                "lifecycle": "current",
                "sources": [{"location": private_path}],
            }
        )

        snapshot = build_snapshot(tuple(data), self.reports_root)
        encoded = json.dumps(snapshot, ensure_ascii=False)

        self.assertNotIn(private_path, encoded)
        self.assertNotIn("sources", encoded)
        self.assertNotIn("responsible_party", encoded)

    def test_snapshot_report_errors_do_not_expose_local_paths(self):
        invalid_path = store.report_path(self.reports_root, date(2026, 8, 28))
        invalid_path.write_text("not a valid report", encoding="utf-8")

        snapshot = build_snapshot(fixture_current_data(), self.reports_root)
        invalid = next(report for report in snapshot["reports"] if report["day"] == "2026-08-28")

        self.assertEqual("日报无法读取", invalid["error"])
        self.assertNotIn(str(invalid_path), json.dumps(snapshot, ensure_ascii=False))

    def test_tasks_reuse_current_brief_due_then_actual_start_order(self):
        data = list(copy.deepcopy(fixture_current_data()))
        template = copy.deepcopy(data[2]["tasks"][0])
        tasks = []
        for task_id, due_at, created_at in (
            ("TASK-DUE-LATER", "2026-09-03", "2026-01-01T00:00:00+08:00"),
            ("TASK-NO-DUE-NEW", None, "2026-01-02T00:00:00+08:00"),
            ("TASK-DUE-SOON", "2026-09-01", "2026-01-03T00:00:00+08:00"),
            ("TASK-NO-DUE-OLD", None, "2026-01-04T00:00:00+08:00"),
            ("TASK-NO-START", None, "2025-01-01T00:00:00+08:00"),
        ):
            task = copy.deepcopy(template)
            task.update({"id": task_id, "due_at": due_at, "created_at": created_at})
            tasks.append(task)
        data[2]["tasks"] = tasks
        events = [
            {"kind": "task_started", "task_id": "TASK-NO-DUE-NEW", "started_at": "2026-08-20"},
            {"kind": "task_started", "task_id": "TASK-NO-DUE-OLD", "started_at": "2026-08-10"},
        ]

        snapshot = build_snapshot(
            tuple(data), self.reports_root, events, reference_date=date(2026, 8, 30)
        )

        item = snapshot["work"]["items"][0]
        self.assertEqual(
            [
                "TASK-DUE-SOON",
                "TASK-DUE-LATER",
                "TASK-NO-DUE-OLD",
                "TASK-NO-DUE-NEW",
                "TASK-NO-START",
            ],
            [task["id"] for task in item["tasks"]],
        )

    def test_work_items_follow_their_earliest_unfinished_task(self):
        data = list(copy.deepcopy(fixture_current_data()))
        undated_item = data[1]["work_items"][0]
        undated_item.update({"id": "WI-UNDATED", "title": "无截止事项", "state": "active"})
        due_item = copy.deepcopy(undated_item)
        due_item.update({"id": "WI-DUE", "title": "临近截止事项"})
        data[1]["work_items"] = [undated_item, due_item]
        undated_task = data[2]["tasks"][0]
        undated_task.update({"id": "TASK-UNDATED", "work_item_id": "WI-UNDATED", "due_at": None})
        due_task = copy.deepcopy(undated_task)
        due_task.update({"id": "TASK-DUE", "work_item_id": "WI-DUE", "due_at": "2026-08-31"})
        data[2]["tasks"] = [undated_task, due_task]

        snapshot = build_snapshot(
            tuple(data),
            self.reports_root,
            [{"kind": "task_started", "task_id": "TASK-UNDATED", "started_at": "2026-08-01"}],
            reference_date=date(2026, 8, 30),
        )

        self.assertEqual(
            ["WI-DUE", "WI-UNDATED"],
            [item["id"] for item in snapshot["work"]["items"]],
        )

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
            events_reader=lambda: [],
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
            self.assertIn("data-tab=\"daily\"", html)
            self.assertIn("role=\"dialog\"", html)
            self.assertIn("aria-modal=\"true\"", html)
            self.assertEqual("no-store", response.headers["Cache-Control"])
            self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])

        with urlopen(self.base_url + "/assets/lifeos-logo.svg", timeout=2) as response:
            self.assertEqual("image/svg+xml", response.headers["Content-Type"])
            self.assertIn(b"LifeOS", response.read())

        with urlopen(self.base_url + "/assets/fonts/Geist-Regular.ttf", timeout=2) as response:
            self.assertEqual("font/ttf", response.headers["Content-Type"])
            self.assertGreater(len(response.read()), 1000)

        status, headers, payload = self.request_json("/api/snapshot")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("测试事项", payload["work"]["items"][0]["title"])

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
