import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wallet import Wallet


class TestWalletPreflight(unittest.TestCase):
    def make_wallet(self):
        wallet = Wallet.__new__(Wallet)
        wallet.logger = logging.getLogger("test.wallet.preflight")
        wallet.w3 = SimpleNamespace(eth=Mock())
        wallet.account = Mock()
        return wallet

    def test_rpc_revert_fails_closed_before_signing(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.side_effect = ValueError("execution reverted")
        result = wallet._send_transaction({
            "from": "0x1", "to": "0x2", "data": "0x1234",
            "value": 0, "gas": 100000,
        })
        self.assertFalse(result.success)
        wallet.account.sign_transaction.assert_not_called()
        wallet.w3.eth.send_raw_transaction.assert_not_called()

    def test_estimate_above_final_limit_fails_closed(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 100001
        result = wallet._send_transaction({
            "from": "0x1", "to": "0x2", "data": "0x1234",
            "value": 0, "gas": 100000,
        })
        self.assertFalse(result.success)
        self.assertIn("exceeds transaction gas limit", result.error)
        wallet.account.sign_transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
