import logging
import json
import os
import tempfile
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

    def test_final_boundary_raises_legacy_gas_price_to_fresh_floor(self):
        wallet = self.make_wallet()
        wallet.normal_gas_price = Mock(return_value=500)
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 90_000
        signed_hash = Mock()
        signed_hash.hex.return_value = "0xabc"
        wallet.account.sign_transaction.return_value = SimpleNamespace(
            raw_transaction=b"signed", hash=signed_hash,
        )
        tx_hash = Mock()
        tx_hash.hex.return_value = "0xabc"
        wallet.w3.eth.send_raw_transaction.return_value = tx_hash
        wallet.w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 1, "gasUsed": 80_000, "effectiveGasPrice": 490,
        }

        result = wallet._send_transaction({
            "from": "0x1", "to": "0x2", "data": "0x1234", "value": 0,
            "gas": 100_000, "gasPrice": 400,
        })

        self.assertTrue(result.success)
        self.assertEqual(wallet.account.sign_transaction.call_args.args[0]["gasPrice"], 500)

    def test_receipt_failure_preserves_broadcast_hash_and_marks_unknown(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 90000
        signed_hash = Mock()
        signed_hash.hex.return_value = "0xabc123"
        signed = SimpleNamespace(raw_transaction=b"signed", hash=signed_hash)
        wallet.account.sign_transaction.return_value = signed
        tx_hash = Mock()
        tx_hash.hex.return_value = "0xabc123"
        wallet.w3.eth.send_raw_transaction.return_value = tx_hash
        wallet.w3.eth.wait_for_transaction_receipt.side_effect = ValueError(
            {"code": -32601, "message": "Method not found"}
        )

        with tempfile.TemporaryDirectory() as directory:
            wallet.config = SimpleNamespace(chain_id=4663)
            wallet.address = "0x1"
            wallet.unresolved_broadcast_path = os.path.join(directory, "guard.json")
            result = wallet._send_transaction({
                "from": "0x1", "to": "0x2", "data": "0x1234",
                "value": 7, "gas": 100000, "nonce": 9,
            })

            with open(wallet.unresolved_broadcast_path, encoding="utf-8") as handle:
                guard = json.load(handle)

        self.assertFalse(result.success)
        self.assertTrue(result.outcome_unknown)
        self.assertEqual(result.tx_hash, "0xabc123")
        self.assertIn("MUST NOT be retried", result.error)
        self.assertEqual(guard["tx_hash"], "0xabc123")
        self.assertEqual(guard["nonce"], 9)
        self.assertEqual(guard["value_wei"], 7)
        self.assertTrue(wallet.has_unresolved_broadcast())

    def test_submission_error_uses_local_signed_hash_and_marks_unknown(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 90_000
        signed_hash = Mock()
        signed_hash.hex.return_value = "0xlocalhash"
        wallet.account.sign_transaction.return_value = SimpleNamespace(
            raw_transaction=b"signed", hash=signed_hash,
        )
        wallet.w3.eth.send_raw_transaction.side_effect = ValueError(
            {"code": -32601, "message": "Method not found"}
        )

        with tempfile.TemporaryDirectory() as directory:
            wallet.config = SimpleNamespace(chain_id=4663)
            wallet.address = "0x1"
            wallet.unresolved_broadcast_path = os.path.join(directory, "guard.json")
            result = wallet._send_transaction({
                "from": "0x1", "to": "0x2", "data": "0x1234",
                "value": 0, "gas": 100_000, "nonce": 480,
            })
            with open(wallet.unresolved_broadcast_path, encoding="utf-8") as handle:
                guard = json.load(handle)

        self.assertFalse(result.success)
        self.assertTrue(result.outcome_unknown)
        self.assertEqual(result.tx_hash, "0xlocalhash")
        self.assertEqual(guard["tx_hash"], "0xlocalhash")
        self.assertEqual(guard["nonce"], 480)

    def test_nonce_too_low_recovers_successful_exact_hash_without_guard(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 90_000
        signed_hash = Mock()
        signed_hash.hex.return_value = "0xlanded"
        wallet.account.sign_transaction.return_value = SimpleNamespace(
            raw_transaction=b"signed", hash=signed_hash,
        )
        wallet.w3.eth.send_raw_transaction.side_effect = ValueError({
            "code": -32000, "message": "nonce too low: tx: 506 state: 507",
        })
        receipt = {
            "status": 1, "gasUsed": 143_626, "effectiveGasPrice": 189_718_000,
        }
        wallet.w3.eth.get_transaction_receipt.return_value = receipt

        with tempfile.TemporaryDirectory() as directory:
            wallet.config = SimpleNamespace(chain_id=4663)
            wallet.address = "0x1"
            wallet.unresolved_broadcast_path = os.path.join(directory, "guard.json")
            result = wallet._send_transaction({
                "from": "0x1", "to": "0x2", "data": "0x1234",
                "value": 7, "gas": 100_000, "nonce": 506,
            })
            self.assertFalse(os.path.exists(wallet.unresolved_broadcast_path))

        self.assertTrue(result.success)
        self.assertFalse(result.outcome_unknown)
        self.assertEqual(result.tx_hash, "0xlanded")
        self.assertIs(result.receipt, receipt)
        self.assertEqual(result.gas_used, 143_626)

    def test_nonce_too_low_without_exact_receipt_still_halts(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 90_000
        signed_hash = Mock()
        signed_hash.hex.return_value = "0xmissing"
        wallet.account.sign_transaction.return_value = SimpleNamespace(
            raw_transaction=b"signed", hash=signed_hash,
        )
        wallet.w3.eth.send_raw_transaction.side_effect = ValueError({
            "code": -32000, "message": "nonce too low: tx: 506 state: 507",
        })
        wallet.w3.eth.get_transaction_receipt.side_effect = ValueError("not found")

        with tempfile.TemporaryDirectory() as directory:
            wallet.config = SimpleNamespace(chain_id=4663)
            wallet.address = "0x1"
            wallet.unresolved_broadcast_path = os.path.join(directory, "guard.json")
            result = wallet._send_transaction({
                "from": "0x1", "to": "0x2", "data": "0x1234",
                "value": 7, "gas": 100_000, "nonce": 506,
            })
            self.assertTrue(os.path.exists(wallet.unresolved_broadcast_path))

        self.assertFalse(result.success)
        self.assertTrue(result.outcome_unknown)

    def test_base_fee_rejection_is_retryable_and_never_creates_guard(self):
        wallet = self.make_wallet()
        wallet.w3.eth.call.return_value = b""
        wallet.w3.eth.estimate_gas.return_value = 90_000
        wallet.normal_gas_price = Mock(return_value=185_440_040)
        signed_hash = Mock()
        signed_hash.hex.return_value = "0xrejected"
        wallet.account.sign_transaction.return_value = SimpleNamespace(
            raw_transaction=b"signed", hash=signed_hash,
        )
        wallet.w3.eth.send_raw_transaction.side_effect = ValueError({
            "code": -32000,
            "message": "max fee per gas less than block base fee: maxFeePerGas: 185440040 baseFee: 186354000",
        })

        with tempfile.TemporaryDirectory() as directory:
            wallet.config = SimpleNamespace(chain_id=4663)
            wallet.address = "0x1"
            wallet.unresolved_broadcast_path = os.path.join(directory, "guard.json")
            result = wallet._send_transaction({
                "from": "0x1", "to": "0x2", "data": "0x1234", "value": 7,
                "gas": 100_000, "gasPrice": 185_440_040, "nonce": 128,
            })
            self.assertFalse(os.path.exists(wallet.unresolved_broadcast_path))

        self.assertFalse(result.success)
        self.assertFalse(result.outcome_unknown)
        self.assertIn("definitively rejected", result.error)
        self.assertFalse(wallet.has_unresolved_broadcast())

    def test_legacy_base_fee_guard_is_archived_and_does_not_halt_startup(self):
        wallet = self.make_wallet()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "unresolved_broadcast.json")
            wallet.unresolved_broadcast_path = path
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "tx_hash": "0xrejected",
                    "error": "Signed transaction may have been broadcast: max fee per gas less than block base fee",
                }, handle)

            self.assertIsNone(wallet._load_unresolved_broadcast())
            self.assertFalse(os.path.exists(path))
            self.assertEqual(len([
                name for name in os.listdir(directory)
                if name.startswith("unresolved_broadcast.json.definitive-rejection.")
            ]), 1)


if __name__ == "__main__":
    unittest.main()
