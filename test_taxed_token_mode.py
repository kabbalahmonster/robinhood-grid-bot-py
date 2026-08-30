import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import load_config
from grid_bot import GridBot


VALID_ENV = {
    "PRIVATE_KEY": "0x" + "1" * 64,
    "RPC_URL": "https://rpc.example.invalid",
    "CHAIN_ID": "4663",
    "TOKEN_ADDRESS": "0x" + "2" * 40,
    "UNISWAP_API_KEY": "test-key",
}


class SequenceWallet:
    def __init__(self, token_balances=(), eth_balances=()):
        self.token_balances = iter(token_balances)
        self.eth_balances = iter(eth_balances)

    def get_token_balance(self, _address):
        raw = next(self.token_balances)
        return raw / 10**18, raw

    def get_eth_balance_wei(self):
        return next(self.eth_balances)


class TaxedTokenModeTests(unittest.TestCase):
    def bot(self, *, taxed=True, fee=5.0, buffer=2.0, use_eth=True, wallet=None):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            taxed_token=taxed,
            token_transfer_fee_percent=fee,
            taxed_token_slippage_buffer_percent=buffer,
            slippage_tolerance=1.5,
            use_eth_trading=use_eth,
            token_address="token",
            weth_address="weth",
        )
        bot.wallet = wallet
        return bot

    def test_config_requires_declared_fee_when_mode_is_enabled(self):
        with patch.dict(os.environ, {**VALID_ENV, "TAXED_TOKEN": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "requires TOKEN_TRANSFER_FEE_PERCENT"):
                load_config()

    def test_config_loads_bounded_taxed_mode(self):
        env = {
            **VALID_ENV,
            "TAXED_TOKEN": "true",
            "TOKEN_TRANSFER_FEE_PERCENT": "5",
            "TAXED_TOKEN_SLIPPAGE_BUFFER_PERCENT": "2",
            "TAXED_TOKEN_FAILURE_COOLDOWN_SECONDS": "300",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertTrue(config.taxed_token)
        self.assertEqual(config.token_transfer_fee_percent, 5)
        self.assertEqual(config.taxed_token_slippage_buffer_percent, 2)

    def test_config_rejects_excessive_total_tolerance(self):
        env = {
            **VALID_ENV,
            "TAXED_TOKEN": "true",
            "TOKEN_TRANSFER_FEE_PERCENT": "12",
            "TAXED_TOKEN_SLIPPAGE_BUFFER_PERCENT": "4",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "must not exceed 15 percent"):
                load_config()

    def test_taxed_slippage_is_fee_plus_bounded_market_buffer(self):
        self.assertAlmostEqual(self.bot()._swap_slippage_fraction(), 0.07)
        self.assertAlmostEqual(self.bot(taxed=False)._swap_slippage_fraction(), 0.015)

    def test_auto_detected_fee_uses_same_bounded_accounting_path(self):
        bot = self.bot(taxed=False, buffer=2.0)
        bot.tax_detector = SimpleNamespace(detected_fee_percent=3.0)
        quote = SimpleNamespace(buy_amount=1_000)
        self.assertTrue(bot._taxed_token_active())
        self.assertAlmostEqual(bot._swap_slippage_fraction(), 0.05)
        self.assertEqual(bot._taxed_quote_return_wei(quote), 970)

    def test_buy_accounting_uses_actual_post_fee_wallet_delta(self):
        bot = self.bot(wallet=SequenceWallet(token_balances=[1_000, 1_950]))
        before = bot._raw_token_balance("token")
        self.assertEqual(bot._measured_token_received_raw(before), 950)

    def test_buy_accounting_is_measured_even_without_tax_mode(self):
        bot = self.bot(taxed=False, wallet=SequenceWallet(token_balances=[1_000, 1_940]))
        before = bot._raw_token_balance("token")
        self.assertEqual(bot._measured_token_received_raw(before, expected_raw=950), 940)

    def test_native_sell_accounting_adds_transaction_gas_back_to_balance_delta(self):
        bot = self.bot(wallet=SequenceWallet(eth_balances=[10_000, 10_700]))
        before = bot._raw_trade_balance()
        result = SimpleNamespace(
            receipt={"gasUsed": 100, "effectiveGasPrice": 3},
            gas_used=None,
            effective_gas_price=None,
        )
        self.assertEqual(bot._measured_trade_received_wei(before, result), 1_000)

    @patch("grid_bot.time.sleep", return_value=None)
    def test_stale_native_balance_reconciles_from_weth_unwrap_log(self, _sleep):
        bot = self.bot(wallet=SequenceWallet(eth_balances=[10_000] * 7))
        bot.wallet.address = "0x" + "a" * 40
        bot.config.weth_address = "0x" + "b" * 40
        before = bot._raw_trade_balance()
        result = SimpleNamespace(receipt={
            "gasUsed": 100,
            "effectiveGasPrice": 3,
            "logs": [{
                "address": bot.config.weth_address,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "c" * 64,
                    "0x" + "0" * 64,
                ],
                "data": hex(2_000),
            }],
        })
        self.assertEqual(bot._measured_trade_received_wei(before, result, 1_900), 2_000)

    @patch("grid_bot.time.sleep", return_value=None)
    def test_unreconciled_stale_balance_uses_validated_quote_floor(self, _sleep):
        bot = self.bot(wallet=SequenceWallet(eth_balances=[10_000] * 7))
        bot.wallet.address = "0x" + "a" * 40
        before = bot._raw_trade_balance()
        result = SimpleNamespace(receipt={"gasUsed": 100, "effectiveGasPrice": 3, "logs": []})
        self.assertEqual(bot._measured_trade_received_wei(before, result, 1_900), 1_900)

    def test_sell_guard_conservatively_applies_declared_fee_to_quote(self):
        quote = SimpleNamespace(buy_amount=1_000)
        self.assertEqual(self.bot()._taxed_quote_return_wei(quote), 950)
        self.assertEqual(self.bot(taxed=False)._taxed_quote_return_wei(quote), 1_000)

    def test_recent_taxed_buy_failure_short_circuits_before_wallet_access(self):
        bot = self.bot(wallet=None)
        bot.config.taxed_token_failure_cooldown_seconds = 300
        bot.last_taxed_token_failure_time = time.time()
        bot.last_buy_time = 0
        bot.gridless_buy_cooldown = 0

        # A missing wallet would fail immediately if the cooldown did not
        # return before position evaluation and balance access.
        self.assertIsNone(bot._check_buys_gridless(1.0))


if __name__ == "__main__":
    unittest.main()
