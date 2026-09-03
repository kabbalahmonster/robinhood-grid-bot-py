import unittest

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

    def test_provider_change_blocks_one_poll_even_when_quotes_are_close(self):
        bot = self.bot()
        bot._sell_quote_consistency_guard("6", "uniswap", 3 * 10**18, now=10)

        result = bot._sell_quote_consistency_guard("6", "sushiswap", 2_970_000_000_000_000_000, now=20)

        self.assertEqual(result["status"], "quote_provider_changed")
        self.assertEqual(result["quote_provider"], "sushiswap")
        self.assertEqual(result["previous_quote_provider"], "uniswap")
        self.assertEqual(result["quote_divergence_percent"], 1.0)
        self.assertIsNone(bot._sell_quote_consistency_guard("6", "sushiswap", 2_960_000_000_000_000_000, now=30))

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


if __name__ == "__main__":
    unittest.main()
