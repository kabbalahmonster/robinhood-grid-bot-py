import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "treasury-transfer"


class TreasuryTransferSummaryTests(unittest.TestCase):
    def test_dry_run_rolls_up_each_bot_and_exact_total(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot_dirs = []
            for name, amount in (("ALPHA", "0.001234567890123456"), ("BETA", "0.002000000000000001")):
                bot_dir = root / name / "robinhood-grid-bot-py"
                bot_dir.mkdir(parents=True)
                (bot_dir / "grid_bot.py").write_text(
                    "import os\n"
                    f"print('Send:         {amount} ETH')\n"
                    f"print('FLEET_TREASURY_SUMMARY|planned|{amount}')\n",
                    encoding="utf-8",
                )
                bot_dirs.append(bot_dir)
            config = root / "fleet.conf"
            config.write_text(
                'FLEET_TREASURY_RECIPIENT="0x0000000000000000000000000000000000000004"\n'
                f'FLEET_BOT_DIRS=("{bot_dirs[0]}" "{bot_dirs[1]}")\n',
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(SCRIPT), "--config", str(config), "--asset", "ETH",
                 "--amount", "available", "--position-reserve-eth", "0.002"],
                cwd=root, env={**os.environ, "HOME": str(root)},
                text=True, capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ALPHA", result.stdout)
        self.assertIn("0.001234567890123456", result.stdout)
        self.assertIn("BETA", result.stdout)
        self.assertIn("0.002000000000000001", result.stdout)
        self.assertIn(
            "Total that would be transferred: 0.003234567890123457 ETH from 2 bot(s); 0 skipped, 0 failed.",
            result.stdout,
        )
        self.assertNotIn("FLEET_TREASURY_SUMMARY|", result.stdout)


if __name__ == "__main__":
    unittest.main()
