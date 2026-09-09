import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "update-bot"


class UpdateBotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.bot_parent = self.root / "ROBINVAULT"
        self.bot = self.bot_parent / "robinhood-grid-bot-py"
        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.git("init", "-b", "main", str(self.seed), cwd=self.root)
        (self.seed / "grid_bot.py").write_text("# main\n", encoding="utf-8")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "main", cwd=self.seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "-u", "origin", "main", cwd=self.seed)
        self.git("switch", "-c", "canary", cwd=self.seed)
        (self.seed / "CANARY").write_text("yes\n", encoding="utf-8")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "canary", cwd=self.seed)
        self.git("push", "-u", "origin", "canary", cwd=self.seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.bot_parent.mkdir()
        self.git("clone", str(self.remote), str(self.bot), cwd=self.root)
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            f'FLEET_ENTRYPOINT="grid_bot.py"\nFLEET_BOT_DIRS=("{self.bot}")\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, *args, cwd):
        return subprocess.run(
            ["git", "-c", "user.name=Fleet Test", "-c", "user.email=fleet@example.invalid", *args],
            cwd=cwd, text=True, capture_output=True, check=True,
        )

    def run_script(self, *args):
        return subprocess.run(
            [str(SCRIPT), "ROBINVAULT", "--config", str(self.config), *args],
            cwd=self.root, env={**os.environ, "HOME": str(self.root)},
            text=True, capture_output=True,
        )

    def test_lists_local_and_remote_branches(self):
        result = self.run_script("--list-branches")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Current:  main", result.stdout)
        self.assertIn("origin/canary", result.stdout)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.bot).stdout.strip(), "main")

    def test_switches_to_remote_branch_and_back_to_main(self):
        switched = self.run_script("--branch", "canary", "--no-restart")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.bot).stdout.strip(), "canary")
        self.assertTrue((self.bot / "CANARY").exists())

        returned = self.run_script("--branch", "main", "--no-restart")
        self.assertEqual(returned.returncode, 0, returned.stderr)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.bot).stdout.strip(), "main")

    def test_default_fast_forwards_current_tracking_branch(self):
        self.git("switch", "main", cwd=self.seed)
        (self.seed / "UPDATE").write_text("new\n", encoding="utf-8")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "update main", cwd=self.seed)
        self.git("push", cwd=self.seed)
        expected = self.git("rev-parse", "HEAD", cwd=self.seed).stdout.strip()

        result = self.run_script("--no-restart")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.bot).stdout.strip(), expected)

    def test_divergence_is_refused_without_moving_head(self):
        self.git("switch", "main", cwd=self.seed)
        (self.seed / "REMOTE").write_text("remote\n", encoding="utf-8")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "remote", cwd=self.seed)
        self.git("push", cwd=self.seed)
        (self.bot / "LOCAL").write_text("local\n", encoding="utf-8")
        self.git("add", ".", cwd=self.bot)
        self.git("commit", "-m", "local", cwd=self.bot)
        original = self.git("rev-parse", "HEAD", cwd=self.bot).stdout.strip()

        result = self.run_script("--no-restart")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("diverged", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.bot).stdout.strip(), original)

    def test_check_does_not_switch(self):
        result = self.run_script("--branch", "canary", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would create local tracking branch canary", result.stdout)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.bot).stdout.strip(), "main")

    def test_tracked_changes_block_switch_but_untracked_files_survive(self):
        (self.bot / "grid_bot.py").write_text("dirty\n", encoding="utf-8")
        blocked = self.run_script("--branch", "canary", "--no-restart")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Tracked modifications", blocked.stderr)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.bot).stdout.strip(), "main")

        self.git("restore", "grid_bot.py", cwd=self.bot)
        (self.bot / "runtime.log").write_text("preserve\n", encoding="utf-8")
        result = self.run_script("--branch", "canary", "--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.bot / "runtime.log").exists())


if __name__ == "__main__":
    unittest.main()
