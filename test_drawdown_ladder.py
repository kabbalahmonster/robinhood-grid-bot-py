"""Tests for the opt-in gridless drawdown-ladder allocator."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from drawdown_ladder import (
    DrawdownLadderPlan,
    LadderStateError,
    build_plan,
    generate_levels,
    level_is_crossed,
    load_plan,
    mark_exit,
    reconcile_confirmed_positions,
    record_fill,
    save_plan,
    status_payload,
    validate_context,
)
from grid_bot import GridBot
import gridless


WEI = 10**18


def ladder_config(**overrides):
    values = {
        "chain_id": 4663,
        "token_address": "0x0000000000000000000000000000000000000001",
        "tradeable_balance_percent": 100.0,
        "max_active_positions": 50,
        "gridless_min_position_eth": 0.001,
        "gridless_ladder_max_budget_eth": 0.0,
        "gridless_ladder_terminal_drawdown_percent": 90.0,
        "gridless_ladder_spacing": "linear",
        "gridless_ladder_include_reference_entry": False,
        "gridless_ladder_expiry_seconds": 3600,
        "gridless_ladder_rearm_policy": "after_exit",
        "gridless_ladder_rearm_cooldown_seconds": 60,
        "gridless_allocation_mode": "drawdown_ladder",
        "gridless_buy_execution_margin": 50.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DrawdownLadderGeometryTests(unittest.TestCase):
    def test_linear_levels_reach_terminal_drawdown(self):
        levels = generate_levels(1.0, 3, 90, "linear")
        for actual, expected in zip(levels, [0.7, 0.4, 0.1]):
            self.assertAlmostEqual(actual, expected)

    def test_log_levels_are_equal_price_ratios(self):
        levels = generate_levels(1.0, 3, 90, "log")
        self.assertAlmostEqual(levels[-1], 0.1)
        self.assertAlmostEqual(levels[1] / levels[0], levels[2] / levels[1])

    def test_reference_entry_includes_both_endpoints(self):
        levels = generate_levels(1.0, 3, 90, "linear", True)
        for actual, expected in zip(levels, [1.0, 0.55, 0.1]):
            self.assertAlmostEqual(actual, expected)

    def test_single_level_still_targets_terminal(self):
        self.assertAlmostEqual(generate_levels(1.0, 1, 90, "log", True)[0], 0.1)


class DrawdownLadderStateTests(unittest.TestCase):
    def test_build_plan_snapshots_budget_count_and_remainder(self):
        config = ladder_config(
            tradeable_balance_percent=50,
            max_active_positions=4,
            gridless_min_position_eth=0.01,
            gridless_ladder_max_budget_eth=0.035,
        )
        plan = build_plan(2.0, int(0.1 * WEI), config, now=100)
        self.assertEqual(plan.budget_wei, 35_000_000_000_000_000)
        self.assertEqual(len(plan.level_prices), 3)
        self.assertEqual(
            sum(plan.amount_for_level(i) for i in range(3)), plan.budget_wei
        )
        self.assertEqual(plan.expires_at, 3700)

    def test_budget_too_small_does_not_arm(self):
        self.assertIsNone(build_plan(1.0, int(0.0009 * WEI), ladder_config()))

    def test_fill_advances_exactly_one_level_and_terminal_state(self):
        plan = build_plan(
            1.0,
            int(0.002 * WEI),
            ladder_config(max_active_positions=2),
            now=100,
        )
        self.assertTrue(level_is_crossed(plan, plan.level_prices[0]))
        record_fill(plan, 0, plan.amount_for_level(0))
        self.assertEqual(plan.next_level_index, 1)
        with self.assertRaises(LadderStateError):
            record_fill(plan, 1, plan.amount_for_level(1) - 1)
        record_fill(plan, 1, plan.amount_for_level(1))
        self.assertEqual(plan.status, "terminal")
        self.assertEqual(plan.spent_wei, plan.budget_wei)

    def test_persistence_round_trip_and_corruption_fail_closed(self):
        plan = build_plan(1.0, int(0.003 * WEI), ladder_config(), now=100)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ladder.json")
            save_plan(plan, path)
            self.assertEqual(load_plan(path).to_dict(), plan.to_dict())
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 999}, handle)
            with self.assertRaises(LadderStateError):
                load_plan(path)

    def test_context_rejects_foreign_and_duplicate_positions(self):
        plan = build_plan(1.0, int(0.003 * WEI), ladder_config(), now=100)
        foreign = {"0": {"ladder_id": "wrong", "ladder_level_index": 0}}
        with self.assertRaises(LadderStateError):
            validate_context(plan, foreign, ladder_config())
        duplicate = {
            "0": {"ladder_id": plan.id, "ladder_level_index": 0},
            "1": {"ladder_id": plan.id, "ladder_level_index": 0},
        }
        with self.assertRaises(LadderStateError):
            validate_context(plan, duplicate, ladder_config())

    def test_reconciles_confirmed_position_after_checkpoint_crash(self):
        plan = build_plan(1.0, int(0.002 * WEI), ladder_config(), now=100)
        principal = plan.amount_for_level(0)
        positions = {
            "0": {
                "ladder_id": plan.id,
                "ladder_level_index": 0,
                "ladder_principal_wei": principal,
            }
        }
        validate_context(plan, positions, ladder_config())
        self.assertTrue(reconcile_confirmed_positions(plan, positions))
        self.assertEqual(plan.next_level_index, 1)
        self.assertFalse(reconcile_confirmed_positions(plan, positions))

    def test_status_exposes_spent_and_reserved_capital(self):
        plan = build_plan(1.0, int(0.002 * WEI), ladder_config(), now=100)
        record_fill(plan, 0, plan.amount_for_level(0))
        payload = status_payload(plan)
        self.assertEqual(payload["levels_filled"], 1)
        self.assertAlmostEqual(payload["spent_eth"] + payload["reserved_eth"], 0.002)


class DrawdownLadderBotLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_cwd = os.getcwd()
        os.chdir(self.temporary.name)
        self.bot = GridBot.__new__(GridBot)
        self.bot.config = ladder_config()
        self.bot.trade_token_name = "ETH"
        self.bot._mark_buy_tournament_aborted = MagicMock()

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temporary.cleanup()

    def test_arms_frozen_plan_and_reloads_same_plan(self):
        first = self.bot._prepare_gridless_ladder(1.0, {}, int(0.05 * WEI), now=100)
        second = self.bot._prepare_gridless_ladder(0.5, {}, int(0.02 * WEI), now=200)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.reference_price, 1.0)
        self.assertEqual(second.budget_wei, int(0.05 * WEI))

    def test_expired_never_filled_plan_does_not_silently_rearm(self):
        plan = self.bot._prepare_gridless_ladder(1.0, {}, int(0.05 * WEI), now=100)
        expired = self.bot._prepare_gridless_ladder(0.5, {}, int(0.05 * WEI), now=4000)
        self.assertEqual(expired.id, plan.id)
        self.assertEqual(expired.status, "expired")

    def test_completed_exit_rearms_only_after_cooldown(self):
        plan = build_plan(
            1.0,
            int(0.001 * WEI),
            ladder_config(max_active_positions=1),
            now=100,
        )
        record_fill(plan, 0, plan.amount_for_level(0))
        mark_exit(plan, 200, cycle_closed=True)
        save_plan(plan)
        same = self.bot._prepare_gridless_ladder(0.8, {}, int(0.01 * WEI), now=250)
        replacement = self.bot._prepare_gridless_ladder(
            0.8, {}, int(0.01 * WEI), now=261
        )
        self.assertEqual(same.id, plan.id)
        self.assertNotEqual(replacement.id, plan.id)
        self.assertEqual(replacement.reference_price, 0.8)

    def test_final_exit_closes_partially_filled_cycle(self):
        plan = build_plan(1.0, int(0.002 * WEI), ladder_config(), now=100)
        record_fill(plan, 0, plan.amount_for_level(0))
        mark_exit(plan, 200, cycle_closed=True)
        self.assertEqual(plan.status, "closed")
        self.assertEqual(plan.next_level_index, 1)

    def test_terminal_without_confirmed_final_exit_does_not_rearm(self):
        plan = build_plan(
            1.0,
            int(0.001 * WEI),
            ladder_config(max_active_positions=1),
            now=100,
        )
        record_fill(plan, 0, plan.amount_for_level(0))
        # This represents ambiguous cross-file state after a hard stop. Safety
        # requires an explicit closed checkpoint, not merely an empty ledger.
        save_plan(plan)
        same = self.bot._prepare_gridless_ladder(0.8, {}, int(0.01 * WEI), now=1000)
        self.assertEqual(same.id, plan.id)
        self.assertEqual(same.status, "terminal")

    def test_open_positions_without_plan_are_rejected(self):
        with self.assertRaises(LadderStateError):
            self.bot._prepare_gridless_ladder(
                1.0, {"0": {"balance": 1}}, int(0.01 * WEI), now=100
            )

    def test_execution_price_guard_blocks_recovered_route(self):
        context = {
            "trigger_price": 0.5,
            "reference_price": 1.0,
        }
        # 1 ETH / 1 token = 1.0 execution price, above the 0.75 allowance.
        quote = SimpleNamespace(buy_amount=WEI)
        self.bot.token_unit = WEI
        self.assertFalse(
            self.bot._ladder_execution_price_allowed(quote, WEI, context)
        )
        self.bot._mark_buy_tournament_aborted.assert_called_once()

    def test_execution_price_guard_accepts_price_below_margin(self):
        context = {"trigger_price": 0.5, "reference_price": 1.0}
        quote = SimpleNamespace(buy_amount=2 * WEI)
        self.bot.token_unit = WEI
        self.assertTrue(self.bot._ladder_execution_price_allowed(quote, WEI, context))

    def test_polling_arms_then_attempts_only_next_crossed_rung(self):
        self.bot.config.use_eth_trading = True
        self.bot.config.eth_gas_reserve = 0.001
        self.bot.config.weth_address = "0x0000000000000000000000000000000000000002"
        self.bot.wallet = MagicMock()
        self.bot.wallet.get_eth_balance.return_value = 0.051
        self.bot._taxed_token_active = MagicMock(return_value=False)
        self.bot.last_taxed_token_failure_time = 0
        self.bot.last_buy_time = 0
        self.bot.gridless_buy_cooldown = 0
        self.bot._execute_buy_gridless = MagicMock()
        self.bot._funding_warning = None

        with patch("gridless.load_positions", return_value={}):
            self.bot._check_buys_gridless(1.0)
            self.bot._execute_buy_gridless.assert_not_called()
            plan = load_plan()
            self.bot._check_buys_gridless(plan.level_prices[0])

        self.bot._execute_buy_gridless.assert_called_once()
        args = self.bot._execute_buy_gridless.call_args
        self.assertEqual(args.args[1], plan.amount_for_level(0))
        self.assertEqual(args.kwargs["ladder_context"]["level_index"], 0)


class DrawdownLadderPositionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.positions_path = os.path.join(self.temporary.name, "positions.json")
        self.path_patch = patch.object(gridless, "POSITIONS_FILE", self.positions_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temporary.cleanup()

    def test_position_provenance_survives_round_trip(self):
        position_id = gridless.add_position(
            123,
            456,
            ladder_id="ladder-1",
            ladder_level_index=7,
            ladder_principal_wei=100,
        )
        position = gridless.load_positions()[position_id]
        self.assertEqual(position["ladder_id"], "ladder-1")
        self.assertEqual(position["ladder_level_index"], 7)
        self.assertEqual(position["ladder_principal_wei"], 100)

    def test_partial_provenance_is_rejected(self):
        with self.assertRaises(ValueError):
            gridless.add_position(123, 456, ladder_id="ladder-1")

    def test_ladder_mode_suppresses_legacy_buy_focus_only(self):
        config = ladder_config(
            gridless_sell_threshold=5,
            gridless_stoploss_enabled=False,
            gridless_stoploss_threshold=-25,
            gridless_leading_edge=True,
            pnl_trigger_by_min_profit=False,
            token_decimals=18,
        )
        positions = {"0": {"cost_wei": WEI, "balance": 10 * WEI}}
        observations = {
            "0": {"buy_pnl": -20, "sell_pnl": 6},
        }
        focus = gridless.trigger_focus_candidates(positions, config, observations)
        self.assertEqual(focus["buy"], {})
        self.assertTrue(focus["sell"]["sell"]["triggered"])


if __name__ == "__main__":
    unittest.main()
