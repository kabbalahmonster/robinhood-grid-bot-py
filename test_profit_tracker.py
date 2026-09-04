import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from profit_tracker import ProfitTracker


class TestProfitTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "profit.json")
        self.tracker = ProfitTracker(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_records_profit_and_loss_in_wei(self):
        self.assertTrue(self.tracker.record_sale(25, "0xprofit"))
        self.assertTrue(self.tracker.record_sale(-10, "0xloss"))
        self.assertEqual(self.tracker.realized_profit_wei, 15)
        self.assertEqual(self.tracker.realized_sales, 2)
        self.assertEqual(self.tracker.state["profitable_sales"], 1)
        self.assertEqual(self.tracker.state["losing_sales"], 1)

    def test_duplicate_transaction_is_not_counted(self):
        self.assertTrue(self.tracker.record_sale(25, "0xABC"))
        self.assertFalse(self.tracker.record_sale(25, "0xabc"))
        self.assertEqual(self.tracker.realized_profit_wei, 25)
        self.assertEqual(self.tracker.realized_sales, 1)

    def test_survives_new_session(self):
        self.tracker.record_sale(10**18, "0xprofit")
        reloaded = ProfitTracker(self.path)
        self.assertEqual(reloaded.realized_profit_eth, 1.0)
        self.assertEqual(reloaded.realized_sales, 1)

    def test_reset_creates_non_destructive_baseline(self):
        self.tracker.record_sale(100, "0xbefore")
        all_time_total = self.tracker.state["realized_profit_wei"]
        self.tracker.reset_baseline()
        self.assertEqual(self.tracker.realized_profit_wei, 0)
        self.assertEqual(self.tracker.realized_sales, 0)
        self.assertEqual(self.tracker.state["realized_profit_wei"], all_time_total)
        self.tracker.record_sale(30, "0xafter")
        self.assertEqual(self.tracker.realized_profit_wei, 30)
        self.assertEqual(self.tracker.realized_sales, 1)

    def test_state_is_created_with_tracking_timestamp(self):
        with open(self.path) as handle:
            data = json.load(handle)
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["tracking_started_at"])

    def test_missing_transaction_hash_is_rejected(self):
        with self.assertRaises(ValueError):
            self.tracker.record_sale(1, "")

    def test_period_profits_use_rolling_windows(self):
        now = datetime.now(timezone.utc)
        self.tracker.state["tracking_started_at"] = (now - timedelta(days=40)).isoformat()
        self.tracker.state["profit_history"] = [
            {"timestamp": (now - timedelta(minutes=30)).isoformat(), "profit_wei": 10**18, "tx_hash": "0x1"},
            {"timestamp": (now - timedelta(hours=3)).isoformat(), "profit_wei": 2 * 10**18, "tx_hash": "0x2"},
            {"timestamp": (now - timedelta(days=2)).isoformat(), "profit_wei": 4 * 10**18, "tx_hash": "0x3"},
        ]
        periods = self.tracker.period_profits_eth(now)
        self.assertEqual(periods["1h"], 1.0)
        self.assertEqual(periods["2h"], 1.0)
        self.assertEqual(periods["4h"], 3.0)
        self.assertEqual(periods["6h"], 3.0)
        self.assertEqual(periods["24h"], 3.0)
        self.assertEqual(periods["3d"], 7.0)
        self.assertEqual(periods["week"], 7.0)

    def test_period_uses_total_when_tracking_started_inside_window(self):
        now = datetime.now(timezone.utc)
        self.tracker.state["tracking_started_at"] = (now - timedelta(hours=2)).isoformat()
        self.tracker.state["realized_profit_wei"] = 3 * 10**18
        self.assertEqual(self.tracker.period_profits_eth(now)["6h"], 3.0)


if __name__ == "__main__":
    unittest.main()
