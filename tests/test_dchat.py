import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lifeos_config.core import configure_dchat, default_payload, load_config
from lifeos_dchat.client import DChatClientError, DwsDChatAdapter
from lifeos_dchat.core import DChatService, TimeWindow
from lifeos_dchat.evidence import build_index, build_pack
from lifeos_dchat.store import DChatStore, DChatStoreError


REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"


class FakeDChatClient:
    def __init__(self, chats, messages):
        self.chats = chats
        self.messages = messages
        self.calls = []
        self.failures = {}

    def list_chats(self):
        return self.chats

    def dump_messages(self, conversation_id, from_value, to_value, limit):
        self.calls.append((conversation_id, from_value, to_value, limit))
        if conversation_id in self.failures:
            raise DChatClientError(self.failures[conversation_id], "synthetic failure")
        start = datetime.fromisoformat(from_value)
        end = datetime.fromisoformat(to_value)
        selected = [
            item for item in self.messages.get(conversation_id, [])
            if start <= datetime.fromisoformat(item["timestamp"]) < end
        ]
        return selected[-limit:]


def message(key, timestamp, text):
    return {"key": key, "timestamp": timestamp, "text": text, "attachment": {"kind": "metadata-only"}}


class DChatScopeTest(unittest.TestCase):
    def test_scope_reads_all_direct_types_regardless_of_account_name_and_only_tagged_groups(self):
        chats = [
            {"vchannel_id": "dm", "type": "p2p", "name": "项目小助手"},
            {"vchannel_id": "ext-dm", "type": "extp2p", "name": "Official-looking account"},
            {"vchannel_id": "project", "type": "channel", "tag_ids": ["lifeos"], "name": "Silent project"},
            {"vchannel_id": "ops", "type": "channel", "tag_ids": [], "name": "Busy ops"},
            {"vchannel_id": "bad-tags", "type": "channel", "name": "Missing tags"},
            {"vchannel_id": "bot", "type": "p2bot", "tag_ids": ["lifeos"]},
        ]
        messages = {
            key: [message(f"{key}-1", "2026-08-24T08:00:00+00:00", "synthetic")]
            for key in ("dm", "ext-dm", "project", "ops", "bad-tags", "bot")
        }
        client = FakeDChatClient(chats, messages)
        result = DChatService(client, "lifeos", limit=10).scan(
            TimeWindow.from_values("2026-08-24", "2026-08-25")
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual({"dm", "ext-dm", "project"}, {call[0] for call in client.calls})
        self.assertEqual(3, result["summary"]["collect_body"])
        self.assertEqual(2, result["summary"]["metadata_only"])
        self.assertEqual(1, result["summary"]["excluded"])
        project = next(row for row in result["conversations"] if row["conversation_id"] == "project")
        self.assertEqual(1, len(project["messages"]))
        ops = next(row for row in result["conversations"] if row["conversation_id"] == "ops")
        self.assertEqual([], ops["messages"])

    def test_inventory_latest_ts_skips_only_conversations_quiet_before_window(self):
        old_ms = int(datetime.fromisoformat("2026-08-23T23:59:59+08:00").timestamp() * 1000)
        chats = [
            {"vchannel_id": "old-dm", "type": "p2p", "latest_ts": old_ms},
            {"vchannel_id": "old-group", "type": "channel", "tag_ids": ["lifeos"], "latest_ts": str(old_ms)},
            {"vchannel_id": "at-start", "type": "p2p", "latest_ts": "2026-08-24T00:00:00+08:00"},
            {"vchannel_id": "after-window", "type": "p2p", "latest_ts": "2026-08-25T08:00:00+08:00"},
            {"vchannel_id": "missing", "type": "p2p"},
            {"vchannel_id": "invalid", "type": "p2p", "latest_ts": "not-a-time"},
        ]
        client = FakeDChatClient(chats, {})

        result = DChatService(client, "lifeos", limit=10).scan(
            TimeWindow.from_values("2026-08-24", "2026-08-25")
        )

        self.assertEqual(
            {"at-start", "after-window", "missing", "invalid"},
            {call[0] for call in client.calls},
        )
        self.assertEqual(4, result["summary"]["body_queries"])
        self.assertEqual(2, result["summary"]["skipped_before_window"])
        self.assertEqual(6, result["summary"]["collect_body"])
        old_dm = next(row for row in result["conversations"] if row["conversation_id"] == "old-dm")
        self.assertEqual("complete", old_dm["status"])
        self.assertEqual([], old_dm["messages"])
        self.assertEqual("inventory_latest_ts", old_dm["windows"][0]["strategy"])
        self.assertFalse(old_dm["windows"][0]["queried"])

    def test_limit_recursively_splits_or_reports_truncation(self):
        chat = [{"vchannel_id": "dm", "type": "p2p"}]
        client = FakeDChatClient(chat, {
            "dm": [
                message("m1", "2026-08-24T01:00:00+00:00", "one"),
                message("m2", "2026-08-24T09:00:00+00:00", "two"),
            ]
        })
        result = DChatService(client, "lifeos", limit=2).scan(
            TimeWindow.from_values("2026-08-24", "2026-08-25")
        )
        row = result["conversations"][0]
        self.assertEqual("complete", row["status"])
        self.assertEqual({"m1", "m2"}, {item["message_key"] for item in row["messages"]})
        self.assertGreater(len(client.calls), 1)

        crowded = FakeDChatClient(chat, {
            "dm": [
                message("same-1", "2026-08-24T08:00:00+00:00", "one"),
                message("same-2", "2026-08-24T08:00:00+00:00", "two"),
            ]
        })
        result = DChatService(crowded, "lifeos", limit=2).scan(
            TimeWindow.from_values("2026-08-24T15:59:59+08:00", "2026-08-24T16:00:01+08:00")
        )
        self.assertEqual("partial", result["status"])
        self.assertIn("range_truncated", result["conversations"][0]["warnings"])

    def test_missing_message_time_reports_bounded_shape_diagnostics(self):
        class RawClient(FakeDChatClient):
            def dump_messages(self, conversation_id, from_value, to_value, limit):
                self.calls.append((conversation_id, from_value, to_value, limit))
                return [
                    {"key": f"missing-{index}", "createTime": "not-a-time", "text": "secret"}
                    for index in range(10)
                ]

        client = RawClient([{"vchannel_id": "dm", "type": "p2p"}], {})
        result = DChatService(client, "lifeos", limit=20).scan(
            TimeWindow.from_values("2026-08-24", "2026-08-25")
        )

        row = result["conversations"][0]
        details = [item for item in row["warnings"] if "message_ref=" in item]
        self.assertEqual("partial", row["status"])
        self.assertEqual(8, len(details))
        self.assertTrue(all("timestamp_fields=createTime:str" in item for item in details))
        self.assertIn("message_time_missing:omitted=2", row["warnings"])
        self.assertNotIn("secret", " ".join(row["warnings"]))

    def test_created_ts_is_accepted_as_message_time(self):
        class RawClient(FakeDChatClient):
            def dump_messages(self, conversation_id, from_value, to_value, limit):
                self.calls.append((conversation_id, from_value, to_value, limit))
                return [{
                    "key": "created-ts",
                    "created_at": "not-an-iso-time",
                    "created_ts": 1787875200000,
                }]

        client = RawClient([{"vchannel_id": "dm", "type": "p2p"}], {})
        result = DChatService(client, "lifeos", limit=20).scan(
            TimeWindow.from_values("2026-08-28", "2026-08-29")
        )

        row = result["conversations"][0]
        self.assertEqual("complete", row["status"])
        self.assertEqual([], row["warnings"])
        self.assertEqual("2026-08-28T00:00:00+00:00", row["messages"][0]["occurred_at"])


class DChatStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "lifeos" / "dchat"
        self.store = DChatStore(self.root)
        self.window = TimeWindow.from_values("2026-08-24", "2026-08-25")

    def tearDown(self):
        self.temporary.cleanup()

    def scan(self, text):
        client = FakeDChatClient(
            [{"vchannel_id": "dm", "type": "p2p", "name": "Synthetic Human"}],
            {"dm": [message("stable-key", "2026-08-24T08:00:00+00:00", text)]},
        )
        return self.store.write_scan(DChatService(client, "lifeos", limit=10).scan(self.window))

    def test_raw_revisions_are_immutable_and_views_are_supporting(self):
        first = self.scan("original")
        repeated = self.scan("original")
        self.assertNotEqual(first["scan_id"], repeated["scan_id"])
        self.assertEqual(1, self.store.usage()["revisions"])

        changed = self.scan("edited")
        self.assertEqual("complete", changed["status"])
        self.assertEqual(2, self.store.usage()["revisions"])
        start, end = self.window.query_bounds()
        rows = self.store.query_messages(start, end)
        self.assertEqual(1, len(rows))
        current = self.store.read_revision(rows[0]["json_path"])
        self.assertEqual("edited", current["payload"]["text"])

        index = build_index(self.store, start, end)
        self.assertEqual("supporting", index["evidence_level"])
        self.assertEqual(1, index["summary"]["messages"])
        packed = build_pack(self.store, start, end, None, 100_000)
        self.assertEqual("supporting", packed["evidence_level"])
        self.assertEqual("edited", packed["messages"][0]["payload"]["text"])

    def test_project_candidates_are_supplied_by_project_manifests(self):
        self.scan("project discussion")
        start, end = self.window.query_bounds()
        display = self.window.to_dict()
        index = build_index(
            self.store, start, end,
            source_window=(display["from"], display["to"]),
            project_rows=[{"conversation_id": "dm", "projects": ["alpha", "beta"]}],
        )
        self.assertTrue(index["conversations"][0]["projects_confirmed"])
        self.assertEqual(["alpha", "beta"], index["conversations"][0]["project_candidates"])

    def test_tag_removal_stops_reads_without_erasing_archived_body(self):
        tagged = FakeDChatClient(
            [{"vchannel_id": "group", "type": "channel", "tag_ids": ["lifeos"]}],
            {"group": [message("group-key", "2026-08-24T08:00:00+00:00", "kept")]},
        )
        self.store.write_scan(DChatService(tagged, "lifeos", limit=10).scan(self.window))
        untagged = FakeDChatClient(
            [{"vchannel_id": "group", "type": "channel", "tag_ids": []}],
            {"group": [message("new-key", "2026-08-24T09:00:00+00:00", "must not read")]},
        )
        self.store.write_scan(DChatService(untagged, "lifeos", limit=10).scan(self.window))
        self.assertEqual([], untagged.calls)
        start, end = self.window.query_bounds()
        display = self.window.to_dict()
        index = build_index(
            self.store, start, end,
            source_window=(display["from"], display["to"]),
        )
        self.assertEqual(1, index["summary"]["messages"])
        self.assertEqual("metadata_only", index["conversations"][0]["scope"])
        self.assertIn("body_not_read_in_source_scan", index["conversations"][0]["warnings"])

    def test_runtime_permissions_and_validation(self):
        self.scan("private")
        self.assertEqual(0o700, stat.S_IMODE(self.root.stat().st_mode))
        for path in self.root.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(expected, stat.S_IMODE(path.stat().st_mode), path)
        self.assertEqual([], self.store.validate(set()))
        self.assertFalse((self.root.parent / "work").exists())
        self.assertFalse((self.root.parent / "sessions").exists())
        self.assertFalse((self.root.parent / "git").exists())
        self.assertFalse((self.root.parent / "reports").exists())

    def test_validate_ignores_finder_metadata_but_keeps_managed_file_permission_checks(self):
        manifest = self.scan("private")
        usage = self.store.usage()
        finder_files = [self.root / ".DS_Store", self.root / "messages" / ".DS_Store"]
        for path in finder_files:
            path.write_bytes(b"synthetic finder metadata")
            os.chmod(path, 0o644)

        self.assertEqual([], self.store.validate(set()))
        self.assertEqual(usage, self.store.usage())

        manifest_path = self.root / "scans" / f"{manifest['scan_id']}.json"
        os.chmod(manifest_path, 0o644)
        findings = self.store.validate(set())
        self.assertTrue(any(
            item["scope"] == str(manifest_path) and item["problem"] == "文件权限应为 0o600"
            for item in findings
        ))

    def test_validate_reports_broken_scan_manifest_before_daily_reads_it(self):
        manifest = self.scan("private")
        path = self.root / "scans" / f"{manifest['scan_id']}.json"
        path.write_text("{broken", encoding="utf-8")
        findings = self.store.validate(set())
        self.assertTrue(any(item["problem"] == "scan manifest 不可读" for item in findings))

    def test_persisted_json_requires_an_explicit_schema1(self):
        manifest = self.scan("private")
        manifest_path = self.root / "scans" / f"{manifest['scan_id']}.json"
        original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for version in (None, 2):
            candidate = dict(original_manifest)
            if version is None:
                candidate.pop("schema_version")
            else:
                candidate["schema_version"] = version
            manifest_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.subTest(kind="scan", version=version):
                with self.assertRaisesRegex(DChatStoreError, "schema_version 必须为 1"):
                    self.store.read_scan(manifest["scan_id"])
                self.assertTrue(any(
                    "scan manifest schema_version 必须为 1" in item["problem"]
                    for item in self.store.validate(set())
                ))
        manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")

        start, end = self.window.query_bounds()
        revision_row = self.store.query_messages(start, end)[0]
        revision_path = self.root / revision_row["json_path"]
        original_revision = json.loads(revision_path.read_text(encoding="utf-8"))
        for version in (None, 2):
            candidate = dict(original_revision)
            if version is None:
                candidate.pop("schema_version")
            else:
                candidate["schema_version"] = version
            revision_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.subTest(kind="revision", version=version):
                with self.assertRaisesRegex(DChatStoreError, "schema_version 必须为 1"):
                    self.store.read_revision(revision_row["json_path"])
                self.assertTrue(any(
                    "revision schema_version 必须为 1" in item["problem"]
                    for item in self.store.validate(set())
                ))
        revision_path.write_text(json.dumps(original_revision), encoding="utf-8")

        metadata_path = self.root / original_manifest["metadata_ref"]
        original_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for version in (None, 2):
            metadata = dict(original_metadata)
            if version is None:
                metadata.pop("schema_version")
            else:
                metadata["schema_version"] = version
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.subTest(kind="metadata", version=version):
                self.assertTrue(any(
                    item["problem"] == "metadata snapshot schema_version 必须为 1"
                    for item in self.store.validate(set())
                ))


class DChatConfigTest(unittest.TestCase):
    def test_config_is_private_idempotent_and_does_not_return_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "private" / "config.json"
            wrapper = Path(directory) / "dws-wrapper"
            wrapper.write_text("synthetic", encoding="utf-8")
            first = configure_dchat("opaque-tag", str(wrapper), config_path)
            second = configure_dchat("opaque-tag", str(wrapper), config_path)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertNotIn("attention_tag_id", first)
            self.assertEqual(0o600, stat.S_IMODE(config_path.stat().st_mode))
            config = load_config(config_path, allow_missing=False)
            self.assertEqual("opaque-tag", config.dchat.attention_tag_id)

    def test_dws_adapter_uses_export_files_without_requiring_executable_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "wrapper.sh"
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$1\" == --debug ]]; then shift; fi\n"
                "for last; do true; done\n"
                "if [[ \"$1\" == chat ]]; then\n"
                "  printf '%s' '{\"ok\":true,\"data\":{\"chats\":[{\"vchannel_id\":\"dm\",\"type\":\"p2p\"}]}}' > \"$last\"\n"
                "else\n"
                "  printf '%s' '{\"ok\":true,\"data\":{\"messages\":[{\"key\":\"m1\"}]}}' > \"$last\"\n"
                "fi\n",
                encoding="utf-8",
            )
            os.chmod(wrapper, 0o600)
            client = DwsDChatAdapter(str(wrapper))
            self.assertEqual("dm", client.list_chats()[0]["vchannel_id"])
            self.assertEqual("m1", client.dump_messages("dm", "2026-08-24", "2026-08-25", 10)[0]["key"])

    def test_dws_adapter_reports_sandbox_ipc_denial_without_desktop_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "wrapper.sh"
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' 'error: failed to dial socket for workspace-server `ipc:///tmp/dc-workspace-server.prod.socket`: Permission denied' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            client = DwsDChatAdapter(str(wrapper))
            with self.assertRaises(DChatClientError) as caught:
                client.list_chats()
            self.assertEqual("client_ipc_forbidden", caught.exception.kind)
            self.assertIn("重跑同一条", str(caught.exception))
            self.assertIn("不要改用桌面操作", str(caught.exception))


class DChatCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "lifeos"
        self.environment = os.environ.copy()
        self.environment["LIFEOS_HOME"] = str(self.data_dir)
        self.environment["LIFEOS_CONFIG"] = str(Path(self.temporary.name) / "config.json")
        self.wrapper = Path(self.temporary.name) / "fake-dws.sh"
        self.wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"$1\" == --debug ]]; then shift; fi\n"
            "for last; do true; done\n"
            "if [[ \"$1\" == chat ]]; then\n"
            "  printf '%s' '{\"ok\":true,\"data\":{\"chats\":["
            "{\"vchannel_id\":\"dm\",\"type\":\"p2p\",\"name\":\"Synthetic DM\"},"
            "{\"vchannel_id\":\"project\",\"type\":\"channel\",\"tag_ids\":[\"focus\"]},"
            "{\"vchannel_id\":\"ops\",\"type\":\"channel\",\"tag_ids\":[]}]}}' > \"$last\"\n"
            "elif [[ \"$4\" == dm ]]; then\n"
            "  printf '%s' '{\"ok\":true,\"data\":{\"messages\":[{\"key\":\"dm-1\",\"timestamp\":\"2026-08-24T08:00:00+00:00\",\"text\":\"synthetic direct\"}]}}' > \"$last\"\n"
            "else\n"
            "  printf '%s' '{\"ok\":true,\"data\":{\"messages\":[{\"key\":\"group-1\",\"timestamp\":\"2026-08-24T09:00:00+00:00\",\"text\":\"synthetic group\"}]}}' > \"$last\"\n"
            "fi\n",
            encoding="utf-8",
        )
        self.data_dir.mkdir(parents=True)
        for index, key in enumerate(("alpha", "beta"), start=1):
            project_root = Path(self.temporary.name) / key
            project_root.mkdir()
            manifest = project_root / "lifeos-project.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "project_key": key,
                "name": key.title(),
                "aliases": [],
                "scope": "project",
                "sources": {"dchat": {"groups": [{
                    "vid": "123456",
                    "name": "Synthetic Project",
                    "description": "合成项目群",
                }]}, "cooper": {"resources": []}},
            }), encoding="utf-8")
        config = default_payload()
        config["modules"]["projects"]["roots"] = [str(Path(self.temporary.name))]
        Path(self.environment["LIFEOS_CONFIG"]).write_text(
            json.dumps(config), encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_lifeos(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def test_public_cli_scan_index_pack_projects_usage_and_validate(self):
        configured = json.loads(self.run_lifeos(
            "dchat", "configure", "--attention-tag-id", "focus",
            "--dws-wrapper", str(self.wrapper), "--json",
        ).stdout)
        self.assertTrue(configured["changed"])
        self.assertNotIn("attention_tag_id", configured)

        scanned = json.loads(self.run_lifeos(
            "dchat", "scan", "--from", "2026-08-24", "--to", "2026-08-25", "--json",
        ).stdout)
        self.assertEqual("complete", scanned["status"])
        self.assertEqual(2, scanned["summary"]["messages"])
        self.assertEqual(1, scanned["summary"]["metadata_only"])

        indexed = json.loads(self.run_lifeos(
            "dchat", "index", "--from", "2026-08-24", "--to", "2026-08-25", "--json",
        ).stdout)
        self.assertEqual("supporting", indexed["evidence_level"])
        self.assertEqual(2, indexed["summary"]["messages"])
        packed = json.loads(self.run_lifeos(
            "dchat", "pack", "--from", "2026-08-24", "--to", "2026-08-25", "--max-bytes", "100000", "--json",
        ).stdout)
        self.assertEqual(2, len(packed["messages"]))

        listed = json.loads(self.run_lifeos("dchat", "projects", "list", "--json").stdout)
        self.assertEqual(1, listed["total"])
        self.assertEqual(["alpha", "beta"], listed["projects"][0]["projects"])
        usage = json.loads(self.run_lifeos("dchat", "usage", "--json").stdout)
        self.assertEqual(2, usage["messages"])
        self.assertTrue(json.loads(self.run_lifeos("dchat", "validate", "--json").stdout)["ok"])

    def test_validate_distinguishes_an_unconfigured_optional_domain(self):
        result = self.run_lifeos("dchat", "validate", "--json", check=False)
        self.assertEqual(1, result.returncode)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("config", payload["findings"][0]["scope"])
        self.assertFalse((self.data_dir / "dchat").exists())

    def test_read_only_commands_do_not_initialize_an_unused_store(self):
        commands = [
            ("dchat", "scans", "--json"),
            ("dchat", "index", "--from", "2026-08-24", "--to", "2026-08-25", "--json"),
            ("dchat", "pack", "--from", "2026-08-24", "--to", "2026-08-25", "--json"),
            ("dchat", "projects", "list", "--json"),
            ("dchat", "usage", "--json"),
        ]
        for command in commands:
            self.run_lifeos(*command)
            self.assertFalse((self.data_dir / "dchat").exists(), command)

    def test_project_mapping_write_command_is_removed(self):
        rejected = self.run_lifeos(
            "dchat", "projects", "set", "--conversation", "project",
            check=False,
        )
        self.assertEqual(2, rejected.returncode)
        self.assertIn("invalid choice", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
