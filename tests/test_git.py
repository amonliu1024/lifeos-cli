import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifeos_git.store import GitStore, GitStoreError


REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPT = REPO_DIR / "lifeos.py"


@unittest.skipUnless(shutil.which("git"), "git executable is required")
class GitCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name) / "lifeos"
        self.repo_dir = Path(self.temporary_directory.name) / "checkout"
        self.environment = os.environ.copy()
        self.environment["LIFEOS_HOME"] = str(self.data_dir)
        self.data_dir.mkdir()
        (self.data_dir / "projects.json").write_text(
            '{"schema_version":1,"updated_at":"2026-08-27T00:00:00+08:00","projects":[]}\n',
            encoding="utf-8",
        )
        self.run_git("init", str(self.repo_dir))
        self.run_git("-C", str(self.repo_dir), "config", "user.name", "Synthetic User")
        self.run_git("-C", str(self.repo_dir), "config", "user.email", "synthetic@example.invalid")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_git(self, *arguments, env=None, check=True):
        return subprocess.run(
            ["git", *arguments],
            env=env or self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def commit(self, message, timestamp, filename):
        path = self.repo_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(message + "\n", encoding="utf-8")
        self.run_git("-C", str(self.repo_dir), "add", filename)
        commit_env = self.environment.copy()
        commit_env["GIT_AUTHOR_DATE"] = timestamp
        commit_env["GIT_COMMITTER_DATE"] = timestamp
        self.run_git(
            "-C",
            str(self.repo_dir),
            "commit",
            "-m",
            message,
            "-m",
            "Synthetic body",
            env=commit_env,
        )

    def run_lifeos(self, *arguments, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            env=self.environment,
            check=check,
            text=True,
            capture_output=True,
        )

    def test_register_scan_and_show_preserve_minimal_commit_evidence(self):
        self.commit(
            "feat: add Git evidence",
            "2026-08-15T18:20:00+08:00",
            "lifeos_git/core.py",
        )
        self.commit(
            "outside daily window",
            "2026-08-16T01:00:00+08:00",
            "outside.txt",
        )

        added = self.run_lifeos(
            "git",
            "repos",
            "add",
            "--key",
            "lifeos-cli",
            "--root",
            str(self.repo_dir),
            "--json",
        )
        self.assertTrue(json.loads(added.stdout)["changed"])

        scanned = self.run_lifeos(
            "git",
            "scan",
            "--from",
            "2026-08-15",
            "--to",
            "2026-08-16",
            "--json",
        )
        payload = json.loads(scanned.stdout)
        self.assertEqual("complete", payload["status"])
        self.assertEqual(1, payload["summary"]["commits"])
        repository = payload["repositories"][0]
        self.assertEqual("lifeos-cli", repository["repo_key"])
        self.assertEqual(1, len(repository["commits"]))
        commit = repository["commits"][0]
        self.assertEqual("lifeos-cli", commit["repo_key"])
        self.assertEqual("feat: add Git evidence\n\nSynthetic body", commit["commit_message"])
        self.assertEqual("2026-08-15T18:20:00+08:00", commit["committed_at"])
        self.assertEqual(os.path.realpath(self.repo_dir), commit["project_key"])
        self.assertNotIn("ref", commit)
        self.assertNotIn("subject", commit)
        self.assertNotIn("changed_paths", commit)

        scan_id = payload["scan_id"]
        shown = json.loads(self.run_lifeos("git", "show", scan_id, "--json").stdout)
        self.assertEqual(payload, shown)
        self.assertEqual([], json.loads(self.run_lifeos("git", "validate", "--json").stdout)["findings"])

    def test_scan_failure_isolated_to_one_registered_repository(self):
        self.run_lifeos(
            "git",
            "repos",
            "add",
            "--key",
            "lifeos-cli",
            "--root",
            str(self.repo_dir),
        )
        registry = self.data_dir / "git" / "repos.json"
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["repositories"].append(
            {
                "key": "missing-checkout",
                "root": str(Path(self.temporary_directory.name) / "missing"),
                "enabled": True,
            }
        )
        registry.write_text(json.dumps(payload), encoding="utf-8")

        result = self.run_lifeos(
            "git",
            "scan",
            "--from",
            "2026-08-15",
            "--to",
            "2026-08-16",
            "--json",
            check=False,
        )
        self.assertEqual(1, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual("partial", payload["status"])
        self.assertEqual(1, payload["summary"]["repos_failed"])
        failed = next(item for item in payload["repositories"] if item["repo_key"] == "missing-checkout")
        self.assertEqual("failed", failed["status"])
        self.assertTrue(failed["warnings"])

    def test_validate_reports_widened_private_directory_permissions(self):
        self.run_lifeos(
            "git",
            "repos",
            "add",
            "--key",
            "lifeos-cli",
            "--root",
            str(self.repo_dir),
        )
        scan = self.run_lifeos(
            "git",
            "scan",
            "--from",
            "2026-08-15",
            "--to",
            "2026-08-16",
            "--json",
        )
        scan_id = json.loads(scan.stdout)["scan_id"]
        os.chmod(self.data_dir / "git", 0o755)

        result = self.run_lifeos(
            "git", "validate", "--scan", scan_id, "--json", check=False
        )
        self.assertEqual(1, result.returncode)
        findings = json.loads(result.stdout)["findings"]
        self.assertTrue(any("目录权限" in item["problem"] for item in findings))

    def test_validate_reports_an_enabled_repository_with_a_stale_root(self):
        self.run_lifeos(
            "git", "repos", "add", "--key", "lifeos-cli",
            "--root", str(self.repo_dir),
        )
        registry_path = self.data_dir / "git" / "repos.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["repositories"][0]["root"] = str(
            Path(self.temporary_directory.name) / "missing-checkout"
        )
        registry_path.write_text(json.dumps(registry), encoding="utf-8")

        result = self.run_lifeos("git", "validate", "--json", check=False)

        self.assertEqual(1, result.returncode)
        findings = json.loads(result.stdout)["findings"]
        self.assertTrue(any(
            item["scope"] == "repo:lifeos-cli" and "仓库目录不存在" in item["problem"]
            for item in findings
        ))

    def test_registry_and_scan_require_an_explicit_schema1(self):
        store = GitStore(self.data_dir / "git")
        self.run_lifeos(
            "git", "repos", "add", "--key", "lifeos-cli",
            "--root", str(self.repo_dir),
        )
        registry_path = self.data_dir / "git" / "repos.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for version in (None, 2):
            candidate = dict(registry)
            if version is None:
                candidate.pop("schema_version")
            else:
                candidate["schema_version"] = version
            registry_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.subTest(kind="registry", version=version):
                with self.assertRaisesRegex(GitStoreError, "schema_version 必须为 1"):
                    store.load_registry()

        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        self.commit("schema scan", "2026-08-15T18:20:00+08:00", "schema.txt")
        scan = json.loads(self.run_lifeos(
            "git", "scan", "--from", "2026-08-15", "--to", "2026-08-16",
            "--json",
        ).stdout)
        scan_path = self.data_dir / "git" / "scans" / f"{scan['scan_id']}.json"
        for version in (None, 2):
            candidate = dict(scan)
            if version is None:
                candidate.pop("schema_version")
            else:
                candidate["schema_version"] = version
            scan_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.subTest(kind="scan", version=version):
                with self.assertRaisesRegex(GitStoreError, "schema_version 必须为 1"):
                    store.read_scan(scan["scan_id"])


if __name__ == "__main__":
    unittest.main()
