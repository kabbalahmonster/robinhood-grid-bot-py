import json
import logging
import os
import tempfile
import unittest

from grid_bot import DashboardEventHandler, GridBot, _safe_event_message


class TestDashboardEvents(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bot = GridBot.__new__(GridBot)
        self.bot.dashboard_events_file = os.path.join(self.temp_dir.name, "events.json")
        self.bot._dashboard_event_lock = __import__('threading').Lock()
        self.bot.dashboard_events = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_records_and_persists_event(self):
        self.bot._record_dashboard_event("error", "quote_failed", "Quote failed", source="grid_bot.zero_x")
        with open(self.bot.dashboard_events_file) as handle:
            persisted = json.load(handle)
        self.assertEqual(persisted[0]["level"], "error")
        self.assertEqual(persisted[0]["code"], "quote_failed")
        self.assertEqual(persisted[0]["source"], "grid_bot.zero_x")

    def test_consecutive_duplicates_are_counted(self):
        self.bot._record_dashboard_event("warning", "rpc_warning", "RPC slow")
        self.bot._record_dashboard_event("warning", "rpc_warning", "RPC slow")
        self.assertEqual(len(self.bot.dashboard_events), 1)
        self.assertEqual(self.bot.dashboard_events[0]["count"], 2)

    def test_event_history_is_bounded(self):
        for index in range(55):
            self.bot._record_dashboard_event("warning", f"warning_{index}", f"Warning {index}")
        self.assertEqual(len(self.bot.dashboard_events), 50)
        self.assertEqual(self.bot.dashboard_events[0]["code"], "warning_5")

    def test_redacts_secret_material(self):
        key = "a" * 64
        message = _safe_event_message(f"api_key={key} private={key}")
        self.assertNotIn(key, message)
        self.assertIn("[REDACTED]", message)

    def test_logging_handler_maps_errors(self):
        self.bot.dashboard_events = []
        handler = DashboardEventHandler(self.bot._record_dashboard_event)
        record = logging.LogRecord("grid_bot.zero_x", logging.ERROR, __file__, 1, "API unavailable", (), None)
        handler.emit(record)
        self.assertEqual(self.bot.dashboard_events[0]["level"], "error")
        self.assertEqual(self.bot.dashboard_events[0]["code"], "log_error")


if __name__ == "__main__":
    unittest.main()
