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
            min_profit_fee_transfer_eth=0,
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
        bot._load_profit_fee_accrual = Mock(return_value={"pending_wei": 0, "sale_tx_hashes": []})
        bot._save_profit_fee_accrual = Mock()
        return bot

    def test_fee_below_minimum_is_durably_deferred(self):
        bot = self.make_bot(percent=10)
        bot.config.min_profit_fee_transfer_eth = 0.0001
        bot._load_profit_fee_accrual = Mock(return_value={"pending_wei": 0, "sale_tx_hashes": []})
        bot._save_profit_fee_accrual = Mock()

        entry = bot._charge_profit_fee(0.0005 * 10**18, "0xsale1")

        self.assertEqual(entry["status"], "deferred")
        self.assertEqual(entry["fee_wei"], int(0.00005 * 10**18))
        bot.wallet.transfer_erc20.assert_not_called()
        bot._save_profit_fee_accrual.assert_called_once()

    def test_accrued_fee_flushes_when_new_sale_reaches_minimum(self):
        bot = self.make_bot(percent=10)
        bot.config.min_profit_fee_transfer_eth = 0.0001
        bot._load_profit_fee_accrual = Mock(return_value={
            "pending_wei": int(0.00006 * 10**18), "sale_tx_hashes": ["0xsale1"],
        })
        bot._save_profit_fee_accrual = Mock()
        bot.wallet.transfer_erc20.return_value = TransactionResult(success=True, tx_hash="0xfee")

        entry = bot._charge_profit_fee(0.0005 * 10**18, "0xsale2")

        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["fee_wei"], int(0.00011 * 10**18))
        bot.wallet.transfer_erc20.assert_called_once_with(
            bot.config.weth_address, bot.config.profit_fee_wallet,
            int(0.00011 * 10**18), wait_for_receipt=True,
        )
        self.assertEqual(bot._save_profit_fee_accrual.call_count, 2)

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

    def test_native_fee_retries_once_after_prebroadcast_stale_fee_rejection(self):
        bot = self.make_bot(native=True, percent=10)
        initial = {"gas": 21_000, "gasPrice": 368_330_840}
        rebuilt = {"gas": 21_000, "gasPrice": 386_024_020}
        bot.wallet.build_eth_transfer_transaction.side_effect = [initial, rebuilt]
        bot.wallet.get_eth_balance_wei.return_value = 10**18
        bot.wallet.transfer_eth.side_effect = [
            TransactionResult(
                success=False,
                error="max fee per gas less than block base fee: baseFee: 369080000",
            ),
            TransactionResult(success=True, tx_hash="0xfee"),
        ]
        bot.wallet.is_base_fee_too_low_error.return_value = True
        bot.wallet.base_fee_from_error.return_value = 369_080_000

        entry = bot._charge_profit_fee(10**17, "0xsale")

        self.assertEqual(entry["status"], "success")
        self.assertEqual(bot.wallet.transfer_eth.call_count, 2)
        self.assertEqual(bot.wallet.build_eth_transfer_transaction.call_args_list[1].args[2], 376_461_600)
        self.assertEqual(bot.wallet.transfer_eth.call_args_list[1].args[0], rebuilt)

    def test_native_fee_retries_local_hash_after_definitive_rejection(self):
        bot = self.make_bot(native=True, percent=10)
        bot.wallet.build_eth_transfer_transaction.return_value = {"gas": 21_000, "gasPrice": 1}
        bot.wallet.get_eth_balance_wei.return_value = 10**18
        bot.wallet.transfer_eth.side_effect = [
            TransactionResult(
                success=False,
                tx_hash="0xdeterministic-not-accepted",
                error="RPC definitively rejected signed transaction before broadcast: "
                      "max fee per gas less than block base fee: baseFee: 369080000",
            ),
            TransactionResult(success=True, tx_hash="0xfee"),
        ]
        bot.wallet.is_base_fee_too_low_error.return_value = True
        bot.wallet.is_definitive_prebroadcast_rejection.return_value = True
        bot.wallet.base_fee_from_error.return_value = 369_080_000

        entry = bot._charge_profit_fee(10**17, "0xsale")

        self.assertEqual(entry["status"], "success")
        self.assertEqual(bot.wallet.transfer_eth.call_count, 2)

    def test_native_fee_never_retries_when_broadcast_outcome_is_unknown(self):
        bot = self.make_bot(native=True, percent=10)
        bot.wallet.build_eth_transfer_transaction.return_value = {"gas": 21_000, "gasPrice": 1}
        bot.wallet.get_eth_balance_wei.return_value = 10**18
        bot.wallet.transfer_eth.return_value = TransactionResult(
            success=False,
            outcome_unknown=True,
            error="max fee per gas less than block base fee",
        )
        bot.wallet.is_base_fee_too_low_error.return_value = True

        entry = bot._charge_profit_fee(10**17, "0xsale")

        self.assertEqual(entry["status"], "failed")
        bot.wallet.transfer_eth.assert_called_once()

    def test_native_fee_never_retries_without_rejecting_base_fee(self):
        bot = self.make_bot(native=True, percent=10)
        bot.wallet.build_eth_transfer_transaction.return_value = {"gas": 21_000, "gasPrice": 1}
        bot.wallet.get_eth_balance_wei.return_value = 10**18
        bot.wallet.transfer_eth.return_value = TransactionResult(
            success=False,
            error="max fee per gas less than block base fee",
        )
        bot.wallet.is_base_fee_too_low_error.return_value = True
        bot.wallet.base_fee_from_error.return_value = 0

        entry = bot._charge_profit_fee(10**17, "0xsale")

        self.assertEqual(entry["status"], "failed")
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
