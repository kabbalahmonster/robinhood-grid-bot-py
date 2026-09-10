import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "cleanup-logs"


class CleanupLogsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bots = []
        for name in ("ALPHA", "BETA"):
            bot = self.root / name / "robinhood-grid-bot-py"
            (bot / "logs").mkdir(parents=True)
            (bot / "grid_bot.py").write_text("pass\n")
            self.bots.append(bot)
        self.config = self.root / "fleet.conf"
        self.config.write_text(f'FLEET_BOT_DIRS=("{self.bots[0]}" "{self.bots[1]}")\n')
        now = time.time()
        for bot in self.bots:
            old = bot / "logs" / "bot_old.log"
            old.write_bytes(b"old")
            os.utime(old, (now - 8 * 86400, now - 8 * 86400))
            (bot / "logs" / "bot_current.log").write_bytes(b"current")
            (bot / "logs" / "ignore.txt").write_bytes(b"ignore")

    def tearDown(self):
        self.temp.cleanup()

    def run_cleanup(self, *args):
        return subprocess.run([str(SCRIPT), *args, "--config", str(self.config)],
                              text=True, capture_output=True)

    def test_preview_and_selected_apply(self):
        preview = self.run_cleanup("--older-than", "1 week", "--only", "ALPHA")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("Would delete 1 log file(s), 3 bytes", preview.stdout)
        self.assertTrue((self.bots[0] / "logs" / "bot_old.log").exists())
        applied = self.run_cleanup("--older-than", "7d", "--only", "ALPHA",
                                   "--apply", "--confirm-delete-logs")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse((self.bots[0] / "logs" / "bot_old.log").exists())
        self.assertTrue((self.bots[0] / "logs" / "bot_current.log").exists())
        self.assertTrue((self.bots[1] / "logs" / "bot_old.log").exists())

    def test_exclude_and_all_preserve_latest_by_default(self):
        result = self.run_cleanup("--older-than", "all", "--exclude", "BETA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would delete 1 log file(s)", result.stdout)
        self.assertNotIn("BETA:", result.stdout)

    def test_apply_requires_confirmation(self):
        result = self.run_cleanup("--older-than", "1h", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--apply requires --confirm-delete-logs", result.stderr)

    def test_refuses_symlinked_logs_directory(self):
        target = self.root / "outside"
        target.mkdir()
        (target / "external.log").write_bytes(b"do not delete")
        logs = self.bots[0] / "logs"
        for path in logs.iterdir():
            path.unlink()
        logs.rmdir()
        logs.symlink_to(target, target_is_directory=True)
        result = self.run_cleanup(
            "--older-than", "all", "--only", "ALPHA", "--keep-latest", "0",
            "--apply", "--confirm-delete-logs",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REFUSED logs path", result.stderr)
        self.assertTrue((target / "external.log").exists())


if __name__ == "__main__":
    unittest.main()
