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
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lifeos import VERSION
from lifeos_config.core import default_payload
from lifeos_work.model import brief_date_label
from lifeos_work.runtime import WorkTransaction
from lifeos_work.views import (
    render_achievements,
    render_brief,
    render_glossary,
    render_ideas,
    render_now,
    render_projects,
    render_tasks,
    render_work_items,
)


REPO_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "runtime"
SCRIPT = REPO_DIR / "lifeos.py"
CHANGELOG = REPO_DIR / "CHANGELOG.md"
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
        config = default_payload()
        config["modules"]["projects"]["roots"] = [str(self.data_dir)]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)
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

    def created_id(self, result):
        return result.stdout.split("：", 1)[0].split()[-1]

    def validate_runtime(self):
        return self.run_cli("validate")


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
        for name, collection in (
            ("projects.json", "projects"),
            ("work-items.json", "work_items"),
            ("tasks.json", "tasks"),
            ("ideas.json", "ideas"),
            ("achievements.json", "achievements"),
        ):
            payload = json.loads((self.data_dir / name).read_text(encoding="utf-8"))
            expected_version = 2 if name == "projects.json" else 1
            self.assertEqual(expected_version, payload["schema_version"])
            self.assertEqual([], payload[collection])
        glossary = json.loads((self.data_dir / "glossary.json").read_text(encoding="utf-8"))
        self.assertEqual(1, glossary["schema_version"])
        self.assertEqual("ENT-SELF", glossary["terms"][0]["id"])
        self.assertEqual("测试用户", glossary["terms"][0]["name"])
        self.assertEqual(["sample-user"], glossary["terms"][0]["aliases"])
        self.run_cli("validate")
        self.assertEqual(0o700, self.data_dir.stat().st_mode & 0o777)
        for path in self.data_dir.iterdir():
            if path.is_file():
                self.assertEqual(
                    0o600,
                    path.stat().st_mode & 0o777,
                    f"{path.name} must be owner-only",
                )

        init_help = self.run_cli("init", "--help").stdout
        self.assertNotIn("--idempotency-key", init_help)

        before = (self.data_dir / "glossary.json").read_bytes()
        repeated = self.run_cli(
            "init", "--self-name", "另一用户", "--source", "重复初始化",
            check=False,
        )
        self.assertNotEqual(0, repeated.returncode)
        self.assertIn("不会覆盖", repeated.stderr)
        self.assertEqual(before, (self.data_dir / "glossary.json").read_bytes())

    def test_validate_rejects_blank_persisted_self_identity(self):
        self.run_cli(
            "init", "--self-name", "测试用户", "--self-alias", "sample-user",
            "--source", "本人确认",
        )
        glossary_path = self.data_dir / "glossary.json"
        payload = json.loads(glossary_path.read_text(encoding="utf-8"))
        payload["terms"][0]["name"] = "   "
        glossary_path.write_text(json.dumps(payload), encoding="utf-8")
        invalid_name = self.run_cli("validate", check=False)
        self.assertNotEqual(0, invalid_name.returncode)
        self.assertIn("ENT-SELF 必须是具备规范名称的本人实体", invalid_name.stderr)

        payload["terms"][0]["name"] = "测试用户"
        payload["terms"][0]["aliases"] = ["   "]
        glossary_path.write_text(json.dumps(payload), encoding="utf-8")
        invalid_alias = self.run_cli("validate", check=False)
        self.assertNotEqual(0, invalid_alias.returncode)
        self.assertIn("ENT-SELF aliases 结构非法", invalid_alias.stderr)

    def test_validate_detects_and_refresh_repairs_permission_drift(self):
        self.run_cli(
            "init", "--self-name", "测试用户", "--source", "本人确认"
        )
        lock_path = self.data_dir / ".lifeos.lock"
        facts_path = self.data_dir / "tasks.json"
        self.data_dir.chmod(0o755)
        lock_path.chmod(0o644)
        facts_path.chmod(0o644)

        invalid = self.run_cli("validate", check=False)
        self.assertEqual(1, invalid.returncode)
        self.assertIn("Runtime 目录权限必须为 0700", invalid.stderr)
        self.assertIn("Work 锁文件权限必须为 0600", invalid.stderr)
        self.assertIn("tasks.json", invalid.stderr)

        self.run_cli("refresh")
        self.run_cli("validate")
        self.assertEqual(0o700, self.data_dir.stat().st_mode & 0o777)
        self.assertEqual(0o600, lock_path.stat().st_mode & 0o777)
        self.assertEqual(0o600, facts_path.stat().st_mode & 0o777)

    def test_validate_rejects_unfinished_transaction_marker(self):
        self.run_cli(
            "init", "--self-name", "测试用户", "--source", "本人确认"
        )
        (self.data_dir / ".work-transaction-synthetic").mkdir(mode=0o700)
        invalid = self.run_cli("validate", check=False)
        self.assertEqual(1, invalid.returncode)
        self.assertIn("发现未完成的 Work 事务", invalid.stderr)


class WorkTransactionFailureTest(unittest.TestCase):
    def test_multi_target_failure_restores_runtime_and_keeps_audit_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            tasks_path = data_dir / "tasks.json"
            glossary_path = data_dir / "glossary.json"
            events_path = data_dir / "events.jsonl"
            view_path = data_dir / "now.md"
            tasks_path.write_text("old tasks\n", encoding="utf-8")
            glossary_path.write_text("old glossary\n", encoding="utf-8")
            events_path.write_text("old event\n", encoding="utf-8")
            view_path.write_text("old view\n", encoding="utf-8")
            current = [{}, {}, {"changed": True}, {"changed": True}, {}, {}]
            transaction = WorkTransaction(SimpleNamespace(), current, [])
            transaction.original[2] = {"changed": False}
            transaction.original[3] = {"changed": False}
            writes = []

            from lifeos_work import runtime as work_runtime

            original_atomic_write_json = work_runtime.atomic_write_json

            def failing_write(path, value):
                writes.append(path)
                if path == glossary_path:
                    raise OSError("synthetic glossary write failure")
                return original_atomic_write_json(path, value)

            stderr = io.StringIO()
            with (
                patch(
                    "lifeos_work.runtime.CURRENT_TARGETS",
                    {
                        "tasks": (tasks_path, 2),
                        "glossary": (glossary_path, 3),
                    },
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
                patch("lifeos_work.runtime.append_event"),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                transaction.commit(
                    ("tasks", "glossary"),
                    {"event_id": "EVT-TEST", "summary": "synthetic"},
                )

            self.assertIn(tasks_path, writes)
            self.assertIn(glossary_path, writes)
            self.assertIn("已恢复事务前状态", stderr.getvalue())
            self.assertEqual(b"old tasks\n", tasks_path.read_bytes())
            self.assertEqual(b"old glossary\n", glossary_path.read_bytes())
            self.assertEqual(b"old event\n", events_path.read_bytes())
            self.assertEqual(b"old view\n", view_path.read_bytes())
            self.assertFalse(list(data_dir.glob(".work-transaction-*")))
            self.assertTrue(any((data_dir / "backups").iterdir()))

    def test_single_target_event_failure_restores_fact_view_and_event_log(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            tasks_path = data_dir / "tasks.json"
            events_path = data_dir / "events.jsonl"
            view_path = data_dir / "now.md"
            tasks_path.write_text("old tasks\n", encoding="utf-8")
            events_path.write_text("old event\n", encoding="utf-8")
            view_path.write_text("old view\n", encoding="utf-8")
            current = [{}, {}, {"changed": True}, {}, {}, {}]
            transaction = WorkTransaction(SimpleNamespace(), current, [])
            transaction.original[2] = {"changed": False}

            stderr = io.StringIO()
            with (
                patch(
                    "lifeos_work.runtime.CURRENT_TARGETS",
                    {"tasks": (tasks_path, 2)},
                ),
                patch("lifeos_work.runtime.DATA_DIR", data_dir),
                patch("lifeos_work.runtime.LOCK_PATH", data_dir / ".lock"),
                patch("lifeos_work.runtime.EVENTS_PATH", events_path),
                patch("lifeos_work.runtime.current_data_errors", return_value=[]),
                patch("lifeos_work.runtime.project_registry_errors", return_value=[]),
                patch(
                    "lifeos_work.runtime.current_view_contents",
                    return_value={view_path: "new view\n"},
                ),
                patch(
                    "lifeos_work.runtime.append_event",
                    side_effect=OSError("synthetic event append failure"),
                ),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                transaction.commit(
                    "tasks", {"event_id": "EVT-TEST", "summary": "synthetic"}
                )

            self.assertIn("已恢复事务前状态", stderr.getvalue())
            self.assertEqual(b"old tasks\n", tasks_path.read_bytes())
            self.assertEqual(b"old event\n", events_path.read_bytes())
            self.assertEqual(b"old view\n", view_path.read_bytes())
            self.assertFalse(list(data_dir.glob(".work-transaction-*")))


class PublicInterfaceTest(unittest.TestCase):
    def run_root_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )

    def test_version_source_cli_and_latest_release_are_consistent(self):
        self.assertRegex(
            VERSION,
            r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$",
        )
        self.assertEqual(
            f"LifeOS v{VERSION}",
            self.run_root_cli("--version").stdout.strip(),
        )
        match = re.search(
            r"^## v([^\s]+)\s*$",
            CHANGELOG.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertIsNotNone(match, "CHANGELOG must contain a released version section")
        self.assertEqual(VERSION, match.group(1))
        changelog = CHANGELOG.read_text(encoding="utf-8")
        self.assertLess(
            changelog.index("## Unreleased"),
            changelog.index(f"## v{VERSION}"),
        )

    def test_no_arguments_show_home_instead_of_a_parser_error(self):
        result = self.run_root_cli()

        self.assertEqual("", result.stderr)
        self.assertEqual(
            [
                " ⢀⣠⠀⠀⠀⡀",
                "⣰⢫⢖⣫⣝⡂⠙⣆",
                "⡇⣏⢾⠀⠀⡷⠀⢸",
                "⠹⣜⠦⣄⣠⠴⣣⠏",
                " ⠈⠙⠒⠒⠋⠁",
                " LifeOS",
                f" v{VERSION}",
            ],
            result.stdout.splitlines()[:7],
        )
        self.assertIn("以数据，照见人生。", result.stdout)
        self.assertIn("让行动有迹，让经历成知。", result.stdout)
        self.assertIn("lifeos --help", result.stdout)

    def test_task_schedule_options_follow_the_current_owner(self):
        task_help = self.run_root_cli("work", "task-add", "--help").stdout
        for retained in ("--next-action", "--due", "--completion-criteria"):
            self.assertIn(retained, task_help)

        update_help = self.run_root_cli("work", "task-update", "--help").stdout
        self.assertNotIn("--due", update_help)

        reschedule_help = self.run_root_cli(
            "work", "task-reschedule", "--help"
        ).stdout
        for option in ("--due", "--clear-due", "--reason-code", "--note"):
            self.assertIn(option, reschedule_help)

    def test_work_help_explains_high_consequence_boundaries(self):
        root_help = self.run_root_cli("--help").stdout
        self.assertIn("sessions 只读 Agent 应用来源，维护私有派生索引", root_help)
        self.assertIn("git      只读本地 Git 提交，维护日报辅助证据快照", root_help)
        self.assertIn("reports  日报与周期报的结构和状态", root_help)

        work_help = self.run_root_cli("work", "--help").stdout
        for phrase in (
            "无过滤参数时返回全部记录",
            "刷新派生视图并追加不可变 events.jsonl",
            "--source 是本次记录的事实来源",
            "--idempotency-key 是重试时使用的稳定键",
            "--due 是结果硬截止",
        ):
            self.assertIn(phrase, work_help)

    def test_work_command_help_explains_high_risk_boundaries(self):
        expected = {
            "init": ("全新的当前 Work Runtime", "不导入或覆盖"),
            "work-item-milestone-update": (
                "completed 必须同时具备 --summary、--completion-source 和 --decision",
                "--activate-next",
            ),
            "task-reschedule": (
                "只调整待办的 due_at",
                "--reason-code",
            ),
            "refresh": ("不改变事实 JSON 或 events.jsonl", "派生 Markdown"),
            "validate": ("只读校验", "派生视图"),
        }
        for command, phrases in expected.items():
            help_text = self.run_root_cli("work", command, "--help").stdout
            for phrase in phrases:
                with self.subTest(command=command, phrase=phrase):
                    self.assertIn(phrase, help_text)

class CurrentBriefViewTest(unittest.TestCase):
    def test_current_brief_shows_only_idea_names(self):
        output = render_brief(
            {"work_items": []},
            {"tasks": []},
            {
                "ideas": [
                    {
                        "text": "第一条闪念",
                        "status": "inbox",
                        "context": "第一条不应展示的上下文",
                        "created_at": "2026-08-14T10:00:00+08:00",
                    },
                    {
                        "text": "第二条闪念",
                        "status": "incubating",
                        "context": "第二条不应展示的上下文",
                        "created_at": "2026-08-15T10:00:00+08:00",
                    },
                ]
            },
            "current",
            reference_date=date(2026, 8, 15),
        )

        self.assertIn("💡 闪念\n\n- 第二条闪念\n- 第一条闪念", output)
        self.assertNotIn("不应展示的上下文", output)
        self.assertNotIn("刚记下", output)
        self.assertNotIn("酝酿中", output)

    def test_current_brief_uses_item_tree_and_time_tags(self):
        output = render_brief(
            {
                "work_items": [
                    {
                        "id": "WI-TEST-001",
                        "title": "测试事项",
                        "state": "active",
                        "next_gate": "完成测试事项",
                        "milestones": [],
                    },
                    {
                        "id": "WI-TEST-002",
                        "title": "路线事项",
                        "state": "active",
                        "next_gate": None,
                        "milestones": [
                            {
                                "title": "阶段一",
                                "status": "current",
                                "outcome": "阶段一结果",
                                "target_at": "2026-08-21",
                            }
                        ],
                    },
                ]
            },
            {
                "tasks": [
                    {
                        "id": "TASK-TEST-001",
                        "outcome": "关联待办",
                        "work_item_id": "WI-TEST-001",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-14",
                        "next_action": {"text": "今天执行", "at": "2026-08-15"},
                    },
                    {
                        "id": "TASK-TEST-004",
                        "outcome": "路线当前待办",
                        "work_item_id": "WI-TEST-002",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": None,
                    },
                    {
                        "id": "TASK-TEST-002",
                        "outcome": "他人待办",
                        "work_item_id": "WI-TEST-002",
                        "status": "waiting",
                        "responsible_party": {"kind": "organization", "name": "项目组"},
                        "due_at": "2026-08-20",
                        "next_action": None,
                    },
                    {
                        "id": "TASK-TEST-003",
                        "outcome": "独立待办",
                        "work_item_id": None,
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-21",
                        "next_action": None,
                    },
                ]
            },
            {"ideas": []},
            "current",
            reference_date=date(2026, 8, 15),
        )

        self.assertIn(
            "总览：**2** 个事项 · **4** 条当前待办 · 我**3** / 他人**1**",
            output,
        )
        self.assertIn("总览：**2** 个事项 · **4** 条当前待办 · 我**3** / 他人**1**\n\n---\n\n📍 当前事项", output)
        self.assertIn("- 【测试事项】 · 推进中", output)
        self.assertIn("  - 下一门槛：完成测试事项", output)
        self.assertIn(
            "    - 关联待办 | `已逾期 1 天`",
            output,
        )
        self.assertIn("- 【路线事项】 · 推进中", output)
        self.assertIn("  - 当前里程碑：阶段一 ｜ `8月21日前完成`", output)
        self.assertIn(
            "    - 他人待办 | 👥 项目组 · 待当前节点完成 · `5 天后到期`\n"
            "    - 路线当前待办",
            output,
        )
        self.assertNotIn("👥 项目组 · 项目组", output)
        self.assertIn("🧩 独立待办\n\n- 独立待办 | `8月21日到期`", output)
        self.assertEqual(
            "明天到期",
            brief_date_label("2026-08-16", date(2026, 8, 15), "due"),
        )
        self.assertEqual(
            "8月21日到期",
            brief_date_label("2026-08-21", date(2026, 8, 15), "due"),
        )
        self.assertIsNone(
            brief_date_label("2026-08-16", date(2026, 8, 15), "action")
        )
        self.assertIn("💡 闪念\n\n*暂无*", output)
        self.assertNotIn("🙋 我的待办", output)
        self.assertNotIn("👥 他人待办", output)

        reminder = render_brief(
            {"work_items": []},
            {"tasks": []},
            {"ideas": []},
            "reminder",
            reference_date=date(2026, 8, 15),
        )
        self.assertIn("**📌 提醒｜8月15日**", reminder)

    def test_current_brief_hides_active_items_without_visible_tasks(self):
        output = render_brief(
            {
                "work_items": [
                    {
                        "id": "WI-HAS-TASK",
                        "title": "有待办事项",
                        "state": "active",
                        "next_gate": "完成有待办事项",
                        "milestones": [],
                    },
                    {
                        "id": "WI-NO-TASK",
                        "title": "无待办事项",
                        "state": "active",
                        "next_gate": "完成无待办事项",
                        "milestones": [],
                    },
                ]
            },
            {
                "tasks": [
                    {
                        "id": "TASK-VISIBLE",
                        "outcome": "关联待办",
                        "work_item_id": "WI-HAS-TASK",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": None,
                    }
                ]
            },
            {"ideas": []},
            "current",
            reference_date=date(2026, 8, 15),
        )

        self.assertIn("- 【有待办事项】 · 推进中", output)
        self.assertNotIn("无待办事项", output)
        self.assertIn("总览：**1** 个事项 · **1** 条当前待办 · 我**1** / 他人**0**", output)


class DisplaySeparatorTest(unittest.TestCase):
    def test_markdown_display_views_use_horizontal_rules_between_sections(self):
        now = render_now(
            {"projects": []},
            {"work_items": []},
            {"tasks": [], "updated_at": None},
        )
        self.assertIn("> 项目、事项关联均可选；项目事实仍以对应 Project Workspace 为准。\n\n---\n\n## 待办（0）", now)
        self.assertIn("当前没有活跃待办。\n\n---\n\n## 事项（0）", now)

        projects = render_projects(
            {
                "projects": [
                    {
                        "id": "PRJ-1",
                        "name": "项目一",
                        "tracking_state": "active",
                        "aliases": [],
                        "fact_source": {"kind": "workspace", "location": "one"},
                    },
                    {
                        "id": "PRJ-2",
                        "name": "项目二",
                        "tracking_state": "active",
                        "aliases": [],
                        "fact_source": {"kind": "workspace", "location": "two"},
                    },
                ],
                "updated_at": None,
            }
        )
        self.assertIn("> Work 只保存个人跟踪关系；名称与当前目录由 Project Catalog 动态补全。\n\n---\n\n## PRJ-1", projects)
        self.assertIn("事实源**：workspace · one\n\n---\n\n## PRJ-2", projects)

        tasks = render_tasks(
            {
                "tasks": [
                    {"id": "TASK-1", "outcome": "待办一", "status": "active"},
                    {"id": "TASK-2", "outcome": "待办二", "status": "active"},
                ],
                "updated_at": None,
            }
        )
        self.assertIn("- **项目**：无\n\n---\n\n## TASK-2 · 待办二", tasks)

        work_items = render_work_items(
            {
                "work_items": [
                    {"id": "WI-1", "title": "事项一", "state": "active"},
                    {"id": "WI-2", "title": "事项二", "state": "active"},
                ],
                "updated_at": None,
            }
        )
        self.assertIn("- **类型**：轻量事项\n\n---\n\n## WI-2 · 事项二", work_items)

        glossary = render_glossary({"terms": [], "updated_at": None})
        self.assertIn("> 作用：帮助不同 Agent 和会话识别人员、组织、项目、系统及专有概念。\n\n---\n\n| ID |", glossary)
        self.assertIn("| --- | --- | --- | --- | --- |\n\n---\n\n## 使用规则", glossary)

        ideas = render_ideas({"ideas": []})
        self.assertIn("> 这里承载随口想法与未成形念头；记录本身不产生责任、期限或待办。\n\n---\n\n当前没有活跃闪念。", ideas)
        self.assertIn("当前没有活跃闪念。\n\n---\n\n## 使用规则", ideas)

        achievements = render_achievements(
            {
                "achievements": [
                    {
                        "id": "ACH-1",
                        "title": "成果一",
                        "lifecycle": "current",
                        "outcome": "结果一",
                        "task_links": [{"task_id": "TASK-1"}],
                        "reuse": "复用一",
                        "key_learnings": ["经验一"],
                    },
                    {
                        "id": "ACH-2",
                        "title": "成果二",
                        "lifecycle": "current",
                        "outcome": "结果二",
                        "task_links": [{"task_id": "TASK-2"}],
                        "reuse": "复用二",
                        "key_learnings": ["经验二"],
                    },
                ],
                "updated_at": None,
            }
        )
        self.assertIn("- 经验一\n\n---\n\n## ACH-2 · 成果二", achievements)


class ScheduledBriefViewTest(unittest.TestCase):
    def test_current_orders_undated_tasks_by_started_date(self):
        output = render_brief(
            {
                "work_items": [
                    {
                        "id": "WI-STARTED-ORDER",
                        "title": "开始时间排序事项",
                        "state": "active",
                        "next_gate": "依次完成当前节点和下一节点",
                        "milestones": [],
                    }
                ]
            },
            {
                "tasks": [
                    {
                        "id": "TASK-NOT-STARTED",
                        "outcome": "尚未开始的下一节点",
                        "work_item_id": "WI-STARTED-ORDER",
                        "status": "waiting",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": None,
                    },
                    {
                        "id": "TASK-STARTED-LATER",
                        "outcome": "较晚开始的当前节点",
                        "work_item_id": "WI-STARTED-ORDER",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": None,
                    },
                    {
                        "id": "TASK-STARTED-EARLIER",
                        "outcome": "较早开始的当前节点",
                        "work_item_id": "WI-STARTED-ORDER",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": None,
                    },
                ]
            },
            {"ideas": []},
            "current",
            reference_date=date(2026, 8, 25),
            events=[
                {
                    "kind": "task_started",
                    "task_id": "TASK-STARTED-LATER",
                    "started_at": "2026-08-01",
                },
                {
                    "kind": "task_started",
                    "task_id": "TASK-STARTED-LATER",
                    "started_at": "2026-08-20",
                },
                {
                    "kind": "task_started",
                    "task_id": "TASK-STARTED-EARLIER",
                    "started_at": "2026-08-12",
                },
            ],
        )

        positions = [
            output.index(title)
            for title in (
                "较早开始的当前节点",
                "较晚开始的当前节点",
                "尚未开始的下一节点",
            )
        ]
        self.assertEqual(sorted(positions), positions)

    def test_current_orders_item_tasks_by_due_date_before_status(self):
        output = render_brief(
            {
                "work_items": [
                    {
                        "id": "WI-DUE-ORDER",
                        "title": "截止日排序事项",
                        "state": "active",
                        "next_gate": "按紧迫程度完成待办",
                        "milestones": [],
                    }
                ]
            },
            {
                "tasks": [
                    {
                        "id": "TASK-NO-DUE",
                        "outcome": "无截止日",
                        "work_item_id": "WI-DUE-ORDER",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": None,
                    },
                    {
                        "id": "TASK-FAR",
                        "outcome": "较晚到期",
                        "work_item_id": "WI-DUE-ORDER",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-09-10",
                        "next_action": None,
                    },
                    {
                        "id": "TASK-OVERDUE-RECENT",
                        "outcome": "近期逾期",
                        "work_item_id": "WI-DUE-ORDER",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-20",
                        "next_action": None,
                    },
                    {
                        "id": "TASK-DUE-SOON",
                        "outcome": "三天后到期",
                        "work_item_id": "WI-DUE-ORDER",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-28",
                        "next_action": None,
                    },
                    {
                        "id": "TASK-OVERDUE-OLDEST",
                        "outcome": "逾期最久",
                        "work_item_id": "WI-DUE-ORDER",
                        "status": "waiting",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-14",
                        "next_action": None,
                    },
                ]
            },
            {"ideas": []},
            "current",
            reference_date=date(2026, 8, 25),
        )

        positions = [
            output.index(title)
            for title in (
                "逾期最久",
                "近期逾期",
                "三天后到期",
                "较晚到期",
                "无截止日",
            )
        ]
        self.assertEqual(sorted(positions), positions)

    def test_current_keeps_active_item_window_and_one_waiting_node(self):
        output = render_brief(
            {
                "work_items": [
                    {
                        "id": "WI-ACTIVE",
                        "title": "推进事项",
                        "state": "active",
                        "next_gate": "形成可验证结果",
                        "milestones": [],
                    },
                    {
                        "id": "WI-WAITING",
                        "title": "等待事项",
                        "state": "waiting",
                        "next_gate": "等待输入",
                        "milestones": [],
                    },
                ]
            },
            {
                "tasks": [
                    {
                        "id": "TASK-A",
                        "outcome": "当前节点 A",
                        "work_item_id": "WI-ACTIVE",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": {"text": "推进 A", "at": "2026-08-15"},
                        "due_at": None,
                    },
                    {
                        "id": "TASK-B",
                        "outcome": "直接下一节点 B",
                        "work_item_id": "WI-ACTIVE",
                        "status": "waiting",
                        "responsible_party": {"kind": "person", "name": "伙伴"},
                        "next_action": {"text": "承接 B", "at": "2026-08-17"},
                        "due_at": None,
                    },
                    {
                        "id": "TASK-C",
                        "outcome": "更远节点 C",
                        "work_item_id": "WI-ACTIVE",
                        "status": "waiting",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": {"text": "承接 C", "at": "2026-08-18"},
                        "due_at": None,
                    },
                    {
                        "id": "TASK-LATE",
                        "outcome": "窗口外待办",
                        "work_item_id": "WI-ACTIVE",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": {"text": "稍后推进", "at": "2026-09-01"},
                        "due_at": None,
                    },
                    {
                        "id": "TASK-PAUSED",
                        "outcome": "暂停待办",
                        "work_item_id": "WI-ACTIVE",
                        "status": "paused",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": None,
                        "due_at": None,
                    },
                    {
                        "id": "TASK-BLOCKED-ITEM",
                        "outcome": "等待事项下的待办",
                        "work_item_id": "WI-WAITING",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": None,
                        "due_at": None,
                    },
                    {
                        "id": "TASK-STANDALONE",
                        "outcome": "近期独立待办",
                        "work_item_id": None,
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": {"text": "独立推进", "at": "2026-08-31"},
                        "due_at": None,
                    },
                ]
            },
            {"ideas": []},
            "current",
            reference_date=date(2026, 8, 16),
        )

        self.assertIn("📌 当前简报｜8月16日", output)
        self.assertIn(
            "总览：**1** 个事项 · **4** 条当前待办 · 我**3** / 他人**1**",
            output,
        )
        self.assertIn("- 【推进事项】 · 推进中", output)
        self.assertIn("    - 当前节点 A", output)
        self.assertIn(
            "    - 直接下一节点 B | 👥 伙伴 · 待当前节点完成",
            output,
        )
        self.assertIn("🧩 独立待办\n\n- 近期独立待办", output)
        for hidden in (
            "更远节点 C",
            "暂停待办",
            "等待事项",
            "等待事项下的待办",
        ):
            self.assertNotIn(hidden, output)

    def test_closeout_deduplicates_dates_keeps_status_and_stable_ties(self):
        output = render_brief(
            {"work_items": []},
            {
                "tasks": [
                    {
                        "id": "TASK-BOTH",
                        "outcome": "双重命中",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-14",
                        "next_action": {"text": "继续处理", "at": "2026-08-12"},
                    },
                    {
                        "id": "TASK-WAITING",
                        "outcome": "等待结果",
                        "status": "waiting",
                        "responsible_party": {"kind": "organization", "name": "项目组"},
                        "due_at": "2026-08-15",
                        "next_action": None,
                    },
                    {
                        "id": "TASK-FIRST",
                        "outcome": "同日行动一",
                        "status": "paused",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": None,
                        "next_action": {"text": "回看一", "at": "2026-08-13"},
                    },
                    {
                        "id": "TASK-SECOND",
                        "outcome": "同日行动二",
                        "status": "active",
                        "responsible_party": None,
                        "due_at": None,
                        "next_action": {"text": "回看二", "at": "2026-08-13"},
                    },
                    {
                        "id": "TASK-TODAY",
                        "outcome": "今天不命中",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-16",
                        "next_action": {"text": "今天处理", "at": "2026-08-16"},
                    },
                ]
            },
            {"ideas": []},
            "closeout",
            reference_date=date(2026, 8, 16),
        )

        self.assertIn("📌 18:00 晚间收口提醒｜8月16日", output)
        self.assertIn("**2** 条结果逾期", output)
        self.assertIn("总览：**2** 条结果逾期\n\n---\n\n🔴 结果逾期", output)
        self.assertIn(
            "  - 负责人：项目组 ｜ 状态：待前置完成 ｜ 截止：`2026-08-15`", output
        )
        self.assertEqual(1, output.count("双重命中"))
        self.assertIn("`已逾期 2 天`", output)
        self.assertNotIn("原定行动待确认", output)
        self.assertIn("  - 负责人：项目组 ｜ 状态：待前置完成", output)
        self.assertIn("  - 当前行动：继续处理", output)
        self.assertNotIn("原定行动：回看一", output)
        self.assertNotIn("\n继续处理\n", output)
        self.assertNotIn("负责人：未知", output)
        self.assertNotIn("同日行动一", output)
        self.assertNotIn("同日行动二", output)
        self.assertNotIn("今天不命中", output)
        self.assertTrue(output.rstrip().endswith("---"))

    def test_current_does_not_guess_next_from_only_waiting_tasks(self):
        output = render_brief(
            {
                "work_items": [
                    {
                        "id": "WI-AMBIGUOUS",
                        "title": "待办顺序未显式记录",
                        "state": "active",
                        "next_gate": "确认当前可执行节点",
                        "milestones": [],
                    }
                ]
            },
            {
                "tasks": [
                    {
                        "id": "TASK-WAITING-ONE",
                        "outcome": "等待节点一",
                        "work_item_id": "WI-AMBIGUOUS",
                        "status": "waiting",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": None,
                        "due_at": None,
                    },
                    {
                        "id": "TASK-WAITING-TWO",
                        "outcome": "等待节点二",
                        "work_item_id": "WI-AMBIGUOUS",
                        "status": "waiting",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "next_action": {"text": "稍后处理", "at": "2026-08-20"},
                        "due_at": None,
                    },
                ]
            },
            {"ideas": []},
            "current",
            reference_date=date(2026, 8, 16),
        )

        self.assertIn("- 【待办顺序未显式记录】 · 推进中", output)
        self.assertIn("下一门槛：确认当前可执行节点", output)
        self.assertNotIn("等待节点一", output)
        self.assertNotIn("等待节点二", output)

    def test_closeout_empty_state_is_one_sentence(self):
        output = render_brief(
            {"work_items": []},
            {
                "tasks": [
                    {
                        "id": "TASK-TODAY",
                        "outcome": "今天处理",
                        "status": "active",
                        "responsible_party": {"kind": "self", "name": "我"},
                        "due_at": "2026-08-16",
                        "next_action": {"text": "今天处理", "at": "2026-08-16"},
                    }
                ]
            },
            {"ideas": []},
            "closeout",
            reference_date=date(2026, 8, 16),
        )
        self.assertEqual(
            "今天没有仍待收口的结果逾期待办。\n",
            output,
        )

    def test_unknown_responsibility_is_neither_self_nor_other(self):
        tasks = {
            "tasks": [
                {
                    "id": "TASK-UNKNOWN",
                    "outcome": "未明确责任方的待办",
                    "work_item_id": None,
                    "status": "active",
                    "responsible_party": {"kind": "unknown", "name": "未确认"},
                    "due_at": "2026-08-16",
                    "next_action": {"text": "等待确认责任方"},
                }
            ]
        }
        current = render_brief(
            {"work_items": []}, tasks, {"ideas": []}, "current",
            reference_date=date(2026, 8, 16),
        )
        self.assertIn("总览：**0** 个事项 · **1** 条当前待办", current)
        self.assertNotIn("他人**", current)

        reminder = render_brief(
            {"work_items": []}, tasks, {"ideas": []}, "reminder",
            reference_date=date(2026, 8, 16),
        )
        self.assertIn("**📋 待办**", reminder)
        self.assertIn("未明确责任方的待办", reminder)
        self.assertNotIn("**👥 他人待办**", reminder)
        self.assertNotIn("未确认", reminder)


class TaskActionProgressViewTest(unittest.TestCase):
    def _task(self, **overrides):
        task = {
            "id": "TASK-PROGRESS-001",
            "outcome": "推进中的待办",
            "work_item_id": None,
            "status": "active",
            "responsible_party": {"kind": "self", "name": "我"},
            "next_action": {"text": "继续推进"},
            "due_at": None,
        }
        task.update(overrides)
        return task

    def _events(self):
        return [
            {
                "event_id": "EVT-PROGRESS-001",
                "occurred_at": "2026-08-12T09:00:00+08:00",
                "actor": {"kind": "user", "name": "我"},
                "kind": "task_started",
                "summary": "开始推进",
                "task_id": "TASK-PROGRESS-001",
                "started_at": "2026-08-12",
                "sources": ["test"],
            }
        ]

    def test_started_days_are_derived_from_event_and_no_action_date_group_exists(self):
        tasks = {"tasks": [self._task()]}

        current = render_brief(
            {"work_items": []}, tasks, {"ideas": []}, "current",
            reference_date=date(2026, 8, 18),
            events=self._events(),
        )
        self.assertIn("已推进 6 天", current)
        self.assertNotIn("已逾期", current)

        reminder = render_brief(
            {"work_items": []}, tasks, {"ideas": []}, "reminder",
            reference_date=date(2026, 8, 18),
            events=self._events(),
        )
        self.assertNotIn("原定行动待确认", reminder)
        self.assertNotIn("明天行动", reminder)

        closeout = render_brief(
            {"work_items": []}, tasks, {"ideas": []}, "closeout",
            reference_date=date(2026, 8, 18),
            events=self._events(),
        )
        self.assertEqual("今天没有仍待收口的结果逾期待办。\n", closeout)

    def test_past_due_is_only_result_overdue(self):
        task = self._task(due_at="2026-08-15")
        closeout = render_brief(
            {"work_items": []}, {"tasks": [task]}, {"ideas": []}, "closeout",
            reference_date=date(2026, 8, 18),
        )
        self.assertIn("**1** 条结果逾期", closeout)
        self.assertIn("`已逾期 3 天`", closeout)
        self.assertNotIn("原定行动待确认", closeout)


class TaskStartCommandTest(CLITestCase):
    def test_task_start_records_date_and_is_idempotent(self):
        self.validate_runtime()
        arguments = (
            "task-start", "TASK-20260725-002",
            "--started-at", "2026-07-28",
            "--source", "test backfill",
            "--idempotency-key", "test:task-start:TASK-20260725-002",
        )
        first = self.run_cli(*arguments)
        self.assertIn("开始推进日期", first.stdout)

        tasks = self.load_json("tasks.json")["tasks"]
        task = next(item for item in tasks if item["id"] == "TASK-20260725-002")
        self.assertNotIn("started_at", task)

        events = [
            json.loads(line)
            for line in (self.data_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        started = [event for event in events if event.get("kind") == "task_started"]
        self.assertEqual(1, len(started))
        self.assertEqual("2026-07-28", started[0]["started_at"])
        self.assertNotIn("started_at_basis", started[0])

        retry = self.run_cli(*arguments)
        self.assertIn("未重复写入", retry.stdout)
        events_after_retry = [
            json.loads(line)
            for line in (self.data_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len([event for event in events_after_retry if event.get("kind") == "task_started"]))

        confirmed_arguments = (
            "task-start", "TASK-20260725-002",
            "--started-at", "2026-08-01",
            "--source", "本人确认实际开始日期",
            "--actor-kind", "user",
            "--actor-name", "测试用户",
            "--idempotency-key", "test:task-start-confirmed:TASK-20260725-002",
        )
        self.run_cli(*confirmed_arguments)
        self.run_cli("validate")
        updated_task = next(
            item
            for item in self.load_json("tasks.json")["tasks"]
            if item["id"] == "TASK-20260725-002"
        )
        self.assertNotIn("started_at", updated_task)
        final_events = [
            json.loads(line)
            for line in (self.data_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(
            ["2026-07-28", "2026-08-01"],
            [
                event["started_at"]
                for event in final_events
                if event.get("kind") == "task_started"
            ],
        )


class WorkWorkflowTest(CLITestCase):
    def test_validate_rejects_retired_action_date_event_fields(self):
        task_id = self.add_task("--due", "2026-08-12")
        events_path = self.data_dir / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        created = next(
            event for event in reversed(events)
            if event.get("kind") == "task_created" and event.get("task_id") == task_id
        )
        created["schedule"] = {"next_action.at": "2026-08-12"}
        events_path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        invalid_baseline = self.run_cli("validate", check=False)
        self.assertNotEqual(0, invalid_baseline.returncode)
        self.assertIn("task_created schedule 结构非法", invalid_baseline.stderr)

        created["schedule"] = {"due_at": "2026-08-12"}
        events_path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        self.run_cli(
            "task-reschedule", task_id, "--due", "2026-08-13",
            "--reason-code", "priority_changed", "--source", "test fixture",
        )
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        events[-1]["schedule_changes"][0]["field"] = "next_action.at"
        events_path.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        invalid_change = self.run_cli("validate", check=False)
        self.assertNotEqual(0, invalid_change.returncode)
        self.assertIn("计划日期变更事件 change 结构非法", invalid_change.stderr)

    def setUp(self):
        super().setUp()
        self.validate_runtime()

    def add_task(self, *links, outcome="合成待办"):
        return self.created_id(self.run_cli(
            "task-add", "--outcome", outcome, *links,
            "--responsible-party", "测试用户", "--responsible-kind", "self",
            "--responsible-entity", "ENT-SELF",
            "--next-action", "执行合成动作",
            "--why", "保护 Work 契约",
            "--completion-criteria", "目标结果可复核",
            "--source", "test fixture",
        ))

    def add_work_item(self, title="轻量事项"):
        return self.created_id(self.run_cli(
            "work-item-add", "--title", title,
            "--next-gate", "完成最近一步", "--source", "test fixture",
        ))

    def test_standalone_task_is_the_fast_path_for_one_off_work(self):
        task_id = self.add_task(outcome="一次性事项")
        task = next(value for value in self.load_json("tasks.json")["tasks"] if value["id"] == task_id)
        self.assertIsNone(task["work_item_id"])
        self.assertIsNone(task["milestone_id"])
        self.assertEqual({"text": "执行合成动作"}, task["next_action"])

    def test_self_responsibility_uses_ent_self_instead_of_supplied_alias(self):
        task_id = self.created_id(self.run_cli(
            "task-add", "--outcome", "本人规范化待办",
            "--responsible-kind", "self",
            "--next-action", "验证本人身份", "--source", "test fixture",
        ))
        task = next(
            value for value in self.load_json("tasks.json")["tasks"]
            if value["id"] == task_id
        )
        self.assertEqual(
            {"kind": "self", "name": "测试用户", "entity_id": "ENT-SELF"},
            task["responsible_party"],
        )
        self.assertNotIn("责任方：测试用户", self.run_cli("tasks").stdout)

        external_id = self.created_id(self.run_cli(
            "task-add", "--outcome", "切换为本人负责",
            "--responsible-party", "项目组", "--responsible-kind", "organization",
            "--next-action", "切换责任方", "--source", "test fixture",
        ))
        self.run_cli(
            "task-update", external_id, "--responsible-party", "另一个昵称",
            "--responsible-kind", "self", "--source", "test fixture",
        )
        task = next(
            value for value in self.load_json("tasks.json")["tasks"]
            if value["id"] == external_id
        )
        self.assertEqual(
            {"kind": "self", "name": "测试用户", "entity_id": "ENT-SELF"},
            task["responsible_party"],
        )

    def test_term_update_cascades_any_entity_name_to_task_references(self):
        entity_id = self.created_id(self.run_cli(
            "term-add", "--name", "旧组织名", "--kind", "organization",
            "--description", "用于验证通用实体改名级联。",
            "--source", "test fixture",
        ))
        invalid = self.run_cli(
            "task-add", "--outcome", "错误实体名称待办",
            "--responsible-kind", "organization",
            "--responsible-party", "非规范简称",
            "--responsible-entity", entity_id,
            "--next-action", "不应写入", "--source", "test fixture",
            check=False,
        )
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("必须与 entity_id 的规范名称一致", invalid.stderr)
        task_id = self.created_id(self.run_cli(
            "task-add", "--outcome", "由具名组织负责的待办",
            "--responsible-kind", "organization",
            "--responsible-party", "旧组织名",
            "--responsible-entity", entity_id,
            "--next-action", "推进组织待办", "--source", "test fixture",
        ))

        result = self.run_cli(
            "term-update", entity_id, "--name", "新组织名",
            "--source", "本人确认",
        )

        self.assertIn("同步 1 条责任方引用", result.stdout)
        self.assertIn("备份：", result.stdout)
        self.assertTrue(any((self.data_dir / "backups").iterdir()))
        task = next(
            value for value in self.load_json("tasks.json")["tasks"]
            if value["id"] == task_id
        )
        self.assertEqual("新组织名", task["responsible_party"]["name"])
        self.assertEqual(entity_id, task["responsible_party"]["entity_id"])
        self.run_cli("validate")

    def test_term_update_ent_self_uses_the_same_generic_cascade(self):
        result = self.run_cli(
            "term-update", "ENT-SELF", "--name", "新测试用户",
            "--source", "本人确认",
        )

        self.assertIn("责任方引用", result.stdout)
        for task in self.load_json("tasks.json")["tasks"]:
            if (task.get("responsible_party") or {}).get("kind") == "self":
                self.assertEqual("新测试用户", task["responsible_party"]["name"])
                self.assertEqual("ENT-SELF", task["responsible_party"]["entity_id"])
        self.run_cli("validate")

    def test_term_update_without_rename_stays_single_target(self):
        tasks_before = (self.data_dir / "tasks.json").read_bytes()
        backups_before = {
            path.name for path in (self.data_dir / "backups").iterdir()
        }

        result = self.run_cli(
            "term-update", "ENT-SELF",
            "--description", "更新后的合成本人实体描述。",
            "--source", "test fixture",
        )

        self.assertIn("同步 0 条责任方引用", result.stdout)
        self.assertNotIn("备份：", result.stdout)
        self.assertEqual(tasks_before, (self.data_dir / "tasks.json").read_bytes())
        self.assertEqual(
            backups_before,
            {path.name for path in (self.data_dir / "backups").iterdir()},
        )
        self.run_cli("validate")

    def test_task_update_requires_name_when_changing_from_self_to_external(self):
        task_id = self.add_task()
        result = self.run_cli(
            "task-update", task_id, "--responsible-kind", "person", check=False
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("必须提供真实名称", result.stderr)
        task = next(
            value for value in self.load_json("tasks.json")["tasks"]
            if value["id"] == task_id
        )
        self.assertEqual("self", task["responsible_party"]["kind"])

    def test_transaction_idempotency_does_not_repeat_any_ledger_write(self):
        arguments = (
            "task-add", "--outcome", "幂等合成待办",
            "--responsible-party", "测试用户", "--responsible-kind", "self",
            "--responsible-entity", "ENT-SELF",
            "--next-action", "执行幂等动作",
            "--why", "验证统一写事务", "--completion-criteria", "只写入一次",
            "--source", "test fixture", "--idempotency-key", "transaction-once",
        )
        first = self.run_cli(*arguments)
        ledger_paths = [
            path for path in self.data_dir.iterdir()
            if path.suffix in {".json", ".jsonl", ".md"}
        ]
        before = {path.name: path.read_bytes() for path in ledger_paths}

        second = self.run_cli(*arguments)

        self.assertIn("未重复写入", second.stdout)
        self.assertEqual(
            before,
            {path.name: path.read_bytes() for path in ledger_paths},
        )
        task_id = self.created_id(first)
        self.assertEqual(
            1,
            sum(
                value["id"] == task_id
                for value in self.load_json("tasks.json")["tasks"]
            ),
        )

    def test_validate_detects_stale_view_and_refresh_repairs_it(self):
        now_path = self.data_dir / "now.md"
        now_path.write_text("stale view\n", encoding="utf-8")
        invalid = self.run_cli("validate", check=False)
        self.assertNotEqual(0, invalid.returncode)
        self.assertIn("now.md 与事实源不一致", invalid.stderr)

        self.run_cli("refresh")

        self.run_cli("validate")
        self.assertNotEqual("stale view\n", now_path.read_text(encoding="utf-8"))

    def test_project_tracks_discovered_key_and_state_reason(self):
        project_root = self.data_dir / "project"
        project_root.mkdir()
        manifest_path = project_root / "lifeos-project.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "project_key": "synthetic-project",
            "name": "合成项目",
            "aliases": [],
            "scope": "project",
            "sources": {"dchat": {"groups": []}, "cooper": {"resources": []}},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        project_id = self.created_id(self.run_cli(
            "project-track", "--project-key", "synthetic-project",
            "--tracking-state", "paused", "--reason", "等待重新启动",
            "--source", "test fixture",
        ))
        project = next(value for value in self.load_json("projects.json")["projects"] if value["id"] == project_id)
        self.assertEqual("synthetic-project", project["project_key"])
        self.assertNotIn("manifest_path", project)
        self.assertNotIn("name", project)
        self.assertNotIn("fact_source", project)
        self.assertEqual("等待重新启动", project["status_reason"])

    def test_project_catalog_migration_removes_legacy_manifest_paths(self):
        project_root = self.data_dir / "legacy-project"
        project_root.mkdir()
        manifest_path = project_root / "lifeos-project.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "project_key": "legacy-project",
            "name": "Legacy Project",
            "aliases": [],
            "scope": "project",
            "sources": {"dchat": {"groups": []}, "cooper": {"resources": []}},
        }), encoding="utf-8")
        project_id = self.created_id(self.run_cli(
            "project-track", "--project-key", "legacy-project",
            "--source", "test fixture",
        ))
        legacy = self.load_json("projects.json")
        legacy["schema_version"] = 1
        legacy["projects"][0]["manifest_path"] = str(manifest_path)
        (self.data_dir / "projects.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )

        migrated = self.run_cli(
            "migrate-project-catalog",
            "--source", "test fixture migration",
        )
        self.assertIn("迁移完成", migrated.stdout)
        current = self.load_json("projects.json")
        self.assertEqual(2, current["schema_version"])
        self.assertEqual(project_id, current["projects"][0]["id"])
        self.assertNotIn("manifest_path", current["projects"][0])
        self.assertTrue(list((self.data_dir / "backups").glob("project-catalog-*")))
        self.run_cli("validate")

    def test_moved_project_is_rediscovered_without_work_write(self):
        original_root = self.data_dir / "original-project"
        original_root.mkdir()
        original_manifest = original_root / "lifeos-project.json"
        payload = {
            "schema_version": 1,
            "project_key": "moved-project",
            "name": "迁移项目",
            "aliases": [],
            "scope": "project",
            "sources": {"dchat": {"groups": []}, "cooper": {"resources": []}},
        }
        original_manifest.write_text(json.dumps(payload), encoding="utf-8")
        project_id = self.created_id(self.run_cli(
            "project-track", "--project-key", "moved-project",
            "--source", "test fixture",
        ))
        events_before = (self.data_dir / "events.jsonl").read_bytes()

        relocated_root = self.data_dir / "relocated-project"
        relocated_root.mkdir()
        relocated_manifest = relocated_root / "lifeos-project.json"
        relocated_manifest.write_text(json.dumps(payload), encoding="utf-8")
        original_manifest.unlink()

        discovered = json.loads(self.run_root_cli(
            "project", "discover", "--json"
        ).stdout)
        current = next(
            item for item in discovered["projects"]
            if item["project_key"] == "moved-project"
        )
        self.assertEqual(str(relocated_root), current["root"])
        hydrated = json.loads(self.run_cli("projects", "--json").stdout)
        tracked = next(item for item in hydrated if item["id"] == project_id)
        self.assertEqual(str(relocated_root), tracked["fact_source"]["location"])
        project = next(
            item for item in self.load_json("projects.json")["projects"]
            if item["id"] == project_id
        )
        self.assertNotIn("manifest_path", project)
        self.assertEqual(events_before, (self.data_dir / "events.jsonl").read_bytes())
        self.run_cli("validate")

    def test_invalid_manifest_isolated_from_work_validation(self):
        project_root = self.data_dir / "invalid-manifest-project"
        project_root.mkdir()
        manifest_path = project_root / "lifeos-project.json"
        payload = {
            "schema_version": 1,
            "project_key": "invalid-manifest-project",
            "name": "Invalid Manifest Project",
            "aliases": [],
            "scope": "project",
            "sources": {"dchat": {"groups": []}, "cooper": {"resources": []}},
        }
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        self.run_cli(
            "project-track", "--project-key", "invalid-manifest-project",
            "--source", "test fixture",
        )
        payload["schema_version"] = 2
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        result = self.run_cli("validate")
        self.assertIn("当前不可用", result.stdout)
        project_validation = self.run_root_cli(
            "project", "validate", "--all", check=False
        )
        self.assertEqual(1, project_validation.returncode)
        self.assertIn("schema_version 必须为 1", project_validation.stdout)

    def test_invalid_project_config_does_not_make_core_work_unreadable(self):
        config_path = Path(self.environment["LIFEOS_CONFIG"])
        config_path.write_text(json.dumps({"unknown": True}), encoding="utf-8")
        result = self.run_cli("validate")
        self.assertIn("内部审计与派生视图一致", result.stdout)
        projects = self.run_cli("projects", "--json")
        self.assertEqual([], json.loads(projects.stdout))

    def test_light_work_item_supports_one_or_two_tasks_without_milestones(self):
        work_item_id = self.add_work_item()
        first = self.add_task("--work-item-id", work_item_id, outcome="第一步")
        second = self.add_task("--work-item-id", work_item_id, outcome="第二步")
        work_item = next(value for value in self.load_json("work-items.json")["work_items"] if value["id"] == work_item_id)
        self.assertEqual([], work_item["milestones"])
        linked = [value for value in self.load_json("tasks.json")["tasks"] if value["id"] in {first, second}]
        self.assertTrue(all(value["milestone_id"] is None for value in linked))
        self.run_cli("validate")

    def test_roadmap_derives_next_gate_from_unique_current_milestone(self):
        work_item_id = self.add_work_item("路线事项")
        first = self.created_id(self.run_cli(
            "work-item-milestone-add", work_item_id,
            "--title", "阶段一", "--outcome", "阶段一结果",
            "--completion-criteria", "阶段一验收通过", "--source", "test fixture",
        ))
        second = self.created_id(self.run_cli(
            "work-item-milestone-add", work_item_id,
            "--title", "阶段二", "--outcome", "阶段二结果",
            "--completion-criteria", "阶段二验收通过", "--status", "planned",
            "--source", "test fixture",
        ))
        roadmap_task = self.add_task("--work-item-id", work_item_id, "--milestone-id", first)
        item = next(value for value in self.load_json("work-items.json")["work_items"] if value["id"] == work_item_id)
        self.assertIsNone(item["next_gate"])
        self.assertEqual(first, next(value["id"] for value in item["milestones"] if value["status"] == "current"))

        self.run_cli(
            "task-close", roadmap_task, "--summary", "阶段待办完成",
            "--completion-source", "测试结果", "--no-realized-value",
            "--source", "test fixture",
        )

        self.run_cli(
            "work-item-milestone-update", work_item_id, first,
            "--status", "completed", "--summary", "阶段一已完成",
            "--completion-source", "测试结果", "--decision", "continue",
            "--activate-next", second, "--source", "test fixture",
        )
        item = next(value for value in self.load_json("work-items.json")["work_items"] if value["id"] == work_item_id)
        self.assertEqual(second, next(value["id"] for value in item["milestones"] if value["status"] == "current"))
        self.run_cli("validate")

    def test_task_completion_unifies_summary_sources_values_and_reflections(self):
        task_id = self.add_task()
        self.run_cli(
            "task-close", task_id, "--summary", "完成合成结果",
            "--completion-source", "测试断言", "--value", "capability", "形成能力",
            "--reflection", "以后复用同一验证入口", "--source", "test fixture",
        )
        task = next(value for value in self.load_json("tasks.json")["tasks"] if value["id"] == task_id)
        self.assertEqual("completed", task["status"])
        self.assertEqual("完成合成结果", task["completion"]["summary"])
        self.assertEqual([{"type": "capability", "statement": "形成能力"}], task["completion"]["values"])
        self.assertEqual(["以后复用同一验证入口"], task["completion"]["reflections"])
        self.assertIsNone(task["next_action"])

    def test_task_reschedule_records_dates_direction_and_reason_atomically(self):
        task_id = self.add_task("--due", "2026-08-12")
        self.run_cli(
            "task-reschedule", task_id,
            "--due", "2026-08-16",
            "--reason-code", "priority_changed",
            "--note", "临时插入更高优先级工作",
            "--source", "本人确认",
        )

        task = next(
            value for value in self.load_json("tasks.json")["tasks"]
            if value["id"] == task_id
        )
        self.assertEqual("2026-08-16", task["due_at"])
        self.assertEqual({"text": "执行合成动作"}, task["next_action"])
        event = json.loads(
            (self.data_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual("task_schedule_changed", event["kind"])
        self.assertEqual("priority_changed", event["reason_code"])
        self.assertEqual("临时插入更高优先级工作", event["reason_note"])
        self.assertEqual(
            [
                {
                    "field": "due_at", "from": "2026-08-12",
                    "to": "2026-08-16", "direction": "postponed",
                },
            ],
            event["schedule_changes"],
        )

        history = json.loads(
            self.run_cli(
                "task-schedule-history", task_id, "--json"
            ).stdout
        )
        self.assertEqual("schedule_baseline", history[0]["kind"])
        self.assertEqual(
            {
                "due_at": "2026-08-12",
            },
            history[0]["schedule"],
        )
        self.assertEqual("task_schedule_changed", history[1]["kind"])
        self.run_cli("refresh")
        self.run_cli("validate")

    def test_postponing_or_clearing_schedule_requires_reason(self):
        task_id = self.add_task("--due", "2026-08-12")
        before = (self.data_dir / "events.jsonl").read_text(encoding="utf-8")
        postponed = self.run_cli(
            "task-reschedule", task_id,
            "--due", "2026-08-13", "--source", "test fixture",
            check=False,
        )
        self.assertNotEqual(0, postponed.returncode)
        self.assertIn("--reason-code", postponed.stderr)
        cleared = self.run_cli(
            "task-reschedule", task_id,
            "--clear-due", "--source", "test fixture",
            check=False,
        )
        self.assertNotEqual(0, cleared.returncode)
        self.assertIn("--reason-code", cleared.stderr)
        self.assertEqual(
            before,
            (self.data_dir / "events.jsonl").read_text(encoding="utf-8"),
        )

    def test_schedule_set_or_advance_can_omit_reason_but_note_cannot(self):
        task_id = self.add_task("--due", "2026-08-12")
        self.run_cli(
            "task-reschedule", task_id,
            "--due", "2026-08-11", "--source", "test fixture",
        )
        event = json.loads(
            (self.data_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual("advanced", event["schedule_changes"][0]["direction"])
        self.assertNotIn("reason_code", event)
        failed = self.run_cli(
            "task-reschedule", task_id,
            "--due", "2026-08-10", "--note", "只有说明",
            "--source", "test fixture", check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("--reason-code", failed.stderr)

    def test_task_update_can_clear_next_action_text(self):
        task_id = self.add_task()
        self.run_cli("task-update", task_id, "--clear-next-action")
        task = next(
            value for value in self.load_json("tasks.json")["tasks"]
            if value["id"] == task_id
        )
        self.assertIsNone(task["next_action"])

    def test_completed_task_cannot_be_rescheduled(self):
        task_id = self.add_task()
        self.run_cli(
            "task-close", task_id, "--summary", "完成合成结果",
            "--completion-source", "测试断言", "--no-realized-value",
            "--source", "test fixture",
        )
        failed = self.run_cli(
            "task-reschedule", task_id,
            "--due", "2026-08-20", "--reason-code", "external_change",
            "--source", "test fixture", check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("已完成待办", failed.stderr)

    def test_achievement_uses_outcome_and_sources_without_evidence_wrapper(self):
        task_id = self.add_task()
        self.run_cli(
            "task-close", task_id, "--summary", "完成合成结果",
            "--completion-source", "测试断言", "--no-realized-value",
            "--source", "test fixture",
        )
        achievement_id = self.created_id(self.run_cli(
            "achievement-add", "--title", "合成成果",
            "--task-link", task_id, "origin", "首次形成方法",
            "--context", "合成背景", "--outcome", "形成可复用方法",
            "--learning", "复用同一验证入口",
            "--source-ref", "test", "tests/test_lifeos.py", "合成验证",
            "--reuse", "同类验证", "--source", "test fixture",
        ))
        item = next(value for value in self.load_json("achievements.json")["achievements"] if value["id"] == achievement_id)
        self.assertNotIn("evidence", item)
        self.assertEqual("形成可复用方法", item["outcome"])
        self.assertTrue(any(value.get("label") == "合成验证" for value in item["sources"]))

    def test_archiving_idea_requires_reason(self):
        idea_id = self.created_id(self.run_cli("idea-add", "--text", "待判断想法"))
        failed = self.run_cli("idea-update", idea_id, "--status", "archived", check=False)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("--reason", failed.stderr)
        self.run_cli("idea-update", idea_id, "--status", "archived", "--reason", "已确认不再投入")
        idea = next(value for value in self.load_json("ideas.json")["ideas"] if value["id"] == idea_id)
        self.assertEqual("已确认不再投入", idea["status_reason"])

    def test_new_events_only_keep_meaningful_fields(self):
        self.add_work_item()
        event = json.loads((self.data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertNotIn("content_nature", event)
        self.assertNotIn("reversible", event)


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

    def test_window_returns_every_kind_without_filtering(self):
        self.validate_runtime()
        self.run_cli(
            "work-item-add",
            "--title", "窗口测试事项",
            "--next-gate", "确认窗口行为",
            "--source", "合成验证",
        )
        today = json.loads(
            (self.data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )["occurred_at"][:10]

        kinds = {event["kind"] for event in self.changes("--from", today)}

        self.assertIn("work_item_created", kinds)
        self.assertTrue(len(kinds) >= 1)

    def test_default_behavior_without_a_window_is_unchanged(self):
        self.assertEqual(2, len(self.changes()))
        self.assertEqual(1, len(self.changes("--limit", "1")))

    def test_naive_timestamp_is_rejected(self):
        result = self.run_cli("changes", "--from", "2026-07-25T00:00:00", check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("时区偏移", result.stderr)

    def test_inverted_window_is_rejected(self):
        result = self.run_cli(
            "changes", "--from", "2026-07-29", "--to", "2026-07-25", check=False
        )
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
