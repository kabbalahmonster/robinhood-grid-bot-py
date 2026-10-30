import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grid_bot import GridBot
from config import load_config
from gridless import should_buy


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
            config = load_config()
            self.assertFalse(config.gridless_multi_action_rounds)
            self.assertEqual(config.poll_cadence_mode, "completion_delay")
            self.assertFalse(config.route_tournament_shadow_background)
            self.assertFalse(config.route_tournament_round_route_reuse)
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

    def test_multi_action_isolates_prebroadcast_candidate_exception(self):
        bot = make_bot(True)

        def attempt(position_id, *_args):
            if position_id == "2":
                raise RuntimeError("final gas simulation failed")
            bot.session_sells += 1

        bot._attempt_gridless_sell_candidate = MagicMock(side_effect=attempt)

        with patch("gridless.load_positions", return_value=POSITIONS):
            bot._check_sells_gridless(1.0, PNLS)

        self.assertEqual(
            [call.args[0] for call in bot._attempt_gridless_sell_candidate.call_args_list],
            ["2", "1"],
        )
        self.assertEqual(bot._sell_round_attempted, 2)
        self.assertEqual(bot._sell_round_succeeded, 1)
        self.assertFalse(bot._allow_buy_after_sell_this_cycle)
        self.assertTrue(bot._sell_has_cycle_priority())

    def test_multi_action_exception_stops_on_unresolved_broadcast(self):
        bot = make_bot(True)
        states = iter([False, True])
        bot.wallet = SimpleNamespace(
            has_unresolved_broadcast=lambda: next(states, True)
        )
        bot._attempt_gridless_sell_candidate = MagicMock(
            side_effect=RuntimeError("receipt unavailable")
        )

        with patch("gridless.load_positions", return_value=POSITIONS):
            bot._check_sells_gridless(1.0, PNLS)

        bot._attempt_gridless_sell_candidate.assert_called_once()
        self.assertEqual(bot._sell_round_attempted, 1)
        self.assertEqual(bot._sell_round_succeeded, 0)
        self.assertFalse(bot._allow_buy_after_sell_this_cycle)

    def test_multi_action_exception_stops_when_safety_halted(self):
        bot = make_bot(True)

        def halt_and_fail(*_args):
            bot._safety_halted = True
            raise RuntimeError("sell balance changed unexpectedly")

        bot._attempt_gridless_sell_candidate = MagicMock(
            side_effect=halt_and_fail
        )

        with patch("gridless.load_positions", return_value=POSITIONS):
            bot._check_sells_gridless(1.0, PNLS)

        bot._attempt_gridless_sell_candidate.assert_called_once()
        self.assertEqual(bot._sell_round_attempted, 1)
        self.assertEqual(bot._sell_round_succeeded, 0)
        self.assertFalse(bot._allow_buy_after_sell_this_cycle)

    def test_multi_action_exception_after_broadcast_boundary_halts(self):
        bot = make_bot(True)

        def broadcast_and_fail(*_args):
            bot._sell_candidate_broadcast_started = True
            raise RuntimeError("post-submit checkpoint failed")

        bot._attempt_gridless_sell_candidate = MagicMock(
            side_effect=broadcast_and_fail
        )

        with patch("gridless.load_positions", return_value=POSITIONS):
            bot._check_sells_gridless(1.0, PNLS)

        bot._attempt_gridless_sell_candidate.assert_called_once()
        self.assertTrue(bot._safety_halted)
        self.assertEqual(bot._sell_round_attempted, 1)
        self.assertEqual(bot._sell_round_succeeded, 0)
        self.assertFalse(bot._allow_buy_after_sell_this_cycle)

    def test_legacy_sell_exception_still_propagates(self):
        bot = make_bot(False)
        bot._attempt_gridless_sell_candidate = MagicMock(
            side_effect=RuntimeError("final gas simulation failed")
        )

        with patch("gridless.load_positions", return_value=POSITIONS):
            with self.assertRaisesRegex(RuntimeError, "final gas simulation"):
                bot._check_sells_gridless(1.0, PNLS)

        bot._attempt_gridless_sell_candidate.assert_called_once()

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

    def test_staged_sell_skips_impossible_preapproval_gas_probe(self):
        bot = make_bot(True)
        bot.config.token_address = "token"
        bot.config.zero_x_proxy = "proxy"
        bot.config.moonbag_percentage = 0
        bot.config.min_profit_percent = 5
        bot.config.max_sell_gas_eth = 1
        bot.trade_token_address = "native"
        bot.api_client = SimpleNamespace(name="lifi")
        bot.provider = SimpleNamespace(
            capabilities=SimpleNamespace(exact_amount_approval=False),
        )
        bot.wallet = MagicMock()
        bot.wallet.check_allowance.return_value = 0
        bot.wallet.build_token_approval_transaction.return_value = {"gas": 10}
        bot.wallet.normal_gas_price.return_value = 1
        bot._wallet_can_cover_sell = MagicMock(return_value=True)
        bot._queue_route_shadow = MagicMock()
        quote = SimpleNamespace(
            success=True,
            error=None,
            allowance_target="lifi-router",
        )
        quote._tournament_staged_approval = True
        bot._actionable_quote_with_weth_fallback = MagicMock(
            return_value=(quote, False),
        )
        bot._taxed_quote_return_wei = MagicMock(return_value=1200)
        bot._sell_quote_consistency_guard = MagicMock(return_value=None)
        bot._swap_gas_fields = MagicMock(
            side_effect=AssertionError("swap gas probed before required approval"),
        )
        bot._execute_sell_gridless = MagicMock()

        with patch("gridless.load_positions", return_value={"1": POSITIONS["1"]}):
            bot._attempt_gridless_sell_candidate(
                "1", POSITIONS["1"], 1.0, "PROFIT (sell): 8.0%", 8
            )

        bot._swap_gas_fields.assert_not_called()
        bot.wallet.build_token_approval_transaction.assert_called_once_with(
            "token", "lifi-router", 2**256 - 1,
        )
        bot._execute_sell_gridless.assert_called_once_with(
            "1", POSITIONS["1"], 1.0, quote,
        )

    def test_gate_winner_is_not_vetoed_by_cached_provider_change(self):
        bot = make_bot(True)
        bot.config.route_tournament_mode = "gate"
        bot.config.route_tournament_canary = True
        bot.config.token_address = "token"
        bot.config.zero_x_proxy = "proxy"
        bot.config.moonbag_percentage = 0
        bot.config.min_profit_percent = 5
        bot.config.max_sell_gas_eth = 1
        bot.trade_token_address = "native"
        bot.api_client = SimpleNamespace(name="lifi")
        bot.provider = SimpleNamespace(
            active=SimpleNamespace(name="lifi"),
            capabilities=SimpleNamespace(exact_amount_approval=False),
        )
        bot.wallet = MagicMock()
        bot.wallet.check_allowance.return_value = 0
        bot.wallet.build_token_approval_transaction.return_value = {"gas": 10}
        bot.wallet.normal_gas_price.return_value = 1
        bot._wallet_can_cover_sell = MagicMock(return_value=True)
        bot._queue_route_shadow = MagicMock()
        quote = SimpleNamespace(
            success=True,
            error=None,
            allowance_target="lifi-router",
        )
        quote._tournament_staged_approval = True
        bot._actionable_quote_with_weth_fallback = MagicMock(
            return_value=(quote, False),
        )
        bot._taxed_quote_return_wei = MagicMock(return_value=1200)
        bot._sell_quote_consistency_guard = MagicMock(return_value={
            "status": "quote_provider_disagreement",
            "previous_quote_provider": "uniswap",
            "quote_provider": "lifi",
            "quote_divergence_percent": 17.53,
        })
        bot._best_fresh_sell_route = MagicMock(
            side_effect=AssertionError("gate authority was reopened"),
        )
        bot._execute_sell_gridless = MagicMock()

        with patch("gridless.load_positions", return_value={"1": POSITIONS["1"]}):
            bot._attempt_gridless_sell_candidate(
                "1", POSITIONS["1"], 1.0, "PROFIT (sell): 29.0%", 29
            )

        bot._best_fresh_sell_route.assert_not_called()
        bot._execute_sell_gridless.assert_called_once_with(
            "1", POSITIONS["1"], 1.0, quote,
        )

    def test_final_sell_simulation_failure_is_safe_explicit_abort(self):
        bot = make_bot(True)
        bot.config.route_tournament_mode = "gate"
        bot.config.route_tournament_canary = True
        bot.config.token_address = "token"
        bot.config.zero_x_proxy = "proxy"
        bot.config.moonbag_percentage = 0
        bot.config.min_profit_percent = 5
        bot.config.gridless_stoploss_enabled = False
        bot.config.gridless_stoploss_threshold = -25
        bot.config.use_eth_trading = True
        bot.trade_token_address = "native"
        bot.trade_token_name = "ETH"
        bot.token_unit = 10**18
        bot.provider = SimpleNamespace(
            active=SimpleNamespace(name="lifi"),
            name="lifi",
            capabilities=SimpleNamespace(
                api_managed_approval=False,
                exact_amount_approval=False,
                refresh_after_approval=False,
            ),
        )
        bot.api_client = bot.provider
        bot.wallet = MagicMock()
        bot.wallet.check_allowance.return_value = 100
        bot._gridless_sell_terms = MagicMock(return_value=(100, 1000))
        bot._queue_route_shadow = MagicMock()
        bot._taxed_quote_return_wei = MagicMock(return_value=2000)
        bot._minimum_gas_aware_return_wei = MagicMock(return_value=1100)
        bot._quote_requires_dynamic_gas = MagicMock(return_value=True)
        bot._quote_matches_exact_input = MagicMock(return_value=True)
        bot._gas_within_hard_cap = MagicMock(return_value=True)
        bot._swap_gas_fields = MagicMock(side_effect=[
            (100, 1), RuntimeError("lifi final gas simulation failed"),
        ])
        bot._route_comparisons = {
            "sell": {
                "mode": "execution_preflight",
                "status": "preflight_candidate_selected",
            }
        }
        quote = SimpleNamespace(
            success=True,
            error=None,
            sell_amount=100,
            buy_amount=2000,
            allowance_target="lifi-router",
            weth_fallback=False,
        )

        with patch("gridless.calculate_pnl", return_value=29):
            bot._execute_sell_gridless("1", POSITIONS["1"], 1.0, quote)

        self.assertEqual(bot._swap_gas_fields.call_count, 2)
        bot.wallet._send_transaction.assert_not_called()
        bot._close_incomplete_tournament.assert_called_once_with(
            "sell", reason="final_local_gas_simulation_failed", failed=False,
        )

    def test_multi_buy_pass_consumes_pnl_snapshot_after_first_fill(self):
        bot = make_bot(True)
        bot._pnl_quotes = {"buy": {"sample": 1}, "sell": {"sample": 2}}
        observed = {"1": {"buy_trigger_pnls": {"buy": -20}}}
        attempts = 0

        def check(_price, supplied):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.assertIs(supplied, observed)
                bot.session_buys += 1
            else:
                self.assertEqual(supplied, {})

        bot.check_buys = MagicMock(side_effect=check)

        completed = bot._check_buys_for_round(1.0, observed)

        self.assertEqual(completed, 1)
        self.assertEqual(bot.check_buys.call_count, 2)
        self.assertEqual(bot._pnl_quotes, {"buy": None, "sell": None})

    def test_multi_buy_round_stops_after_one_gap_seed_at_static_price(self):
        bot = make_bot(True)
        bot.config.max_active_positions = 16
        bot.config.gridless_buy_threshold = -10.0
        bot.config.gridless_leading_edge = True
        bot.config.token_decimals = 18
        positions = {
            str(index): {
                "cost_wei": int((0.14 + index * 0.01) * 10**18),
                "balance": 10**18,
            }
            for index in range(10)
        }
        observed = {
            "0": {"buy_trigger_pnls": {"uniswap": -30.0}},
            "10": {"buy_trigger_pnls": {"uniswap": -12.0}},
        }

        def check(price, supplied):
            eligible, _ = should_buy(positions, price, bot.config, supplied)
            if not eligible:
                return
            positions["10"] = {
                "cost_wei": int(0.102 * 10**18),
                "balance": 10**18,
                "threshold_fill_price": 0.1,
                "threshold_entry_kind": "dip",
            }
            bot.session_buys += 1

        bot.check_buys = MagicMock(side_effect=check)

        completed = bot._check_buys_for_round(0.1, observed)

        self.assertEqual(completed, 1)
        self.assertEqual(bot.check_buys.call_count, 2)
        self.assertEqual(len(positions), 11)

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
