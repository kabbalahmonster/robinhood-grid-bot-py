import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grid_bot import GridBot
from config import load_config


def make_bot(enabled):
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(
        gridless_multi_action_rounds=enabled,
        gridless_sell_threshold=5,
        gridless_stoploss_enabled=False,
        gridless_stoploss_threshold=-25,
        bidirectional_pnl_enabled=True,
        route_tournament_mode="off",
        use_gridless=True,
        max_active_positions=8,
    )
    bot.wallet = SimpleNamespace(has_unresolved_broadcast=lambda: False)
    bot.provider = SimpleNamespace()
    bot.token_decimals = 18
    bot.session_sells = 0
    bot.session_buys = 0
    bot._safety_halted = False
    bot._sell_priority_this_cycle = False
    bot._allow_buy_after_sell_this_cycle = False
    bot._pnl_trigger_latches = {"buy": None, "sell": None}
    bot._close_incomplete_tournament = MagicMock()
    return bot


POSITIONS = {
    "1": {"balance": 100, "cost_wei": 1000},
    "2": {"balance": 200, "cost_wei": 2000},
    "3": {"balance": 300, "cost_wei": 3000},
}

PNLS = {
    "1": {"sell_trigger_pnls": {"sell": 8}},
    "2": {"sell_trigger_pnls": {"sell": 12}},
    "3": {"sell_trigger_pnls": {"sell": 2}},
}


class MultiActionRoundTests(unittest.TestCase):
    def test_config_is_legacy_safe_and_requires_explicit_activation(self):
        base = {
            "PRIVATE_KEY": "0x" + "1" * 64,
            "RPC_URL": "https://rpc.example.invalid",
            "CHAIN_ID": "4663",
            "TOKEN_ADDRESS": "0x" + "2" * 40,
            "UNISWAP_API_KEY": "test-key",
        }
        with patch.dict(os.environ, base, clear=True):
            self.assertFalse(load_config().gridless_multi_action_rounds)
        with patch.dict(
            os.environ,
            {**base, "GRIDLESS_MULTI_ACTION_ROUNDS": "true"},
            clear=True,
        ):
            self.assertTrue(load_config().gridless_multi_action_rounds)

    def test_legacy_default_attempts_only_best_sell(self):
        bot = make_bot(False)
        bot._attempt_gridless_sell_candidate = MagicMock()

        with patch("gridless.load_positions", return_value=POSITIONS):
            bot._check_sells_gridless(1.0, PNLS)

        bot._attempt_gridless_sell_candidate.assert_called_once()
        self.assertEqual(
            bot._attempt_gridless_sell_candidate.call_args.args[0], "2"
        )
        self.assertTrue(bot._sell_priority_this_cycle)
        self.assertFalse(bot._allow_buy_after_sell_this_cycle)

    def test_multi_action_snapshots_and_attempts_every_eligible_sell(self):
        bot = make_bot(True)

        def confirm(position_id, *_args):
            bot.session_sells += 1

        bot._attempt_gridless_sell_candidate = MagicMock(side_effect=confirm)

        with patch("gridless.load_positions", return_value=POSITIONS):
            bot._check_sells_gridless(1.0, PNLS)

        self.assertEqual(
            [call.args[0] for call in bot._attempt_gridless_sell_candidate.call_args_list],
            ["2", "1"],
        )
        self.assertEqual(bot._sell_round_attempted, 2)
        self.assertEqual(bot._sell_round_succeeded, 2)
        self.assertTrue(bot._allow_buy_after_sell_this_cycle)
        self.assertFalse(bot._sell_has_cycle_priority())

    def test_changed_position_is_not_quoted_or_sold(self):
        bot = make_bot(True)
        bot._actionable_quote_with_weth_fallback = MagicMock()
        changed = {"1": {**POSITIONS["1"], "balance": 99}}

        with patch("gridless.load_positions", return_value=changed):
            bot._attempt_gridless_sell_candidate(
                "1", POSITIONS["1"], 1.0, "PROFIT (sell): 8.0%", 8
            )

        bot._actionable_quote_with_weth_fallback.assert_not_called()
        self.assertEqual(bot.session_sells, 0)

    def test_multi_buy_pass_reuses_snapshot_until_first_non_fill(self):
        bot = make_bot(True)
        observed = {"1": {"buy_trigger_pnls": {"buy": -20}}}
        attempts = 0

        def check(_price, supplied):
            nonlocal attempts
            self.assertIs(supplied, observed)
            attempts += 1
            if attempts <= 2:
                bot.session_buys += 1

        bot.check_buys = MagicMock(side_effect=check)

        completed = bot._check_buys_for_round(1.0, observed)

        self.assertEqual(completed, 2)
        self.assertEqual(bot.check_buys.call_count, 3)

    def test_unresolved_broadcast_stops_remaining_actions(self):
        bot = make_bot(True)
        states = iter([False, True])
        bot.wallet = SimpleNamespace(
            has_unresolved_broadcast=lambda: next(states, True)
        )
        bot.check_buys = MagicMock()

        self.assertEqual(bot._check_buys_for_round(1.0, {}), 0)
        bot.check_buys.assert_called_once()


if __name__ == "__main__":
    unittest.main()
