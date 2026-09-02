import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from grid_bot import (
    _dashboard_root_url,
    _reset_json_history,
    _terminal_transaction_link,
    _total_successful_treasury_sent_usdg,
    check_config,
    run_native_treasury_transfer,
    run_treasury_transfer,
)


class TestCliCommands(unittest.TestCase):
    def test_terminal_transaction_link_uses_chain_explorer_with_hash_only_label(self):
        tx_hash = "0x" + "a" * 64
        self.assertEqual(
            _terminal_transaction_link(4663, tx_hash),
            f"\033]8;;https://robinhoodchain.blockscout.com/tx/{tx_hash}\033\\{tx_hash}\033]8;;\033\\",
        )
        self.assertIn("basescan.org/tx/", _terminal_transaction_link(8453, tx_hash))
        self.assertIn("etherscan.io/tx/", _terminal_transaction_link(1, tx_hash))

    def test_terminal_transaction_link_falls_back_to_plain_hash_for_unknown_chain(self):
        tx_hash = "0x" + "a" * 64
        self.assertEqual(_terminal_transaction_link(999, tx_hash), tx_hash)

    def test_treasury_total_counts_only_confirmed_usdg_receipts(self):
        usdg = "0x0000000000000000000000000000000000000003"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "treasury_transfers.json")
            with open(path, "w") as handle:
                json.dump([
                    {"success": True, "token_address": usdg, "amount": "12.50"},
                    {"success": False, "token_address": usdg, "amount": "5.00"},
                    {"success": True, "token_address": "0x0000000000000000000000000000000000000004", "amount": "9.00"},
                    {"success": True, "token_address": usdg, "amount": "not-a-number"},
                ], handle)
            self.assertEqual(_total_successful_treasury_sent_usdg(usdg, path), 12.5)

    def test_reset_json_history_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "data", "history.json")
            _reset_json_history(path, "History")
            _reset_json_history(path, "History")
            with open(path) as handle:
                self.assertEqual(json.load(handle), [])
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_dashboard_root_url(self):
        self.assertEqual(
            _dashboard_root_url("https://doomdash.ca/api/status"),
            "https://doomdash.ca/",
        )

    @patch("grid_bot.requests.get")
    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_check_config_is_read_only_and_successful(self, load_config, wallet_class, get):
        load_config.return_value = SimpleNamespace(
            token_symbol="TEST",
            token_address="0x0000000000000000000000000000000000000001",
            usdg_address="0x0000000000000000000000000000000000000003",
            chain_name="Robinhood",
            chain_id=4663,
            swap_provider="uniswap",
            use_uniswap_api=True,
            use_li_fi=False,
            dashboard_url="https://doomdash.ca/api/status",
            dashboard_api_key="configured",
        )
        wallet = wallet_class.return_value
        wallet.w3.eth.chain_id = 4663
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance.return_value = 1.25
        wallet._load_token_info.return_value = SimpleNamespace(symbol="TEST")
        wallet.get_token_balance.side_effect = [
            (42.0, 42 * 10**18),
            (12.5, 12_500_000),
        ]
        get.return_value = Mock(status_code=200)
        get.return_value.raise_for_status.return_value = None

        output = StringIO()
        with redirect_stdout(output):
            result = check_config()

        self.assertEqual(result, 0)
        self.assertIn("no quote requested and no transaction broadcast", output.getvalue())
        self.assertIn("PASS USDG: 12.500000", output.getvalue())
        get.assert_called_once_with("https://doomdash.ca/", timeout=5)

    @patch("grid_bot._append_treasury_receipt")
    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_treasury_dry_run_never_broadcasts(self, load_config, wallet_class, append_receipt):
        load_config.return_value = SimpleNamespace(
            usdg_address="0x0000000000000000000000000000000000000003",
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="USDG", decimals=6)
        wallet.get_token_balance.return_value = (12.5, 12_500_000)
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            transfer_token="USDG",
            amount="all",
            confirm_recipient=None,
            execute=False,
            confirm_bot_stopped=False,
        )

        self.assertEqual(run_treasury_transfer(args), 0)
        wallet.transfer_erc20.assert_not_called()
        append_receipt.assert_not_called()

    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_treasury_non_allowlisted_recipient_requires_exact_confirmation(self, load_config, wallet_class):
        load_config.return_value = SimpleNamespace(
            usdg_address="0x0000000000000000000000000000000000000003",
            treasury_allowed_recipients=[],
        )
        wallet_class.return_value.address = "0x0000000000000000000000000000000000000002"
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            transfer_token="USDG",
            amount="all",
            confirm_recipient=None,
            execute=False,
            confirm_bot_stopped=False,
        )

        self.assertEqual(run_treasury_transfer(args), 2)
        wallet_class.return_value.get_token_balance.assert_not_called()

    @patch("grid_bot._append_treasury_receipt")
    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_native_eth_dry_run_preserves_reserve_and_never_broadcasts(
        self, load_config, wallet_class, append_receipt
    ):
        load_config.return_value = SimpleNamespace(
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
            eth_gas_reserve=0.0005,
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance_wei.return_value = 2_000_000_000_000_000
        wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
        }
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            amount="0.0005",
            confirm_recipient=None,
            confirm_liquidate=False,
            execute=False,
            confirm_bot_stopped=False,
        )

        self.assertEqual(run_native_treasury_transfer(args), 0)
        wallet.transfer_eth.assert_not_called()
        append_receipt.assert_not_called()

    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_native_eth_transfer_refuses_to_spend_configured_reserve(
        self, load_config, wallet_class
    ):
        load_config.return_value = SimpleNamespace(
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
            eth_gas_reserve=0.0005,
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance_wei.return_value = 1_000_000_000_000_000
        wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
        }
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            amount="0.0005",
            confirm_recipient=None,
            confirm_liquidate=False,
            execute=True,
            confirm_bot_stopped=True,
        )

        self.assertEqual(run_native_treasury_transfer(args), 2)
        wallet.transfer_eth.assert_not_called()

    @patch("grid_bot._append_treasury_receipt")
    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_native_eth_liquidation_sends_balance_minus_maximum_fee(
        self, load_config, wallet_class, append_receipt
    ):
        load_config.return_value = SimpleNamespace(
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
            eth_gas_reserve=0.0005,
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance_wei.return_value = 1_000_000_000_000_000
        wallet.address_has_code.return_value = False
        wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
            "value": 1,
        }
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            amount="all",
            confirm_recipient=None,
            confirm_liquidate=True,
            execute=False,
            confirm_bot_stopped=False,
        )

        self.assertEqual(run_native_treasury_transfer(args), 0)
        self.assertEqual(
            wallet.build_eth_transfer_transaction.return_value["value"],
            979_000_000_000_000,
        )
        wallet.transfer_eth.assert_not_called()
        append_receipt.assert_not_called()

    @patch("grid_bot._append_treasury_receipt")
    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_native_eth_available_sends_balance_minus_fee_and_reserve(
        self, load_config, wallet_class, append_receipt
    ):
        load_config.return_value = SimpleNamespace(
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
            eth_gas_reserve=0.0006,
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance_wei.return_value = 2_000_000_000_000_000
        wallet.address_has_code.return_value = False
        wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
            "value": 1,
        }
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            amount="available",
            confirm_recipient=None,
            confirm_liquidate=False,
            execute=False,
            confirm_bot_stopped=False,
        )

        self.assertEqual(run_native_treasury_transfer(args), 0)
        self.assertEqual(
            wallet.build_eth_transfer_transaction.return_value["value"],
            1_379_000_000_000_000,
        )
        wallet.transfer_eth.assert_not_called()
        append_receipt.assert_not_called()

    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_native_eth_available_treats_no_surplus_as_a_safe_skip(
        self, load_config, wallet_class
    ):
        load_config.return_value = SimpleNamespace(
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
            eth_gas_reserve=0.0006,
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance_wei.return_value = 600_000_000_000_000
        wallet.address_has_code.return_value = False
        wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000,
            "gasPrice": 1_000_000_000,
            "value": 1,
        }
        args = SimpleNamespace(
            recipient="0x0000000000000000000000000000000000000004",
            amount="available",
            confirm_recipient=None,
            confirm_liquidate=False,
            execute=True,
            confirm_bot_stopped=True,
        )

        self.assertEqual(run_native_treasury_transfer(args), 0)
        wallet.transfer_eth.assert_not_called()

    @patch("grid_bot.Wallet")
    @patch("grid_bot.load_config")
    def test_native_eth_liquidation_requires_confirmation_and_eoa_recipient(
        self, load_config, wallet_class
    ):
        load_config.return_value = SimpleNamespace(
            treasury_allowed_recipients=["0x0000000000000000000000000000000000000004"],
            eth_gas_reserve=0.0005,
        )
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.get_eth_balance_wei.return_value = 1_000_000_000_000_000
        base_args = dict(
            recipient="0x0000000000000000000000000000000000000004",
            amount="all",
            confirm_recipient=None,
            execute=False,
            confirm_bot_stopped=False,
        )

        self.assertEqual(
            run_native_treasury_transfer(SimpleNamespace(**base_args, confirm_liquidate=False)),
            2,
        )
        wallet.address_has_code.return_value = True
        self.assertEqual(
            run_native_treasury_transfer(SimpleNamespace(**base_args, confirm_liquidate=True)),
            2,
        )
        wallet.build_eth_transfer_transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
