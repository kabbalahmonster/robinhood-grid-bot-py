import unittest
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


if __name__ == "__main__":
    unittest.main()
