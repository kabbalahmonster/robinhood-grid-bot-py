import unittest
from unittest.mock import Mock, patch

from rpc_rotator import ResilientWeb3


class TestRPCReceiptFailover(unittest.TestCase):
    @staticmethod
    def resilient_with_client(client):
        client.provider.endpoint_uri = "https://first.invalid"
        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient.rotator.endpoint_label.return_value = "rpc_1"
        resilient._w3 = client
        resilient._current_url = client.provider.endpoint_uri
        resilient._refresh_connection = Mock()
        return resilient

    def test_candidate_scope_records_only_sanitized_rpc_metadata(self):
        first = Mock()
        first.provider.endpoint_uri = "https://secret-user:key@first.invalid/path"
        first.eth.get_block.return_value = {"number": 1}

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        resilient.rotator = Mock()
        resilient.rotator.endpoint_label.return_value = "rpc_1"
        resilient._w3 = first
        resilient._current_url = first.provider.endpoint_uri

        trace = []
        with resilient.telemetry_scope(trace):
            result = resilient._execute_with_failover("eth.get_block", "latest")

        self.assertEqual(result, {"number": 1})
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["method"], "eth.get_block")
        self.assertEqual(trace[0]["attempts"], 1)
        self.assertEqual(trace[0]["failures"], 0)
        self.assertEqual(trace[0]["endpoint_labels"], ["rpc_1"])
        self.assertNotIn("secret-user", repr(trace))
        self.assertNotIn("first.invalid", repr(trace))

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
        telemetry = resilient.telemetry_snapshot()
        self.assertEqual(telemetry["logical_calls"], 1)
        self.assertEqual(telemetry["attempts"], 2)
        self.assertEqual(telemetry["failures"], 1)
        self.assertEqual(
            telemetry["methods"]["eth.wait_for_transaction_receipt"]["calls"], 1
        )

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

    def test_gas_limit_digits_do_not_masquerade_as_http_500(self):
        first = Mock()
        first.eth.estimate_gas.side_effect = ValueError(
            "execution reverted: failed with 50000000 gas"
        )
        resilient = self.resilient_with_client(first)

        with self.assertRaises(ValueError):
            resilient._execute_with_failover("eth.estimate_gas", {"to": "0x2"})

        first.eth.estimate_gas.assert_called_once()
        resilient.rotator.report_failure.assert_not_called()
        resilient._refresh_connection.assert_not_called()

    def test_transaction_validation_error_does_not_penalize_endpoint(self):
        first = Mock()
        first.eth.estimate_gas.side_effect = ValueError(
            "execution reverted: insufficient allowance"
        )
        resilient = self.resilient_with_client(first)

        with self.assertRaises(ValueError):
            resilient._execute_with_failover("eth.estimate_gas", {"to": "0x2"})

        resilient.rotator.report_failure.assert_not_called()

    def test_base_fee_rejection_retries_once_with_refreshed_fee_cap(self):
        first = Mock()
        first.eth.estimate_gas.side_effect = [
            ValueError({
                "code": -32000,
                "message": (
                    "max fee per gas less than block base fee: "
                    "maxFeePerGas: 50000000 baseFee: 75000000"
                ),
            }),
            123456,
        ]
        first.eth.get_block.return_value = {"baseFeePerGas": 80_000_000}
        first.eth.max_priority_fee = 2_000_000
        resilient = self.resilient_with_client(first)

        result = resilient._execute_with_failover(
            "eth.estimate_gas", {"from": "0x1", "to": "0x2", "value": 0},
        )

        self.assertEqual(result, 123456)
        self.assertEqual(first.eth.estimate_gas.call_count, 2)
        retry_tx = first.eth.estimate_gas.call_args_list[1].args[0]
        self.assertNotIn("gasPrice", retry_tx)
        self.assertEqual(retry_tx["maxPriorityFeePerGas"], 2_000_000)
        self.assertEqual(retry_tx["maxFeePerGas"], 162_000_000)
        resilient.rotator.report_failure.assert_not_called()
        resilient._refresh_connection.assert_not_called()

    def test_base_fee_rejection_retries_at_most_once(self):
        first = Mock()
        error = ValueError(
            "max fee per gas less than block base fee: maxFeePerGas: 1 baseFee: 2"
        )
        first.eth.estimate_gas.side_effect = [error, error]
        first.eth.get_block.return_value = {"baseFeePerGas": 2}
        first.eth.max_priority_fee = 1_000_000
        resilient = self.resilient_with_client(first)

        with self.assertRaises(ValueError):
            resilient._execute_with_failover("eth.estimate_gas", {"to": "0x2"})

        self.assertEqual(first.eth.estimate_gas.call_count, 2)
        resilient.rotator.report_failure.assert_not_called()
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

    def test_exact_transaction_searches_past_not_found_endpoint(self):
        first = Mock()
        first.eth.get_transaction.side_effect = ValueError("transaction not found")
        second = Mock()
        transaction = {"hash": "0xabc", "nonce": 456}
        second.eth.get_transaction.return_value = transaction

        resilient = ResilientWeb3.__new__(ResilientWeb3)
        endpoint_one = Mock(url="https://first.invalid")
        endpoint_two = Mock(url="https://second.invalid")
        resilient.rotator = Mock()
        resilient.rotator._endpoints = [endpoint_one, endpoint_two]
        resilient.rotator._get_web3_for_url.side_effect = [first, second]

        result = resilient.find_transaction("0xabc")

        self.assertEqual(result, transaction)
        first.eth.get_transaction.assert_called_once_with("0xabc")
        second.eth.get_transaction.assert_called_once_with("0xabc")
        endpoint_two.record_success.assert_called_once()


if __name__ == "__main__":
    unittest.main()
