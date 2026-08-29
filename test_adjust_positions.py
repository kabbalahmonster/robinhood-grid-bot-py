import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "adjust-positions"


class AdjustPositionsTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory) / "bots"
        bots = {}
        for name, capacity, filled in (("earn", 7, 2), ("scopl", 5, 4)):
            checkout = root / name / "robinhood-grid-bot-py"
            (checkout / "data").mkdir(parents=True)
            (checkout / "grid_bot.py").touch()
            (checkout / ".env").write_text(f"MAX_ACTIVE_POSITIONS={capacity}\n")
            (checkout / "data" / "fleet_status.json").write_text(json.dumps({"filled_positions": filled}))
            bots[name] = checkout
        config = Path(directory) / "fleet.conf"
        config.write_text(
            f'FLEET_ENTRYPOINT="grid_bot.py"\nFLEET_BOT_DIRS=("{bots["earn"]}" "{bots["scopl"]}")\n'
        )
        return bots, config

    def run_script(self, directory, config, *args):
        return subprocess.run(
            [SCRIPT, "--config", config, *args],
            env={**os.environ, "HOME": directory},
            text=True,
            capture_output=True,
        )

    def test_defaults_to_previewing_one_added_position(self):
        with tempfile.TemporaryDirectory() as directory:
            bots, config = self.fixture(directory)
            result = self.run_script(directory, config, "earn")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("MAX_ACTIVE_POSITIONS: 7 -> 8", result.stdout)
            self.assertIn("PREVIEW ONLY", result.stdout)
            self.assertEqual((bots["earn"] / ".env").read_text(), "MAX_ACTIVE_POSITIONS=7\n")

    def test_applies_count_to_comma_separated_names(self):
        with tempfile.TemporaryDirectory() as directory:
            bots, config = self.fixture(directory)
            result = self.run_script(directory, config, "--apply", "earn,scopl", "2")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((bots["earn"] / ".env").read_text(), "MAX_ACTIVE_POSITIONS=9\n")
            self.assertEqual((bots["scopl"] / ".env").read_text(), "MAX_ACTIVE_POSITIONS=7\n")

    def test_refuses_removal_below_filled_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            bots, config = self.fixture(directory)
            result = self.run_script(directory, config, "--remove", "scopl", "2")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bot has 4 filled positions", result.stderr)
            self.assertEqual((bots["scopl"] / ".env").read_text(), "MAX_ACTIVE_POSITIONS=5\n")

    def test_applies_individual_add_and_remove_deltas_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            bots, config = self.fixture(directory)
            result = self.run_script(directory, config, "--apply", "earn=+3", "scopl=-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("earn             ADD    3", result.stdout)
            self.assertIn("scopl            REMOVE 1", result.stdout)
            self.assertEqual((bots["earn"] / ".env").read_text(), "MAX_ACTIVE_POSITIONS=10\n")
            self.assertEqual((bots["scopl"] / ".env").read_text(), "MAX_ACTIVE_POSITIONS=4\n")

    def test_assignment_form_rejects_remove_flag_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config = self.fixture(directory)
            flagged = self.run_script(directory, config, "--remove", "earn=+1")
            duplicate = self.run_script(directory, config, "earn=+1", "earn=-1")
            self.assertNotEqual(flagged.returncode, 0)
            self.assertIn("cannot be combined", flagged.stderr)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("Duplicate bot assignment", duplicate.stderr)


if __name__ == "__main__":
    unittest.main()
