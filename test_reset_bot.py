import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
HELPER = ROOT / "ops" / "fleet" / "reset-bot.py"
SCRIPT = ROOT / "ops" / "fleet" / "reset-bot"


class ResetBotTests(unittest.TestCase):
    def make_bot(self, directory):
        bot = Path(directory) / "v4"
        (bot / "data").mkdir(parents=True)
        (bot / ".env").write_text("PRIVATE_KEY=do-not-touch\nSTATE_FILE=data/custom_positions.json\n")
        return bot

    def test_preview_does_not_change_state(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            original = {"old": "state"}
            target = bot / "data" / "custom_positions.json"
            target.write_text(json.dumps(original))
            result = subprocess.run(
                [HELPER, "--bot-dir", bot, "--scope", "positions"],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PREVIEW ONLY", result.stdout)
            self.assertEqual(json.loads(target.read_text()), original)

    def test_apply_resets_selected_data_with_backup_and_keeps_env(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            data = bot / "data"
            (data / "custom_positions.json").write_text('{"position": 1}')
            (data / "gridless_positions.json").write_text('{"gridless": 1}')
            (data / "gridless_ladder.json").write_text('{"max_levels": 10}')
            (data / "dashboard_trades.json").write_text('[{"trade": 1}]')
            (data / "dashboard_events.json").write_text('[{"event": 1}]')
            (data / "profit_totals.json").write_text('{"schema_version": 1}')
            (data / "profit_fee_accrual.json").write_text('{"pending_wei": 12}')
            (data / "treasury_reporting_baseline.json").write_text(
                '{"schema_version": 1, "reset_at": "2026-01-01T00:00:00+00:00"}'
            )
            (data / "token_tax_detection.json").write_text('{"seen": true}')
            (data / "treasury_transfers.json").write_text('[{"audit": true}]')
            result = subprocess.run(
                [HELPER, "--bot-dir", bot, "--apply", "--backup-stamp", "test-reset"],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((bot / ".env").read_text(), "PRIVATE_KEY=do-not-touch\nSTATE_FILE=data/custom_positions.json\n")
            self.assertEqual(json.loads((data / "custom_positions.json").read_text()), {})
            self.assertEqual(json.loads((data / "gridless_positions.json").read_text()), {})
            self.assertFalse((data / "gridless_ladder.json").exists())
            self.assertEqual(json.loads((data / "dashboard_trades.json").read_text()), [])
            self.assertEqual(json.loads((data / "dashboard_events.json").read_text()), [])
            self.assertEqual(json.loads((data / "profit_totals.json").read_text())["realized_sales"], 0)
            self.assertEqual(json.loads((data / "profit_fee_accrual.json").read_text())["pending_wei"], 0)
            baseline = json.loads((data / "treasury_reporting_baseline.json").read_text())
            self.assertEqual(baseline["schema_version"], 1)
            self.assertNotEqual(baseline["reset_at"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(json.loads((data / "token_tax_detection.json").read_text()), {})
            self.assertEqual(json.loads((data / "treasury_transfers.json").read_text()), [{"audit": True}])
            self.assertTrue((data / "reset-backups" / "test-reset" / "data" / "custom_positions.json").exists())
            self.assertEqual(
                (data / "reset-backups" / "test-reset" / "data" / "gridless_ladder.json").read_text(),
                '{"max_levels": 10}',
            )
            self.assertTrue(
                (data / "reset-backups" / "test-reset" / "data" /
                 "treasury_reporting_baseline.json").exists()
            )

    def test_fleet_wrapper_requires_explicit_apply_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = self.make_bot(directory)
            (bot / "grid_bot.py").write_text("pass\n")
            config = Path(directory) / "fleet.conf"
            config.write_text(f'FLEET_BOT_DIRS=("{bot}")\n')
            result = subprocess.run(
                [SCRIPT, "v4", "--config", config, "--apply"],
                text=True, capture_output=True, env={**os.environ, "HOME": directory},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--apply requires --confirm-reset", result.stderr)


if __name__ == "__main__":
    unittest.main()
