import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import asset_liquidator
from asset_liquidator import _managed_assets, run_asset_liquidation


TOKEN = "0x0000000000000000000000000000000000000001"
USDG = "0x0000000000000000000000000000000000000002"
WETH = "0x0000000000000000000000000000000000000003"


def config():
    return SimpleNamespace(
        token_symbol="TEST",
        token_address=TOKEN,
        usdg_address=USDG,
        weth_address=WETH,
        zero_x_proxy="0x0000000000000000000000000000000000000004",
        gas_limit_multiplier=1.05,
        gas_price_multiplier=1.05,
        chain_id=4663,
    )


def args(execute=False, confirmed=True, stopped=False, keep_usdg=False):
    return SimpleNamespace(
        execute=execute,
        confirm_liquidate_assets=confirmed,
        confirm_bot_stopped=stopped,
        keep_usdg=keep_usdg,
    )


class TestAssetLiquidator(unittest.TestCase):
    def test_managed_assets_deduplicate_and_prefer_weth_handling(self):
        cfg = config()
        cfg.token_address = WETH
        self.assertEqual(_managed_assets(cfg), [("WETH", WETH), ("USDG", USDG)])

    def test_keep_usdg_excludes_it_from_managed_assets(self):
        self.assertEqual(_managed_assets(config(), keep_usdg=True), [("TEST", TOKEN), ("WETH", WETH)])

    @patch("asset_liquidator.create_swap_provider")
    @patch("asset_liquidator.Wallet")
    @patch("asset_liquidator.load_config")
    def test_keep_usdg_never_reads_or_quotes_usdg(self, load, wallet_cls, provider_factory):
        load.return_value = config()
        wallet = wallet_cls.return_value
        wallet.address = "0x0000000000000000000000000000000000000005"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="TEST", decimals=18)
        wallet.get_token_balance.side_effect = [(1.0, 10**18), (0.0, 0)]
        provider_factory.return_value.build_swap_transaction.return_value = SimpleNamespace(
            success=True, buy_amount=10**18, error=None
        )

        self.assertEqual(run_asset_liquidation(args(keep_usdg=True)), 0)
        read_addresses = [call.args[0] for call in wallet.get_token_balance.call_args_list]
        self.assertEqual(read_addresses, [TOKEN, WETH])
        quoted_addresses = [call.kwargs["sell_token"] for call in provider_factory.return_value.build_swap_transaction.call_args_list]
        self.assertEqual(quoted_addresses, [TOKEN])

    @patch("asset_liquidator.create_swap_provider")
    @patch("asset_liquidator.Wallet")
    @patch("asset_liquidator.load_config")
    def test_dry_run_never_broadcasts_or_clears_positions(self, load, wallet_cls, provider_factory):
        load.return_value = config()
        wallet = wallet_cls.return_value
        wallet.address = "0x0000000000000000000000000000000000000005"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="TEST", decimals=18)
        wallet.get_token_balance.side_effect = [(1.0, 10**18), (0.0, 0), (0.5, 5 * 10**17)]
        quote = SimpleNamespace(success=True, buy_amount=9 * 10**17, error=None)
        provider_factory.return_value.build_swap_transaction.return_value = quote

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                with open("data/positions.json", "w") as handle:
                    json.dump({"1": {"balance": 1}}, handle)
                self.assertEqual(run_asset_liquidation(args()), 0)
                with open("data/positions.json") as handle:
                    self.assertNotEqual(json.load(handle), {})
            finally:
                os.chdir(old_cwd)
        wallet._send_transaction.assert_not_called()
        wallet.unwrap_weth.assert_not_called()

    @patch("asset_liquidator._execute_swap")
    @patch("asset_liquidator.create_swap_provider")
    @patch("asset_liquidator.Wallet")
    @patch("asset_liquidator.load_config")
    def test_success_verifies_zero_then_backs_up_and_clears_both_stores(
        self, load, wallet_cls, provider_factory, execute_swap
    ):
        load.return_value = config()
        wallet = wallet_cls.return_value
        wallet.address = "0x0000000000000000000000000000000000000005"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="TEST", decimals=18)
        wallet.get_token_balance.side_effect = [
            (1.0, 10**18), (2.0, 2 * 10**18), (0.5, 5 * 10**17),
            (0.0, 0), (0.0, 0), (0.0, 0),
        ]
        provider_factory.return_value.build_swap_transaction.return_value = SimpleNamespace(
            success=True, buy_amount=10**18, error=None
        )
        execute_swap.return_value = asset_liquidator.LiquidationResult(True, tx_hash="0xswap")
        wallet.build_weth_withdraw_transaction.return_value = {"to": WETH}
        wallet.unwrap_weth.return_value = SimpleNamespace(success=True, error=None, tx_hash="0xunwrap")

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                for name in ("positions.json", "gridless_positions.json"):
                    with open(f"data/{name}", "w") as handle:
                        json.dump({"position": {"balance": 1}}, handle)
                self.assertEqual(run_asset_liquidation(args(True, True, True)), 0)
                for name in ("positions.json", "gridless_positions.json"):
                    with open(f"data/{name}") as handle:
                        self.assertEqual(json.load(handle), {})
                    self.assertEqual(len([p for p in os.listdir("data") if p.startswith(name + ".pre-liquidation.")]), 1)
                with open("data/asset_liquidations.json") as handle:
                    self.assertTrue(json.load(handle)[-1]["positions_cleared"])
            finally:
                os.chdir(old_cwd)

    @patch("asset_liquidator._execute_swap")
    @patch("asset_liquidator.create_swap_provider")
    @patch("asset_liquidator.Wallet")
    @patch("asset_liquidator.load_config")
    def test_failed_asset_never_clears_positions(self, load, wallet_cls, provider_factory, execute_swap):
        load.return_value = config()
        wallet = wallet_cls.return_value
        wallet.address = "0x0000000000000000000000000000000000000005"
        wallet.get_token_info.return_value = SimpleNamespace(symbol="TEST", decimals=18)
        wallet.get_token_balance.side_effect = [(1.0, 10**18), (0.0, 0), (0.0, 0)]
        provider_factory.return_value.build_swap_transaction.return_value = SimpleNamespace(
            success=True, buy_amount=10**18, error=None
        )
        execute_swap.return_value = asset_liquidator.LiquidationResult(False, error="route failed")

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                os.makedirs("data")
                with open("data/positions.json", "w") as handle:
                    json.dump({"position": {"balance": 1}}, handle)
                with redirect_stdout(StringIO()):
                    self.assertEqual(run_asset_liquidation(args(True, True, True)), 2)
                with open("data/positions.json") as handle:
                    self.assertNotEqual(json.load(handle), {})
            finally:
                os.chdir(old_cwd)

    def test_confirmation_guards(self):
        self.assertEqual(run_asset_liquidation(args(False, False, False)), 2)
        self.assertEqual(run_asset_liquidation(args(True, True, False)), 2)


if __name__ == "__main__":
    unittest.main()
