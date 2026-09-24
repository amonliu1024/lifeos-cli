import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifeos_calendar.core import CalendarService, TimeWindow  # noqa: E402
from lifeos_calendar.evidence import build_index  # noqa: E402
from lifeos_calendar.store import CalendarStore  # noqa: E402
from lifeos_config.core import default_payload  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "lifeos.py"

# 2026-08-24 Asia/Shanghai, expressed as epoch milliseconds like dws does.
BASE_MS = int(datetime.fromisoformat("2026-08-24T00:00:00+08:00").timestamp() * 1000)
HOUR = 3_600_000


def _event(event_id, name, start_hour, hours=1.0, kind="Single"):
    start = BASE_MS + int(start_hour * HOUR)
    return {
        "id": event_id,
        "name": name,
        "type": kind,
        "members": ["1", "2"],
        "time_range": [{"start_time": start, "end_time": start + int(hours * HOUR)}],
    }


SEARCH_EVENTS = [
    _event("E-REVIEW", "空闲回收需求评审", 11),
    _event("E-ROOM", "刘丹预订的会议", 11),
    _event("E-WEEKLY", "国际化行政固定周会", 16, kind="RecurringMaster"),
]
DETAILS = {
    "E-REVIEW": {"summary": "空闲回收需求评审", "eventType": "Single", "icalendarUid": "uid-review",
                 "attendees": [{"displayName": "刘顺 Amon Liu"}, {"displayName": "刘丹"}],
                 "organizerId": "someone", "description": "secret link"},
    "E-ROOM": {"summary": "刘丹预订的会议", "eventType": "Single", "icalendarUid": "uid-room",
               "attendees": [{"displayName": "刘丹"}]},
    "E-WEEKLY": {"summary": "国际化行政固定周会", "eventType": "RecurringMaster", "icalendarUid": "uid-weekly",
                 "attendees": [{"displayName": "刘顺 Amon Liu"}, {"displayName": "Ella"}]},
}


class FakeCalendarClient:
    def __init__(self, events=None, details=None, limit_at=None):
        self.events = list(SEARCH_EVENTS if events is None else events)
        self.details = dict(DETAILS if details is None else details)
        self.limit_at = limit_at
        self.searches = []

    def search(self, start_ms, end_ms):
        self.searches.append((start_ms, end_ms))
        page = [
            item for item in self.events
            if item["time_range"][0]["start_time"] < end_ms and item["time_range"][0]["end_time"] > start_ms
        ]
        return page[: self.limit_at] if self.limit_at else page

    def info(self, event_id):
        return self.details.get(event_id, {})


class CalendarServiceTest(unittest.TestCase):
    def test_scan_keeps_only_contract_fields_and_dedupes_instances(self):
        client = FakeCalendarClient()
        result = CalendarService(client).scan(TimeWindow.from_values("2026-08-24", "2026-08-25"))

        self.assertEqual("complete", result["status"])
        self.assertEqual(3, result["summary"]["events"])
        review = next(item for item in result["events"] if item["event_id"] == "E-REVIEW")
        self.assertEqual(
            {"instance_id", "event_id", "series_key", "summary", "dtstart", "dtend", "type", "attendees"},
            set(review),
        )
        self.assertEqual("uid-review", review["series_key"])
        self.assertEqual(["刘顺 Amon Liu", "刘丹"], review["attendees"])
        self.assertEqual("2026-08-24T11:00:00+08:00", review["dtstart"])
        self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))
        self.assertTrue(review["instance_id"].startswith("CAL-"))

    def test_search_limit_splits_the_window_until_nothing_is_hidden(self):
        # Ten events in one hour would hit the dws page size; splitting must recover them all.
        events = [_event(f"E-{n}", f"会 {n}", 9 + n * 0.05, hours=0.05) for n in range(12)]
        details = {item["id"]: {"summary": item["name"], "attendees": []} for item in events}
        client = FakeCalendarClient(events, details, limit_at=10)
        result = CalendarService(client).scan(TimeWindow.from_values("2026-08-24", "2026-08-25"))

        self.assertEqual(12, result["summary"]["events"])
        self.assertGreater(result["summary"]["queries"], 1)
        self.assertEqual("complete", result["status"])

    def test_detail_failure_marks_partial_but_keeps_the_event(self):
        class FailingInfo(FakeCalendarClient):
            def info(self, event_id):
                from lifeos_calendar.client import CalendarClientError
                raise CalendarClientError("temporary_dependency_failure", "boom")

        result = CalendarService(FailingInfo()).scan(TimeWindow.from_values("2026-08-24", "2026-08-25"))
        self.assertEqual("partial", result["status"])
        self.assertEqual(3, result["summary"]["events"])
        self.assertIn("detail_unavailable:temporary_dependency_failure", result["warnings"])
        self.assertEqual([], result["events"][0]["attendees"])


class CalendarStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CalendarStore(Path(self.temporary.name) / "lifeos" / "calendar")
        self.window = TimeWindow.from_values("2026-08-24", "2026-08-25")

    def tearDown(self):
        self.temporary.cleanup()

    def test_rescan_of_same_window_adds_no_second_snapshot(self):
        result = CalendarService(FakeCalendarClient()).scan(self.window)
        first = self.store.write_scan(result)
        second = self.store.write_scan(result)

        self.assertNotEqual(first["scan_id"], second["scan_id"])
        self.assertEqual(3, self.store.usage()["events"])
        self.assertEqual(2, self.store.usage()["scans"])
        self.assertEqual([], self.store.validate())

    def test_index_marks_time_overlap_and_series_rules(self):
        self.store.write_scan(CalendarService(FakeCalendarClient()).scan(self.window))
        self.store.set_series("uid-weekly", "attend", "国际化行政固定周会")
        values = self.window.to_dict()

        payload = build_index(self.store, values["from"], values["to"])

        by_id = {item["event_id"]: item for item in payload["events"]}
        self.assertEqual([by_id["E-ROOM"]["instance_id"]], by_id["E-REVIEW"]["overlaps"])
        self.assertEqual([], by_id["E-WEEKLY"]["overlaps"])
        self.assertEqual("attend", by_id["E-WEEKLY"]["series_rule"])
        self.assertIsNone(by_id["E-REVIEW"]["series_rule"])
        self.assertEqual({"events": 3, "attend": 1, "skip": 0, "unlisted": 2}, payload["summary"])

        self.store.set_series("uid-weekly", None)
        self.assertEqual({}, self.store.series_rules())
        self.assertEqual(2, len(self.store.series_path.read_text(encoding="utf-8").splitlines()))

    def test_index_without_scan_reports_unknown_source(self):
        values = self.window.to_dict()
        payload = build_index(self.store, values["from"], values["to"])
        self.assertEqual("unknown", payload["source_status"])
        self.assertEqual([], payload["events"])


class CalendarCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "lifeos"
        self.environment = os.environ.copy()
        self.environment["LIFEOS_HOME"] = str(self.data_dir)
        self.environment["LIFEOS_CONFIG"] = str(Path(self.temporary.name) / "config.json")
        fixture = Path(self.temporary.name) / "fixture.json"
        fixture.write_text(json.dumps({"search": SEARCH_EVENTS, "details": DETAILS}), encoding="utf-8")
        self.wrapper = Path(self.temporary.name) / "fake-dws.sh"
        self.wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"FIXTURE='{fixture}'\n"
            "if [[ \"$1\" == --output ]]; then shift 2; fi\n"
            "if [[ \"$1\" == calendar && \"$2\" == search ]]; then\n"
            "  python3 -c \"import json,sys; d=json.load(open('$FIXTURE')); print(json.dumps({'ok': True, 'data': d['search']}))\"\n"
            "elif [[ \"$1\" == calendar && \"$2\" == info ]]; then\n"
            "  python3 -c \"import json,sys; d=json.load(open('$FIXTURE')); print(json.dumps({'ok': True, 'data': d['details'][sys.argv[1]]}))\" \"$3\"\n"
            "else\n"
            "  echo '{\"ok\": false, \"error\": {\"message\": \"unsupported\"}}'\n"
            "fi\n",
            encoding="utf-8",
        )
        self.data_dir.mkdir(parents=True)
        config = default_payload()
        config["modules"]["dchat"] = {"enabled": True, "dws_wrapper": str(self.wrapper)}
        Path(self.environment["LIFEOS_CONFIG"]).write_text(json.dumps(config), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_lifeos(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment, check=check, text=True, capture_output=True,
        )

    def test_validate_reports_the_disabled_source_without_creating_runtime(self):
        result = self.run_lifeos("calendar", "validate", "--json", check=False)
        self.assertEqual(1, result.returncode)
        self.assertEqual("config", json.loads(result.stdout)["findings"][0]["scope"])
        self.assertFalse((self.data_dir / "calendar").exists())
        capabilities = json.loads(self.run_lifeos("capabilities", "--json").stdout)
        self.assertEqual("disabled", capabilities["modules"]["calendar"]["status"])

    def test_public_cli_configure_scan_index_series_usage_and_validate(self):
        configured = json.loads(self.run_lifeos("calendar", "configure", "--json").stdout)
        self.assertTrue(configured["changed"])
        capabilities = json.loads(self.run_lifeos("capabilities", "--json").stdout)
        self.assertEqual("ready", capabilities["modules"]["calendar"]["status"])

        scanned = json.loads(self.run_lifeos(
            "calendar", "scan", "--from", "2026-08-24", "--to", "2026-08-25", "--json",
        ).stdout)
        self.assertEqual("complete", scanned["status"])
        self.assertEqual(3, scanned["summary"]["events"])

        indexed = json.loads(self.run_lifeos(
            "calendar", "index", "--from", "2026-08-24", "--to", "2026-08-25", "--json",
        ).stdout)
        self.assertEqual(scanned["scan_id"], indexed["source_scan_id"])
        weekly = next(item for item in indexed["events"] if item["event_id"] == "E-WEEKLY")
        self.assertIsNone(weekly["series_rule"])

        self.run_lifeos("calendar", "series", "set", "--series", weekly["series_key"], "--rule", "skip")
        listed = json.loads(self.run_lifeos("calendar", "series", "list", "--json").stdout)
        self.assertEqual([{"series_key": "uid-weekly", "rule": "skip", "title": "", "at": listed["series"][0]["at"]}], listed["series"])
        indexed = json.loads(self.run_lifeos(
            "calendar", "index", "--from", "2026-08-24", "--to", "2026-08-25", "--json",
        ).stdout)
        self.assertEqual("skip", next(item for item in indexed["events"] if item["event_id"] == "E-WEEKLY")["series_rule"])

        scans = json.loads(self.run_lifeos("calendar", "scans", "--json").stdout)
        self.assertEqual(1, scans["total"])
        usage = json.loads(self.run_lifeos("calendar", "usage", "--json").stdout)
        self.assertEqual(3, usage["events"])
        self.assertTrue(json.loads(self.run_lifeos("calendar", "validate", "--json").stdout)["ok"])

        # The daily report accepts the instance ids only together with the scan id.
        self.run_lifeos("reports", "begin", "--day", "2026-08-24")
        body = Path(self.temporary.name) / "body.md"
        body.write_text("# 当日概览\n\n合成正文。\n\n## 会议\n\n- 11:00–12:00 空闲回收需求评审 — 国内会议室\n", encoding="utf-8")
        ids = [item["instance_id"] for item in indexed["events"]]
        rejected = self.run_lifeos(
            "reports", "write", "--day", "2026-08-24", "--body-file", str(body),
            "--calendar-event-id", ids[0], check=False,
        )
        self.assertEqual(1, rejected.returncode)
        written = json.loads(self.run_lifeos(
            "reports", "write", "--day", "2026-08-24", "--body-file", str(body),
            "--calendar-scan-id", scanned["scan_id"], *sum([["--calendar-event-id", value] for value in ids], []),
            "--json",
        ).stdout)
        self.assertEqual(3, written["calendar_events"])
        report = (self.data_dir / "reports" / "daily" / "2026-08-24.md").read_text(encoding="utf-8")
        self.assertIn(f"calendar_scan_id: {scanned['scan_id']}", report)
        self.assertIn("calendar_events: 3", report)
        self.assertTrue(json.loads(self.run_lifeos("reports", "validate", "--json").stdout)["ok"])

    def test_report_without_calendar_keeps_old_frontmatter_shape(self):
        self.run_lifeos("reports", "begin", "--day", "2026-08-24")
        body = Path(self.temporary.name) / "body.md"
        body.write_text("# 当日概览\n\n合成正文。\n", encoding="utf-8")
        self.run_lifeos("reports", "write", "--day", "2026-08-24", "--body-file", str(body))
        report = (self.data_dir / "reports" / "daily" / "2026-08-24.md").read_text(encoding="utf-8")
        self.assertNotIn("calendar_", report)
        self.assertTrue(json.loads(self.run_lifeos("reports", "validate", "--json").stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
