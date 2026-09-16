import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from grid_bot import GridBot
from gridless import get_sell_trigger_percent, should_buy
from zero_x import QuoteResult


def quote(sell_amount, buy_amount, minimum_buy_amount, gas=100_000):
    result = QuoteResult(
        success=True,
        sell_amount=sell_amount,
        buy_amount=buy_amount,
        gas=gas,
        gas_price=10**9,
    )
    result.minimum_buy_amount = minimum_buy_amount
    result.output_is_execution_floor = False
    return result


def make_bot():
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(
        bidirectional_pnl_enabled=True,
        pnl_polling_mode="bidirectional",
        pnl_trigger_by_min_profit=False,
        bidirectional_pnl_quote_timeout_seconds=4,
        bidirectional_pnl_max_age_seconds=90,
        moonbag_percentage=0,
        token_address="0x" + "11" * 20,
        weth_address="0x" + "33" * 20,
        zero_x_proxy="0x" + "44" * 20,
        use_eth_trading=True,
        slippage_tolerance=2,
        taxed_token=False,
        gas_limit_multiplier=1.0,
        gas_price_multiplier=1.0,
        gas_price_freshness_multiplier=1.0,
        route_tournament_mode="off",
        max_active_positions=2,
        tradeable_balance_percent=90,
        eth_gas_reserve=0,
        gridless_buy_threshold=-10,
        gridless_sell_threshold=5,
        min_profit_percent=3,
        gridless_leading_edge=False,
    )
    bot.token_decimals = 18
    bot.token_unit = 10**18
    bot.trade_token_address = "0x" + "00" * 20
    bot.wallet = SimpleNamespace(
        address="0x" + "22" * 20,
        normal_gas_price=Mock(return_value=10**9),
        check_allowance=Mock(return_value=2**256 - 1),
        build_token_approval_transaction=Mock(),
    )
    bot.provider = SimpleNamespace(name="uniswap")
    bot.api_client = SimpleNamespace(get_quote=Mock())
    bot._pnl_quotes = {"buy": None, "sell": None}
    bot._next_pnl_poll_side = "buy"
    return bot


class TestBidirectionalPnl(unittest.TestCase):
    def test_alternates_exact_buy_and_next_position_sell_quotes(self):
        bot = make_bot()
        positions = {
            "1": {"balance": 100 * 10**18, "cost_wei": 10**18},
            "2": {"balance": 50 * 10**18, "cost_wei": 6 * 10**17},
        }
        # At capacity the display uses one normal tranche (balance / max slots).
        buy = quote(45 * 10**16, 45 * 10**18, 44 * 10**18)
        sell = quote(100 * 10**18, 12 * 10**17, 11 * 10**17)
        bot.api_client.get_quote.side_effect = [buy, sell]

        first = bot._refresh_bidirectional_pnl(positions, 1.0, now=100)
        second = bot._refresh_bidirectional_pnl(positions, 1.0, now=120)

        self.assertIn("buy_pnl", first["1"])
        self.assertNotIn("sell_pnl", first["1"])
        self.assertAlmostEqual(second["1"]["sell_pnl"], 9.99, places=2)
        self.assertAlmostEqual(second["2"]["sell_pnl"], -8.35, places=2)
        calls = bot.api_client.get_quote.call_args_list
        self.assertEqual(calls[0].kwargs["sell_amount"], 45 * 10**16)
        self.assertEqual(calls[1].kwargs["sell_amount"], 100 * 10**18)
        self.assertEqual(bot._pnl_quotes["sell"]["position_id"], "1")

    def test_buy_mark_includes_output_floor_and_projected_gas(self):
        bot = make_bot()
        positions = {"1": {"balance": 100 * 10**18, "cost_wei": 10**18}}
        # One remaining slot => 0.9 ETH input; floor is 89 tokens and gas 0.0001 ETH.
        bot.api_client.get_quote.return_value = quote(
            9 * 10**17, 90 * 10**18, 89 * 10**18,
        )

        pnls = bot._refresh_bidirectional_pnl(positions, 1.0, now=100)

        expected_price = 0.9001 / 89
        expected_pnl = (expected_price - 0.01) / 0.01 * 100
        self.assertAlmostEqual(pnls["1"]["buy_pnl"], expected_pnl, places=8)
        self.assertEqual(bot._pnl_quotes["buy"]["projected_gas_wei"], 10**14)

    def test_buy_only_mode_never_polls_sell_side(self):
        bot = make_bot()
        bot.config.pnl_polling_mode = "buy"
        positions = {"1": {"balance": 100 * 10**18, "cost_wei": 10**18}}
        bot.api_client.get_quote.return_value = quote(
            9 * 10**17, 90 * 10**18, 89 * 10**18,
        )

        bot._refresh_bidirectional_pnl(positions, 1.0, now=100)
        bot._refresh_bidirectional_pnl(positions, 1.0, now=120)

        self.assertEqual(bot.api_client.get_quote.call_count, 2)
        self.assertTrue(all(
            call.kwargs["sell_token"] == bot.trade_token_address
            for call in bot.api_client.get_quote.call_args_list
        ))
        self.assertIsNone(bot._fresh_pnl_sample("sell", now=120))

    def test_sell_only_mode_polls_exact_next_position(self):
        bot = make_bot()
        bot.config.pnl_polling_mode = "sell"
        positions = {
            "1": {"balance": 100 * 10**18, "cost_wei": 10**18},
            "2": {"balance": 50 * 10**18, "cost_wei": 10**18},
        }
        bot.api_client.get_quote.return_value = quote(
            100 * 10**18, 12 * 10**17, 11 * 10**17,
        )

        pnls = bot._refresh_bidirectional_pnl(positions, 1.0, now=100)

        self.assertIn("sell_pnl", pnls["1"])
        self.assertNotIn("buy_pnl", pnls["1"])
        self.assertEqual(bot._pnl_quotes["sell"]["position_id"], "1")
        self.assertEqual(
            bot.api_client.get_quote.call_args.kwargs["sell_token"],
            bot.config.token_address,
        )

    def test_minimum_profit_can_be_the_sell_trigger(self):
        bot = make_bot()
        self.assertEqual(get_sell_trigger_percent(bot.config), 5)
        bot.config.pnl_trigger_by_min_profit = True
        self.assertEqual(get_sell_trigger_percent(bot.config), 3)

    def test_stale_sell_quote_cannot_wake_sell_logic(self):
        bot = make_bot()
        bot._pnl_quotes["sell"] = {
            "sampled_monotonic": 1,
            "sell_amount_raw": 100,
            "floor_return_wei": 200,
            "projected_gas_wei": 1,
        }
        values = bot._bidirectional_position_pnls(
            {"1": {"balance": 100, "cost_wei": 100}}, now=100
        )
        self.assertNotIn("sell_pnl", values["1"])

    def test_buy_trigger_uses_supplied_net_mark(self):
        config = SimpleNamespace(
            max_active_positions=3,
            gridless_buy_threshold=-10,
            gridless_sell_threshold=5,
            gridless_leading_edge=False,
            token_decimals=18,
        )
        positions = {"1": {"balance": 100 * 10**18, "cost_wei": 10**18}}
        decision, reason = should_buy(
            positions, 0.02, config, {"1": {"buy_pnl": -12.5}}
        )
        self.assertTrue(decision)
        self.assertIn("-12.50%", reason)

    def test_dashboard_rows_expose_both_sides_and_quote_provenance(self):
        bot = make_bot()
        bot._pnl_quotes = {
            "buy": {
                "quoted_at": "buy-time", "provider": "uniswap",
                "projected_gas_wei": 10**14, "sampled_monotonic": 100,
            },
            "sell": {
                "quoted_at": "sell-time", "provider": "sushiswap",
                "projected_gas_wei": 2 * 10**14, "sampled_monotonic": 100,
                "position_id": "1", "basis": "exact_sell_quote_extrapolated_net",
            },
        }
        rows = [{"id": "1", "pnl": 99}, {"id": "2", "pnl": 99}]
        values = {
            "1": {"buy_pnl": 3.25, "sell_pnl": 1.5},
            "2": {"buy_pnl": -2.0, "sell_pnl": -4.0},
        }
        bot._attach_bidirectional_pnls(rows, values, now=110)

        self.assertEqual(rows[0]["buy_pnl"], 3.25)
        self.assertEqual(rows[0]["sell_pnl"], 1.5)
        self.assertEqual(rows[0]["pnl"], 3.25)
        self.assertEqual(rows[1]["sell_quote_source_position_id"], "1")


if __name__ == "__main__":
    unittest.main()
