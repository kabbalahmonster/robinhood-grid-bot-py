import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
SCRIPT = ROOT / "ops" / "fleet" / "fleet-git-status"


class FleetGitStatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.bot_root = self.root / "bots"
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.seed.mkdir()
        self.git("init", "-b", "main", cwd=self.seed)
        (self.seed / "grid_bot.py").write_text("# bot\n", encoding="utf-8")
        self.git("add", "grid_bot.py", cwd=self.seed)
        self.git("commit", "-m", "seed", cwd=self.seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "-u", "origin", "main", cwd=self.seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        for name in ("alpha", "beta"):
            checkout = self.bot_root / name / "robinhood-grid-bot-py"
            checkout.parent.mkdir(parents=True)
            self.git("clone", str(self.remote), str(checkout), cwd=self.root)
        self.alpha = self.bot_root / "alpha" / "robinhood-grid-bot-py"
        self.beta = self.bot_root / "beta" / "robinhood-grid-bot-py"
        self.git("switch", "-c", "feature/alpha", cwd=self.alpha)
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            f'FLEET_BOT_ROOT="{self.bot_root}"\n'
            'FLEET_BOT_NAMES=(alpha beta)\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, *args, cwd):
        return subprocess.run(
            [
                "git", "-c", "user.name=Fleet Test",
                "-c", "user.email=fleet@example.invalid", *args,
            ],
            cwd=cwd, text=True, capture_output=True, check=True,
        )

    def run_status(self, *args):
        return subprocess.run(
            [str(SCRIPT), "--config", str(self.config), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.root)},
            text=True, capture_output=True, check=False,
        )

    def test_reports_all_branches_commits_and_tracking(self):
        result = self.run_status()

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("alpha\tbranch=feature/alpha\t", lines[0])
        self.assertIn("upstream=none", lines[0])
        self.assertIn("beta\tbranch=main\t", lines[1])
        self.assertIn("upstream=origin/main", lines[1])
        self.assertIn("ahead=0\tbehind=0\ttracked_dirty=no", lines[1])

    def test_only_filters_and_ignores_untracked_runtime_files(self):
        (self.beta / "runtime.log").write_text("runtime\n", encoding="utf-8")

        result = self.run_status("--only", "beta")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("alpha\t", result.stdout)
        self.assertIn("beta\tbranch=main", result.stdout)
        self.assertIn("tracked_dirty=no", result.stdout)

    def test_tracked_edit_and_detached_head_are_visible(self):
        (self.beta / "grid_bot.py").write_text("# changed\n", encoding="utf-8")
        self.git("checkout", "--detach", cwd=self.alpha)

        result = self.run_status()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alpha\tbranch=detached", result.stdout)
        self.assertIn("beta\tbranch=main", result.stdout)
        self.assertIn("tracked_dirty=yes", result.stdout)


if __name__ == "__main__":
    unittest.main()
