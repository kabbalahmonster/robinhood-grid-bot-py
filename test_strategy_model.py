import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
COMMAND = ROOT / "ops" / "fleet" / "strategy-model"


class StrategyModelTests(unittest.TestCase):
    def test_models_geometric_coverage_and_writes_portable_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.html"
            result = subprocess.run(
                [str(COMMAND), "--buy-triggers", "10", "--sell-triggers", "5,10",
                 "--positions", "3", "--output", str(output)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            self.assertIn("Buy / sell comparison", output.read_text())
            data = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(len(data["strategies"]), 2)
            first = data["strategies"][0]
            self.assertAlmostEqual(first["last_funded_entry_drawdown_percent"], 19.0)
            self.assertAlmostEqual(first["capacity_boundary_drawdown_percent"], 27.1)
            self.assertAlmostEqual(first["rebound_to_newest_exit_percent"], 16.6666666667)
            with output.with_suffix(".csv").open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_minimum_profit_floor_controls_effective_sell(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "floor.html"
            result = subprocess.run(
                [str(COMMAND), "--buy-triggers", "10", "--sell-triggers", "3",
                 "--min-profit", "7", "--output", str(output)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(output.with_suffix(".json").read_text())["strategies"][0]
            self.assertEqual(row["effective_sell_percent"], 7.0)

    def test_rejects_invalid_ranges(self):
        result = subprocess.run(
            [str(COMMAND), "--buy-triggers", "0,10"], cwd=ROOT,
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("greater than 0", result.stderr)


if __name__ == "__main__":
    unittest.main()
