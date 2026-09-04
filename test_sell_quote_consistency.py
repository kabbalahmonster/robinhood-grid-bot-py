import unittest
from types import SimpleNamespace

from grid_bot import GridBot


class SellQuoteConsistencyTests(unittest.TestCase):
    def bot(self):
        bot = GridBot.__new__(GridBot)
        bot._last_sell_quotes = {}
        return bot

    def test_first_quote_and_same_provider_confirmation_are_allowed(self):
        bot = self.bot()
        self.assertIsNone(bot._sell_quote_consistency_guard("6", "uniswap", 3_000, now=10))
        self.assertIsNone(bot._sell_quote_consistency_guard("6", "uniswap", 2_900, now=20))

    def test_provider_change_is_flagged_even_when_quotes_are_close(self):
        bot = self.bot()
        bot._sell_quote_consistency_guard("6", "uniswap", 3 * 10**18, now=10)

        result = bot._sell_quote_consistency_guard("6", "sushiswap", 2_970_000_000_000_000_000, now=20)

        self.assertEqual(result["status"], "quote_provider_changed")
        self.assertEqual(result["quote_provider"], "sushiswap")
        self.assertEqual(result["previous_quote_provider"], "uniswap")
        self.assertEqual(result["quote_divergence_percent"], 1.0)
        self.assertIsNone(bot._sell_quote_consistency_guard("6", "sushiswap", 2_960_000_000_000_000_000, now=30))

    def test_small_provider_change_can_be_resolved_from_fresh_routes(self):
        class Provider:
            def __init__(self, name, quote):
                self.name = name
                self.quote = quote

            def build_swap_transaction(self, **_kwargs):
                return self.quote

        bot = self.bot()
        bot._sell_quote_consistency_guard("6", "uniswap", 1_000, now=10)
        change = bot._sell_quote_consistency_guard("6", "sushiswap", 990, now=20)
        self.assertEqual(change["status"], "quote_provider_changed")

        current_quote = SimpleNamespace(success=True, buy_amount=990, gas=0)
        alternate_quote = SimpleNamespace(success=True, buy_amount=1_010, gas=0)
        primary = Provider("uniswap", alternate_quote)
        fallback = Provider("sushiswap", current_quote)
        bot.provider = SimpleNamespace(primary=primary, fallback=fallback)
        bot.config = SimpleNamespace(
            token_address="0xtoken", max_sell_gas_eth=0.0002, max_swap_gas_eth=0.0002,
        )
        bot.trade_token_address = "0xtrade"
        bot.wallet = SimpleNamespace(address="0xwallet")
        bot._swap_slippage_fraction = lambda: 0.02
        bot._taxed_token_active = lambda: False
        bot._swap_gas_fields = lambda quote, _default: (100, 1)

        provider, quote, detail = bot._best_fresh_sell_route(fallback, current_quote, 123)

        self.assertIs(provider, primary)
        self.assertIs(quote, alternate_quote)
        self.assertEqual(detail["status"], "fresh_best_route_selected")

    def test_material_cross_provider_disagreement_is_identified(self):
        bot = self.bot()
        bot._sell_quote_consistency_guard("6", "uniswap", 3 * 10**18, now=10)

        result = bot._sell_quote_consistency_guard("6", "sushiswap", 2_550_000_000_000_000_000, now=20)

        self.assertEqual(result["status"], "quote_provider_disagreement")
        self.assertEqual(result["quote_divergence_percent"], 15.0)
        self.assertEqual(result["quoted_return_eth"], 2.55)
        self.assertEqual(result["previous_quoted_return_eth"], 3.0)

    def test_stale_other_provider_quote_does_not_block(self):
        bot = self.bot()
        bot._sell_quote_consistency_guard("6", "uniswap", 3_000, now=10)
        self.assertIsNone(bot._sell_quote_consistency_guard("6", "sushiswap", 1_000, now=131))

    def test_disagreement_route_check_uses_fresh_alternate_and_net_output(self):
        class Provider:
            def __init__(self, name, quote):
                self.name = name
                self.quote = quote

            def build_swap_transaction(self, **_kwargs):
                return self.quote

        bot = self.bot()
        current_quote = SimpleNamespace(success=True, buy_amount=1_000, gas=0)
        alternate_quote = SimpleNamespace(success=True, buy_amount=1_050, gas=0)
        primary = Provider("uniswap", current_quote)
        fallback = Provider("sushiswap", alternate_quote)
        bot.provider = SimpleNamespace(primary=primary, fallback=fallback)
        bot.config = SimpleNamespace(
            token_address="0xtoken",
            max_sell_gas_eth=0.0002,
            max_swap_gas_eth=0.0002,
        )
        bot.trade_token_address = "0xtrade"
        bot.wallet = SimpleNamespace(address="0xwallet")
        bot._swap_slippage_fraction = lambda: 0.02
        bot._taxed_token_active = lambda: False
        # The fresher Sushi route has a slightly higher fee but still wins net.
        bot._swap_gas_fields = lambda quote, _default: (100 if quote is current_quote else 120, 1)

        provider, quote, detail = bot._best_fresh_sell_route(primary, current_quote, 123)

        self.assertIs(provider, fallback)
        self.assertIs(quote, alternate_quote)
        self.assertEqual(detail["status"], "fresh_best_route_selected")
        self.assertEqual(detail["quote_provider"], "sushiswap")

    def test_disagreement_route_check_excludes_a_route_over_sell_cap(self):
        class Provider:
            def __init__(self, name, quote):
                self.name = name
                self.quote = quote

            def build_swap_transaction(self, **_kwargs):
                return self.quote

        bot = self.bot()
        current_quote = SimpleNamespace(success=True, buy_amount=1_000, gas=0)
        alternate_quote = SimpleNamespace(success=True, buy_amount=9_999, gas=0)
        primary = Provider("uniswap", current_quote)
        fallback = Provider("sushiswap", alternate_quote)
        bot.provider = SimpleNamespace(primary=primary, fallback=fallback)
        bot.config = SimpleNamespace(
            token_address="0xtoken",
            max_sell_gas_eth=0.00000000000000015,
            max_swap_gas_eth=0.00000000000000015,
        )
        bot.trade_token_address = "0xtrade"
        bot.wallet = SimpleNamespace(address="0xwallet")
        bot._swap_slippage_fraction = lambda: 0.02
        bot._taxed_token_active = lambda: False
        bot._swap_gas_fields = lambda quote, _default: (100, 1 if quote is current_quote else 2)

        provider, quote, detail = bot._best_fresh_sell_route(primary, current_quote, 123)

        self.assertIs(provider, primary)
        self.assertIs(quote, current_quote)
        self.assertEqual(detail["quote_provider"], "uniswap")

    def test_gas_cap_probe_uses_cap_compliant_alternate_for_buy_and_sell(self):
        class Provider:
            def __init__(self, name, quote):
                self.name = name
                self.quote = quote
                self.calls = []

            def build_swap_transaction(self, **kwargs):
                self.calls.append(kwargs)
                return self.quote

        for operation in ("buy", "sell"):
            with self.subTest(operation=operation):
                bot = self.bot()
                current_quote = SimpleNamespace(success=True, gas=300)
                alternate_quote = SimpleNamespace(success=True, gas=100)
                primary = Provider("uniswap", current_quote)
                fallback = Provider("sushiswap", alternate_quote)
                bot.provider = SimpleNamespace(primary=primary, fallback=fallback)
                bot.config = SimpleNamespace(
                    max_swap_gas_eth=150 / 10**18,
                    max_buy_gas_eth=150 / 10**18,
                    max_sell_gas_eth=150 / 10**18,
                )
                bot.wallet = SimpleNamespace(address="0xwallet")
                bot._swap_slippage_fraction = lambda: 0.02
                bot._swap_gas_fields = lambda quote, _default: (quote.gas, 1)

                provider, quote, detail = bot._alternate_route_for_gas_cap(
                    primary, current_quote,
                    sell_token="0xsell",
                    buy_token="0xbuy",
                    sell_amount=123,
                    operation=operation,
                    default_gas=300000,
                )

                self.assertIs(provider, fallback)
                self.assertIs(quote, alternate_quote)
                self.assertEqual(detail["status"], "alternate_route_selected_after_gas_cap")
                self.assertEqual(fallback.calls[0]["sell_amount"], 123)


if __name__ == "__main__":
    unittest.main()
