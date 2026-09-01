import unittest
import json
import tempfile
from unittest.mock import patch

from dashboard_reporter import DashboardReporter


class TestDashboardReporter(unittest.TestCase):
    @patch("dashboard_reporter.threading.Thread.start")
    def test_usdg_balance_is_in_status_payload(self, _start):
        reporter = DashboardReporter("https://doomdash.ca/api/status")
        reporter.report(
            eth_balance=0.25,
            gas_reserve_eth=0.0005,
            usdg_balance=123.456,
            buy_point_percent=-14.0,
            sell_point_percent=10.0,
            poll_interval_seconds=30,
            token_symbol="TENDIES",
        )

        self.assertEqual(len(reporter._queue), 1)
        self.assertEqual(reporter._queue[0]["usdg_balance"], 123.456)
        self.assertEqual(reporter._queue[0]["gas_reserve_eth"], 0.0005)
        self.assertEqual(reporter._queue[0]["poll_interval_seconds"], 30)
        self.assertEqual(reporter._queue[0]["buy_point_percent"], -14.0)
        self.assertEqual(reporter._queue[0]["sell_point_percent"], 10.0)
        self.assertEqual(reporter._queue[0]["token_symbol"], "TENDIES")
        self.assertEqual(reporter._queue[0]["sigil"], reporter._sigil)

    @patch("dashboard_reporter.threading.Thread.start")
    def test_needs_gas_is_in_status_payload(self, _start):
        reporter = DashboardReporter("https://doomdash.ca/api/status")
        warning = {"balance_eth": 0.00004, "reserve_eth": 0.0002, "shortfall_eth": 0.00016}

        reporter.report(needs_gas=warning)

        self.assertEqual(reporter._queue[0]["needs_gas"], warning)

    @patch("dashboard_reporter.threading.Thread.start")
    def test_sell_attempt_is_round_scoped_payload_field(self, _start):
        reporter = DashboardReporter("https://doomdash.ca/api/status")
        attempt = {
            "status": "quote_below_minimum",
            "quoted_profit_eth": 0.001,
            "minimum_profit_eth": 0.002,
        }
        reporter.report(sell_attempt=attempt)
        reporter.report()

        self.assertEqual(reporter._queue[0]["sell_attempt"], attempt)
        self.assertIsNone(reporter._queue[1]["sell_attempt"])

    @patch("dashboard_reporter.threading.Thread.start")
    def test_taxed_token_execution_metadata_is_reported(self, _start):
        reporter = DashboardReporter("https://doomdash.ca/api/status")
        reporter.report(
            taxed_token=True,
            token_transfer_fee_percent=5.0,
            swap_slippage_percent=7.0,
        )

        payload = reporter._queue[0]
        self.assertTrue(payload["taxed_token"])
        self.assertEqual(payload["token_transfer_fee_percent"], 5.0)
        self.assertEqual(payload["swap_slippage_percent"], 7.0)

    @patch("dashboard_reporter.threading.Thread.start")
    def test_local_status_snapshot_is_atomic_and_contains_public_status(self, _start):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/data/fleet_status.json"
            reporter = DashboardReporter(
                "https://doomdash.ca/api/status",
                api_key="secret-not-for-snapshot",
                bot_id="PRISM",
                local_status_path=path,
            )
            reporter.report(token_symbol="PRISM", filled_positions=3, max_positions=7)
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["bot_id"], "PRISM")
            self.assertEqual(payload["filled_positions"], 3)
            self.assertNotIn("api_key", payload)


if __name__ == "__main__":
    unittest.main()
