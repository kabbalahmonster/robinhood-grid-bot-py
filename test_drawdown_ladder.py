"""Tests for adaptive reusable gridless drawdown allocation."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from drawdown_ladder import (
    LadderStateError,
    advance_rearms,
    anchor_survivor_bootstrap,
    adopt_legacy_positions,
    build_plan,
    eligible_level,
    generate_levels,
    load_plan,
    mark_exit,
    reconcile_confirmed_positions,
    reconcile_adoption_provenance,
    reanchor_survivor,
    record_fill,
    refresh_plan_funding,
    save_plan,
    status_payload,
    validate_context,
)
from grid_bot import GridBot
from config import load_config
import gridless


WEI = 10**18


def ladder_config(**overrides):
    values = {
        "chain_id": 4663,
        "token_address": "0x0000000000000000000000000000000000000001",
        "tradeable_balance_percent": 100.0,
        "max_active_positions": 20,
        "gridless_min_position_eth": 0.001,
        "gridless_ladder_max_budget_eth": 0.0,
        "gridless_ladder_terminal_drawdown_percent": 95.0,
        "gridless_ladder_spacing": "linear",
        "gridless_ladder_include_reference_entry": False,
        "gridless_ladder_expiry_seconds": 0,
        "gridless_ladder_rearm_policy": "after_exit",
        "gridless_ladder_rearm_cooldown_seconds": 0,
        "gridless_allocation_mode": "drawdown_ladder",
        "gridless_leading_edge": True,
        "gridless_sell_threshold": 5.0,
        "bidirectional_pnl_enabled": False,
        "gridless_buy_execution_margin": 50.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def funded_indices(plan):
    return [r.index for r in plan.rungs if r.principal_wei > 0]


class DrawdownLadderGeometryTests(unittest.TestCase):
    def test_survivor_leading_edge_uses_highest_purchase_point_at_half_sell(self):
        positions = {
            "low": {"cost_wei": WEI, "balance": 2 * WEI},
            "high": {"cost_wei": WEI, "balance": WEI},
        }
        config = ladder_config(
            gridless_allocation_mode="survivor",
            max_active_positions=5,
            token_decimals=18,
            gridless_sell_threshold=5,
            bidirectional_pnl_enabled=True,
        )
        triggered, _reason, source_id = gridless.survivor_leading_edge_trigger(
            positions,
            1.025,
            config,
            {
                "low": {"buy_trigger_pnls": {"buy": 10}},
                "high": {"buy_trigger_pnls": {"buy": 2.5}},
            },
        )
        self.assertTrue(triggered)
        self.assertEqual(source_id, "high")

    def test_survivor_rapid_poll_defaults_and_overrides_load(self):
        env = {
            "PRIVATE_KEY": "0x" + "1" * 64,
            "RPC_URL": "https://rpc.example.invalid",
            "CHAIN_ID": "4663",
            "TOKEN_ADDRESS": "0x" + "2" * 40,
            "UNISWAP_API_KEY": "test-key",
            "GRIDLESS_ALLOCATION_MODE": "survivor",
            "SURVIVOR_RAPID_POLL_SECONDS": "2",
            "SURVIVOR_RAPID_POLL_WINDOW_SECONDS": "45",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertEqual(config.survivor_rapid_poll_seconds, 2)
        self.assertEqual(config.survivor_rapid_poll_window_seconds, 45)

    def test_linear_levels_reach_terminal_drawdown(self):
        levels = generate_levels(1.0, 5, 95, "linear")
        self.assertAlmostEqual(levels[0], 0.81)
        self.assertAlmostEqual(levels[-1], 0.05)

    def test_log_levels_are_equal_price_ratios(self):
        levels = generate_levels(1.0, 5, 95, "log")
        self.assertAlmostEqual(levels[-1], 0.05)
        self.assertAlmostEqual(levels[1] / levels[0], levels[4] / levels[3])

    def test_reference_entry_includes_both_endpoints(self):
        levels = generate_levels(1.0, 3, 90, "linear", True)
        for actual, expected in zip(levels, [1.0, 0.55, 0.1]):
            self.assertAlmostEqual(actual, expected)


class AdaptiveLadderStateTests(unittest.TestCase):
    def test_survivor_bootstrap_forms_geometry_from_confirmed_fill_even_if_lower(self):
        config = ladder_config(
            gridless_allocation_mode="survivor", max_active_positions=5,
        )
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        level = min(funded_indices(plan))
        record_fill(
            plan, level, plan.amount_for_level(level), "0",
            filled_at=101, entry_kind="leading_edge",
        )
        self.assertTrue(anchor_survivor_bootstrap(plan, 0.99))
        self.assertEqual(plan.reference_price, 0.99)
        self.assertEqual(plan.reanchor_count, 0)

    def test_survivor_reanchors_upward_and_preserves_open_position(self):
        config = ladder_config(
            gridless_allocation_mode="survivor",
            max_active_positions=5,
        )
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        self.assertEqual(plan.version, 3)
        self.assertEqual(plan.mode, "survivor")
        principal = plan.amount_for_level(0)
        record_fill(plan, 0, principal, "7", filled_at=101)
        old_prices = plan.level_prices

        self.assertTrue(reanchor_survivor(plan, 1.01, now=102))

        self.assertEqual(plan.reference_price, 1.01)
        self.assertEqual(plan.reanchor_count, 1)
        self.assertTrue(all(new > old for new, old in zip(plan.level_prices, old_prices)))
        self.assertEqual(plan.rungs[0].state, "open")
        self.assertEqual(plan.rungs[0].position_id, "7")
        self.assertEqual(plan.rungs[0].open_entry_kind, "ladder")

    def test_survivor_reanchor_requires_a_higher_confirmed_purchase_point(self):
        config = ladder_config(gridless_allocation_mode="survivor")
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        self.assertFalse(reanchor_survivor(plan, 1.0, now=101))
        self.assertFalse(reanchor_survivor(plan, 0.5, now=102))
        self.assertEqual(plan.reference_price, 1.0)

    def test_survivor_allows_multiple_leading_edge_positions(self):
        config = ladder_config(gridless_allocation_mode="survivor")
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        levels = funded_indices(plan)[:2]
        principal = plan.amount_for_level(levels[0])
        record_fill(
            plan, levels[0], principal, "1", filled_at=102,
            entry_kind="leading_edge"
        )
        record_fill(
            plan, levels[1], plan.amount_for_level(levels[1]), "2",
            filled_at=103, entry_kind="leading_edge"
        )
        self.assertEqual(plan.rungs[levels[0]].open_entry_kind, "leading_edge")
        self.assertEqual(plan.rungs[levels[1]].open_entry_kind, "leading_edge")
        mark_exit(plan, levels[0], "1", exited_at=104)
        self.assertIsNone(plan.rungs[levels[0]].open_entry_kind)

    def test_survivor_restart_recovers_confirmed_leading_edge(self):
        config = ladder_config(gridless_allocation_mode="survivor")
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        level = min(funded_indices(plan))
        position = {
            "0": {
                "ladder_id": plan.id,
                "ladder_level_index": level,
                "ladder_principal_wei": plan.amount_for_level(level),
                "ladder_entry_kind": "leading_edge",
                "ladder_fill_price": 1.01,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ladder.json")
            save_plan(plan, path)
            recovered = load_plan(path)
        self.assertTrue(reconcile_confirmed_positions(recovered, position))
        self.assertEqual(recovered.rungs[level].open_entry_kind, "leading_edge")
        self.assertEqual(recovered.reference_price, 1.01)
        validate_context(recovered, position, config)

    def test_survivor_restart_recovers_confirmed_bootstrap_reference(self):
        config = ladder_config(gridless_allocation_mode="survivor")
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        level = min(funded_indices(plan))
        position = {
            "0": {
                "ladder_id": plan.id,
                "ladder_level_index": level,
                "ladder_principal_wei": plan.amount_for_level(level),
                "ladder_entry_kind": "leading_edge",
                "ladder_fill_price": 0.99,
                "ladder_bootstrap_reference": True,
            }
        }
        self.assertTrue(reconcile_confirmed_positions(plan, position))
        self.assertEqual(plan.reference_price, 0.99)
        validate_context(plan, position, config)

    def test_static_v2_plan_cannot_be_reinterpreted_as_survivor(self):
        plan = build_plan(1.0, int(0.005 * WEI), ladder_config(), now=100)
        with self.assertRaisesRegex(LadderStateError, "does not match"):
            validate_context(
                plan,
                {},
                ladder_config(gridless_allocation_mode="survivor"),
            )

    def test_adopts_legacy_positions_and_funds_missing_coverage(self):
        config = ladder_config(max_active_positions=10, token_decimals=18)
        positions = {
            "0": {"cost_wei": int(0.001 * WEI), "balance": int(0.01 * WEI)},
            "1": {"cost_wei": int(0.001 * WEI), "balance": int(0.02 * WEI)},
        }
        plan, adopted = adopt_legacy_positions(
            positions, int(0.003 * WEI), config, current_price=0.04, now=100
        )
        self.assertAlmostEqual(plan.reference_price, 0.1)
        self.assertEqual(plan.funded_count, 5)
        self.assertEqual(sum(r.state == "open" for r in plan.rungs), 2)
        self.assertAlmostEqual(plan.rungs[-1].price, 0.005)
        self.assertEqual(len({p["ladder_level_index"] for p in adopted.values()}), 2)
        validate_context(plan, adopted, config)

    def test_adoption_maps_entries_monotonically_to_distinct_log_rungs(self):
        config = ladder_config(
            max_active_positions=50, token_decimals=18, gridless_ladder_spacing="log"
        )
        current = 0.03
        positions = {}
        for index, loss in enumerate((70, 65, 60, 55, 50)):
            entry = current / (1 - loss / 100)
            balance = int((0.001 / entry) * WEI)
            positions[str(index)] = {
                "cost_wei": int(0.001 * WEI),
                "balance": balance,
            }
        plan, adopted = adopt_legacy_positions(
            positions, int(0.010 * WEI), config, current_price=current, now=100
        )
        indices = [
            adopted[position_id]["ladder_level_index"]
            for position_id in sorted(adopted, key=int)
        ]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(len(set(indices)), 5)
        self.assertEqual(plan.funded_count, 15)
        self.assertAlmostEqual(plan.rungs[-1].price, plan.reference_price * 0.05)
        newly_ready = [
            rung
            for rung in plan.rungs
            if rung.state == "ready" and not rung.adopted_legacy_position
        ]
        self.assertTrue(newly_ready)
        self.assertTrue(all(rung.price < current for rung in newly_ready))
        validate_context(plan, adopted, config)

    def test_interrupted_adoption_recovers_only_exact_position(self):
        config = ladder_config(max_active_positions=5, token_decimals=18)
        positions = {"0": {"cost_wei": int(0.001 * WEI), "balance": int(0.01 * WEI)}}
        plan, adopted = adopt_legacy_positions(
            positions, 0, config, current_price=0.05, now=100
        )
        self.assertTrue(reconcile_adoption_provenance(plan, positions))
        self.assertEqual(positions, adopted)
        validate_context(plan, positions, config)

    def test_adoption_rejects_existing_or_partial_provenance(self):
        config = ladder_config(max_active_positions=5, token_decimals=18)
        position = {
            "0": {
                "cost_wei": int(0.001 * WEI),
                "balance": int(0.01 * WEI),
                "ladder_id": "foreign",
            }
        }
        with self.assertRaises(LadderStateError):
            adopt_legacy_positions(position, 0, config, current_price=0.05, now=100)

    def test_initial_capital_funds_minimum_sized_even_coverage(self):
        plan = build_plan(
            1.0,
            int(0.005 * WEI),
            ladder_config(max_active_positions=20),
            now=100,
        )
        self.assertEqual(plan.funded_count, 5)
        self.assertEqual(funded_indices(plan), [3, 7, 11, 15, 19])
        self.assertEqual(plan.allocated_wei, int(0.005 * WEI))
        self.assertAlmostEqual(plan.rungs[19].price, 0.05)

    def test_budget_too_small_does_not_arm(self):
        self.assertIsNone(build_plan(1.0, int(0.0009 * WEI), ladder_config()))

    def test_deposit_adds_positions_without_moving_existing_triggers(self):
        config = ladder_config(max_active_positions=20)
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        old_prices = {i: plan.rungs[i].price for i in funded_indices(plan)}
        self.assertTrue(refresh_plan_funding(plan, int(0.015 * WEI), config, now=200))
        self.assertEqual(plan.funded_count, 15)
        for index, price in old_prices.items():
            self.assertEqual(plan.rungs[index].price, price)

    def test_five_open_plus_ten_new_positions_becomes_fifteen(self):
        config = ladder_config(max_active_positions=20)
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        positions = {}
        for sequence, index in enumerate(funded_indices(plan)):
            principal = plan.amount_for_level(index)
            position_id = str(sequence)
            record_fill(plan, index, principal, position_id, filled_at=110)
            positions[position_id] = {
                "ladder_id": plan.id,
                "ladder_level_index": index,
                "ladder_principal_wei": principal,
            }
        # Deployed 0.005 plus a new 0.010 wallet deposit is 0.015 strategy capital.
        self.assertTrue(refresh_plan_funding(plan, int(0.010 * WEI), config, now=200))
        self.assertEqual(plan.funded_count, 15)
        self.assertEqual(plan.allocated_wei, int(0.015 * WEI))
        validate_context(plan, positions, config)

    def test_after_maximum_coverage_surplus_grows_position_targets(self):
        config = ladder_config(max_active_positions=5)
        plan = build_plan(1.0, int(0.005 * WEI), config, now=100)
        self.assertEqual(plan.funded_count, 5)
        refresh_plan_funding(plan, int(0.010 * WEI), config, now=200)
        self.assertEqual(plan.funded_count, 5)
        self.assertEqual({r.principal_wei for r in plan.rungs}, {int(0.002 * WEI)})

    def test_tradeable_fraction_and_hard_cap_remain_authoritative(self):
        config = ladder_config(
            tradeable_balance_percent=50,
            max_active_positions=5,
            gridless_ladder_max_budget_eth=0.004,
        )
        plan = build_plan(1.0, int(0.1 * WEI), config, now=100)
        self.assertEqual(plan.allocated_wei, int(0.004 * WEI))
        self.assertEqual(plan.funded_count, 4)

    def test_rung_recycles_only_after_exit_and_reset_above_trigger(self):
        plan = build_plan(
            1.0, int(0.001 * WEI), ladder_config(max_active_positions=1), now=100
        )
        principal = plan.amount_for_level(0)
        record_fill(plan, 0, principal, "7", filled_at=110)
        mark_exit(plan, 0, "7", exited_at=120, realized_profit_wei=10)
        self.assertEqual(plan.rungs[0].state, "waiting_reset")
        self.assertFalse(advance_rearms(plan, plan.rungs[0].price, 0, now=121))
        self.assertTrue(advance_rearms(plan, plan.rungs[0].price * 1.05, 0, now=122))
        self.assertEqual(plan.rungs[0].state, "ready")
        self.assertEqual(eligible_level(plan, plan.rungs[0].price), 0)
        record_fill(plan, 0, principal, "8", filled_at=130)
        self.assertEqual(plan.rungs[0].fill_count, 2)
        self.assertEqual(plan.rungs[0].exit_count, 1)

    def test_ready_rung_triggers_anywhere_at_or_below_its_price(self):
        plan = build_plan(
            1.0, int(0.003 * WEI), ladder_config(max_active_positions=3), now=100
        )
        first_ready = min(funded_indices(plan))
        self.assertEqual(
            eligible_level(plan, plan.rungs[first_ready].price * 0.80),
            first_ready,
        )

    def test_stoploss_below_rung_cannot_immediately_rebuy(self):
        plan = build_plan(
            1.0, int(0.001 * WEI), ladder_config(max_active_positions=1), now=100
        )
        record_fill(plan, 0, plan.amount_for_level(0), "1", filled_at=110)
        mark_exit(plan, 0, "1", exited_at=120, realized_profit_wei=-100)
        self.assertFalse(advance_rearms(plan, plan.rungs[0].price * 0.5, 0, now=130))
        self.assertIsNone(eligible_level(plan, plan.rungs[0].price * 0.5))

    def test_historical_rung_funded_below_market_rearms_after_cooldown(self):
        config = ladder_config(max_active_positions=2)
        plan = build_plan(1.0, int(0.001 * WEI), config, now=100)
        self.assertTrue(
            refresh_plan_funding(
                plan,
                int(0.002 * WEI),
                config,
                now=200,
                current_price=0.01,
            )
        )
        dormant = next(rung for rung in plan.rungs if rung.state == "waiting_reset")
        self.assertFalse(advance_rearms(plan, dormant.price * 1.01, 60, now=250))
        self.assertTrue(advance_rearms(plan, dormant.price * 1.01, 60, now=260))
        self.assertEqual(dormant.state, "ready")

    def test_reconcile_buy_checkpoint_crash(self):
        plan = build_plan(
            1.0, int(0.001 * WEI), ladder_config(max_active_positions=1), now=100
        )
        position = {
            "0": {
                "ladder_id": plan.id,
                "ladder_level_index": 0,
                "ladder_principal_wei": plan.amount_for_level(0),
            }
        }
        self.assertTrue(reconcile_confirmed_positions(plan, position))
        validate_context(plan, position, ladder_config(max_active_positions=1))
        self.assertEqual(plan.rungs[0].state, "open")

    def test_missing_open_position_fails_closed(self):
        plan = build_plan(
            1.0, int(0.001 * WEI), ladder_config(max_active_positions=1), now=100
        )
        record_fill(plan, 0, plan.amount_for_level(0), "0")
        with self.assertRaises(LadderStateError):
            validate_context(plan, {}, ladder_config(max_active_positions=1))

    def test_persistence_and_v1_state_fail_closed(self):
        plan = build_plan(1.0, int(0.003 * WEI), ladder_config(), now=100)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ladder.json")
            save_plan(plan, path)
            self.assertEqual(load_plan(path).to_dict(), plan.to_dict())
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1}, handle)
            with self.assertRaises(LadderStateError):
                load_plan(path)

    def test_status_reports_density_cycles_and_size(self):
        plan = build_plan(
            1.0, int(0.002 * WEI), ladder_config(max_active_positions=2), now=100
        )
        record_fill(plan, 0, plan.amount_for_level(0), "0")
        mark_exit(plan, 0, "0", exited_at=120, realized_profit_wei=100)
        payload = status_payload(plan)
        self.assertEqual(payload["levels_funded"], 2)
        self.assertEqual(payload["completed_cycles"], 1)
        self.assertEqual(payload["realized_profit_eth"], 100 / WEI)


class AdaptiveLadderBotTests(unittest.TestCase):
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

    def test_prepare_preserves_reference_and_grows_density_after_deposit(self):
        first = self.bot._prepare_gridless_ladder(1.0, {}, int(0.005 * WEI), now=100)
        second = self.bot._prepare_gridless_ladder(0.5, {}, int(0.015 * WEI), now=200)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.reference_price, 1.0)
        self.assertEqual(second.funded_count, 15)

    def test_indefinite_plan_does_not_expire(self):
        plan = self.bot._prepare_gridless_ladder(1.0, {}, int(0.005 * WEI), now=100)
        same = self.bot._prepare_gridless_ladder(0.5, {}, int(0.005 * WEI), now=10**9)
        self.assertEqual(same.id, plan.id)
        self.assertEqual(same.status, "active")

    def test_open_positions_without_plan_are_adopted(self):
        self.bot.config.token_decimals = 18
        positions = {"0": {"cost_wei": int(0.001 * WEI), "balance": int(0.01 * WEI)}}
        plan = self.bot._prepare_gridless_ladder(
            0.05, positions, int(0.01 * WEI), now=100
        )
        self.assertAlmostEqual(plan.reference_price, 0.1)
        self.assertEqual(plan.funded_count, 11)
        self.assertEqual(positions["0"]["ladder_id"], plan.id)
        validate_context(plan, positions, self.bot.config)

    def test_execution_price_guard_blocks_recovered_route(self):
        context = {"trigger_price": 0.5, "reference_price": 1.0}
        quote = SimpleNamespace(buy_amount=WEI)
        self.bot.token_unit = WEI
        self.assertFalse(self.bot._ladder_execution_price_allowed(quote, WEI, context))

    def test_polling_attempts_highest_crossed_ready_rung(self):
        self.bot.config.use_eth_trading = True
        self.bot.config.eth_gas_reserve = 0.001
        self.bot.config.weth_address = "0x0000000000000000000000000000000000000002"
        self.bot.wallet = MagicMock()
        self.bot.wallet.get_eth_balance.return_value = 0.006
        self.bot._taxed_token_active = MagicMock(return_value=False)
        self.bot.last_taxed_token_failure_time = 0
        self.bot.last_buy_time = 0
        self.bot.gridless_buy_cooldown = 0
        self.bot._execute_buy_gridless = MagicMock()
        self.bot._funding_warning = None
        with patch("gridless.load_positions", return_value={}):
            self.bot._check_buys_gridless(1.0)
            plan = load_plan()
            first_index = min(funded_indices(plan))
            self.bot._check_buys_gridless(plan.rungs[first_index].price)
        self.bot._execute_buy_gridless.assert_called_once()
        call = self.bot._execute_buy_gridless.call_args
        self.assertEqual(call.kwargs["ladder_context"]["level_index"], first_index)

    def test_fresh_survivor_immediately_attempts_off_ladder_bootstrap_buy(self):
        self.bot.config = ladder_config(
            gridless_allocation_mode="survivor", max_active_positions=50,
        )
        self.bot.config.use_eth_trading = True
        self.bot.config.eth_gas_reserve = 0.001
        self.bot.config.weth_address = "0x0000000000000000000000000000000000000002"
        self.bot.wallet = MagicMock()
        self.bot.wallet.get_eth_balance.return_value = 0.018
        self.bot._taxed_token_active = MagicMock(return_value=False)
        self.bot.last_taxed_token_failure_time = 0
        self.bot.last_buy_time = 0
        self.bot.gridless_buy_cooldown = 0
        self.bot._execute_buy_gridless = MagicMock()
        self.bot._funding_warning = None
        with patch("gridless.load_positions", return_value={}):
            self.bot._check_buys_gridless(0.0000002751)
        self.bot._execute_buy_gridless.assert_called_once()
        context = self.bot._execute_buy_gridless.call_args.kwargs["ladder_context"]
        self.assertEqual(context["entry_kind"], "leading_edge")
        self.assertTrue(context["bootstrap_reference"])
        self.assertEqual(load_plan().reference_price, 0.0000002751)

    def test_survivor_half_sell_trigger_attempts_off_ladder_leading_edge(self):
        self.bot.config = ladder_config(
            gridless_allocation_mode="survivor",
            max_active_positions=5,
        )
        self.bot.config.use_eth_trading = True
        self.bot.config.eth_gas_reserve = 0.001
        self.bot.config.weth_address = "0x0000000000000000000000000000000000000002"
        self.bot.wallet = MagicMock()
        self.bot.wallet.get_eth_balance.return_value = 0.006
        self.bot._taxed_token_active = MagicMock(return_value=False)
        self.bot.last_taxed_token_failure_time = 0
        self.bot.last_buy_time = 0
        self.bot.gridless_buy_cooldown = 0
        self.bot._execute_buy_gridless = MagicMock()
        self.bot._funding_warning = None
        positions = {
            "0": {
                "cost_wei": int(0.001 * WEI),
                "balance": int(0.001 * WEI),
            }
        }
        with patch("gridless.load_positions", return_value=positions):
            plan, adopted = adopt_legacy_positions(
                positions, int(0.005 * WEI), self.bot.config,
                current_price=1.0, now=100,
            )
            save_plan(plan)
            gridless.save_positions(adopted)
            self.bot._check_buys_gridless(
                1.025, {"0": {"buy_trigger_pnls": {"buy": 2.5}}}
            )
        call = self.bot._execute_buy_gridless.call_args
        self.assertEqual(call.kwargs["ladder_context"]["entry_kind"], "leading_edge")
        self.assertAlmostEqual(call.kwargs["ladder_context"]["trigger_price"], 1.025)
        self.assertEqual(load_plan().reference_price, 1.0)

    def test_survivor_market_high_without_half_sell_trigger_does_not_buy_or_reanchor(self):
        self.bot.config = ladder_config(
            gridless_allocation_mode="survivor", max_active_positions=5,
        )
        self.bot.config.use_eth_trading = True
        self.bot.config.eth_gas_reserve = 0.001
        self.bot.config.weth_address = "0x0000000000000000000000000000000000000002"
        self.bot.wallet = MagicMock()
        self.bot.wallet.get_eth_balance.return_value = 0.006
        self.bot._taxed_token_active = MagicMock(return_value=False)
        self.bot.last_taxed_token_failure_time = 0
        self.bot.last_buy_time = 0
        self.bot.gridless_buy_cooldown = 0
        self.bot._execute_buy_gridless = MagicMock()
        self.bot._funding_warning = None
        positions = {"0": {"cost_wei": 10**15, "balance": 10**15}}
        plan, adopted = adopt_legacy_positions(
            positions, int(0.005 * WEI), self.bot.config,
            current_price=1.0, now=100,
        )
        save_plan(plan)
        gridless.save_positions(adopted)
        with patch("gridless.load_positions", return_value=adopted):
            self.bot._check_buys_gridless(
                2.0, {"0": {"buy_trigger_pnls": {"buy": 2.49}}}
            )
        self.bot._execute_buy_gridless.assert_not_called()
        self.assertEqual(load_plan().reference_price, 1.0)


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

    def test_leading_edge_fill_price_survives_round_trip(self):
        position_id = gridless.add_position(
            123, 456, ladder_id="ladder-1", ladder_level_index=7,
            ladder_principal_wei=100, ladder_entry_kind="leading_edge",
            ladder_fill_price=1.234, ladder_bootstrap_reference=True,
        )
        position = gridless.load_positions()[position_id]
        self.assertEqual(position["ladder_entry_kind"], "leading_edge")
        self.assertAlmostEqual(position["ladder_fill_price"], 1.234)
        self.assertTrue(position["ladder_bootstrap_reference"])

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
        observations = {"0": {"buy_pnl": -20, "sell_pnl": 6}}
        focus = gridless.trigger_focus_candidates(positions, config, observations)
        self.assertEqual(focus["buy"], {})
        self.assertTrue(focus["sell"]["sell"]["triggered"])


if __name__ == "__main__":
    unittest.main()
