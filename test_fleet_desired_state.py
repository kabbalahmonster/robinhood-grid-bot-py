import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parent / "ops" / "fleet"


class FleetDesiredStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scripts = self.root / "fleet"
        shutil.copytree(SOURCE, self.scripts)
        self.bot = self.root / "bots" / "alpha" / "repo"
        self.bot.mkdir(parents=True)
        (self.bot / "grid_bot.py").write_text("pass\n")
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            f'FLEET_SESSION="test_fleet"\nFLEET_WINDOW="fleet"\n'
            f'FLEET_BOT_DIRS=("{self.bot}")\nFLEET_START_STAGGER=0\n'
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.session = self.root / "session"
        self._write_tmux()
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_STATE_DIR": str(self.root / "state"),
            "XDG_STATE_HOME": str(self.root),
        }

    def tearDown(self):
        self.temp.cleanup()

    def _write_tmux(self):
        script = self.bin / "tmux"
        script.write_text(
            "#!/usr/bin/env bash\nset -eu\nstate=${TEST_TMUX_STATE:?}\n"
            "case $1 in\n"
            " has-session) test -e \"$state\";;\n"
            " kill-session) rm -f \"$state\";;\n"
            " new-session) touch \"$state\";;\n"
            " display-message) printf '%%0\\n';;\n"
            " split-window) printf '%%1\\n';;\n"
            " *) :;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        os.environ["TEST_TMUX_STATE"] = str(self.session)

    def run_command(self, command, *args, **kwargs):
        return subprocess.run(
            [str(self.scripts / command), *args, "--config", str(self.config)],
            env={**self.env, "TEST_TMUX_STATE": str(self.session)},
            text=True,
            capture_output=True,
            **kwargs,
        )

    def markers(self):
        return list((self.root / "state").glob("*.desired-stopped"))

    def test_stop_if_running_records_intent_even_when_already_stopped(self):
        result = self.run_command("stop-fleet", "--if-running")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.markers()), 1)

    def test_successful_start_commits_running_intent(self):
        self.run_command("stop-fleet", "--if-running", check=True)
        result = self.run_command("start-fleet", "--detach")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.session.exists())
        self.assertEqual(self.markers(), [])

    def test_start_uses_headless_safe_tmux_geometry(self):
        start = (self.scripts / "start-fleet").read_text()
        self.assertIn('-x "$FLEET_TMUX_WIDTH" -y "$FLEET_TMUX_HEIGHT"', start)

    def test_guardian_tolerates_snapshot_stat_race(self):
        guardian = (self.scripts / "fleet-guardian").read_text()
        self.assertIn('stat -c %Y -- "$snapshot" 2>/dev/null || true', guardian)
        self.assertIn('[[ ! "$snapshot_mtime" =~ ^[0-9]+$ ]]', guardian)

    def test_failed_start_preserves_stopped_intent(self):
        self.run_command("stop-fleet", "--if-running", check=True)
        (self.bot / "grid_bot.py").unlink()
        result = self.run_command("start-fleet", "--detach")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.markers()), 1)
        self.assertFalse(self.session.exists())

    def test_failed_start_from_legacy_running_default_records_stopped_intent(self):
        (self.bot / "grid_bot.py").unlink()
        result = self.run_command("start-fleet", "--detach")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.markers()), 1)
        self.assertFalse(self.session.exists())

    def test_restart_finishes_running(self):
        self.session.touch()
        result = self.run_command("restart-fleet", "--detach")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.session.exists())
        self.assertEqual(self.markers(), [])

    def test_guardian_does_not_restore_intentionally_stopped_fleet(self):
        self.run_command("stop-fleet", "--if-running", check=True)
        failure_file = Path(f"{self.markers()[0]}.guardian") / "repo.failures"
        failure_file.parent.mkdir(parents=True)
        failure_file.write_text("2\n")
        result = self.run_command("fleet-guardian", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.session.exists())
        self.assertFalse(failure_file.exists())

    def test_guardian_restores_missing_desired_running_fleet(self):
        result = self.run_command("fleet-guardian", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.session.exists())
        self.assertIn("restoring missing", result.stderr)

    def test_supervisor_delegates_desired_state_handling_to_guardian(self):
        trace = self.root / "guardian.trace"
        guardian = self.scripts / "fleet-guardian"
        guardian.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "{trace}"\n'
        )
        guardian.chmod(0o755)
        result = self.run_command("fleet-supervisor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(trace.read_text(), f"--config {self.config}\n")
        self.assertFalse(self.session.exists())


if __name__ == "__main__":
    unittest.main()
