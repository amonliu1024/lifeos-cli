import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]


class SkillSyncScriptTest(unittest.TestCase):
    def test_syncs_supported_skill_targets(self):
        targets = {
            "sync-to-ccswitch.sh": ".cc-switch/skills/lifeos",
            "sync-to-smartwork.sh": ".SmartWork/skills/lifeos",
        }
        with tempfile.TemporaryDirectory() as temporary:
            test_home = Path(temporary)
            environment = {**os.environ, "HOME": str(test_home)}

            for script_name, relative_target in targets.items():
                with self.subTest(script=script_name):
                    script = REPO_DIR / "scripts" / script_name
                    target = test_home / relative_target

                    synced = subprocess.run(
                        [str(script)],
                        check=True,
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertTrue((target / "SKILL.md").is_file())
                    self.assertIn("  OK: lifeos", synced.stdout)
                    self.assertIn("=== Done: 1 skill synced ===", synced.stdout)

                    drift = target / "obsolete.txt"
                    drift.write_text("obsolete", encoding="utf-8")
                    subprocess.run(
                        [str(script)],
                        check=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertFalse(drift.exists())


if __name__ == "__main__":
    unittest.main()
