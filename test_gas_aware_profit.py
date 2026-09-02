import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from grid_bot import GridBot


class GasAwareProfitTests(unittest.TestCase):
    def make_bot(self):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(gas_limit_multiplier=1.0, gas_price_multiplier=1.0)
        bot.wallet = Mock()
        bot.wallet.w3.eth.gas_price = 400_000_000
        return bot

    def test_projected_sell_return_includes_gas_and_profit_target(self):
        bot = self.make_bot()
        quote = SimpleNamespace(gas=200_000, gas_price=400_000_000)

        required = bot._minimum_gas_aware_return_wei(
            sold_cost_wei=1_000_000_000_000_000,
            quote=quote,
            min_profit_percent=2.0,
        )

        self.assertEqual(required, 1_100_000_000_000_000)

    def test_confirmed_sale_profit_deducts_receipt_gas(self):
        bot = self.make_bot()
        result = SimpleNamespace(
            receipt={"gasUsed": 200_000, "effectiveGasPrice": 400_000_000},
            gas_used=None,
            effective_gas_price=None,
        )

        profit = bot._net_sale_profit_wei(
            received_wei=1_120_000_000_000_000,
            sold_cost_wei=1_000_000_000_000_000,
            result=result,
        )

        self.assertEqual(profit, 40_000_000_000_000)

    def test_projected_gas_uses_configured_headroom(self):
        bot = self.make_bot()
        bot.config.gas_limit_multiplier = 1.05
        bot.config.gas_price_multiplier = 1.05
        quote = SimpleNamespace(gas=200_000, gas_price=400_000_000)

        self.assertEqual(bot._projected_gas_cost_wei(quote), 88_200_000_000_000)

    def test_normal_rpc_price_overrides_provider_fast_price(self):
        bot = self.make_bot()
        quote = SimpleNamespace(gas=200_000, gas_price=2_000_000_000)

        gas_limit, gas_price = bot._swap_gas_fields(quote)

        self.assertEqual(gas_limit, 200_000)
        self.assertEqual(gas_price, 400_000_000)

    def test_setup_and_swap_gas_are_both_deducted_from_sale_profit(self):
        bot = self.make_bot()
        result = SimpleNamespace(
            receipt={"gasUsed": 200_000, "effectiveGasPrice": 400_000_000},
            gas_used=None,
            effective_gas_price=None,
        )

        profit = bot._net_sale_profit_wei(
            1_150_000_000_000_000,
            1_000_000_000_000_000,
            result,
            setup_gas_wei=20_000_000_000_000,
        )

        self.assertEqual(profit, 50_000_000_000_000)

    def test_hard_gas_cap_blocks_expensive_swap(self):
        bot = self.make_bot()
        bot.config.max_swap_gas_eth = 0.00004

        self.assertFalse(bot._gas_within_hard_cap(200_000, 400_000_000, "buy"))


if __name__ == "__main__":
    unittest.main()
