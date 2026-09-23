import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifeos_config.core import ConfigError, default_payload, normalize_config
from tests.test_web import FIXTURE_DIR, write_report


REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"
FAKE_RSYNC = """#!{python}
import os, shutil, sys
args = sys.argv[1:]
with open(os.environ["RSYNC_ARGS_FILE"], "w") as handle:
    handle.write("\\n".join(args))
print("cd+++++++ data/")
for source in args[args.index("--") + 1:-1]:
    shutil.copytree(source, os.path.join(os.environ["RSYNC_CAPTURE"], os.path.basename(source)))
sys.exit(int(os.environ.get("FAKE_RSYNC_EXIT", "0")))
"""


class MirrorCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.runtime = self.root / "runtime"
        self.config_path = self.root / "private" / "config.json"
        self.args_file = self.root / "rsync-args"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        rsync = bin_dir / "rsync"
        rsync.write_text(FAKE_RSYNC.format(python=sys.executable), encoding="utf-8")
        rsync.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment["HOME"] = str(self.root / "home")
        self.environment["LIFEOS_HOME"] = str(self.runtime)
        self.environment["LIFEOS_CONFIG"] = str(self.config_path)
        self.environment["PATH"] = f"{bin_dir}{os.pathsep}{self.environment['PATH']}"
        self.environment["RSYNC_ARGS_FILE"] = str(self.args_file)
        self.capture = self.root / "captured"
        self.capture.mkdir()
        self.environment["RSYNC_CAPTURE"] = str(self.capture)

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

    def write_runtime(self):
        shutil.copytree(FIXTURE_DIR, self.runtime)
        os.chmod(self.runtime, 0o700)
        write_report(self.runtime / "reports")
        (self.runtime / "reports" / ".DS_Store").write_text("finder\n")
        for private in ("dchat", "sessions", "git", "backups"):
            (self.runtime / private).mkdir()
            (self.runtime / private / "evidence.json").write_text("private\n")

    def test_push_sends_necessary_data_and_published_site(self):
        self.write_runtime()
        self.run_cli("mirror", "configure", "--target", "lab:lifeos-mirror")
        result = self.run_cli("mirror", "push", "--json")

        payload = json.loads(result.stdout)
        self.assertEqual(1, payload["reports_published"])
        argv = self.args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("--delete", argv)
        self.assertNotIn("--dry-run", argv)
        self.assertEqual("lab:lifeos-mirror/", argv[-1])
        self.assertEqual(
            ["data", "site"], [Path(item).name for item in argv[argv.index("--") + 1:-1]]
        )

        data = self.capture / "data"
        self.assertEqual(
            {
                "projects.json", "work-items.json", "tasks.json", "events.jsonl",
                "glossary.json", "ideas.json", "achievements.json", "reports",
            },
            {path.name for path in data.iterdir()},
        )
        self.assertEqual(
            ["2026-08-29.md"],
            sorted(path.name for path in (data / "reports").rglob("*") if path.is_file()),
        )
        site = self.capture / "site"
        snapshot = json.loads((site / "api" / "snapshot").read_text(encoding="utf-8"))
        self.assertTrue(snapshot["published"])
        detail = json.loads((site / "api" / "reports" / "2026-08-29").read_text(encoding="utf-8"))
        self.assertIn("只读 Web 工作台", detail["body"])
        self.assertTrue((site / "index.html").is_file())
        self.assertTrue((site / "assets" / "app.js").is_file())
        captured = [path.relative_to(self.capture).as_posix() for path in self.capture.rglob("*")]
        self.assertFalse(
            [item for item in captured if any(
                part in item for part in ("dchat", "sessions", "git", "backups", "config", ".md.")
            ) or item.endswith(("now.md", "projects.md", "tasks.md"))]
        )
        modes = {oct(path.stat().st_mode & 0o777) for path in data.rglob("*") if path.is_file()}
        self.assertEqual({"0o600"}, modes)

    def test_dry_run_does_not_ask_rsync_to_write(self):
        self.write_runtime()
        self.run_cli("mirror", "configure", "--target", "lab:mirror")
        result = self.run_cli("mirror", "push", "--dry-run", "--json")
        argv = self.args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("--dry-run", argv[:argv.index("--")])
        self.assertTrue(json.loads(result.stdout)["dry_run"])
        self.assertIn("cd+++++++ data/", result.stderr)

    def test_push_requires_configured_target(self):
        self.write_runtime()
        result = self.run_cli("mirror", "push", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("尚未配置镜像目标", result.stderr)
        self.assertFalse(self.args_file.exists())

    def test_rsync_failure_is_reported(self):
        self.write_runtime()
        self.run_cli("mirror", "configure", "--target", "lab:mirror")
        self.environment["FAKE_RSYNC_EXIT"] = "255"
        result = self.run_cli("mirror", "push", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("rsync 退出码 255", result.stderr)

    def test_configure_rejects_non_ssh_targets(self):
        for target in (
            "/srv/lifeos", "lab:", "-oProxyCommand=x:y", "lab:-rf", "lab:a b",
            "lab:m;touch${IFS}x", "lab:$(id)", "lab:`id`", "lab:a|b", "lab:a&b", "lab:a'b",
        ):
            result = self.run_cli("mirror", "configure", f"--target={target}", check=False)
            self.assertEqual(1, result.returncode, target)
        self.assertFalse(self.config_path.exists())

    def test_capabilities_report_mirror_state(self):
        before = json.loads(self.run_cli("capabilities", "--json").stdout)
        self.assertEqual("disabled", before["modules"]["mirror"]["status"])
        self.run_cli("mirror", "configure", "--target", "lab:mirror")
        after = json.loads(self.run_cli("capabilities", "--json").stdout)
        self.assertEqual("ready", after["modules"]["mirror"]["status"])


class MirrorConfigTest(unittest.TestCase):
    def test_mirror_is_optional_and_strict(self):
        self.assertIsNone(
            normalize_config(default_payload(), Path("/synthetic"), exists=True).mirror_target
        )
        payload = default_payload()
        payload["modules"]["mirror"] = {"target": "lab:mirror", "password": "x"}
        with self.assertRaisesRegex(ConfigError, "未知字段"):
            normalize_config(payload, Path("/synthetic"), exists=True)


if __name__ == "__main__":
    unittest.main()
