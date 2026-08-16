import unittest
from unittest.mock import patch

from dashboard_reporter import DashboardReporter


class TestDashboardReporter(unittest.TestCase):
    @patch("dashboard_reporter.threading.Thread.start")
    def test_usdg_balance_is_in_status_payload(self, _start):
        reporter = DashboardReporter("https://doomdash.ca/api/status")
        reporter.report(eth_balance=0.25, usdg_balance=123.456)

        self.assertEqual(len(reporter._queue), 1)
        self.assertEqual(reporter._queue[0]["usdg_balance"], 123.456)

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


if __name__ == "__main__":
    unittest.main()
