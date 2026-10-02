import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grid_bot import GridBot, _dashboard_strategy_mode


class GridBotInitialStateTests(unittest.TestCase):
    def test_survivor_rapid_poll_window_shortens_main_loop_delay(self):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            poll_interval_seconds=6,
            survivor_rapid_poll_seconds=1,
        )
        bot._survivor_rapid_poll_until = 100.0
        self.assertEqual(bot._next_main_loop_delay(now=99.0), 1)
        self.assertEqual(bot._next_main_loop_delay(now=100.0), 6)

    def test_survivor_rapid_window_forces_fresh_sell_side_observation(self):
        bot = GridBot.__new__(GridBot)
        bot._survivor_rapid_poll_until = 100.0
        bot._pnl_poll_sequence = MagicMock(return_value=["buy", "sell"])
        self.assertEqual(
            bot._select_pnl_poll_side({"0": {}}, now=99.0),
            ("sell", "post_sell_rapid"),
        )

    def test_crossed_sell_lane_outranks_simultaneous_buy_lane(self):
        bot = GridBot.__new__(GridBot)
        bot._survivor_rapid_poll_until = 0.0
        bot._pnl_poll_sequence = MagicMock(
            return_value=["buy", "sell", "legacy"]
        )
        bot._pnl_trigger_latches = {"buy": "buy", "sell": "sell"}

        self.assertEqual(
            bot._select_pnl_poll_side({"0": {}}, now=99.0),
            ("sell", "triggered_sell_priority"),
        )

    def test_sell_attempt_or_latch_suppresses_buy_for_cycle(self):
        bot = GridBot.__new__(GridBot)
        bot._sell_priority_this_cycle = False
        bot._pnl_trigger_latches = {"buy": "buy", "sell": None}
        self.assertFalse(bot._sell_has_cycle_priority())

        bot._sell_priority_this_cycle = True
        self.assertTrue(bot._sell_has_cycle_priority())

        bot._sell_priority_this_cycle = False
        bot._pnl_trigger_latches["sell"] = "legacy"
        self.assertTrue(bot._sell_has_cycle_priority())

    @patch("grid_bot.time.monotonic", return_value=50.0)
    def test_confirmed_survivor_sell_arms_rapid_poll_window(self, _monotonic):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            gridless_allocation_mode="survivor",
            poll_interval_seconds=6,
            survivor_rapid_poll_seconds=1,
            survivor_rapid_poll_window_seconds=30,
        )
        bot._survivor_rapid_poll_until = 0.0
        bot._arm_survivor_rapid_polling()
        self.assertEqual(bot._survivor_rapid_poll_until, 80.0)

    def test_legacy_mode_does_not_arm_survivor_rapid_polling(self):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            gridless_allocation_mode="threshold",
            survivor_rapid_poll_window_seconds=30,
        )
        bot._survivor_rapid_poll_until = 0.0
        bot._arm_survivor_rapid_polling()
        self.assertEqual(bot._survivor_rapid_poll_until, 0.0)

    def test_dashboard_strategy_mode_is_explicit_and_legacy_safe(self):
        self.assertEqual(
            _dashboard_strategy_mode(SimpleNamespace(use_gridless=False)), "grid"
        )
        self.assertEqual(
            _dashboard_strategy_mode(SimpleNamespace(use_gridless=True)),
            "gridless_threshold",
        )
        self.assertEqual(
            _dashboard_strategy_mode(SimpleNamespace(
                use_gridless=True,
                gridless_allocation_mode="drawdown_ladder",
            )),
            "drawdown_ladder",
        )
        self.assertEqual(
            _dashboard_strategy_mode(SimpleNamespace(
                use_gridless=True,
                gridless_allocation_mode="survivor",
            )),
            "survivor",
        )

    @patch("grid_bot.create_reporter_from_config", return_value=None)
    @patch("grid_bot.create_swap_provider")
    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    @patch.object(GridBot, "_load_dashboard_trades", return_value=[])
    @patch.object(GridBot, "_load_dashboard_events", return_value=[])
    @patch.object(GridBot, "_setup_logging")
    def test_funding_warning_exists_before_first_cycle(
        self,
        _setup_logging,
        _load_events,
        _load_trades,
        load_config,
        wallet_class,
        create_provider,
        _create_reporter,
    ):
        load_config.return_value = SimpleNamespace(
            chain_id=4663,
            token_address="0x0000000000000000000000000000000000000001",
            auto_detect_token_transfer_fee=False,
            auto_detect_token_transfer_fee_max_percent=0,
            taxed_token=False,
            gridless_buy_cooldown_seconds=300,
            use_eth_trading=True,
            weth_address="0x0000000000000000000000000000000000000002",
            dashboard_url="",
        )
        wallet_class.return_value.get_token_info.return_value = SimpleNamespace(
            symbol="TEST", decimals=18
        )
        create_provider.return_value = MagicMock(name="provider")

        bot = GridBot()

        self.assertIsNone(bot._funding_warning)


if __name__ == "__main__":
    unittest.main()
