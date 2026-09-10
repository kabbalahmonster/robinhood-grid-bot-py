import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "treasury-transfer"


class TreasuryTransferSummaryTests(unittest.TestCase):
    def _record_bot_stopped(self, root, config, bot_dir):
        common = SCRIPT.parent / "fleet-common.sh"
        subprocess.run(
            ["bash", "-c", 'source "$1"; fleet_load_config "$2"; fleet_record_bot_stopped "$3"',
             "record-stop", str(common), str(config), str(bot_dir)],
            cwd=root, env={**os.environ, "HOME": str(root)}, check=True,
        )

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

    def test_single_stopped_bot_can_execute_while_fleet_session_is_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot_dirs = []
            for name in ("ALPHA", "BETA"):
                bot_dir = root / name / "robinhood-grid-bot-py"
                bot_dir.mkdir(parents=True)
                (bot_dir / "grid_bot.py").write_text(
                    "import sys\n"
                    "assert '--execute' in sys.argv\n"
                    "assert '--confirm-bot-stopped' in sys.argv\n"
                    "print('FLEET_TREASURY_SUMMARY|confirmed|0.001')\n",
                    encoding="utf-8",
                )
                bot_dirs.append(bot_dir)
            config = root / "fleet.conf"
            config.write_text(
                'FLEET_SESSION="live-fleet"\n'
                'FLEET_TREASURY_RECIPIENT="0x0000000000000000000000000000000000000004"\n'
                f'FLEET_BOT_DIRS=("{bot_dirs[0]}" "{bot_dirs[1]}")\n',
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            tmux.chmod(0o755)
            self._record_bot_stopped(root, config, bot_dirs[0])

            result = subprocess.run(
                [str(SCRIPT), "--config", str(config), "--only", "ALPHA", "--asset", "ETH",
                 "--amount", "0.001", "--execute", "--confirm-bot-stopped"],
                cwd=root,
                env={**os.environ, "HOME": str(root), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
                text=True, capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Total confirmed for treasury: 0.001 ETH from 1 bot(s)", result.stdout)
        self.assertIn("ALPHA", result.stdout)
        self.assertNotIn("BETA", result.stdout)

    def test_single_bot_execute_requires_durable_stopped_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot_dir = root / "ALPHA" / "robinhood-grid-bot-py"
            bot_dir.mkdir(parents=True)
            (bot_dir / "grid_bot.py").write_text("pass\n", encoding="utf-8")
            config = root / "fleet.conf"
            config.write_text(
                'FLEET_TREASURY_RECIPIENT="0x0000000000000000000000000000000000000004"\n'
                f'FLEET_BOT_DIRS=("{bot_dir}")\n', encoding="utf-8",
            )
            result = subprocess.run(
                [str(SCRIPT), "--config", str(config), "--only", "ALPHA", "--asset", "ETH",
                 "--amount", "0.001", "--execute", "--confirm-bot-stopped"],
                cwd=root, env={**os.environ, "HOME": str(root)}, text=True, capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Selected bot is not durably stopped; run stop-bot first", result.stderr)


if __name__ == "__main__":
    unittest.main()
