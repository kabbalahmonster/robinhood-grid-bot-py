import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "usdg-sweep"
RECIPIENT = "0x" + "a" * 40


class UsdgSweepCycleTests(unittest.TestCase):
    def fixture(self, directory, bot_exit=0):
        root = Path(directory)
        checkout = root / "bots" / "earn" / "robinhood-grid-bot-py"
        checkout.mkdir(parents=True)
        (checkout / "grid_bot.py").write_text(
            f"import sys\nprint('fake sweep')\nsys.exit({bot_exit})\n"
        )
        config = root / "fleet.conf"
        config.write_text(
            f'FLEET_SESSION="test-fleet"\nFLEET_START_STAGGER=0\n'
            f'FLEET_TREASURY_RECIPIENT="{RECIPIENT}"\n'
            f'FLEET_ENTRYPOINT="grid_bot.py"\nFLEET_BOT_DIRS=("{checkout}")\n'
        )
        state = root / "tmux-state"
        state.touch()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        tmux = fake_bin / "tmux"
        tmux.write_text(
            "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
            "case \"${1:-}\" in\n"
            "  has-session) [[ -f \"$TMUX_STATE\" ]] ;;\n"
            "  kill-session) rm -f \"$TMUX_STATE\" ;;\n"
            "  new-session) touch \"$TMUX_STATE\" ;;\n"
            "  display-message) printf '%%0\\n' ;;\n"
            "  *) : ;;\n"
            "esac\n"
        )
        tmux.chmod(0o755)
        env = {**os.environ, "HOME": directory, "TMUX_STATE": str(state), "PATH": f"{fake_bin}:{os.environ['PATH']}"}
        return config, state, env

    def run_cycle(self, config, env):
        return subprocess.run(
            [SCRIPT, "--config", config, "--execute", "--cycle-fleet"],
            env=env,
            text=True,
            capture_output=True,
        )

    def test_cycle_stops_sweeps_and_restores_running_fleet(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, env = self.fixture(directory)
            result = self.run_cycle(config, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(state.exists())
            self.assertIn("Stopped fleet tmux session", result.stdout)
            self.assertIn("Restarting fleet after USDG sweep", result.stdout)
            self.assertIn("Started 1 bots", result.stdout)

    def test_cycle_restores_fleet_after_failed_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            config, state, env = self.fixture(directory, bot_exit=1)
            result = self.run_cycle(config, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(state.exists())
            self.assertIn("Restarting fleet after USDG sweep", result.stdout)

    def test_cycle_requires_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, env = self.fixture(directory)
            result = subprocess.run(
                [SCRIPT, "--config", config, "--cycle-fleet"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires --execute", result.stderr)


if __name__ == "__main__":
    unittest.main()
