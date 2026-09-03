import logging
import unittest
from types import SimpleNamespace

from grid_bot import GridBot, logger


class LoggingVerbosityTests(unittest.TestCase):
    def setUp(self):
        self.original_level = logger.level
        self.bot = GridBot.__new__(GridBot)

    def tearDown(self):
        logger.setLevel(self.original_level)

    def test_info_mode_suppresses_round_summaries(self):
        self.bot.config = SimpleNamespace(compact_mode=False)
        logger.setLevel(logging.INFO)
        self.assertEqual(self.bot._round_summary_mode(), "quiet")

    def test_debug_mode_enables_full_round_summaries(self):
        self.bot.config = SimpleNamespace(compact_mode=False)
        logger.setLevel(logging.DEBUG)
        self.assertEqual(self.bot._round_summary_mode(), "debug")

    def test_compact_mode_explicitly_enables_condensed_round_summaries(self):
        self.bot.config = SimpleNamespace(compact_mode=True)
        logger.setLevel(logging.INFO)
        self.assertEqual(self.bot._round_summary_mode(), "compact")


if __name__ == "__main__":
    unittest.main()
