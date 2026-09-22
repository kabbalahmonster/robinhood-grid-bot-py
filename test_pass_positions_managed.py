import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parent / "ops" / "fleet"


class ManagedPassPositionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scripts = self.root / "fleet"
        shutil.copytree(SOURCE, self.scripts)
        self.bots = {}
        for name in ("alpha", "beta", "gamma"):
            bot = self.root / "bots" / name
            bot.mkdir(parents=True)
            (bot / "grid_bot.py").write_text("pass\n")
            self.bots[name] = bot
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            'FLEET_SESSION="test_fleet"\nFLEET_WINDOW="fleet"\n'
            f'FLEET_BOT_DIRS=("{self.bots["alpha"]}" "{self.bots["beta"]}" "{self.bots["gamma"]}")\n'
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.session = self.root / "session"
        self.session.touch()
        self.trace = self.root / "pass.trace"
        self._write_tmux()
        self._write_pass_stub()
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FLEET_STATE_DIR": str(self.root / "state"),
            "TEST_TMUX_STATE": str(self.session),
            "TEST_ALPHA_DIR": str(self.bots["alpha"]),
            "TEST_BETA_DIR": str(self.bots["beta"]),
            "TEST_GAMMA_DIR": str(self.bots["gamma"]),
            "PASS_TRACE": str(self.trace),
            "PASS_RESULT": "0",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _write_tmux(self):
        script = self.bin / "tmux"
        script.write_text(
            "#!/usr/bin/env bash\nset -eu\n"
            "case $1 in\n"
            " has-session) test -e \"${TEST_TMUX_STATE:?}\";;\n"
            " list-panes) printf '%%0\\t%s\\n%%1\\t%s\\n%%2\\t%s\\n' "
            "\"${TEST_ALPHA_DIR:?}\" \"${TEST_BETA_DIR:?}\" \"${TEST_GAMMA_DIR:?}\";;\n"
            " *) :;;\n"
            "esac\n"
        )
        script.chmod(0o755)

    def _write_pass_stub(self):
        script = self.scripts / "pass-positions.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "if '--list-involved' in sys.argv:\n"
            "    print('alpha\\nbeta')\n"
            "    raise SystemExit(0)\n"
            "pathlib.Path(os.environ['PASS_TRACE']).write_text('executed\\n')\n"
            "raise SystemExit(int(os.environ.get('PASS_RESULT', '0')))\n"
        )

    def run_command(self, *args, result="0"):
        return subprocess.run(
            [str(self.scripts / "pass-positions"), *args, "--config", str(self.config)],
            env={**self.env, "PASS_RESULT": result},
            text=True,
            capture_output=True,
        )

    def stop_beta(self):
        result = subprocess.run(
            [str(self.scripts / "stop-bot"), "beta", "--config", str(self.config)],
            env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def marker_names(self):
        names = []
        for marker in (self.root / "state").glob("*.bots/*.desired-stopped"):
            for line in marker.read_text().splitlines():
                if line.startswith("name="):
                    names.append(line.removeprefix("name="))
        return sorted(names)

    def test_success_restarts_only_involved_bot_that_was_running(self):
        self.stop_beta()

        result = self.run_command(
            "--from", "alpha", "--to", "beta", "--positions", "1",
            "--execute", "--manage-bots",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.trace.exists())
        self.assertEqual(self.marker_names(), ["beta"])
        self.assertIn("Automatically stopped 1", result.stdout)
        self.assertIn("Automatically restarted 1", result.stdout)

    def test_failure_leaves_every_previously_running_involved_bot_stopped(self):
        result = self.run_command(
            "--from", "alpha", "--to", "beta", "--positions", "1",
            "--execute", "--auto-stop-restart", result="7",
        )

        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.marker_names(), ["alpha", "beta"])
        self.assertIn("remain intentionally stopped", result.stderr)

    def test_managed_mode_requires_execute_and_replaces_global_confirmation(self):
        missing_execute = self.run_command(
            "--from", "alpha", "--to", "beta", "--positions", "1", "--manage-bots"
        )
        both = self.run_command(
            "--from", "alpha", "--to", "beta", "--positions", "1",
            "--execute", "--manage-bots", "--confirm-fleet-stopped",
        )
        self.assertNotEqual(missing_execute.returncode, 0)
        self.assertIn("requires --execute", missing_execute.stderr)
        self.assertNotEqual(both.returncode, 0)
        self.assertIn("not both", both.stderr)

    def test_managed_mode_refuses_absent_but_desired_running_fleet(self):
        self.session.unlink()
        result = self.run_command(
            "--from", "alpha", "--to", "beta", "--positions", "1",
            "--execute", "--manage-bots",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absent but desired-running", result.stderr)
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
