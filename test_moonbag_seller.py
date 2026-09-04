import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import moonbag_seller
from swap_provider import FallbackSwapProvider


TOKEN = "0x0000000000000000000000000000000000000001"
WETH = "0x0000000000000000000000000000000000000002"


def config(gridless=True):
    return SimpleNamespace(
        token_address=TOKEN, weth_address=WETH, zero_x_proxy=WETH,
        use_gridless=gridless, use_eth_trading=False, chain_id=4663,
        slippage_tolerance=2.0, taxed_token=False,
        gas_limit_multiplier=1.05, gas_price_multiplier=1.0,
        gas_price_freshness_multiplier=1.01, max_sell_gas_eth=0.0001,
        eth_gas_reserve=0.0005,
    )


def args(execute=False, confirmed=False, stopped=False, send_to_treasury=False):
    return SimpleNamespace(
        execute=execute,
        confirm_sell_moonbag=confirmed,
        confirm_bot_stopped=stopped,
        send_to_treasury=send_to_treasury,
        confirm_send_to_treasury=send_to_treasury,
        recipient="0x0000000000000000000000000000000000000004" if send_to_treasury else None,
        confirm_recipient="0x0000000000000000000000000000000000000004" if send_to_treasury else None,
    )


class TestMoonbagSeller(unittest.TestCase):
    def test_calculates_only_balance_above_allocated_positions(self):
        allocation = moonbag_seller.calculate_moonbag(1_000, 750)
        self.assertEqual(allocation.moonbag_raw, 250)

    def test_refuses_position_deficit(self):
        with self.assertRaisesRegex(ValueError, "exceeds the wallet"):
            moonbag_seller.calculate_moonbag(749, 750)

    def test_position_reader_fails_closed_on_malformed_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "positions.json")
            with open(path, "w") as handle:
                json.dump({"1": {"balance": "100"}}, handle)
            with self.assertRaisesRegex(ValueError, "invalid raw balance"):
                moonbag_seller._allocated_position_balance(moonbag_seller.Path(path))

    def test_treasury_forward_sends_only_net_new_eth_and_preserves_starting_balance(self):
        wallet = Mock()
        wallet.get_eth_balance_wei.return_value = 10**18 + 900_000
        wallet.build_eth_transfer_transaction.return_value = {
            "gas": 21_000, "gasPrice": 10, "value": 1,
        }
        wallet.transfer_eth.return_value = SimpleNamespace(success=True, tx_hash="0xtreasury")

        result, amount = moonbag_seller._forward_actual_proceeds(
            wallet, config(), "0x0000000000000000000000000000000000000004",
            eth_before_sale=10**18, weth_before_sale=None,
        )

        self.assertEqual(amount, 690_000)
        self.assertEqual(result.tx_hash, "0xtreasury")
        self.assertEqual(wallet.build_eth_transfer_transaction.return_value["value"], 690_000)
        wallet.transfer_eth.assert_called_once_with(
            wallet.build_eth_transfer_transaction.return_value, wait_for_receipt=True
        )

    @patch("moonbag_seller.create_swap_provider")
    @patch("moonbag_seller.Wallet")
    @patch("moonbag_seller.load_config")
    def test_dry_run_quotes_exact_excess_and_never_broadcasts(self, load, wallet_class, provider_factory):
        load.return_value = config()
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000003"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="FOX", decimals=2)
        wallet.get_token_balance.return_value = (10.0, 1000)
        wallet.normal_gas_price.return_value = 100_000_000
        wallet.get_eth_balance_wei.return_value = 10**18
        quote = SimpleNamespace(success=True, error=None, buy_amount=10**15, gas=100_000, gas_price=0)
        provider_factory.return_value.build_swap_transaction.return_value = quote

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                with open("data/gridless_positions.json", "w") as handle:
                    json.dump({"1": {"balance": 700}, "2": {"balance": 50}}, handle)
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(moonbag_seller.run_moonbag_sale(args()), 0)
            finally:
                os.chdir(old_cwd)

        provider_factory.return_value.build_swap_transaction.assert_called_once()
        self.assertEqual(provider_factory.return_value.build_swap_transaction.call_args.kwargs["sell_amount"], 250)
        wallet._send_transaction.assert_not_called()
        wallet.approve_token.assert_not_called()
        self.assertIn("Moonbag:    2.50", output.getvalue())

    @patch("moonbag_seller.create_swap_provider")
    @patch("moonbag_seller.Wallet")
    @patch("moonbag_seller.load_config")
    def test_execute_requires_both_confirmations_before_loading_config(self, load, wallet_class, provider_factory):
        self.assertEqual(moonbag_seller.run_moonbag_sale(args(True, False, True)), 2)
        self.assertEqual(moonbag_seller.run_moonbag_sale(args(True, True, False)), 2)
        load.assert_not_called()

    @patch("moonbag_seller.create_swap_provider")
    @patch("moonbag_seller.Wallet")
    @patch("moonbag_seller.load_config")
    def test_gas_cap_blocks_before_approval_or_swap(self, load, wallet_class, provider_factory):
        cfg = config()
        cfg.max_sell_gas_eth = 0.000001
        load.return_value = cfg
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000003"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="FOX", decimals=2)
        wallet.get_token_balance.return_value = (10.0, 1000)
        wallet.normal_gas_price.return_value = 1_000_000_000
        wallet.get_eth_balance_wei.return_value = 10**18
        provider_factory.return_value.build_swap_transaction.return_value = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=100_000, gas_price=0
        )
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                with open("data/gridless_positions.json", "w") as handle:
                    json.dump({"1": {"balance": 750}}, handle)
                self.assertEqual(moonbag_seller.run_moonbag_sale(args(True, True, True)), 2)
            finally:
                os.chdir(old_cwd)
        wallet.approve_token.assert_not_called()
        wallet._send_transaction.assert_not_called()

    def test_tries_fallback_when_primary_quote_exceeds_sell_gas_cap(self):
        cfg = config()
        cfg.max_sell_gas_eth = 0.0002
        wallet = Mock()
        wallet.normal_gas_price.return_value = 1_000_000_000
        wallet.get_eth_balance_wei.return_value = 10**18
        primary = Mock(name="primary")
        primary.name = "uniswap"
        primary.build_swap_transaction.return_value = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=1_000_000, gas_price=0
        )
        fallback = Mock(name="fallback")
        fallback.name = "sushiswap"
        fallback.build_swap_transaction.return_value = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=100_000, gas_price=0
        )
        provider = FallbackSwapProvider(primary, fallback)

        selected, quote = moonbag_seller._select_provider_quote(provider, wallet, cfg, 250)

        self.assertIs(selected, fallback)
        self.assertIs(quote, fallback.build_swap_transaction.return_value)
        primary.build_swap_transaction.assert_called_once()
        fallback.build_swap_transaction.assert_called_once()

    def test_refuses_when_every_route_exceeds_sell_gas_cap(self):
        cfg = config()
        cfg.max_sell_gas_eth = 0.0002
        wallet = Mock()
        wallet.normal_gas_price.return_value = 1_000_000_000
        wallet.get_eth_balance_wei.return_value = 10**18
        primary = Mock(name="primary")
        primary.name = "uniswap"
        primary.build_swap_transaction.return_value = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=1_000_000, gas_price=0
        )
        fallback = Mock(name="fallback")
        fallback.name = "sushiswap"
        fallback.build_swap_transaction.return_value = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=900_000, gas_price=0
        )
        provider = FallbackSwapProvider(primary, fallback)

        with self.assertRaisesRegex(ValueError, "uniswap: projected sell gas.*sushiswap: projected sell gas"):
            moonbag_seller._select_provider_quote(provider, wallet, cfg, 250)

    @patch("moonbag_seller.create_swap_provider")
    @patch("moonbag_seller.Wallet")
    @patch("moonbag_seller.load_config")
    def test_execute_sells_exact_excess_and_preserves_allocation(self, load, wallet_class, provider_factory):
        load.return_value = config()
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000003"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="FOX", decimals=2)
        wallet.get_token_balance.side_effect = [(10.0, 1000), (7.5, 750)]
        wallet.normal_gas_price.return_value = 100_000_000
        wallet.get_eth_balance_wei.return_value = 10**18
        wallet.check_allowance.return_value = 1000
        wallet.w3.eth.get_transaction_count.return_value = 9
        wallet._send_transaction.return_value = SimpleNamespace(success=True, error=None, tx_hash="0xsale")
        quote = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=100_000, gas_price=0,
            allowance_target=WETH, to=WETH, data="0x1234", value=0,
        )
        provider = provider_factory.return_value
        provider.name = "sushiswap"
        provider.capabilities = SimpleNamespace(
            api_managed_approval=False, refresh_after_approval=True, quote_requires_preparation=False
        )
        provider.build_swap_transaction.return_value = quote

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                with open("data/gridless_positions.json", "w") as handle:
                    json.dump({"1": {"balance": 750}}, handle)
                self.assertEqual(moonbag_seller.run_moonbag_sale(args(True, True, True)), 0)
            finally:
                os.chdir(old_cwd)

        self.assertEqual(provider.build_swap_transaction.call_args.kwargs["sell_amount"], 250)
        wallet.check_allowance.assert_called_once_with(TOKEN, WETH, use_permit2=False)
        wallet.approve_token.assert_not_called()
        wallet._send_transaction.assert_called_once()

    @patch("moonbag_seller.create_swap_provider")
    @patch("moonbag_seller.Wallet")
    @patch("moonbag_seller.load_config")
    def test_api_managed_execution_refreshes_and_prepares_after_approval_check(
        self, load, wallet_class, provider_factory
    ):
        load.return_value = config()
        wallet = wallet_class.return_value
        wallet.address = "0x0000000000000000000000000000000000000003"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="FOX", decimals=2)
        wallet.get_token_balance.side_effect = [(10.0, 1000), (7.5, 750)]
        wallet.normal_gas_price.return_value = 100_000_000
        wallet.get_eth_balance_wei.return_value = 10**18
        wallet.w3.eth.get_transaction_count.return_value = 9
        wallet._send_transaction.return_value = SimpleNamespace(success=True, error=None, tx_hash="0xsale")
        initial = SimpleNamespace(success=True, error=None, buy_amount=10**15, gas=100_000, gas_price=0)
        refreshed = SimpleNamespace(success=True, error=None, raw_response={"quote": True})
        prepared = SimpleNamespace(
            success=True, error=None, buy_amount=10**15, gas=100_000, gas_price=0,
            to=WETH, data="0x1234", value=0,
        )
        provider = provider_factory.return_value
        provider.name = "uniswap"
        provider.capabilities = SimpleNamespace(
            api_managed_approval=True, refresh_after_approval=False, quote_requires_preparation=True
        )
        provider.build_swap_transaction.return_value = initial
        provider.check_approval.return_value = {"cancel": None, "approval": None}
        provider.get_quote.return_value = refreshed
        provider.prepare_swap.return_value = prepared

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                with open("data/gridless_positions.json", "w") as handle:
                    json.dump({"1": {"balance": 750}}, handle)
                self.assertEqual(moonbag_seller.run_moonbag_sale(args(True, True, True)), 0)
            finally:
                os.chdir(old_cwd)

        provider.check_approval.assert_called_once_with(token=TOKEN, amount=250, wallet=wallet.address)
        provider.prepare_swap.assert_called_once_with(refreshed)
        wallet._send_transaction.assert_called_once()


if __name__ == "__main__":
    unittest.main()
