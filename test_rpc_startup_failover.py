import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rpc_rotator import RPCEndpoint, ResilientWeb3, create_web3
from wallet import Wallet


class TestRPCStartupFailover(unittest.TestCase):
    def test_endpoint_display_url_does_not_expose_credentials_or_api_path(self):
        endpoint = RPCEndpoint(
            "https://username:password@rpc.example/private-api-key"
        )

        self.assertEqual(endpoint.display_url, "rpc.example")

    @staticmethod
    def resilient_with(candidates):
        resilient = ResilientWeb3.__new__(ResilientWeb3)
        endpoints = []
        clients = {}
        for index, candidate in enumerate(candidates, start=1):
            url = f"https://rpc-{index}.invalid"
            endpoint = Mock(url=url, display_url=f"rpc-{index}.invalid")
            endpoints.append(endpoint)
            candidate.provider.endpoint_uri = url
            clients[url] = candidate
        resilient.rotator = Mock()
        resilient.rotator._endpoints = endpoints
        resilient.rotator._get_web3_for_url.side_effect = clients.__getitem__
        resilient.rotator._lock = threading.Lock()
        resilient.rotator._current_index = 1
        resilient._w3 = candidates[0]
        resilient._current_url = endpoints[0].url
        return resilient, endpoints

    def test_startup_selects_next_reachable_endpoint(self):
        first = Mock()
        first.is_connected.return_value = False
        second = Mock()
        second.is_connected.side_effect = OSError("connection refused")
        third = Mock()
        third.is_connected.return_value = True
        resilient, endpoints = self.resilient_with([first, second, third])

        self.assertTrue(resilient.is_connected())

        self.assertIs(resilient._w3, third)
        self.assertEqual(resilient._current_url, endpoints[2].url)
        self.assertEqual(resilient.rotator._current_index, 0)
        endpoints[0].record_failure.assert_called_once()
        endpoints[1].record_failure.assert_called_once()
        endpoints[2].record_success.assert_called_once()

    def test_startup_returns_false_only_after_every_endpoint_fails(self):
        first = Mock()
        first.is_connected.return_value = False
        second = Mock()
        second.is_connected.return_value = False
        resilient, endpoints = self.resilient_with([first, second])

        self.assertFalse(resilient.is_connected())

        endpoints[0].record_failure.assert_called_once()
        endpoints[1].record_failure.assert_called_once()

    @patch("rpc_rotator.ResilientWeb3")
    def test_single_rpc_urls_entry_does_not_fall_back_to_legacy_rpc_url(
            self, resilient_class):
        config = SimpleNamespace(
            chain_id=4663,
            rpc_urls=["https://public.invalid"],
            rpc_url="https://legacy-secret.invalid/key",
        )

        result = create_web3(config)

        self.assertIs(result, resilient_class.return_value)
        resilient_class.assert_called_once_with(
            chain_id=4663, rpc_urls=["https://public.invalid"],
        )

    @patch("wallet.create_web3")
    def test_wallet_pool_connection_error_does_not_report_legacy_url(self, create):
        create.return_value.is_connected.return_value = False
        config = SimpleNamespace(
            rpc_urls=["https://one.invalid", "https://two.invalid"],
            rpc_url="https://secret.example/private-api-key",
        )

        with self.assertRaisesRegex(
                ConnectionError, r"configured RPC_URLS pool \(2 endpoints\)") as caught:
            Wallet(config)

        self.assertNotIn("secret.example", str(caught.exception))
        self.assertNotIn("private-api-key", str(caught.exception))

    @patch("wallet.create_web3")
    def test_wallet_single_connection_error_reports_host_without_secret_path(
            self, create):
        create.return_value.is_connected.return_value = False
        config = SimpleNamespace(
            rpc_urls=None,
            rpc_url="https://rpc.example/private-api-key",
        )

        with self.assertRaisesRegex(ConnectionError, "RPC_URL host rpc.example") as caught:
            Wallet(config)

        self.assertNotIn("private-api-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
