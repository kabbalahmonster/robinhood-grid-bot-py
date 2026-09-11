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

    @patch("rpc_rotator.time.sleep", return_value=None)
    def test_method_not_found_retries_identical_broadcast_on_next_endpoint(self, _sleep):
        first = Mock()
        first.provider.endpoint_uri = "https://first.invalid"
        first.eth.send_raw_transaction.side_effect = ValueError(
            {"code": -32601, "message": "Method not found"}
        )
        second = Mock()
        second.provider.endpoint_uri = "https://second.invalid"
        second.eth.send_raw_transaction.return_value = "0xabc"

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient._w3 = first
        resilient._current_url = first.provider.endpoint_uri
        resilient._refresh_connection = Mock(side_effect=lambda: (
            setattr(resilient, "_w3", second),
            setattr(resilient, "_current_url", second.provider.endpoint_uri),
        ))

        result = resilient._execute_with_failover(
            "eth.send_raw_transaction", b"identical-signed-bytes",
        )

        self.assertEqual(result, "0xabc")
        first.eth.send_raw_transaction.assert_called_once_with(b"identical-signed-bytes")
        second.eth.send_raw_transaction.assert_called_once_with(b"identical-signed-bytes")
        resilient._refresh_connection.assert_called_once()

    def test_ambiguous_timeout_does_not_replay_broadcast(self):
        first = Mock()
        first.provider.endpoint_uri = "https://first.invalid"
        first.eth.send_raw_transaction.side_effect = TimeoutError("request timeout")

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient._w3 = first
        resilient._current_url = first.provider.endpoint_uri
        resilient._refresh_connection = Mock()

        with self.assertRaises(TimeoutError):
            resilient._execute_with_failover("eth.send_raw_transaction", b"signed")
        resilient._refresh_connection.assert_not_called()

    @patch("rpc_rotator.time.sleep", return_value=None)
    def test_timeout_after_capability_failover_does_not_reach_third_endpoint(self, _sleep):
        first = Mock()
        first.provider.endpoint_uri = "https://first.invalid"
        first.eth.send_raw_transaction.side_effect = ValueError(
            {"code": -32601, "message": "Method not found"}
        )
        second = Mock()
        second.provider.endpoint_uri = "https://second.invalid"
        second.eth.send_raw_transaction.side_effect = TimeoutError("request timeout")
        third = Mock()
        third.provider.endpoint_uri = "https://third.invalid"

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient._w3 = first
        resilient._current_url = first.provider.endpoint_uri
        endpoints = iter((second, third))

        def refresh():
            next_endpoint = next(endpoints)
            resilient._w3 = next_endpoint
            resilient._current_url = next_endpoint.provider.endpoint_uri

        resilient._refresh_connection = Mock(side_effect=refresh)

        with self.assertRaises(TimeoutError):
            resilient._execute_with_failover("eth.send_raw_transaction", b"signed")
        self.assertEqual(resilient._refresh_connection.call_count, 1)
        third.eth.send_raw_transaction.assert_not_called()

    def test_exact_hash_receipt_searches_all_endpoints(self):
        first = Mock()
        first.eth.get_transaction_receipt.side_effect = ValueError("transaction not found")
        second = Mock()
        receipt = {"status": 1, "transactionHash": "0xabc"}
        second.eth.get_transaction_receipt.return_value = receipt

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        endpoint_one = Mock(url="https://first.invalid")
        endpoint_two = Mock(url="https://second.invalid")
        resilient.rotator = Mock()
        resilient.rotator._endpoints = [endpoint_one, endpoint_two]
        resilient.rotator._get_web3_for_url.side_effect = [first, second]

        result = resilient.find_transaction_receipt("0xabc")

        self.assertEqual(result, receipt)
        second.eth.get_transaction_receipt.assert_called_once_with("0xabc")
        endpoint_two.record_success.assert_called_once()


if __name__ == "__main__":
    unittest.main()
