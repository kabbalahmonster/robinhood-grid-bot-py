import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / "ops" / "fleet" / "fleet-membership"

class FleetMembershipTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.bot_root = self.root / "bots"
        for name in ("alpha", "beta", "gamma"):
            (self.bot_root / name / "checkout").mkdir(parents=True)
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            f'FLEET_BOT_ROOT="{self.bot_root}"\nFLEET_CHECKOUT_DIRNAME="checkout"\nFLEET_BOT_NAMES=(alpha beta)\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_script(self, *args):
        return subprocess.run(
            [SCRIPT, "--config", self.config, *args],
            env={**os.environ, "HOME": str(self.root)}, text=True, capture_output=True,
        )

    def effective_names(self):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s\\n" "${FLEET_BOT_NAMES[@]}"', "bash", self.config],
            text=True, capture_output=True, check=True,
        )
        return result.stdout.splitlines()

    def test_add_preview_changes_nothing(self):
        before = self.config.read_text(encoding="utf-8")
        result = self.run_script("add", "gamma")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("After  (3): alpha beta gamma", result.stdout)
        self.assertIn("PREVIEW ONLY", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_add_and_remove_only_change_effective_config_list(self):
        added = self.run_script("--apply", "add", "gamma")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.effective_names(), ["alpha", "beta", "gamma"])
        removed = self.run_script("--apply", "remove", "alpha,gamma")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.effective_names(), ["beta"])
        text = self.config.read_text(encoding="utf-8")
        self.assertEqual(text.count("# BEGIN fleet-membership managed override"), 1)
        for name in ("alpha", "beta", "gamma"):
            self.assertTrue((self.bot_root / name / "checkout").is_dir())

    def test_add_requires_existing_checkout(self):
        result = self.run_script("--apply", "add", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Bot checkout does not exist", result.stderr)
        self.assertEqual(self.effective_names(), ["alpha", "beta"])

    def test_rejects_duplicate_unknown_remove_and_empty_fleet(self):
        duplicate = self.run_script("add", "alpha")
        unknown = self.run_script("remove", "gamma")
        empty = self.run_script("remove", "alpha,beta")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("Refusing to remove every bot", empty.stderr)

    def test_rejects_explicit_directory_membership(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8") + 'FLEET_BOT_DIRS=("/tmp/bot")\n', encoding="utf-8",
        )
        result = self.run_script("add", "gamma")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FLEET_BOT_DIRS is configured", result.stderr)

if __name__ == "__main__":
    unittest.main()
