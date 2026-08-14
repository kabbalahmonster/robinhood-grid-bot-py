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


if __name__ == "__main__":
    unittest.main()
