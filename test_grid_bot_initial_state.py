import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grid_bot import GridBot


class GridBotInitialStateTests(unittest.TestCase):
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
