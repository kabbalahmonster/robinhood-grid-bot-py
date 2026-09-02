import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wallet import TransactionResult, Wallet


class TestWalletNativeTransfer(unittest.TestCase):
    def test_build_eth_transfer_uses_estimate_and_configured_multipliers(self):
        wallet = Wallet.__new__(Wallet)
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.config = SimpleNamespace(
            chain_id=4663,
            gas_limit_multiplier=1.05,
            gas_price_multiplier=1.10,
            gas_price_freshness_multiplier=1.0,
        )
        wallet.w3 = SimpleNamespace(eth=Mock())
        wallet.w3.eth.get_transaction_count.return_value = 7
        wallet.w3.eth.estimate_gas.return_value = 21_000
        wallet.w3.eth.gas_price = 1_000_000_000
        wallet.w3.eth.get_block.return_value = {"baseFeePerGas": 900_000_000}

        tx = wallet.build_eth_transfer_transaction(
            "0x0000000000000000000000000000000000000004",
            500_000_000_000_000,
        )

        self.assertEqual(tx["value"], 500_000_000_000_000)
        self.assertEqual(tx["nonce"], 7)
        self.assertEqual(tx["chainId"], 4663)
        self.assertEqual(tx["gas"], 22_050)
        self.assertEqual(tx["gasPrice"], 1_100_000_000)
        wallet.w3.eth.estimate_gas.assert_called_once()
        wallet.w3.eth.get_transaction_count.assert_called_once_with(wallet.address, "pending")

    def test_normal_gas_price_never_falls_below_latest_base_fee(self):
        wallet = Wallet.__new__(Wallet)
        wallet.logger = logging.getLogger("test.wallet")
        wallet.config = SimpleNamespace(
            gas_price_multiplier=1.0,
            gas_price_freshness_multiplier=1.01,
        )
        wallet.w3 = SimpleNamespace(eth=Mock())
        wallet.w3.eth.gas_price = 378_657_080
        wallet.w3.eth.get_block.return_value = {"baseFeePerGas": 382_202_000}

        self.assertEqual(wallet.normal_gas_price(), 386_024_020)

    def test_erc20_transfer_rebuilds_once_after_prebroadcast_stale_fee_rejection(self):
        wallet = Wallet.__new__(Wallet)
        wallet.logger = logging.getLogger("test.wallet")
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.w3 = SimpleNamespace(eth=Mock())
        transfer = wallet.w3.eth.contract.return_value.functions.transfer.return_value
        transfer.build_transaction.side_effect = lambda params: dict(params)
        wallet.w3.eth.get_transaction_count.side_effect = [7, 7]
        wallet.normal_gas_price = Mock(side_effect=[378_657_080, 386_024_020])
        wallet._send_transaction = Mock(side_effect=[
            TransactionResult(
                success=False,
                error="max fee per gas less than block base fee: baseFee: 4322480000",
            ),
            TransactionResult(success=True, tx_hash="0xabc"),
        ])

        result = wallet.transfer_erc20(
            "0x0000000000000000000000000000000000000003",
            "0x0000000000000000000000000000000000000004",
            123,
        )

        self.assertTrue(result.success)
        self.assertEqual(transfer.build_transaction.call_count, 2)
        self.assertEqual(wallet._send_transaction.call_count, 2)
        self.assertEqual(wallet.normal_gas_price.call_args_list[1].args, (4_408_929_600,))
        self.assertEqual(
            wallet._send_transaction.call_args_list[1].args[0]["gasPrice"],
            386_024_020,
        )
        self.assertEqual(
            wallet.w3.eth.get_transaction_count.call_args_list[0].args,
            (wallet.address, "pending"),
        )

    def test_erc20_transfer_never_retries_after_hash_assignment(self):
        wallet = Wallet.__new__(Wallet)
        wallet.logger = logging.getLogger("test.wallet")
        wallet.address = "0x0000000000000000000000000000000000000002"
        wallet.w3 = SimpleNamespace(eth=Mock())
        transfer = wallet.w3.eth.contract.return_value.functions.transfer.return_value
        transfer.build_transaction.side_effect = lambda params: dict(params)
        wallet.normal_gas_price = Mock(return_value=400_000_000)
        wallet._send_transaction = Mock(return_value=TransactionResult(
            success=False,
            tx_hash="0xaccepted",
            error="max fee per gas less than block base fee",
        ))

        result = wallet.transfer_erc20(
            "0x0000000000000000000000000000000000000003",
            "0x0000000000000000000000000000000000000004",
            123,
        )

        self.assertFalse(result.success)
        wallet._send_transaction.assert_called_once()


if __name__ == "__main__":
    unittest.main()
