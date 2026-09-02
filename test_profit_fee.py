import unittest
from unittest.mock import Mock, patch
import os
from types import SimpleNamespace

from config import load_config
from grid_bot import GridBot
from wallet import TransactionResult


class ProfitFeeTests(unittest.TestCase):
    def make_bot(self, *, native=False, percent=10):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            profit_fee_percent=percent,
            profit_fee_wallet="0x1111111111111111111111111111111111111111",
            use_eth_trading=native,
            weth_address="0x2222222222222222222222222222222222222222",
            eth_gas_reserve=0.001,
            max_swap_gas_eth=0,
        )
        bot.trade_token_name = "ETH" if native else "WETH"
        bot.wallet = Mock()
        bot.wallet.address = "0x3333333333333333333333333333333333333333"
        bot.wallet.normal_gas_price.return_value = 1
        bot._record_profit_fee = Mock()
        return bot

    def test_weth_fee_is_percentage_of_positive_profit(self):
        bot = self.make_bot(percent=12.5)
        bot.wallet.transfer_erc20.return_value = TransactionResult(success=True, tx_hash="0xfee")

        entry = bot._charge_profit_fee(2 * 10**18, "0xsale")

        bot.wallet.transfer_erc20.assert_called_once_with(
            bot.config.weth_address,
            bot.config.profit_fee_wallet,
            250_000_000_000_000_000,
            wait_for_receipt=True,
        )
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["sale_tx_hash"], "0xsale")

    def test_loss_and_zero_percentage_do_not_transfer(self):
        bot = self.make_bot(percent=10)
        self.assertIsNone(bot._charge_profit_fee(-1, "0xloss"))
        bot.config.profit_fee_percent = 0
        self.assertIsNone(bot._charge_profit_fee(10**18, "0xdisabled"))
        bot.wallet.transfer_erc20.assert_not_called()

    def test_native_fee_preserves_gas_reserve(self):
        bot = self.make_bot(native=True, percent=10)
        bot.wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
        }
        bot.wallet.get_eth_balance_wei.return_value = 10**18
        bot.wallet.transfer_eth.return_value = TransactionResult(success=True, tx_hash="0xfee")

        entry = bot._charge_profit_fee(10**17, "0xsale")

        self.assertEqual(entry["fee_wei"], 10**16)
        bot.wallet.transfer_eth.assert_called_once()

    def test_native_fee_transfer_obeys_separate_gas_cap(self):
        bot = self.make_bot(native=True, percent=10)
        bot.config.max_fee_transfer_gas_eth = 0.00001
        bot.wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
        }

        entry = bot._charge_profit_fee(10**17, "0xsale")

        self.assertEqual(entry["status"], "failed")
        self.assertIn("blocked by gas cap", entry["error"])
        bot.wallet.transfer_eth.assert_not_called()

    def test_failed_fee_is_audited_without_raising(self):
        bot = self.make_bot()
        bot.wallet.transfer_erc20.return_value = TransactionResult(success=False, error="nope")
        entry = bot._charge_profit_fee(10**18, "0xsale")
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["error"], "nope")
        bot._record_profit_fee.assert_called_once()

    def test_config_requires_wallet_and_caps_total_distribution(self):
        env = {
            "PRIVATE_KEY": "0x" + "01" * 32,
            "RPC_URL": "https://rpc.example",
            "CHAIN_ID": "4663",
            "TOKEN_ADDRESS": "0x2222222222222222222222222222222222222222",
            "SWAP_PROVIDER": "sushiswap",
            "USE_UNISWAP_API": "false",
            "PROFIT_FEE_PERCENT": "10",
            "PROFIT_FEE_WALLET": "",
            "BANK_PERCENTAGE": "20",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "PROFIT_FEE_WALLET"):
                load_config()
        env["PROFIT_FEE_WALLET"] = "0x1111111111111111111111111111111111111111"
        env["BANK_PERCENTAGE"] = "95"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "must not exceed 100"):
                load_config()


if __name__ == "__main__":
    unittest.main()
