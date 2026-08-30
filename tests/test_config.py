import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifeos_config.core import ConfigError, default_payload, normalize_config
from lifeos_sessions.adapters import resolve_selected_sources


REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"


class ConfigCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config_path = self.root / "private" / "config.json"
        self.environment = os.environ.copy()
        self.environment["HOME"] = str(self.home)
        self.environment["LIFEOS_HOME"] = str(self.root / "runtime")
        self.environment["LIFEOS_CONFIG"] = str(self.config_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def test_init_validate_and_no_overwrite(self):
        created = self.run_cli("config", "init", "--json")
        payload = json.loads(created.stdout)
        self.assertEqual("created", payload["status"])
        self.assertEqual(default_payload(), json.loads(self.config_path.read_text()))
        self.assertEqual(0o600, stat.S_IMODE(self.config_path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.config_path.parent.stat().st_mode))

        validated = self.run_cli("config", "validate", "--json")
        self.assertEqual("valid", json.loads(validated.stdout)["status"])
        repeated = self.run_cli("config", "init", check=False)
        self.assertEqual(1, repeated.returncode)
        self.assertIn("已存在", repeated.stderr)

    def test_capabilities_do_not_create_config_or_runtime(self):
        result = self.run_cli("capabilities", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual("unconfigured", payload["config"]["status"])
        self.assertEqual("ready", payload["modules"]["work"]["status"])
        self.assertEqual("disabled", payload["modules"]["dchat"]["status"])
        self.assertFalse(self.config_path.exists())
        self.assertFalse((self.root / "runtime").exists())

    def test_enabled_dchat_reports_ready_only_for_existing_wrapper(self):
        wrapper = self.root / "dws"
        wrapper.write_text("synthetic", encoding="utf-8")
        payload = default_payload()
        payload["modules"]["dchat"] = {
            "enabled": True,
            "dws_wrapper": str(wrapper),
        }
        self.config_path.parent.mkdir()
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_cli("capabilities", "--json")
        self.assertEqual(
            "ready", json.loads(result.stdout)["modules"]["dchat"]["status"]
        )

    def test_disabling_every_session_source_makes_scan_fail_without_runtime(self):
        payload = default_payload()
        payload["modules"]["sessions"]["sources"] = []
        self.config_path.parent.mkdir()
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_cli(
            "sessions", "scan", "--source", "all",
            "--from", "2026-08-28T00:00:00+08:00",
            "--to", "2026-08-29T00:00:00+08:00",
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("没有启用", result.stderr)
        self.assertFalse((self.root / "runtime").exists())

    def test_project_roots_are_managed_through_public_config_commands(self):
        project_root = self.root / "projects"
        project_root.mkdir()
        added = self.run_cli(
            "config", "project-root", "add", str(project_root), "--json"
        )
        self.assertTrue(json.loads(added.stdout)["changed"])
        listed = json.loads(self.run_cli(
            "config", "project-root", "list", "--json"
        ).stdout)
        self.assertEqual([str(project_root)], listed["roots"])
        removed = self.run_cli(
            "config", "project-root", "remove", str(project_root), "--json"
        )
        self.assertTrue(json.loads(removed.stdout)["changed"])


class ConfigValidationTest(unittest.TestCase):
    def test_unknown_and_credential_fields_are_rejected(self):
        payload = default_payload()
        payload["token"] = "must-not-be-accepted"
        with self.assertRaisesRegex(ConfigError, "未知字段"):
            normalize_config(payload, Path("/synthetic"), exists=True)

    def test_enabled_dchat_requires_wrapper(self):
        payload = default_payload()
        payload["modules"]["dchat"]["enabled"] = True
        with self.assertRaisesRegex(ConfigError, "必须配置"):
            normalize_config(payload, Path("/synthetic"), exists=True)

    def test_source_names_are_unique_and_known(self):
        payload = default_payload()
        payload["modules"]["sessions"]["sources"] = ["codex", "codex"]
        with self.assertRaisesRegex(ConfigError, "重复"):
            normalize_config(payload, Path("/synthetic"), exists=True)
        payload = default_payload()
        payload["modules"]["sessions"]["sources"] = ["unknown"]
        with self.assertRaisesRegex(ConfigError, "未知值"):
            normalize_config(payload, Path("/synthetic"), exists=True)

    def test_session_source_registry_respects_enabled_set(self):
        self.assertEqual(
            ("codex", "claude"),
            resolve_selected_sources(["all"], ["codex", "claude"]),
        )
        with self.assertRaisesRegex(ValueError, "未启用"):
            resolve_selected_sources(["smartwork"], ["codex", "claude"])

    def test_project_roots_must_be_absolute_and_excludes_are_names(self):
        payload = default_payload()
        payload["modules"]["projects"]["roots"] = ["relative"]
        with self.assertRaisesRegex(ConfigError, "绝对路径"):
            normalize_config(payload, Path("/synthetic"), exists=True)
        payload = default_payload()
        payload["modules"]["projects"]["exclude"] = ["nested/cache"]
        with self.assertRaisesRegex(ConfigError, "相对目录名"):
            normalize_config(payload, Path("/synthetic"), exists=True)


if __name__ == "__main__":
    unittest.main()
