import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from grid_bot import GridBot
from wallet import TransactionResult


class GasAwareProfitTests(unittest.TestCase):
    def make_bot(self):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            gas_limit_multiplier=1.0,
            gas_price_multiplier=1.0,
            gas_price_freshness_multiplier=1.0,
        )
        bot.wallet = Mock()
        bot.wallet.w3.eth.gas_price = 400_000_000
        bot.wallet.normal_gas_price.return_value = 400_000_000
        bot.wallet.w3.eth.estimate_gas.side_effect = RuntimeError("simulation unavailable")
        bot.provider = SimpleNamespace(name="sushiswap")
        bot._buy_attempt = None
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

    def test_stale_base_fee_rebuild_requires_definitive_prebroadcast_rejection(self):
        bot = self.make_bot()
        bot.wallet.address = "0x0000000000000000000000000000000000000001"
        bot.wallet.is_base_fee_too_low_error.return_value = True
        bot.wallet.base_fee_from_error.return_value = 121_962_000
        bot.wallet.normal_gas_price.return_value = 125_000_000
        bot.wallet.w3.eth.get_transaction_count.return_value = 44
        tx = {"gas": 404_549, "gasPrice": 121_022_240, "nonce": 43}
        rejected = TransactionResult(
            success=False,
            tx_hash="0xdeterministic-but-not-broadcast",
            error=(
                "RPC definitively rejected signed transaction before broadcast: "
                "max fee per gas less than block base fee: baseFee: 121962000"
            ),
        )

        rebuilt = bot._rebuild_after_stale_base_fee_rejection(tx, rejected)

        self.assertEqual(rebuilt["gasPrice"], 125_000_000)
        self.assertEqual(rebuilt["nonce"], 44)
        self.assertEqual(tx["gasPrice"], 121_022_240)
        bot.wallet.normal_gas_price.assert_called_once_with(124_401_240)
        bot.wallet.w3.eth.get_transaction_count.assert_called_once_with(
            bot.wallet.address, "pending",
        )

    def test_stale_base_fee_rebuild_refuses_ambiguous_outcome(self):
        bot = self.make_bot()
        bot.wallet.is_base_fee_too_low_error.return_value = True
        ambiguous = TransactionResult(
            success=False,
            outcome_unknown=True,
            error="max fee per gas less than block base fee: baseFee: 121962000",
        )

        self.assertIsNone(
            bot._rebuild_after_stale_base_fee_rejection({"gasPrice": 1}, ambiguous)
        )
        bot.wallet.normal_gas_price.assert_not_called()

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
        # The wallet oracle owns configured price headroom, so return its
        # already-normalized 5% result rather than applying it a second time.
        bot.wallet.normal_gas_price.return_value = 420_000_000
        quote = SimpleNamespace(gas=200_000, gas_price=400_000_000)

        self.assertEqual(bot._projected_gas_cost_wei(quote), 88_200_000_000_000)

    def test_normal_rpc_price_overrides_provider_fast_price(self):
        bot = self.make_bot()
        quote = SimpleNamespace(gas=200_000, gas_price=2_000_000_000)

        gas_limit, gas_price = bot._swap_gas_fields(quote)

        self.assertEqual(gas_limit, 200_000)
        self.assertEqual(gas_price, 400_000_000)

    def test_normal_price_uses_wallet_base_fee_aware_oracle(self):
        bot = self.make_bot()
        bot.wallet.normal_gas_price.return_value = 418_000_000
        quote = SimpleNamespace(gas=200_000, gas_price=2_000_000_000)

        _, gas_price = bot._swap_gas_fields(quote)

        self.assertEqual(gas_price, 418_000_000)
        bot.wallet.normal_gas_price.assert_called_once_with()

    def test_executable_quote_uses_rpc_simulation_instead_of_provider_gas(self):
        bot = self.make_bot()
        bot.wallet.address = "0x0000000000000000000000000000000000000001"
        bot.wallet.w3.eth.estimate_gas.side_effect = None
        bot.wallet.w3.eth.estimate_gas.return_value = 120_000
        quote = SimpleNamespace(
            gas=600_000,
            gas_price=2_000_000_000,
            to="0x0000000000000000000000000000000000000002",
            data="0x1234",
            value=0,
        )

        gas_limit, gas_price = bot._swap_gas_fields(quote)

        self.assertEqual(gas_limit, 120_000)
        self.assertEqual(gas_price, 400_000_000)
        bot.wallet.w3.eth.estimate_gas.assert_called_once_with({
            "from": "0x0000000000000000000000000000000000000001",
            "to": "0x0000000000000000000000000000000000000002",
            "data": "0x1234",
            "value": 0,
        })

    def test_required_dynamic_gas_fails_closed_without_provider_fallback(self):
        bot = self.make_bot()
        bot.provider = SimpleNamespace(name="umbra")
        bot.wallet.address = "0x0000000000000000000000000000000000000001"
        quote = SimpleNamespace(
            gas=300_000, gas_price=0,
            to="0x0000000000000000000000000000000000000002",
            data="0x1234", value=0,
        )

        with self.assertRaisesRegex(RuntimeError, "umbra final gas simulation failed"):
            bot._swap_gas_fields(quote, require_simulation=True)

    def test_local_dynamic_gas_estimate_is_reused_for_same_calldata(self):
        bot = self.make_bot()
        bot.wallet.address = "0x0000000000000000000000000000000000000001"
        bot.wallet.w3.eth.estimate_gas.side_effect = None
        bot.wallet.w3.eth.estimate_gas.return_value = 123_456
        quote = SimpleNamespace(
            gas=300_000, gas_price=0,
            to="0x0000000000000000000000000000000000000002",
            data="0x1234", value=0,
        )

        first = bot._swap_gas_fields(quote, require_simulation=True)
        second = bot._swap_gas_fields(quote, require_simulation=True)

        self.assertEqual(first, second)
        self.assertEqual(first[0], 123_456)
        bot.wallet.w3.eth.estimate_gas.assert_called_once()

        quote.data = "0x5678"
        bot.wallet.w3.eth.estimate_gas.return_value = 130_000
        refreshed = bot._swap_gas_fields(quote, require_simulation=True)
        self.assertEqual(refreshed[0], 130_000)
        self.assertEqual(bot.wallet.w3.eth.estimate_gas.call_count, 2)

    def test_exact_amount_provider_never_requests_unlimited_approval(self):
        bot = self.make_bot()
        bot.provider = SimpleNamespace(
            capabilities=SimpleNamespace(exact_amount_approval=True),
        )
        self.assertEqual(bot._provider_approval_amount(987_654), 987_654)

    def test_normal_provider_retains_unlimited_approval_behavior(self):
        bot = self.make_bot()
        bot.provider = SimpleNamespace(
            capabilities=SimpleNamespace(exact_amount_approval=False),
        )
        self.assertEqual(bot._provider_approval_amount(987_654), 2**256 - 1)

    def test_final_profit_guard_can_use_exact_broadcast_gas_plan(self):
        bot = self.make_bot()
        quote = SimpleNamespace(gas=200_000, gas_price=400_000_000)

        required = bot._minimum_gas_aware_return_wei(
            sold_cost_wei=1_000_000_000_000_000,
            quote=quote,
            min_profit_percent=2.0,
            projected_gas_cost_wei=90_000_000_000_000,
        )

        self.assertEqual(required, 1_110_000_000_000_000)

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
        self.assertEqual(bot._buy_attempt["status"], "projected_gas_above_cap")
        self.assertEqual(bot._buy_attempt["quote_provider"], "sushiswap")
        self.assertEqual(bot._buy_attempt["projected_gas_eth"], 0.00008)
        self.assertEqual(bot._buy_attempt["maximum_gas_eth"], 0.00004)

    def test_buy_gas_block_includes_attempt_context(self):
        bot = self.make_bot()
        bot.config.max_buy_gas_eth = 0.00004

        self.assertFalse(bot._gas_within_hard_cap(
            200_000,
            400_000_000,
            "buy",
            {"position_id": "4", "buy_amount_eth": 0.003, "phase": "prepared_quote"},
        ))

        self.assertEqual(bot._buy_attempt["position_id"], "4")
        self.assertEqual(bot._buy_attempt["buy_amount_eth"], 0.003)
        self.assertEqual(bot._buy_attempt["phase"], "prepared_quote")

    def test_gridless_available_slots_can_be_recomputed_inside_execution(self):
        bot = self.make_bot()
        bot.config.max_active_positions = 4

        self.assertEqual(bot._available_gridless_slots({"0": {}, "1": {}}), 2)
        self.assertEqual(bot._available_gridless_slots({str(i): {} for i in range(5)}), 0)

    def test_operation_caps_override_legacy_cap_independently(self):
        bot = self.make_bot()
        bot.config.max_swap_gas_eth = 0.00008
        bot.config.max_buy_gas_eth = 0.00008
        bot.config.max_sell_gas_eth = 0.00015

        self.assertFalse(bot._gas_within_hard_cap(300_000, 400_000_000, "buy"))
        self.assertTrue(bot._gas_within_hard_cap(300_000, 400_000_000, "sell"))

    def test_missing_operation_cap_inherits_legacy_cap(self):
        bot = self.make_bot()
        bot.config.max_swap_gas_eth = 0.00008

        self.assertFalse(bot._gas_within_hard_cap(300_000, 400_000_000, "sell"))

    def test_dashboard_trade_persists_confirmed_gas_fee(self):
        bot = self.make_bot()
        bot.dashboard_trades = []
        bot.wallet.address = "0xwallet"
        bot.config.token_symbol = "GME"
        bot.config.max_active_positions = 7
        with tempfile.TemporaryDirectory() as directory:
            bot.dashboard_trades_file = os.path.join(directory, "trades.json")
            bot._record_dashboard_trade(
                "sell", 0.002, 10, 0.0002, "0xtx",
                profit_eth=0.0001, gas_fee_eth=0.00001234,
            )
            with open(bot.dashboard_trades_file) as handle:
                trades = json.load(handle)

        self.assertEqual(trades[0]["gas_fee_eth"], 0.00001234)


if __name__ == "__main__":
    unittest.main()
