import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wallet import Wallet


class TestWalletNativeTransfer(unittest.TestCase):
    def test_build_eth_transfer_uses_estimate_and_configured_multipliers(self):
        wallet = Wallet.__new__(Wallet)
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.config = SimpleNamespace(gas_limit_multiplier=1.05, gas_price_multiplier=1.10)
        wallet.w3 = SimpleNamespace(eth=Mock())
        wallet.w3.eth.get_transaction_count.return_value = 7
        wallet.w3.eth.estimate_gas.return_value = 21_000
        wallet.w3.eth.gas_price = 1_000_000_000

        tx = wallet.build_eth_transfer_transaction(
            "0x0000000000000000000000000000000000000004",
            500_000_000_000_000,
        )

        self.assertEqual(tx["value"], 500_000_000_000_000)
        self.assertEqual(tx["nonce"], 7)
        self.assertEqual(tx["gas"], 22_050)
        self.assertEqual(tx["gasPrice"], 1_100_000_000)
        wallet.w3.eth.estimate_gas.assert_called_once()


if __name__ == "__main__":
    unittest.main()
