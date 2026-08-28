import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "fleet-watch.py"
SPEC = importlib.util.spec_from_file_location("fleet_watch", SCRIPT)
fleet_watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet_watch)


class TestFleetWatch(unittest.TestCase):
    def bot(self, root, name, payload=None):
        checkout = Path(root) / name / "robinhood-grid-bot-py"
        (checkout / "data").mkdir(parents=True)
        if payload is not None:
            (checkout / "data" / "fleet_status.json").write_text(json.dumps(payload))
        return checkout

    @patch.object(fleet_watch.shutil, "get_terminal_size")
    def test_narrow_view_renders_one_row_and_sell_wait(self, terminal_size):
        terminal_size.return_value = __import__("os").terminal_size((44, 30))
        with tempfile.TemporaryDirectory() as root:
            bot = self.bot(root, "prism", {
                "token_symbol": "PRISM",
                "filled_positions": 3,
                "max_positions": 7,
                "eth_balance": 0.006,
                "poll_interval_seconds": 8,
                "positions": [{"pnl": 8.5}],
                "sell_attempt": {"status": "quote_below_minimum"},
            })
            output = fleet_watch.render([bot], False)
        self.assertIn("PRISM", output)
        self.assertIn("3/7", output)
        self.assertIn("+8.5%", output)
        self.assertIn("SELL WAIT", output)

    @patch.object(fleet_watch.shutil, "get_terminal_size")
    def test_problems_sort_before_healthy_bots(self, terminal_size):
        terminal_size.return_value = __import__("os").terminal_size((80, 30))
        with tempfile.TemporaryDirectory() as root:
            healthy = self.bot(root, "alpha", {"token_symbol": "ALPHA", "poll_interval_seconds": 8})
            missing = self.bot(root, "zeta")
            output = fleet_watch.render([healthy, missing], False)
        self.assertLess(output.index("ZETA"), output.index("ALPHA"))


if __name__ == "__main__":
    unittest.main()
