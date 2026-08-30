import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]


class SkillSyncScriptTest(unittest.TestCase):
    def test_check_is_read_only_and_apply_converges_both_supported_targets(self):
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

                    initial = subprocess.run(
                        [str(script), "--check"],
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertEqual(1, initial.returncode)
                    self.assertFalse(target.exists())

                    subprocess.run(
                        [str(script), "--apply"],
                        check=True,
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertTrue((target / "SKILL.md").is_file())

                    drift = target / "obsolete.txt"
                    drift.write_text("obsolete", encoding="utf-8")
                    checked = subprocess.run(
                        [str(script), "--check"],
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertEqual(1, checked.returncode)
                    self.assertTrue(drift.is_file())

                    subprocess.run(
                        [str(script), "--apply"],
                        check=True,
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertFalse(drift.exists())
                    subprocess.run(
                        [str(script), "--check"],
                        check=True,
                        text=True,
                        capture_output=True,
                        env=environment,
                    )


if __name__ == "__main__":
    unittest.main()
