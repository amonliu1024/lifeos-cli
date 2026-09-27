import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lifeos import VERSION
from lifeos_config.core import default_payload
from lifeos_work.config import TIMEZONE, entry_field_order
from lifeos_work.runtime import WorkTransaction
from lifeos_work.views import render_brief


REPO_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "runtime"
SCRIPT = REPO_DIR / "lifeos.py"
CHANGELOG = REPO_DIR / "CHANGELOG.md"
RETIRED_COMMANDS = (
    "work-items", "work-item-add", "work-item-update", "work-item-milestones",
    "work-item-milestone-add", "work-item-milestone-update", "tasks",
    "task-update", "task-close", "task-reflect", "task-start", "ideas",
    "idea-add", "idea-update", "achievements", "achievement-add",
    "achievement-update", "achievement-archive", "achievement-supersede",
    "migrate-project-catalog",
)


def private_config(path, roots):
    config = default_payload()
    config["modules"]["projects"]["roots"] = [str(root) for root in roots]
    path.write_text(json.dumps(config), encoding="utf-8")
    path.chmod(0o600)


class CLITestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        for path in FIXTURE_DIR.iterdir():
            if path.is_file():
                shutil.copy2(path, self.data_dir / path.name)
                (self.data_dir / path.name).chmod(0o600)
        self.data_dir.chmod(0o700)
        (self.data_dir / "backups").mkdir(mode=0o700)
        self.environment = os.environ.copy()
        self.environment["LIFEOS_HOME"] = str(self.data_dir)
        config_path = self.data_dir / "config.json"
        private_config(config_path, [self.data_dir])
        self.environment["LIFEOS_CONFIG"] = str(config_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "work", *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def run_root_cli(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def load_json(self, name):
        return json.loads((self.data_dir / name).read_text(encoding="utf-8"))

    def entry(self, entry_id):
        return next(
            item for item in self.load_json("entries.json")["entries"]
            if item["id"] == entry_id
        )

    def events(self):
        return [
            json.loads(line)
            for line in (self.data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def created_id(self, result):
        return result.stdout.split("：", 1)[0].split()[-1]

    def validate_runtime(self):
        return self.run_cli("validate")

    def snapshot_state(self):
        return {
            name: (self.data_dir / name).read_bytes()
            for name in ("entries.json", "events.jsonl", "now.md", "insights.md")
        }


class WorkRuntimeInitTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name) / "runtime"
        self.environment = os.environ.copy()
        self.environment["LIFEOS_HOME"] = str(self.data_dir)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "work", *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def test_init_creates_current_runtime_and_never_overwrites(self):
        for arguments in (
            ("--self-name", "   ", "--source", "本人确认"),
            ("--self-name", "测试用户", "--self-alias", "", "--source", "本人确认"),
        ):
            invalid = self.run_cli("init", *arguments, check=False)
            self.assertEqual(2, invalid.returncode)
            self.assertIn("内容不能为空", invalid.stderr)
            self.assertFalse(self.data_dir.exists())

        created = self.run_cli(
            "init",
            "--self-name", "测试用户",
            "--self-alias", "sample-user",
            "--source", "本人确认",
            "--actor-kind", "user",
            "--actor-name", "测试用户",
        )
        self.assertIn("已初始化", created.stdout)
        for name, collection in (("projects.json", "projects"), ("entries.json", "entries")):
            payload = json.loads((self.data_dir / name).read_text(encoding="utf-8"))
            self.assertEqual({"updated_at", collection}, set(payload))
            self.assertEqual([], payload[collection])
        for retired in ("tasks.json", "work-items.json", "ideas.json", "achievements.json"):
            self.assertFalse((self.data_dir / retired).exists())
        glossary = json.loads((self.data_dir / "glossary.json").read_text(encoding="utf-8"))
        self.assertNotIn("schema_version", glossary)
        self.assertEqual("ENT-SELF", glossary["terms"][0]["id"])
        self.assertEqual(["sample-user"], glossary["terms"][0]["aliases"])
        self.run_cli("validate")
        self.assertEqual(0o700, self.data_dir.stat().st_mode & 0o777)
        for path in self.data_dir.iterdir():
            if path.is_file():
                self.assertEqual(0o600, path.stat().st_mode & 0o777, path.name)

        self.assertNotIn("--idempotency-key", self.run_cli("init", "--help").stdout)
        before = (self.data_dir / "glossary.json").read_bytes()
        repeated = self.run_cli(
            "init", "--self-name", "另一用户", "--source", "重复初始化", check=False,
        )
        self.assertNotEqual(0, repeated.returncode)
        self.assertIn("不会覆盖", repeated.stderr)
        self.assertEqual(before, (self.data_dir / "glossary.json").read_bytes())

    def test_validate_rejects_blank_persisted_self_identity(self):
        self.run_cli("init", "--self-name", "测试用户", "--source", "本人确认")
        glossary_path = self.data_dir / "glossary.json"
        payload = json.loads(glossary_path.read_text(encoding="utf-8"))
        payload["terms"][0]["name"] = "   "
        glossary_path.write_text(json.dumps(payload), encoding="utf-8")
        invalid = self.run_cli("validate", check=False)
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("ENT-SELF 必须是具备规范名称的本人实体", invalid.stderr)

    def test_validate_detects_and_refresh_repairs_permission_drift(self):
        self.run_cli("init", "--self-name", "测试用户", "--source", "本人确认")
        lock_path = self.data_dir / ".lifeos.lock"
        facts_path = self.data_dir / "entries.json"
        self.data_dir.chmod(0o755)
        lock_path.chmod(0o644)
        facts_path.chmod(0o644)

        invalid = self.run_cli("validate", check=False)
        self.assertEqual(1, invalid.returncode)
        self.assertIn("Runtime 目录权限必须为 0700", invalid.stderr)
        self.assertIn("Work 锁文件权限必须为 0600", invalid.stderr)
        self.assertIn("entries.json", invalid.stderr)

        self.run_cli("refresh")
        self.run_cli("validate")
        self.assertEqual(0o600, facts_path.stat().st_mode & 0o777)

    def test_validate_rejects_unfinished_transaction_marker(self):
        self.run_cli("init", "--self-name", "测试用户", "--source", "本人确认")
        (self.data_dir / ".work-transaction-synthetic").mkdir(mode=0o700)
        invalid = self.run_cli("validate", check=False)
        self.assertEqual(1, invalid.returncode)
        self.assertIn("发现未完成的 Work 事务", invalid.stderr)


class WorkTransactionFailureTest(unittest.TestCase):
    def _commit_with_failure(self, data_dir, targets, *, failing_path=None, failing_event=False):
        entries_path = data_dir / "entries.json"
        glossary_path = data_dir / "glossary.json"
        events_path = data_dir / "events.jsonl"
        view_path = data_dir / "now.md"
        entries_path.write_text("old entries\n", encoding="utf-8")
        glossary_path.write_text("old glossary\n", encoding="utf-8")
        events_path.write_text("old event\n", encoding="utf-8")
        view_path.write_text("old view\n", encoding="utf-8")
        current = [{}, {"changed": True}, {"changed": "glossary" in targets}]
        transaction = WorkTransaction(SimpleNamespace(), current, [])
        transaction.original[1] = {"changed": False}
        transaction.original[2] = {"changed": False}
        writes = []

        from lifeos_work import runtime as work_runtime

        original_atomic_write_json = work_runtime.atomic_write_json

        def failing_write(path, value):
            writes.append(path)
            if path == failing_path:
                raise OSError("synthetic write failure")
            return original_atomic_write_json(path, value)

        stderr = io.StringIO()
        with (
            patch(
                "lifeos_work.runtime.CURRENT_TARGETS",
                {"entries": (entries_path, 1), "glossary": (glossary_path, 2)},
            ),
            patch("lifeos_work.runtime.DATA_DIR", data_dir),
            patch("lifeos_work.runtime.LOCK_PATH", data_dir / ".lock"),
            patch("lifeos_work.runtime.EVENTS_PATH", events_path),
            patch("lifeos_work.runtime.current_data_errors", return_value=[]),
            patch("lifeos_work.runtime.project_registry_errors", return_value=[]),
            patch("lifeos_work.runtime.atomic_write_json", side_effect=failing_write),
            patch(
                "lifeos_work.runtime.current_view_contents",
                return_value={view_path: "new view\n"},
            ),
            patch(
                "lifeos_work.runtime.append_event",
                side_effect=OSError("synthetic event failure") if failing_event else None,
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            transaction.commit(targets, {"event_id": "EVT-TEST", "summary": "synthetic"})
        self.assertIn("已恢复事务前状态", stderr.getvalue())
        self.assertEqual(b"old entries\n", entries_path.read_bytes())
        self.assertEqual(b"old glossary\n", glossary_path.read_bytes())
        self.assertEqual(b"old event\n", events_path.read_bytes())
        self.assertEqual(b"old view\n", view_path.read_bytes())
        self.assertFalse(list(data_dir.glob(".work-transaction-*")))
        return writes

    def test_multi_target_failure_restores_runtime_and_keeps_audit_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            writes = self._commit_with_failure(
                data_dir,
                ("entries", "glossary"),
                failing_path=data_dir / "glossary.json",
            )
            self.assertIn(data_dir / "entries.json", writes)
            self.assertTrue(any((data_dir / "backups").iterdir()))

    def test_single_target_event_failure_restores_fact_view_and_event_log(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            self._commit_with_failure(
                Path(temporary_directory), "entries", failing_event=True
            )


class PublicInterfaceTest(unittest.TestCase):
    def run_root_cli(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    def test_version_source_cli_and_latest_release_are_consistent(self):
        self.assertEqual(
            f"LifeOS v{VERSION}",
            self.run_root_cli("--version").stdout.strip(),
        )
        changelog = CHANGELOG.read_text(encoding="utf-8")
        match = re.search(r"^## v([^\s]+)\s*$", changelog, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(VERSION, match.group(1))
        self.assertLess(changelog.index("## Unreleased"), changelog.index(f"## v{VERSION}"))

    def test_no_arguments_show_home_with_the_current_slogan(self):
        result = self.run_root_cli()
        self.assertEqual("", result.stderr)
        self.assertEqual(
            [
                "╭────╴  ╶──╮",
                f"│ ╭─────╴  │   LifeOS v{VERSION}",
                "│ │ ╭──╮   │",
                "│ │ ╵  ╵ ╷ │   做过的有记录，",
                "│ ╰──────╯ │   想明白的留下来。",
                "╰──────────╯",
            ],
            result.stdout.splitlines()[:6],
        )
        self.assertIn("lifeos --help", result.stdout)

    def test_work_help_lists_entry_commands_and_retires_the_old_model(self):
        work_help = self.run_root_cli("work", "--help").stdout
        for command in (
            "task-add", "note-add", "question-add", "insight-add", "entry-update",
            "task-done", "task-schedule", "entry-drop", "entry-keep",
        ):
            self.assertIn(command, work_help)
        for phrase in (
            "--source 是本次记录的事实来源",
            "--idempotency-key 是重试时使用的稳定键",
            "--due 是结果硬截止",
            "7 天内到期算紧急",
        ):
            self.assertIn(phrase, work_help)
        commands = set(re.findall(r"^    ([a-z0-9-]+)\s", work_help, re.MULTILINE))
        for retired in RETIRED_COMMANDS:
            self.assertNotIn(retired, commands)

    def test_help_text_never_uses_the_word_commitment(self):
        texts = [self.run_root_cli("--help").stdout, self.run_root_cli("work", "--help").stdout]
        work_help = texts[1]
        for command in re.findall(r"^    ([a-z0-9-]+)\s", work_help, re.MULTILINE):
            texts.append(self.run_root_cli("work", command, "--help").stdout)
        for text in texts:
            self.assertNotIn("承诺", text)


def synthetic_entry(kind, entry_id, **fields):
    entry = {field: None for field in entry_field_order(kind)}
    entry.update(
        {
            "id": entry_id,
            "kind": kind,
            "text": entry_id,
            "status": "open",
            "created_at": "2026-09-01T10:00:00+08:00",
            "updated_at": "2026-09-20T10:00:00+08:00",
        }
    )
    if kind in {"task", "question"}:
        entry["starred"] = False
    if kind == "insight":
        entry["context"] = "测试来由"
    entry.update(fields)
    return entry


class BriefViewTest(unittest.TestCase):
    PROJECTS = {"projects": [{"project_key": "brazil", "name": "Brazil 一期"}]}
    REFERENCE = date(2026, 9, 26)

    def brief(self, entries, mode="current", reference=None):
        return render_brief(
            self.PROJECTS, {"entries": entries}, mode, reference or self.REFERENCE
        )

    def test_current_brief_orders_my_tasks_by_importance_then_urgency(self):
        entries = [
            synthetic_entry("task", "TASK-20260901-004", text="无星无截止"),
            synthetic_entry("task", "TASK-20260901-003", text="无星已逾期", due="2026-09-20"),
            synthetic_entry("task", "TASK-20260901-002", text="有星无截止", starred=True),
            synthetic_entry(
                "task", "TASK-20260901-001", text="有星明天截止", starred=True,
                due="2026-09-27", project="brazil",
            ),
        ]
        output = self.brief(entries)
        positions = [
            output.index(text) for text in ("有星明天截止", "有星无截止", "无星已逾期", "无星无截止")
        ]
        self.assertEqual(sorted(positions), positions)
        headings = ["★ 重要且紧急", "★ 重要不紧急", "⏰ 紧急", "· 其余"]
        heading_positions = [output.index(heading) for heading in headings]
        self.assertEqual(sorted(heading_positions), heading_positions)
        self.assertLess(heading_positions[0], positions[0])
        self.assertLess(positions[0], heading_positions[1])
        self.assertIn("有星明天截止 · Brazil 一期 · `明天到期`", output)
        self.assertIn("无星已逾期 · `已逾期 6 天`", output)

    def test_seven_days_is_the_urgency_boundary(self):
        entries = [
            synthetic_entry("task", "TASK-20260901-001", text="第七天", due="2026-10-03"),
            synthetic_entry("task", "TASK-20260901-002", text="第八天", due="2026-10-04"),
        ]
        output = self.brief(entries)
        urgent = output[output.index("⏰ 紧急"):output.index("· 其余")]
        self.assertIn("第七天", urgent)
        self.assertNotIn("第八天", urgent)

    def test_tasks_owned_by_others_form_the_waiting_group(self):
        entries = [
            synthetic_entry("task", "TASK-20260901-001", text="我的待办"),
            synthetic_entry(
                "task", "TASK-20260901-002", text="提供每小时热榜推送", owner="凯健",
                due="2026-09-30",
            ),
            synthetic_entry("question", "ASK-20260901-001", text="普通问题"),
            synthetic_entry("question", "ASK-20260901-002", text="重要问题", starred=True),
            synthetic_entry("note", "NOTE-20260901-001", text="一个点子"),
            synthetic_entry("note", "NOTE-20260901-002", text="已划掉", status="dropped", note="不做"),
        ]
        output = self.brief(entries)
        quadrants = output[:output.index("⏳ 等别人")]
        self.assertIn("我的待办", quadrants)
        self.assertNotIn("提供每小时热榜推送", quadrants)
        self.assertIn("- 提供每小时热榜推送 · 👤 凯健 · `4 天后到期`", output)
        self.assertIn("待办 **1** 条 · 等别人 **1**", output)
        self.assertEqual(3, output.count("（无）"))
        self.assertLess(output.index("重要问题"), output.index("普通问题"))
        self.assertIn("- 一个点子", output)
        self.assertNotIn("已划掉", output)

    def test_scheduled_tasks_stay_hidden_until_their_month(self):
        task = synthetic_entry(
            "task", "TASK-20260901-001", text="排到十一月", status="scheduled", month="2026-11",
        )
        self.assertNotIn("排到十一月", self.brief([task], reference=date(2026, 10, 15)))
        self.assertNotIn("排到十一月", self.brief([task], "monthly", reference=date(2026, 10, 15)))
        self.assertIn("排到十一月", self.brief([task], reference=date(2026, 11, 2)))
        monthly = self.brief([task], "monthly", reference=date(2026, 11, 2))
        self.assertIn("排到的月份已到", monthly)
        self.assertIn("`TASK-20260901-001` • 排到十一月", monthly)

    def test_monthly_check_lists_stale_entries_and_stars_not_touched_this_month(self):
        entries = [
            synthetic_entry("task", "TASK-20260901-001", text="很久没动", updated_at="2026-08-20T10:00:00+08:00"),
            synthetic_entry("note", "NOTE-20260901-001", text="很久的点子", updated_at="2026-08-01T10:00:00+08:00"),
            synthetic_entry(
                "task", "TASK-20260901-003", text="重要的事", starred=True,
                updated_at="2026-08-30T10:00:00+08:00",
            ),
            synthetic_entry("task", "TASK-20260901-004", text="刚动过"),
            synthetic_entry("insight", "INS-20260901-001", text="有效洞见", updated_at="2026-01-01T10:00:00+08:00"),
        ]
        output = self.brief(entries, "monthly")
        stale = output[output.index("💤"):output.index("★ 星标还成立吗")]
        self.assertIn("很久没动", stale)
        self.assertIn("很久的点子", stale)
        self.assertNotIn("刚动过", output)
        self.assertNotIn("有效洞见", output)
        self.assertIn("重要的事", output[output.index("★ 星标还成立吗"):])
        touched = self.brief(
            [synthetic_entry("task", "TASK-20260901-003", text="重要的事", starred=True)],
            "monthly",
        )
        self.assertEqual("本月盘点没有要处理的。\n", touched)

    def test_reminder_and_closeout_only_look_at_hard_deadlines(self):
        entries = [
            synthetic_entry("task", "TASK-20260901-001", text="逾期", due="2026-09-20"),
            synthetic_entry("task", "TASK-20260901-002", text="五天后", due="2026-10-01"),
            synthetic_entry("task", "TASK-20260901-003", text="很远", due="2026-12-01"),
        ]
        reminder = self.brief(entries, "reminder")
        self.assertIn("逾期", reminder)
        self.assertIn("五天后", reminder)
        self.assertNotIn("很远", reminder)
        closeout = self.brief(entries, "closeout")
        self.assertIn("总览：**1** 条结果逾期", closeout)
        self.assertNotIn("五天后", closeout)


class EntryWorkflowTest(CLITestCase):
    def setUp(self):
        super().setUp()
        self.validate_runtime()

    def add(self, command, *arguments):
        return self.created_id(self.run_cli(command, *arguments))

    def test_a_note_is_not_a_task_until_it_is_converted(self):
        note_id = self.add("note-add", "--text", "想给国际行政做支宣传片")
        self.assertTrue(note_id.startswith("NOTE-"))
        brief = self.run_cli("brief", "--mode", "current").stdout
        self.assertIn("想给国际行政做支宣传片", brief[brief.index("– 随记"):])
        self.assertNotIn("想给国际行政做支宣传片", brief[:brief.index("– 随记")])

        before_events = len(self.events())
        task_id = self.add("task-add", "--text", "做一支宣传片", "--from", note_id, "--source", "本人确认")
        note = self.entry(note_id)
        self.assertEqual(("converted", task_id), (note["status"], note["ref"]))
        self.assertEqual("open", self.entry(task_id)["status"])
        events = self.events()
        self.assertEqual(before_events + 1, len(events))
        self.assertEqual((task_id, note_id), (events[-1]["entry_id"], events[-1]["related_entry_id"]))
        self.assertEqual("本人确认", events[-1]["sources"][0])

        again = self.run_cli("question-add", "--text", "再转一次", "--from", note_id, "--source", "x", check=False)
        self.assertIn("已经了结", again.stderr)
        self.validate_runtime()

    def test_every_entry_has_exactly_the_agreed_fields(self):
        ids = {
            "task": self.add("task-add", "--text", "一条待办", "--source", "x"),
            "note": self.add("note-add", "--text", "一条随记"),
            "question": self.add("question-add", "--text", "一条疑问", "--source", "x"),
            "insight": self.add("insight-add", "--text", "一条洞见", "--context", "来由", "--source", "x"),
        }
        common = ["id", "kind", "text", "project", "status", "note", "ref"]
        trailing = ["context", "created_at", "updated_at"]
        expected = {
            "task": [*common, "starred", "owner", "due", "month", *trailing],
            "note": [*common, *trailing],
            "question": [*common, "starred", *trailing],
            "insight": [*common, *trailing],
        }
        for kind, entry_id in ids.items():
            self.assertEqual(expected[kind], list(self.entry(entry_id)), kind)

    def test_notes_are_required_where_a_status_needs_explaining(self):
        task_id = self.add("task-add", "--text", "要做的事", "--source", "x")
        before = self.snapshot_state()
        for arguments in (
            ("task-done", task_id, "--source", "x"),
            ("entry-drop", task_id, "--source", "x"),
            ("insight-add", "--text", "没有来由", "--source", "x"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, check=False)
                self.assertEqual(2, result.returncode)
                self.assertIn("required", result.stderr)
        refused = self.run_cli("task-schedule", task_id, "--month", "2020-01", "--source", "x", check=False)
        self.assertIn("以后的月份", refused.stderr)
        self.assertEqual(before, self.snapshot_state())

        entries = json.loads((self.data_dir / "entries.json").read_text(encoding="utf-8"))
        target = next(item for item in entries["entries"] if item["id"] == task_id)
        target["status"] = "done"
        (self.data_dir / "entries.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        invalid = self.run_cli("validate", check=False)
        self.assertIn("当前状态必须写 note", invalid.stderr)

    def test_status_changes_keep_earlier_notes_in_the_history(self):
        task_id = self.add("task-add", "--text", "先排后做", "--source", "x")
        next_month = (datetime.now(TIMEZONE).date().replace(day=1) + timedelta(days=40)).strftime("%Y-%m")
        self.run_cli("task-schedule", task_id, "--month", next_month, "--note", "先等试运行", "--source", "x")
        self.assertNotIn("先排后做", self.run_cli("brief", "--mode", "current").stdout)
        self.run_cli("entry-update", task_id, "--reopen", "--source", "x")
        task = self.entry(task_id)
        self.assertEqual(("open", None), (task["status"], task["month"]))
        self.run_cli("task-done", task_id, "--note", "已交付", "--source", "验收邮件")
        task = self.entry(task_id)
        self.assertEqual(("done", "已交付"), (task["status"], task["note"]))

        transitions = [
            (event.get("status_from"), event.get("status_to"), event.get("note"))
            for event in self.events()
            if event.get("entry_id") == task_id and event.get("status_to")
        ]
        self.assertEqual(
            [
                (None, "open", None),
                ("open", "scheduled", "先等试运行"),
                ("scheduled", "open", None),
                ("open", "done", "已交付"),
            ],
            transitions,
        )
        shown = self.run_cli("show", task_id).stdout
        self.assertIn("历史：", shown)
        self.assertIn("open → scheduled", shown)
        refused = self.run_cli("entry-update", task_id, "--text", "改一下", check=False)
        self.assertIn("不能再修改", refused.stderr)

        entries = self.load_json("entries.json")
        next(item for item in entries["entries"] if item["id"] == task_id)["updated_at"] = "2026-01-05T18:00:00+08:00"
        (self.data_dir / "entries.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        self.run_cli("entry-update", task_id, "--context", "客户要的", "--note", "已交付给客户", "--source", "x")
        task = self.entry(task_id)
        self.assertEqual(
            ("done", "客户要的", "已交付给客户", "2026-01-05T18:00:00+08:00"),
            (task["status"], task["context"], task["note"], task["updated_at"]),
        )
        self.assertEqual("entry_updated", self.events()[-1]["kind"])

    def test_deadline_changes_keep_their_own_history(self):
        task_id = self.add("task-add", "--text", "有截止的事", "--due", "2026-12-01", "--source", "x")
        postponed = self.run_cli("task-reschedule", task_id, "--due", "2026-12-20", "--source", "x", check=False)
        self.assertIn("--reason-code", postponed.stderr)
        self.run_cli(
            "task-reschedule", task_id, "--due", "2026-12-20", "--reason-code", "priority_changed", "--source", "x"
        )
        self.assertEqual("2026-12-20", self.entry(task_id)["due"])
        history = json.loads(self.run_cli("task-schedule-history", task_id, "--json").stdout)
        self.assertEqual("2026-12-01", history[0]["due"])
        self.assertEqual("postponed", history[1]["schedule_changes"][0]["direction"])

    def test_questions_and_insights(self):
        question_id = self.add("question-add", "--text", "管理员能不能绕过部门范围", "--star", "--source", "x")
        insight_id = self.add(
            "insight-add", "--text", "管理员自己配的约束，不为管理员开例外",
            "--context", "工位绑定时决定管理员也受部门范围限制", "--answers", question_id,
            "--source", "本人确认",
        )
        question = self.entry(question_id)
        self.assertEqual(("done", insight_id), (question["status"], question["ref"]))
        self.assertEqual("管理员自己配的约束，不为管理员开例外", question["note"])

        self.run_cli("entry-update", insight_id, "--note", "已写进 CLAUDE.md 实现原则", "--source", "x")
        self.assertEqual("open", self.entry(insight_id)["status"])
        newer = self.add("insight-add", "--text", "新判断", "--context", "后来的事实", "--source", "x")
        self.run_cli("entry-drop", insight_id, "--note", "被新判断取代", "--by", newer, "--source", "x")
        old = self.entry(insight_id)
        self.assertEqual(("dropped", newer, "被新判断取代"), (old["status"], old["ref"], old["note"]))
        insights_view = (self.data_dir / "insights.md").read_text(encoding="utf-8")
        self.assertIn("新判断", insights_view)
        self.assertNotIn("不为管理员开例外", insights_view)
        refused = self.run_cli("entry-drop", question_id, "--note", "x", "--by", newer, "--source", "x", check=False)
        self.assertIn("已经了结", refused.stderr)

    def test_keep_touches_the_entry_and_stars_follow_the_kind(self):
        task_id = self.add("task-add", "--text", "重要的事", "--star", "--source", "x")
        same_day = self.run_cli("entry-keep", task_id, check=False)
        self.assertIn("今天已经动过", same_day.stderr)
        entries = self.load_json("entries.json")
        next(item for item in entries["entries"] if item["id"] == task_id)["updated_at"] = "2026-08-01T10:00:00+08:00"
        (self.data_dir / "entries.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        self.run_cli("entry-keep", task_id)
        self.assertEqual(datetime.now(TIMEZONE).date().isoformat(), self.entry(task_id)["updated_at"][:10])
        self.assertEqual("entry_kept", self.events()[-1]["kind"])
        note_id = self.add("note-add", "--text", "点子")
        refused = self.run_cli("entry-update", note_id, "--star", check=False)
        self.assertIn("只有待办和疑问可以标星", refused.stderr)

    def test_projects_are_referenced_by_key(self):
        unknown = self.run_cli("task-add", "--text", "挂错项目", "--project", "nope", "--source", "x", check=False)
        self.assertIn("没有跟踪这个项目", unknown.stderr)
        projects = self.load_json("projects.json")
        projects["projects"].append(
            {"project_key": "sample", "tracking_state": "active", "status_reason": None,
             "updated_at": "2026-09-01T10:00:00+08:00"}
        )
        (self.data_dir / "projects.json").write_text(json.dumps(projects), encoding="utf-8")
        task_id = self.add("task-add", "--text", "挂对项目", "--project", "sample", "--source", "x")
        self.assertEqual("sample", self.entry(task_id)["project"])
        found = json.loads(self.run_cli("entries", "--project", "sample", "--json").stdout)
        self.assertEqual([task_id], [item["id"] for item in found])
        self.run_cli("project-update", "sample", "--tracking-state", "paused", "--reason", "先停", "--source", "x")
        self.assertEqual("paused", self.load_json("projects.json")["projects"][0]["tracking_state"])

    def test_idempotency_key_prevents_duplicate_entries(self):
        for _ in range(2):
            self.run_cli("note-add", "--text", "只记一次", "--idempotency-key", "note:once")
        notes = [item for item in self.load_json("entries.json")["entries"] if item["text"] == "只记一次"]
        self.assertEqual(1, len(notes))

    def test_term_rename_updates_owner_names(self):
        term_id = self.add(
            "term-add", "--name", "凯健", "--kind", "person", "--description", "合成人员", "--source", "x"
        )
        task_id = self.add("task-add", "--text", "他负责", "--owner", "凯健", "--source", "x")
        done_id = self.add("task-add", "--text", "他交付过的", "--owner", "凯健", "--source", "x")
        self.run_cli("task-done", done_id, "--note", "已交付", "--source", "x")
        entries = self.load_json("entries.json")
        next(item for item in entries["entries"] if item["id"] == done_id)["updated_at"] = "2026-01-05T18:00:00+08:00"
        (self.data_dir / "entries.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        self.run_cli("term-update", term_id, "--name", "凯健老师", "--source", "x")
        self.assertEqual("凯健老师", self.entry(task_id)["owner"])
        done = self.entry(done_id)
        self.assertEqual(("凯健老师", "2026-01-05T18:00:00+08:00"), (done["owner"], done["updated_at"]))
        self.validate_runtime()

    def test_entries_query_and_review(self):
        self.add("note-add", "--text", "关于报价的点子")
        self.add("insight-add", "--text", "能上线的九成比上不了线的十成值钱", "--context", "报价封版上线", "--source", "x")
        task_id = self.add("task-add", "--text", "做完的事", "--source", "x")
        self.run_cli("task-done", task_id, "--note", "做完了", "--source", "x")
        found = json.loads(self.run_cli("entries", "--kind", "note", "--query", "报价", "--json").stdout)
        self.assertEqual(["关于报价的点子"], [item["text"] for item in found])
        review = json.loads(
            self.run_cli("review", "--month", datetime.now(TIMEZONE).strftime("%Y-%m"), "--json").stdout
        )
        self.assertEqual(["能上线的九成比上不了线的十成值钱"], [item["text"] for item in review["insights"]])
        self.assertEqual(["做完了"], [item["note"] for item in review["items"]])


class ChangesWindowTest(CLITestCase):
    """`changes --from/--to` is the day-level input a daily report needs."""

    def changes(self, *arguments, check=True):
        result = self.run_cli("changes", "--json", *arguments, check=check)
        return json.loads(result.stdout) if result.returncode == 0 else result

    def test_window_is_half_open(self):
        events = self.changes("--from", "2026-07-25", "--to", "2026-07-29")
        self.assertEqual(["EVT-20260725-001"], [event["event_id"] for event in events])
        events = self.changes("--from", "2026-07-29", "--to", "2026-07-30")
        self.assertEqual(["EVT-20260729-001"], [event["event_id"] for event in events])

    def test_timestamp_boundary_excludes_the_upper_edge(self):
        events = self.changes(
            "--from", "2026-07-25T16:40:00+08:00", "--to", "2026-07-29T01:35:36+08:00"
        )
        self.assertEqual(["EVT-20260725-001"], [event["event_id"] for event in events])

    def test_window_returns_new_entry_events(self):
        self.validate_runtime()
        self.run_cli("note-add", "--text", "窗口测试")
        today = self.events()[-1]["occurred_at"][:10]
        kinds = {event["kind"] for event in self.changes("--from", today)}
        self.assertIn("entry_created", kinds)

    def test_default_behavior_without_a_window_is_unchanged(self):
        self.assertEqual(2, len(self.changes()))
        self.assertEqual(1, len(self.changes("--limit", "1")))

    def test_naive_timestamp_is_rejected(self):
        result = self.run_cli("changes", "--from", "2026-07-25T00:00:00", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("时区偏移", result.stderr)

    def test_inverted_window_is_rejected(self):
        result = self.run_cli("changes", "--from", "2026-07-29", "--to", "2026-07-25", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("--to 必须晚于 --from", result.stderr)

    def test_unreadable_timestamps_are_reported_not_dropped_silently(self):
        events_path = self.data_dir / "events.jsonl"
        events_path.write_text(
            events_path.read_text(encoding="utf-8")
            + json.dumps({"event_id": "EVT-BROKEN", "kind": "x", "occurred_at": "昨天"})
            + "\n",
            encoding="utf-8",
        )
        result = self.run_cli("changes", "--json", "--from", "2026-07-25", "--to", "2026-07-30")
        self.assertNotIn("EVT-BROKEN", result.stdout)
        self.assertIn("occurred_at", result.stderr)


if __name__ == "__main__":
    unittest.main()
