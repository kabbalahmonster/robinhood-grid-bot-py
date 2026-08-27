import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_SCRIPT = Path(__file__).parent / "ops" / "fleet" / "update-this-checkout"


class UpdateThisCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.operations = self.root / "operations"
        self.live_bot = self.root / "live-bot"

        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.git("init", "-b", "main", str(self.seed), cwd=self.root)
        (self.seed / "ops" / "fleet").mkdir(parents=True)
        shutil.copy2(SOURCE_SCRIPT, self.seed / "ops" / "fleet" / "update-this-checkout")
        (self.seed / "README.md").write_text("initial\n", encoding="utf-8")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "Initial", cwd=self.seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "-u", "origin", "main", cwd=self.seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.git("clone", str(self.remote), str(self.operations), cwd=self.root)
        self.git("clone", str(self.remote), str(self.live_bot), cwd=self.root)
        self.original_commit = self.git("rev-parse", "HEAD", cwd=self.operations).stdout.strip()

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

    def run_updater(self, *args):
        return subprocess.run(
            [str(self.operations / "ops" / "fleet" / "update-this-checkout"), *args],
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def test_check_is_read_only_and_update_only_changes_own_checkout(self):
        checked = self.run_updater("--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("would fast-forward by 1 commit(s)", checked.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.operations).stdout.strip(), self.original_commit)

        updated = self.run_updater()
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn("Updated only this checkout", updated.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.operations).stdout.strip(), self.updated_commit)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.live_bot).stdout.strip(), self.original_commit)

    def test_dirty_checkout_is_refused(self):
        (self.operations / "README.md").write_text("local edit\n", encoding="utf-8")
        result = self.run_updater()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dirty worktree", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.operations).stdout.strip(), self.original_commit)


if __name__ == "__main__":
    unittest.main()
