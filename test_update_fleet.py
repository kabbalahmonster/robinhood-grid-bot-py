import os
import subprocess
import tempfile
import unittest
from pathlib import Path


UPDATE_FLEET = Path(__file__).parent / "ops" / "fleet" / "update-fleet"


class UpdateFleetTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"

        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.git("init", "-b", "main", str(self.seed), cwd=self.root)
        (self.seed / "grid_bot.py").write_text("# bot\n", encoding="utf-8")
        (self.seed / "README.md").write_text("initial\n", encoding="utf-8")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "Initial", cwd=self.seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "-u", "origin", "main", cwd=self.seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.git("clone", str(self.remote), str(self.alpha), cwd=self.root)
        self.git("clone", str(self.remote), str(self.beta), cwd=self.root)
        self.original_commit = self.git("rev-parse", "HEAD", cwd=self.alpha).stdout.strip()

        self.config = self.root / "fleet.conf"
        self.config.write_text(
            f'FLEET_ENTRYPOINT="grid_bot.py"\nFLEET_BOT_DIRS=("{self.alpha}" "{self.beta}")\n',
            encoding="utf-8",
        )

        (self.seed / "REMOTE_UPDATE").write_text("new\n", encoding="utf-8")
        self.git("add", "REMOTE_UPDATE", cwd=self.seed)
        self.git("commit", "-m", "Remote update", cwd=self.seed)
        self.git("push", cwd=self.seed)
        self.updated_commit = self.git("rev-parse", "HEAD", cwd=self.seed).stdout.strip()

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, *args, cwd):
        return subprocess.run(
            ["git", "-c", "user.name=Fleet Test", "-c", "user.email=fleet@example.invalid", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )

    def run_update(self):
        return subprocess.run(
            [str(UPDATE_FLEET), "--config", str(self.config)],
            cwd=self.root,
            env={**os.environ, "HOME": str(self.root)},
            text=True,
            capture_output=True,
        )

    def test_untracked_files_do_not_block_updates(self):
        (self.alpha / "amount:").touch()
        (self.beta / "wallet.txt").write_text("private local data\n", encoding="utf-8")

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.alpha).stdout.strip(), self.updated_commit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.beta).stdout.strip(), self.updated_commit)
        self.assertTrue((self.alpha / "amount:").exists())
        self.assertTrue((self.beta / "wallet.txt").exists())

    def test_all_tracked_and_branch_blockers_are_reported_before_any_pull(self):
        (self.alpha / "README.md").write_text("local edit\n", encoding="utf-8")
        self.git("checkout", "--detach", cwd=self.beta)

        result = self.run_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"Tracked modifications: {self.alpha}", result.stderr)
        self.assertIn(f"Detached HEAD: {self.beta}", result.stderr)
        self.assertIn("2 checkout(s) blocked", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.alpha).stdout.strip(), self.original_commit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.beta).stdout.strip(), self.original_commit)


if __name__ == "__main__":
    unittest.main()
