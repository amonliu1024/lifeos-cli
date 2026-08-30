import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifeos_config.core import default_payload
from lifeos_sessions.core import SourceScanRequest, TimeWindow
from lifeos_sessions.smartwork import SmartworkAdapter


FIXTURES = Path(__file__).parent / "fixtures" / "sessions" / "smartwork"
REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"


def slice_dict(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def install_smartwork_fixture(root: Path) -> None:
    shutil.copytree(FIXTURES / "sessions", root / "sessions")
    connection = sqlite3.connect(root / "session-index.sqlite")
    try:
        connection.executescript((FIXTURES / "session-index.sql").read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


class SmartworkAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temporary_directory.name)
        self.root = self.temp_dir / ".SmartWork"
        self.root.mkdir()
        install_smartwork_fixture(self.root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def request(
        self,
        from_value="2026-08-08T00:00:00Z",
        to_value="2026-08-09T00:00:00Z",
        *,
        includes=(),
        checkpoint=None,
    ):
        return SourceScanRequest(
            window=TimeWindow.from_values(from_value, to_value),
            includes=tuple(includes),
            checkpoint=checkpoint,
            temp_dir=self.temp_dir,
        )

    def scan(self, request=None):
        return SmartworkAdapter(root=self.root).scan(request or self.request())

    def test_projects_agent_turns_subagents_and_bounded_content_from_jsonl(self):
        index_before = (self.root / "session-index.sqlite").read_bytes()
        result = self.scan()
        self.assertEqual("complete", result.status)
        self.assertEqual(index_before, (self.root / "session-index.sqlite").read_bytes())
        items = [slice_dict(item) for item in result.slices]
        self.assertTrue(items)
        self.assertTrue(all(item["native_unit"]["kind"] == "turn" for item in items))
        main = next(item for item in items if item["source_meta"]["thread_id"] == "sw-main")
        ordinary = next(item for item in items if item["source_meta"]["thread_id"] == "sw-ordinary")
        self.assertEqual("turn-main", main["native_unit"]["id"])
        self.assertEqual("主会话生成标题", main["conversation"]["title"])
        self.assertEqual("/synthetic/index-workspace", main["workspace"])
        self.assertEqual(
            ["请完成合成检查", "合成检查已完成"],
            [block["text"] for block in main["blocks"]],
        )
        self.assertIn("src/synthetic.py", main["execution_evidence"]["changed_targets"])
        self.assertEqual(
            ["合成工具失败"],
            ordinary["execution_evidence"]["failures"],
        )
        serialized = json.dumps(items, ensure_ascii=False)
        for private_value in (
            "SYSTEM_INJECTION_MUST_NOT_APPEAR",
            "PRIVATE_REASONING_MUST_NOT_APPEAR",
            "PRIVATE_TOOL_OUTPUT_MUST_NOT_APPEAR",
            "PRIVATE_STDOUT_MUST_NOT_APPEAR",
            "PRIVATE_CHILD_OUTPUT_MUST_NOT_APPEAR",
        ):
            self.assertNotIn(private_value, serialized)
        parent = main
        child = next(item for item in items if item["source_meta"]["thread_id"] == "sw-child")
        self.assertEqual("sw-child", child["conversation"]["id"])
        self.assertTrue(child["source_meta"]["is_subagent"])
        self.assertEqual("sw-main", child["source_meta"]["parent_thread_id"])
        self.assertEqual("turn-main", child["source_meta"]["parent_turn_id"])
        self.assertEqual(1, len(parent["delegations"]))
        self.assertEqual("子任务：核对合成文件", parent["delegations"][0]["task"])
        self.assertEqual("子任务核对完成", parent["delegations"][0]["result"])
        main_turns = {
            item["native_unit"]["id"]
            for item in items
            if item["source_meta"]["thread_id"] == "sw-main"
        }
        self.assertEqual({"turn-main"}, main_turns)

    def test_exact_checkpoint_reuses_without_reading_and_append_invalidates(self):
        first = self.scan()
        self.assertIn("cache_generation", first.checkpoint)
        second = self.scan(self.request(checkpoint=first.checkpoint))
        self.assertEqual(0, second.stats["files_read"])
        self.assertFalse(second.slices)
        self.assertEqual(
            {slice_dict(item)["slice_id"] for item in first.slices},
            set(second.reused_slice_ids),
        )
        incomplete_checkpoint = dict(first.checkpoint)
        incomplete_checkpoint.pop("cache_generation")
        rescanned = self.scan(self.request(checkpoint=incomplete_checkpoint))
        self.assertGreater(rescanned.stats["files_read"], 0)

        main_path = self.root / "sessions" / "2026" / "07" / "01" / "rollout-main.jsonl"
        with main_path.open("a", encoding="utf-8") as handle:
            for record in (
                {
                    "timestamp": "2026-08-08T04:00:00Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-appended"},
                },
                {
                    "timestamp": "2026-08-08T04:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "追加合成问题", "id": "append-user"},
                },
                {
                    "timestamp": "2026-08-08T04:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "追加合成结果", "id": "append-complete"},
                },
            ):
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        third = self.scan(self.request(checkpoint=first.checkpoint))
        self.assertGreater(third.stats["files_read"], 0)
        self.assertIn("turn-appended", {slice_dict(item)["native_unit"]["id"] for item in third.slices})

    def test_missing_sqlite_falls_back_to_jsonl_without_failing(self):
        (self.root / "session-index.sqlite").unlink()
        result = self.scan()
        self.assertEqual("complete", result.status)
        self.assertTrue(any(str(warning).startswith("index_unavailable:") for warning in result.warnings))
        self.assertTrue(result.slices)
        main = next(item for item in result.slices if slice_dict(item)["source_meta"]["thread_id"] == "sw-main")
        ordinary = next(item for item in result.slices if slice_dict(item)["source_meta"]["thread_id"] == "sw-ordinary")
        self.assertEqual("主会话生成标题", slice_dict(main)["conversation"]["title"])
        self.assertIsNone(slice_dict(ordinary)["conversation"]["title"])
        self.assertEqual("/synthetic/jsonl-workspace", slice_dict(main)["workspace"])

    def test_untrusted_index_metadata_warns_without_hiding_or_escaping_jsonl(self):
        outside = self.temp_dir / "outside.jsonl"
        outside.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-08T05:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "OUTSIDE_AUTHORITY_MUST_NOT_APPEAR",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.root / "session-index.sqlite")
        try:
            connection.execute(
                "INSERT INTO sessions(id,title,lastAt,lastMessageAt,lastActivityAt,workdir,sessionPath,isSubAgent) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "sw-missing",
                    "缺失合成会话",
                    1786176040000,
                    1786176040000,
                    1786176040000,
                    "/synthetic/missing",
                    "sessions/2026/08/08/rollout-missing.jsonl",
                    0,
                ),
            )
            connection.execute("UPDATE sessions SET lastActivityAt = 1 WHERE id = 'sw-main'")
            connection.execute("UPDATE sessions SET id = 'sw-index-stale' WHERE id = 'sw-ordinary'")
            connection.execute(
                "INSERT INTO sessions(id,title,lastAt,lastMessageAt,lastActivityAt,workdir,sessionPath,isSubAgent) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "sw-outside",
                    "越界合成会话",
                    1786176040000,
                    1786176040000,
                    1786176040000,
                    "/synthetic/outside",
                    str(outside),
                    0,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        result = self.scan()
        self.assertEqual("partial", result.status)
        self.assertTrue(any(str(warning).startswith("index_path_missing:sw-missing") for warning in result.warnings))
        self.assertTrue(result.slices)
        ids = {slice_dict(item)["source_meta"]["thread_id"] for item in result.slices}
        self.assertIn("sw-main", ids)
        self.assertTrue(
            any(
                str(warning).startswith("index_path_outside_sessions:sw-outside")
                for warning in result.warnings
            )
        )
        self.assertNotIn(
            "OUTSIDE_AUTHORITY_MUST_NOT_APPEAR",
            json.dumps([slice_dict(item) for item in result.slices], ensure_ascii=False),
        )
        ordinary = next(
            slice_dict(item)
            for item in result.slices
            if slice_dict(item)["conversation"]["id"] == "sw-ordinary"
        )
        self.assertEqual("sw-ordinary", ordinary["conversation"]["id"])
        self.assertTrue(
            any(
                str(warning).startswith("index_identity_mismatch:")
                for warning in result.warnings
            )
        )
        selected = self.scan(
            self.request(includes=("smartwork:sw-ordinary",))
        )
        self.assertEqual(1, len(selected.slices))
        self.assertEqual(
            "sw-ordinary",
            slice_dict(selected.slices[0])["conversation"]["id"],
        )

    def test_include_selects_one_conversation_without_persistent_scope(self):
        request = self.request(includes=("smartwork:sw-ordinary",))
        result = self.scan(request)
        self.assertEqual("complete", result.status)
        self.assertEqual(1, len(result.slices))
        self.assertEqual("sw-ordinary", slice_dict(result.slices[0])["conversation"]["id"])

        repeated = self.scan(
            self.request(
                includes=("smartwork:sw-ordinary",),
                checkpoint=result.checkpoint,
            )
        )
        self.assertEqual(0, repeated.stats["files_read"])
        self.assertEqual(
            {slice_dict(result.slices[0])["slice_id"]},
            set(repeated.reused_slice_ids),
        )

class SessionsCLIIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.runtime = self.root / "runtime"
        self.home.mkdir()
        self.runtime.mkdir()
        self.config_path = self.root / "config.json"
        config = default_payload()
        config["modules"]["projects"]["roots"] = [str(self.root)]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        codex_fixture = Path(__file__).parent / "fixtures" / "sessions" / "codex"
        shutil.copytree(codex_fixture, self.home / ".codex")
        claude_fixture = Path(__file__).parent / "fixtures" / "sessions" / "claude" / "projects"
        shutil.copytree(claude_fixture, self.home / ".claude" / "projects")
        smartwork_root = self.home / ".SmartWork"
        smartwork_root.mkdir()
        install_smartwork_fixture(smartwork_root)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "LIFEOS_HOME": str(self.runtime),
            "LIFEOS_CONFIG": str(self.config_path),
            "PYTHONPYCACHEPREFIX": str(self.root / "pycache"),
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "sessions", *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_scan_list_show_validate_and_repeat_are_a_runnable_loop(self):
        scan_arguments = (
            "scan",
            "--source",
            "codex",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--json",
        )
        first = self.run_cli(*scan_arguments)
        self.assertEqual(1, first.returncode, first.stderr)
        first_manifest = json.loads(first.stdout)
        self.assertEqual("partial", first_manifest["status"])
        self.assertGreater(first_manifest["sources"][0]["stats"]["created"], 0)

        scans = self.run_cli(
            "scans",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--status",
            "partial",
            "--json",
        )
        self.assertEqual(0, scans.returncode, scans.stderr)
        scan_records = json.loads(scans.stdout)
        self.assertEqual(first_manifest["scan_id"], scan_records[0]["scan_id"])
        self.assertTrue(scan_records[0]["manifest_valid"])
        self.assertEqual("partial", scan_records[0]["status"])
        self.assertEqual("codex", scan_records[0]["sources"][0]["source"])

        other_window = self.run_cli(
            "scans",
            "--from",
            "2026-08-09T00:00:00Z",
            "--to",
            "2026-08-10T00:00:00Z",
            "--json",
        )
        self.assertEqual(0, other_window.returncode, other_window.stderr)
        self.assertEqual([], json.loads(other_window.stdout))

        listed = self.run_cli("list", "--query", "已检查", "--json")
        self.assertEqual(0, listed.returncode, listed.stderr)
        items = json.loads(listed.stdout)
        self.assertTrue(items)
        shown = self.run_cli("show", items[0]["slice_id"], "--json")
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertEqual(items[0]["slice_id"], json.loads(shown.stdout)["slice_id"])
        validated = self.run_cli("validate")
        self.assertEqual(0, validated.returncode, validated.stderr)

        second = self.run_cli(*scan_arguments)
        self.assertEqual(1, second.returncode, second.stderr)
        second_manifest = json.loads(second.stdout)
        self.assertGreater(second_manifest["sources"][0]["stats"]["reused"], 0)
        self.assertEqual(0, second_manifest["sources"][0]["stats"]["files_read"])

    def test_invalid_window_and_read_only_validate_do_not_create_store(self):
        invalid = self.run_cli(
            "scan",
            "--source",
            "codex",
            "--from",
            "2026-08-08T00:00:00",
            "--to",
            "2026-08-09T00:00:00Z",
        )
        self.assertEqual(2, invalid.returncode)
        self.assertFalse((self.runtime / "sessions").exists())
        validated = self.run_cli("validate")
        self.assertEqual(1, validated.returncode)
        self.assertFalse((self.runtime / "sessions").exists())
        listed = self.run_cli("list", "--json")
        self.assertEqual(0, listed.returncode)
        self.assertEqual([], json.loads(listed.stdout))
        scans = self.run_cli(
            "scans",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--json",
        )
        self.assertEqual(0, scans.returncode)
        self.assertEqual([], json.loads(scans.stdout))
        self.assertNotIn("Traceback", listed.stderr)

    def test_projects_are_derived_read_only_from_discovered_manifest(self):
        project_root = self.root / "space-workplace-management-system"
        project_root.mkdir()
        manifest = project_root / "lifeos-project.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "project_key": "space-workplace-management-system",
            "name": "空间与工位管理系统",
            "aliases": [],
            "scope": "project",
            "sources": {"dchat": {"groups": []}, "cooper": {"resources": []}},
        }, ensure_ascii=False), encoding="utf-8")
        result = self.run_cli("projects", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("project-catalog/lifeos-project.json", payload["authority"])
        self.assertEqual(str(project_root), payload["projects"][0]["roots"][0])
        removed = self.run_cli("projects", "add", "--key", "another", "--root", "/another")
        self.assertEqual(2, removed.returncode)

    def test_pack_and_smartwork_scan_are_runnable_public_interfaces(self):
        scanned = self.run_cli(
            "scan",
            "--source",
            "smartwork",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--json",
        )
        self.assertEqual(0, scanned.returncode, scanned.stderr)
        manifest = json.loads(scanned.stdout)
        self.assertEqual(3, manifest["sources"][0]["stats"]["created"])
        listed = self.run_cli("list", "--source", "smartwork", "--json")
        self.assertEqual(3, len(json.loads(listed.stdout)))

        packed = self.run_cli(
            "pack",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--max-bytes",
            "20000",
            "--json",
        )
        self.assertEqual(0, packed.returncode, packed.stderr)
        pack = json.loads(packed.stdout)
        self.assertTrue(pack["pack_id"].startswith("PAK-"))
        self.assertTrue(pack["activities"])
        self.assertLessEqual(pack["byte_size"], 20000)
        self.assertEqual(pack["byte_size"], len(packed.stdout.encode("utf-8")))

    def test_public_help_exposes_only_agent_sources_and_no_persistent_scope(self):
        sessions_help = self.run_cli("--help")
        self.assertEqual(0, sessions_help.returncode, sessions_help.stderr)
        self.assertIn("Agent 应用会话", sessions_help.stdout)
        self.assertIn("可展示 Activity 骨架", sessions_help.stdout)
        self.assertNotIn("全量 Activity 骨架", sessions_help.stdout)
        self.assertNotIn("D-Chat", sessions_help.stdout)
        self.assertNotIn("scope", sessions_help.stdout)

        scan_help = self.run_cli("scan", "--help")
        self.assertEqual(0, scan_help.returncode, scan_help.stderr)
        self.assertIn("codex", scan_help.stdout)
        self.assertIn("claude", scan_help.stdout)
        self.assertIn("smartwork", scan_help.stdout)
        self.assertIn("SOURCE:CONVERSATION-ID", scan_help.stdout)
        self.assertNotIn("--scope", scan_help.stdout)

        unqualified = self.run_cli(
            "scan",
            "--source",
            "codex",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--include",
            "thread-codex-1",
        )
        self.assertEqual(2, unqualified.returncode)
        self.assertIn("source:conversation-id", unqualified.stderr)

        mismatched = self.run_cli(
            "scan",
            "--source",
            "codex",
            "--from",
            "2026-08-08T00:00:00Z",
            "--to",
            "2026-08-09T00:00:00Z",
            "--include",
            "smartwork:sw-main",
        )
        self.assertEqual(2, mismatched.returncode)
        self.assertIn("必须同时出现在 --source 中", mismatched.stderr)

        removed = self.run_cli("scope")
        self.assertEqual(2, removed.returncode)

    def test_each_sessions_help_explains_view_granularity_and_side_effects(self):
        expected = {
            "scan": (
                "ISO-8601", "写入私有 Sessions 派生存储", "omission",
                "all 展开为 codex、claude、smartwork",
            ),
            "rebuild": (
                "staging", "--apply", "active Sessions",
                "all 展开为 codex、claude、smartwork",
            ),
            "list": ("ConversationSlice", "半开窗口", "全文索引"),
            "show": ("SLICE-ID", "不可变历史版本", "执行证据"),
            "validate": ("不创建、不修复", "FTS", "SCAN-ID"),
            "usage": ("真实字节数", "观测增速", "只读诊断"),
            "scans": ("scan manifest", "不回源", "半开窗口"),
            "compact": ("FTS optimize", "VACUUM", "不删除"),
            "prune": ("dry-run", "--apply", "不可逆"),
            "index": (
                "可展示 Activity", "cleaning_summary", "字节预算", "suppressed_activities",
            ),
            "projects": ("只读", "lifeos-project.json", "不再维护第二份私有项目映射"),
            "pack": ("Activity", "--max-bytes", "omission", "均可选", "无筛选时"),
        }
        for command, snippets in expected.items():
            with self.subTest(command=command):
                result = self.run_cli(command, "--help")
                self.assertEqual(0, result.returncode, result.stderr)
                for snippet in snippets:
                    self.assertIn(snippet, result.stdout)


if __name__ == "__main__":
    unittest.main()
