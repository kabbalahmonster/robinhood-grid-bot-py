import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import drawdown_ladder
import gridless
from grid_bot import GridBot, _allocate_integer_pro_rata


def bare_check(bot, price, observations):
    return inspect.unwrap(GridBot._check_sells_gridless)(bot, price, observations)


class SurvivorBatchSellTests(unittest.TestCase):
    def make_bot(self, *, enabled=True, mode="survivor", moonbag=0):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            survivor_batch_sell_enabled=enabled,
            gridless_allocation_mode=mode,
            gridless_sell_threshold=5.0,
            pnl_trigger_by_min_profit=False,
            gridless_stoploss_enabled=True,
            gridless_stoploss_threshold=-25.0,
            moonbag_percentage=moonbag,
            bidirectional_pnl_enabled=True,
            min_profit_percent=1.0,
            max_swap_gas_eth=0,
            max_sell_gas_eth=0,
            gridless_ladder_rearm_policy="after_exit",
            token_address="0xtoken",
            zero_x_proxy="0xspender",
        )
        bot.token_decimals = 0
        bot.token_unit = 1
        bot._wallet_can_cover_sell = Mock(return_value=True)
        bot.trade_token_address = "0xtrade"
        bot._actionable_quote_with_weth_fallback = Mock(return_value=(
            SimpleNamespace(success=True, error=None), False
        ))
        bot._observe_token_tax_failure = Mock()
        bot._recover_survivor_batch_settlement = Mock(return_value=True)
        bot._save_batch_settlement_journal = Mock()
        bot._clear_batch_settlement_journal = Mock()
        bot._clear_exact_approval_guard = Mock()
        bot._execute_sell_gridless = Mock()
        bot._sell_priority_this_cycle = False
        return bot

    def test_exact_pro_rata_allocation_conserves_remainders(self):
        allocated = _allocate_integer_pro_rata(11, [("2", 1), ("1", 1), ("3", 1)])
        self.assertEqual(allocated, {"2": 4, "1": 4, "3": 3})
        self.assertEqual(sum(allocated.values()), 11)

    @patch("gridless.load_positions")
    def test_batches_only_fresh_profitable_non_stoploss_lots_and_keeps_moonbags(
            self, load_positions):
        positions = {
            "1": {"balance": 101, "cost_wei": 1000},
            "2": {"balance": 202, "cost_wei": 2000},
            "3": {"balance": 303, "cost_wei": 3000},
            "4": {"balance": 404, "cost_wei": 4000},
        }
        load_positions.return_value = positions
        bot = self.make_bot(moonbag=10)
        observations = {
            "1": {"sell_trigger_pnl": 6},
            "2": {"sell_trigger_pnl": 7},
            "3": {"sell_trigger_pnl": 4},       # below threshold
            "4": {"sell_trigger_pnls": {"a": 8, "b": -30}},  # stop loss lane
        }

        bare_check(bot, 1.0, observations)

        bot._execute_sell_gridless.assert_called_once()
        args, kwargs = bot._execute_sell_gridless.call_args
        self.assertEqual(args[0], "survivor-batch")
        lots = kwargs["batch_lots"]
        self.assertEqual([lot["position_id"] for lot in lots], ["1", "2"])
        self.assertEqual([lot["sell_amount"] for lot in lots], [91, 182])
        self.assertEqual([lot["moonbag_tokens"] for lot in lots], [10, 20])
        self.assertEqual(args[1]["balance"], 273)
        quote_kwargs = bot._actionable_quote_with_weth_fallback.call_args.kwargs
        self.assertEqual(quote_kwargs["sell_amount"], 273)
        self.assertEqual(
            quote_kwargs["sold_cost_wei"],
            sum(lot["sold_cost_wei"] for lot in lots),
        )

    @patch("gridless.load_positions")
    def test_disabled_and_non_survivor_never_batch(self, load_positions):
        load_positions.return_value = {
            "1": {"balance": 100, "cost_wei": 1000},
            "2": {"balance": 100, "cost_wei": 1000},
        }
        observations = {
            "1": {"sell_trigger_pnl": 6},
            "2": {"sell_trigger_pnl": 7},
        }
        for enabled, mode in ((False, "survivor"), (True, "threshold")):
            bot = self.make_bot(enabled=enabled, mode=mode)
            # Stop immediately at the ordinary single-lot wallet check.
            bot._wallet_can_cover_sell.return_value = False
            bare_check(bot, 1.0, observations)
            bot._execute_sell_gridless.assert_not_called()
            self.assertEqual(bot._wallet_can_cover_sell.call_count, 1)
            self.assertNotEqual(
                bot._wallet_can_cover_sell.call_args.args[1], "survivor-batch"
            )

    @patch("gridless.load_positions")
    def test_one_fresh_candidate_falls_back_to_single_sell(self, load_positions):
        load_positions.return_value = {
            "1": {"balance": 100, "cost_wei": 1000},
            "2": {"balance": 100, "cost_wei": 1000},
        }
        bot = self.make_bot()
        bot._pnl_quotes = {}
        bot._wallet_can_cover_sell.return_value = False
        bare_check(bot, 1.0, {
            "1": {"sell_trigger_pnl": 6},
            "2": {},  # stale/missing authoritative mark
        })
        self.assertEqual(bot._wallet_can_cover_sell.call_args.args[1], "1")
        bot._execute_sell_gridless.assert_not_called()

    @patch("drawdown_ladder.save_plan")
    @patch("drawdown_ladder.mark_exit")
    @patch("drawdown_ladder.load_plan")
    @patch("gridless.save_positions")
    @patch("gridless.load_positions")
    def test_settlement_is_one_trade_with_per_lot_profit_fee_and_atomic_close(
            self, load_positions, save_positions, load_plan, mark_exit, save_plan):
        lots = [
            {"position_id": "1", "position": {"ladder_id": "L", "ladder_level_index": 0},
             "sell_amount": 1, "sold_cost_wei": 3, "moonbag_tokens": 1},
            {"position_id": "2", "position": {"ladder_id": "L", "ladder_level_index": 1},
             "sell_amount": 2, "sold_cost_wei": 10, "moonbag_tokens": 2},
        ]
        load_positions.return_value = {"1": {}, "2": {}, "keep": {}}
        load_plan.return_value = SimpleNamespace(id="L")
        bot = self.make_bot()
        bot.config.gridless_ladder_rearm_policy = "after_exit"
        bot.config.bank_percentage = 50
        bot.trade_token_name = "ETH"
        bot.session_sells = 0
        bot.session_profit_weth = 0
        bot.profit_tracker = Mock()
        bot._receipt_gas_cost_wei = Mock(return_value=2)
        events = []
        bot._charge_profit_fee = Mock(side_effect=lambda *a, **k: events.append("fee"))
        save_positions.side_effect = lambda *_: events.append("positions")
        save_plan.side_effect = lambda *_: events.append("ladder")
        bot._clear_exact_approval_guard = Mock()
        bot._record_dashboard_trade = Mock()
        bot.bank_profit = Mock()
        bot._arm_survivor_rapid_polling = Mock()
        bot.last_buy_time = 123
        bot._safety_halted = False
        result = SimpleNamespace(tx_hash="0xbatch")

        # Proceeds split 5/10 and gas 1/1 => mixed lot profits +1/-1.
        bot._settle_confirmed_survivor_batch(lots, 2.0, result, 15)

        self.assertEqual(events, ["fee", "positions", "ladder"])
        self.assertEqual(save_positions.call_args.args[0], {"keep": {}})
        self.assertEqual([call.kwargs["realized_profit_wei"] for call in mark_exit.call_args_list], [1, -1])
        bot._charge_profit_fee.assert_called_once_with(
            1, "0xbatch", per_lot_profits=unittest.mock.ANY
        )
        bot.profit_tracker.record_sale.assert_called_once_with(0, "0xbatch")
        self.assertEqual(bot.session_sells, 1)
        self.assertEqual(bot._record_dashboard_trade.call_count, 1)
        bot.bank_profit.assert_not_called()

    @patch("drawdown_ladder.save_plan")
    @patch("drawdown_ladder.mark_exit")
    @patch("drawdown_ladder.load_plan")
    @patch("gridless.save_positions")
    @patch("gridless.load_positions")
    def test_restart_recovers_partial_positions_and_ladder_checkpoint(
            self, load_positions, save_positions, load_plan, mark_exit, save_plan):
        bot = self.make_bot()
        bot.trade_token_name = "ETH"
        bot.session_sells = 0
        bot.session_profit_weth = 0
        bot.profit_tracker = Mock()
        bot._charge_profit_fee = Mock()
        bot._fee_audit_contains_sale = Mock(return_value=False)
        bot._record_dashboard_trade = Mock()
        bot.dashboard_trades = []
        bot._safety_halted = False
        # Position 1 was already removed and rung 0 already checkpointed;
        # position/rung 2 remain from a crash between the two files.
        load_positions.return_value = {"2": {}, "keep": {}}
        rungs = [
            SimpleNamespace(state="waiting_reset", position_id=None),
            SimpleNamespace(state="open", position_id="2"),
        ]
        load_plan.return_value = SimpleNamespace(id="L", rungs=rungs)
        def exit_rung(plan, index, **kwargs):
            plan.rungs[index].state = "waiting_reset"
            plan.rungs[index].position_id = None
        mark_exit.side_effect = exit_rung
        journal = {
            "schema_version": 1, "tx_hash": "0xconfirmed",
            "received_wei": 20, "gas_wei": 2, "sell_amount": 3,
            "price": 2.0, "fee_checkpointed": True,
            "positions_checkpointed": False, "ladder_checkpointed": False,
            "lots": [
                {"position_id": "1", "profit_wei": 3, "ladder_id": "L", "ladder_level_index": 0},
                {"position_id": "2", "profit_wei": -1, "ladder_id": "L", "ladder_level_index": 1},
            ],
        }
        bot._load_batch_settlement_journal = Mock(return_value=journal)

        self.assertTrue(GridBot._recover_survivor_batch_settlement(bot))

        save_positions.assert_called_once_with({"keep": {}})
        mark_exit.assert_called_once()
        self.assertEqual(mark_exit.call_args.args[1], 1)
        save_plan.assert_called_once_with(load_plan.return_value)
        bot.profit_tracker.record_sale.assert_called_once_with(2, "0xconfirmed")
        bot._charge_profit_fee.assert_not_called()  # fee stage was durable
        bot._record_dashboard_trade.assert_called_once()
        bot._clear_batch_settlement_journal.assert_called_once()
        self.assertEqual(bot.session_sells, 1)
        self.assertEqual(bot.session_profit_weth, 2 / 10**18)
        self.assertFalse(bot._safety_halted)

    @patch("gridless.save_positions")
    @patch("gridless.load_positions")
    def test_confirmed_setup_gas_is_deferred_once_for_a_later_retry(
            self, load_positions, save_positions):
        bot = self.make_bot()
        positions = {
            "1": {"balance": 100, "cost_wei": 100},
            "2": {"balance": 200, "cost_wei": 200},
        }
        load_positions.return_value = positions
        lots = [
            {"position_id": "1", "sell_amount": 100, "sold_cost_wei": 100},
            {"position_id": "2", "sell_amount": 200, "sold_cost_wei": 200},
        ]

        bot._defer_batch_sell_gas_cost(lots, 3)

        saved = save_positions.call_args.args[0]
        self.assertEqual(saved["1"]["deferred_sell_gas_wei"], 1)
        self.assertEqual(saved["2"]["deferred_sell_gas_wei"], 2)
        # Current-attempt snapshots stay unchanged (the executor subtracts its
        # local setup gas); only a later retry incorporates the durable gas.
        self.assertEqual([lot["sold_cost_wei"] for lot in lots], [100, 200])
        self.assertEqual(bot._gridless_sell_terms(saved["1"])[1], 101)
        self.assertEqual(bot._gridless_sell_terms(saved["2"])[1], 202)

    def test_per_lot_fee_rounding_charges_only_positive_lots(self):
        bot = self.make_bot()
        bot.config.profit_fee_percent = 50
        bot.config.profit_fee_wallet = "0xfee"
        bot.config.min_profit_fee_transfer_eth = 999
        bot.wallet = Mock()
        bot.wallet.address = "0xwallet"
        bot.trade_token_name = "ETH"
        bot._load_profit_fee_accrual = Mock(return_value={"pending_wei": 0, "sale_tx_hashes": []})
        bot._save_profit_fee_accrual = Mock()
        bot._record_profit_fee = Mock()
        entry = bot._charge_profit_fee(
            4, "0xbatch", per_lot_profits=[3, -5, 1]
        )
        # floor(3*50%) + floor(1*50%) = 1, not floor(4*50%) = 2.
        self.assertEqual(entry["sale_fee_wei"], 1)

    def test_aggregate_minimum_profit_rejection_never_broadcasts(self):
        bot = self.make_bot()
        # Exercise the real executor up to its aggregate economic gate.
        bot.trade_token_name = "ETH"
        bot.api_client = Mock()
        bot.api_client.name = "mock"
        bot.provider = Mock()
        bot.provider.capabilities.api_managed_approval = False
        bot._queue_route_shadow = Mock()
        bot._provider_requires_exact_approval = Mock(return_value=False)
        bot._taxed_quote_return_wei = Mock(return_value=999)
        bot._minimum_gas_aware_return_wei = Mock(return_value=1000)
        bot._project_future_weth_unwrap_gas_wei = Mock(return_value=0)
        bot.wallet = Mock()
        bot.wallet.check_allowance.return_value = 10_000
        quote = SimpleNamespace(
            success=True, sell_amount=300, weth_fallback=False,
            allowance_target="0xspender", error=None,
        )
        lots = [
            {"position_id": "1", "position": {}, "sell_amount": 100,
             "sold_cost_wei": 400, "moonbag_tokens": 0},
            {"position_id": "2", "position": {}, "sell_amount": 200,
             "sold_cost_wei": 500, "moonbag_tokens": 0},
        ]
        aggregate = {"balance": 300, "cost_wei": 900}

        inspect.unwrap(GridBot._execute_sell_gridless)(
            bot, "survivor-batch", aggregate, 4.0,
            pre_fetched_quote=quote, batch_lots=lots,
        )

        bot._minimum_gas_aware_return_wei.assert_called_once()
        self.assertEqual(
            bot._minimum_gas_aware_return_wei.call_args.args[0], 900
        )
        bot.wallet._send_transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
