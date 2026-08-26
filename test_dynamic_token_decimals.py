import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from grid_bot import GridBot
from gridless import calculate_pnl, get_buy_price, should_buy
from wallet import Wallet


class DynamicTokenDecimalsTests(unittest.TestCase):
    def test_nine_decimal_position_price_and_pnl(self):
        # 0.001 ETH bought 0.002 NET. NET has 9 decimals.
        position = {"cost_wei": 10**15, "balance": 2_000_000}

        self.assertAlmostEqual(get_buy_price(position, token_decimals=9), 0.5)
        self.assertAlmostEqual(
            calculate_pnl(position, current_price=0.55, token_decimals=9),
            10.0,
        )

    def test_strategy_uses_runtime_token_decimals(self):
        position = {"cost_wei": 10**15, "balance": 2_000_000}
        config = SimpleNamespace(
            token_decimals=9,
            max_active_positions=4,
            gridless_buy_threshold=-10.0,
            gridless_sell_threshold=10.0,
            gridless_leading_edge=False,
        )

        should_buy_flag, reason = should_buy({"0": position}, 0.44, config)

        self.assertTrue(should_buy_flag)
        self.assertIn("-12.00%", reason)

    def test_provider_raw_ratio_is_normalized_to_eth_per_token(self):
        bot = GridBot.__new__(GridBot)
        bot.token_decimals = 9
        bot.token_unit = 10**9
        bot.trade_token_address = "0xeth"
        bot.config = SimpleNamespace(token_address="0xnet")
        bot.provider = MagicMock()
        bot.provider.run_with_fallback = None
        bot.provider.capabilities.price_requires_taker = False
        # Raw-unit ratio: 1e15 wei / 2e6 base units = 5e8.
        bot.provider.get_price.return_value = 500_000_000

        self.assertAlmostEqual(bot.get_token_price(), 0.5)

    def test_wallet_reads_and_caches_nine_decimals(self):
        token = MagicMock()
        token.functions.symbol.return_value.call.return_value = "NET"
        token.functions.decimals.return_value.call.return_value = 9
        wallet = Wallet.__new__(Wallet)
        wallet.w3 = MagicMock()
        wallet.w3.eth.contract.return_value = token
        wallet._token_info_cache = {}

        info = wallet.get_token_info("0x0000000000000000000000000000000000000001")

        self.assertEqual(info.decimals, 9)
        self.assertIs(wallet.get_token_info(info.address), info)
        self.assertEqual(token.functions.decimals.return_value.call.call_count, 1)

    def test_wallet_refuses_to_guess_when_decimals_lookup_fails(self):
        token = MagicMock()
        token.functions.symbol.return_value.call.return_value = "NET"
        token.functions.decimals.return_value.call.side_effect = OSError("RPC failed")
        wallet = Wallet.__new__(Wallet)
        wallet.w3 = MagicMock()
        wallet.w3.eth.contract.return_value = token
        wallet._token_info_cache = {}

        with self.assertRaisesRegex(RuntimeError, "Could not read decimals"):
            wallet.get_token_info("0x0000000000000000000000000000000000000001")


if __name__ == "__main__":
    unittest.main()
