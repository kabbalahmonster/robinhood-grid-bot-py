import unittest
from unittest.mock import Mock, patch

from rpc_rotator import ResilientWeb3


class TestRPCReceiptFailover(unittest.TestCase):
    @patch("rpc_rotator.time.sleep", return_value=None)
    def test_method_not_found_fails_over_for_receipt_lookup(self, _sleep):
        first = Mock()
        first.provider.endpoint_uri = "https://first.invalid"
        first.eth.wait_for_transaction_receipt.side_effect = ValueError(
            {"code": -32601, "message": "Method not found"}
        )
        second = Mock()
        second.provider.endpoint_uri = "https://second.invalid"
        receipt = {"status": 1, "transactionHash": "0xabc"}
        second.eth.wait_for_transaction_receipt.return_value = receipt

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient._w3 = first
        resilient._current_url = first.provider.endpoint_uri
        resilient._refresh_connection = Mock(side_effect=lambda: (
            setattr(resilient, "_w3", second),
            setattr(resilient, "_current_url", second.provider.endpoint_uri),
        ))

        result = resilient._execute_with_failover(
            "eth.wait_for_transaction_receipt", "0xabc", timeout=120,
        )

        self.assertEqual(result, receipt)
        resilient._refresh_connection.assert_called_once()

    def test_method_not_found_does_not_replay_broadcast(self):
        first = Mock()
        first.provider.endpoint_uri = "https://first.invalid"
        first.eth.send_raw_transaction.side_effect = ValueError(
            {"code": -32601, "message": "Method not found"}
        )

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient._w3 = first
        resilient._current_url = first.provider.endpoint_uri
        resilient._refresh_connection = Mock()

        with self.assertRaises(ValueError):
            resilient._execute_with_failover("eth.send_raw_transaction", b"signed")
        resilient._refresh_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
