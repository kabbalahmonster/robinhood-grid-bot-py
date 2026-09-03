import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops/fleet/sell-moonbags"


class TestSellMoonbagsFleetCommand(unittest.TestCase):
    def _fleet(self, root: Path):
        for name, symbol in (("alpha", "CHUMP"), ("beta", "WTH"), ("gamma", "INDEX")):
            bot = root / name / "robinhood-grid-bot-py"
            bot.mkdir(parents=True)
            (bot / "config.py").write_text(
                "from types import SimpleNamespace\n"
                f"def load_config(): return SimpleNamespace(token_symbol={symbol!r})\n"
            )
            (bot / "grid_bot.py").write_text(
                "import sys\nprint('CALLED ' + ' '.join(sys.argv[1:]))\n"
            )
        config = root / "fleet.conf"
        config.write_text(
            f'FLEET_BOT_ROOT="{root}"\n'
            'FLEET_BOT_NAMES=(alpha beta gamma)\n'
            'FLEET_CHECKOUT_DIRNAME="robinhood-grid-bot-py"\n'
        )
        return config

    def test_single_multiple_and_all_selectors(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._fleet(Path(directory))
            single = subprocess.run(
                [str(SCRIPT), "--config", str(config), "CHUMP"], text=True, capture_output=True
            )
            self.assertEqual(single.returncode, 0, single.stderr)
            self.assertEqual(single.stdout.count("CALLED --sell-moonbag"), 1)
            self.assertIn("alpha (CHUMP)", single.stdout)

            multiple = subprocess.run(
                [str(SCRIPT), "--config", str(config), "CHUMP,WTH"], text=True, capture_output=True
            )
            self.assertEqual(multiple.returncode, 0, multiple.stderr)
            self.assertEqual(multiple.stdout.count("CALLED --sell-moonbag"), 2)

            all_result = subprocess.run(
                [str(SCRIPT), "--config", str(config), "all"], text=True, capture_output=True
            )
            self.assertEqual(all_result.returncode, 0, all_result.stderr)
            self.assertEqual(all_result.stdout.count("CALLED --sell-moonbag"), 3)

    def test_unknown_coin_is_rejected_before_bot_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._fleet(Path(directory))
            result = subprocess.run(
                [str(SCRIPT), "--config", str(config), "NOPE"], text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unknown coin or bot name", result.stderr)
            self.assertNotIn("CALLED", result.stdout)


if __name__ == "__main__":
    unittest.main()
