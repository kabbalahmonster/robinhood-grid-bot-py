import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from grid_bot import _dashboard_root_url, _reset_json_history, check_config


class TestCliCommands(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
