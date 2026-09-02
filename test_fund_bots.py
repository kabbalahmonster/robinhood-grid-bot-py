import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "fund-bots.py"
SPEC = importlib.util.spec_from_file_location("fund_bots", SCRIPT)
fund_bots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fund_bots)


class TestFundBots(unittest.TestCase):
    def test_positive_eth_rejects_zero_and_non_finite_values(self):
        for value in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fund_bots.positive_eth(value, "amount")

    def test_load_env_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "treasury.env"
            path.write_text("PRIVATE_KEY=secret\nRPC_URL=https://example.invalid\n")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                fund_bots.load_env(path)

    def test_current_gas_price_uses_latest_base_fee_floor(self):
        w3 = SimpleNamespace(eth=Mock())
        w3.eth.gas_price = 378_657_080
        w3.eth.get_block.return_value = {"baseFeePerGas": 382_202_000}
        self.assertEqual(fund_bots.current_gas_price(w3), 386_024_020)

    def test_send_top_up_retries_stale_fee_before_hash_only(self):
        w3 = SimpleNamespace(eth=Mock())
        w3.eth.gas_price = 378_657_080
        w3.eth.get_block.return_value = {"baseFeePerGas": 382_202_000}
        w3.eth.get_transaction_count.return_value = 9
        w3.eth.get_balance.return_value = 10**18
        tx_hash = Mock()
        tx_hash.hex.return_value = "0xconfirmed"
        w3.eth.send_raw_transaction.side_effect = [
            ValueError("max fee per gas less than block base fee"),
            tx_hash,
        ]
        w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}
        account = Mock()
        account.address = "0x0000000000000000000000000000000000000001"
        account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"signed")

        result, tx = fund_bots.send_top_up(
            w3,
            account,
            "0x0000000000000000000000000000000000000002",
            1000,
            4663,
            500,
        )

        self.assertEqual(result, "0xconfirmed")
        self.assertEqual(tx["nonce"], 9)
        self.assertEqual(w3.eth.send_raw_transaction.call_count, 2)

    def test_send_top_up_rechecks_reserve_before_signing(self):
        w3 = SimpleNamespace(eth=Mock())
        w3.eth.gas_price = 1_000_000_000
        w3.eth.get_block.return_value = {"baseFeePerGas": 1_000_000_000}
        w3.eth.get_balance.return_value = 21_000_000_001_499
        account = Mock()
        account.address = "0x0000000000000000000000000000000000000001"

        with self.assertRaisesRegex(ValueError, "reserve"):
            fund_bots.send_top_up(
                w3,
                account,
                "0x0000000000000000000000000000000000000002",
                1000,
                4663,
                500,
            )
        account.sign_transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
