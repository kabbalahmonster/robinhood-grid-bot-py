import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "update-variable"


class TestUpdateVariable(unittest.TestCase):
    def _fixture(self, directory):
        root = Path(directory) / "bots"
        checkouts = []
        for name in ("alpha", "broken", "omega"):
            checkout = root / name / "robinhood-grid-bot-py"
            checkout.mkdir(parents=True)
            (checkout / "grid_bot.py").touch()
            checkouts.append(checkout)
        (checkouts[0] / ".env").write_text("ETH_GAS_RESERVE=0.001\n")
        (checkouts[2] / ".env").write_text("ETH_GAS_RESERVE=0.001\n")
        config = Path(directory) / "fleet.conf"
        quoted = " ".join(f'"{path}"' for path in checkouts)
        config.write_text(f'FLEET_BOT_DIRS=({quoted})\n')
        return checkouts, config

    def test_strict_apply_stops_before_writing_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            checkouts, config = self._fixture(directory)
            result = subprocess.run(
                [SCRIPT, "--config", config, "--apply", "ETH_GAS_RESERVE=0.0005"],
                text=True, capture_output=True, env={**os.environ, "HOME": directory},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing .env", result.stderr)
            self.assertEqual((checkouts[0] / ".env").read_text(), "ETH_GAS_RESERVE=0.001\n")
            self.assertEqual((checkouts[2] / ".env").read_text(), "ETH_GAS_RESERVE=0.001\n")

    def test_skip_errors_applies_atomically_to_valid_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            checkouts, config = self._fixture(directory)
            result = subprocess.run(
                [SCRIPT, "--config", config, "--apply", "--skip-errors", "ETH_GAS_RESERVE=0.0005"],
                text=True, capture_output=True, env={**os.environ, "HOME": directory},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SKIPPED broken", result.stderr)
            self.assertIn("Skipped 1 invalid target(s): broken", result.stdout)
            self.assertIn("Updated 2 .env files", result.stdout)
            self.assertEqual((checkouts[0] / ".env").read_text(), "ETH_GAS_RESERVE=0.0005\n")
            self.assertFalse((checkouts[1] / ".env").exists())
            self.assertEqual((checkouts[2] / ".env").read_text(), "ETH_GAS_RESERVE=0.0005\n")


if __name__ == "__main__":
    unittest.main()
