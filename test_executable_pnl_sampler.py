import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from grid_bot import GridBot


def make_bot():
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(
        executable_pnl_sample_seconds=120,
        executable_pnl_near_trigger_seconds=60,
        executable_pnl_near_trigger_margin_percent=5,
        gridless_sell_threshold=100,
        moonbag_percentage=0,
        token_address="0x" + "11" * 20,
        use_eth_trading=True,
        slippage_tolerance=2,
        taxed_token=False,
        gas_limit_multiplier=1.0,
    )
    bot.token_decimals = 18
    bot.token_unit = 10**18
    bot.trade_token_address = "0x" + "00" * 20
    bot.wallet = SimpleNamespace(
        address="0x" + "22" * 20,
        normal_gas_price=Mock(return_value=100),
    )
    bot.provider = SimpleNamespace(name="uniswap")
    bot.api_client = SimpleNamespace(get_quote=Mock())
    bot._route_comparisons = {}
    bot._executable_pnl_sample = None
    bot._next_executable_pnl_sample_at = 0.0
    return bot


class TestExecutablePnlSampler(unittest.TestCase):
    @patch("grid_bot.random.uniform", return_value=1.0)
    def test_quotes_highest_spot_position_once_then_reuses_cache(self, _uniform):
        bot = make_bot()
        bot.api_client.get_quote.return_value = SimpleNamespace(
            success=True,
            sell_amount=100 * 10**18,
            buy_amount=2 * 10**18,
            minimum_buy_amount=None,
            output_is_execution_floor=False,
            gas=300000,
        )
        positions = {
            "1": {"balance": 100 * 10**18, "cost_wei": 10**18},
            "2": {"balance": 100 * 10**18, "cost_wei": 2 * 10**18},
        }

        first = bot._refresh_executable_pnl_sample(positions, 0.015, now=100)
        second = bot._refresh_executable_pnl_sample(positions, 0.015, now=150)

        self.assertEqual(first["position_id"], "1")
        self.assertEqual(first["basis"], "exact_reverse_quote")
        self.assertEqual(first["pnl_percent"], 96.0)
        self.assertIs(second, first)
        self.assertEqual(bot.api_client.get_quote.call_count, 1)
        self.assertEqual(bot._next_executable_pnl_sample_at, 220)

    @patch("grid_bot.random.uniform", return_value=1.0)
    def test_near_trigger_uses_one_minute_interval(self, _uniform):
        bot = make_bot()
        bot.config.gridless_sell_threshold = 55

        self.assertEqual(bot._executable_pnl_sample_interval(49.9), 120)
        self.assertEqual(bot._executable_pnl_sample_interval(50), 60)

    @patch("grid_bot.random.uniform", return_value=1.0)
    def test_reuses_same_cycle_tournament_without_provider_request(self, _uniform):
        bot = make_bot()
        bot._route_comparisons = {
            "sell": {
                "mode": "execution_preflight",
                "status": "preflight_candidate_selected",
                "updated_at": "2026-09-15T12:00:00+00:00",
                "selected_hypothetical_winner": {
                    "provider": "sushiswap", "settlement": "native"
                },
                "candidates": [{
                    "provider": "sushiswap",
                    "settlement": "native",
                    "projected_profit_percent": 12.345,
                    "projected_net_score": str(1123450000000000000),
                    "projected_total_gas_wei": str(25000000000000),
                }],
            }
        }

        sample = bot._refresh_executable_pnl_sample(
            {"7": {"balance": 100 * 10**18, "cost_wei": 10**18}},
            0.02,
            now=100,
        )

        self.assertEqual(sample["basis"], "tournament")
        self.assertEqual(sample["pnl_percent"], 12.35)
        self.assertEqual(sample["provider"], "sushiswap")
        bot.api_client.get_quote.assert_not_called()

    @patch("grid_bot.random.uniform", return_value=1.0)
    def test_position_change_invalidates_old_sample_when_refresh_fails(self, _uniform):
        bot = make_bot()
        bot._executable_pnl_sample = {
            "position_id": "1", "sell_amount_raw": 100, "sold_cost_wei": 100,
        }
        bot._next_executable_pnl_sample_at = 999
        bot.api_client.get_quote.return_value = SimpleNamespace(success=False)

        sample = bot._refresh_executable_pnl_sample(
            {"2": {"balance": 200, "cost_wei": 100}}, 10**18, now=100
        )

        self.assertIsNone(sample)
        self.assertIsNone(bot._executable_pnl_sample)
        self.assertEqual(bot.api_client.get_quote.call_count, 1)

    def test_attaches_only_to_exact_sampled_position(self):
        positions = [{"id": "1", "pnl": 2}, {"id": "2", "pnl": 3}]
        sample = {
            "position_id": "2", "pnl_percent": -4.5,
            "net_return_eth": 0.0028, "projected_gas_eth": 0.0001,
            "provider": "uniswap", "settlement": "native",
            "basis": "exact_reverse_quote", "quoted_at": "now",
            "sell_amount_raw": 123,
        }

        GridBot._attach_executable_pnl_sample(positions, sample)

        self.assertNotIn("executable_pnl", positions[0])
        self.assertEqual(positions[1]["executable_pnl"], -4.5)
        self.assertEqual(positions[1]["executable_sell_amount_raw"], "123")


if __name__ == "__main__":
    unittest.main()
