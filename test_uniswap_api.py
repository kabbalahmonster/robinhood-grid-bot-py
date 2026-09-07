import unittest
import uniswap_api
import tempfile
import json
from types import SimpleNamespace
from unittest.mock import patch

from uniswap_api import UniswapAPIClient


class TestUniswapAPIClient(unittest.TestCase):
    def test_protocol_hint_lookup_does_not_evict_expired_execution_cache_entry(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key", uniswap_permit2_disabled=True,
            chain_id=4663, anti_mev_jitter=False,
        )
        client = UniswapAPIClient(config)
        payload = {
            "tokenInChainId": 4663, "tokenOutChainId": 4663,
            "tokenIn": "0xin", "tokenOut": "0xout", "type": "EXACT_INPUT",
        }
        key = client._protocol_cache_key(payload)
        client._protocol_cache[key] = ("V4", 0)

        assert client.protocol_hint_for("0xin", "0xout") is None
        assert client._protocol_cache[key] == ("V4", 0)

    def test_successful_hinted_response_after_deadline_is_rejected(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key", uniswap_permit2_disabled=True,
            chain_id=4663, anti_mev_jitter=False,
        )
        response = SimpleNamespace(
            status_code=200, text="", headers={},
            json=lambda: {"quote": {"input": {"amount": "100"}, "output": {"amount": "95"}}, "tx": {}},
        )
        client = UniswapAPIClient(config)

        with patch("uniswap_api.time.monotonic", side_effect=[100.0, 100.0, 101.0, 101.0]), \
                patch.object(client, "_post_json", return_value=response):
            result = client.get_quote(
                sell_token="0xin", buy_token="0xout", sell_amount=100,
                taker_address="0xtaker", preferred_protocol="V4",
                quote_timeout_seconds=0.5,
            )

        assert not result.success
        assert result.error == "shadow quote deadline elapsed"

    def test_read_only_protocol_hint_starts_with_known_capability(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        response = SimpleNamespace(
            status_code=200,
            text="",
            headers={},
            json=lambda: {
                "quote": {
                    "input": {"amount": "100"},
                    "output": {"amount": "95"},
                },
                "tx": {},
            },
        )

        with patch("uniswap_api.requests.post", return_value=response) as post:
            result = UniswapAPIClient(config).get_quote(
                sell_token="0x0000000000000000000000000000000000000001",
                buy_token="0x0000000000000000000000000000000000000002",
                sell_amount=100,
                taker_address="0x0000000000000000000000000000000000000003",
                preferred_protocol="V4",
            )

        self.assertTrue(result.success)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(json.loads(post.call_args.kwargs["data"])["protocols"], ["V4"])

    def test_stale_read_only_protocol_hint_falls_back_to_default_routing(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        no_route = SimpleNamespace(status_code=404, text="NoRouteFoundError", headers={})
        success = SimpleNamespace(
            status_code=200, text="", headers={},
            json=lambda: {"quote": {"input": {"amount": "100"}, "output": {"amount": "95"}}, "tx": {}},
        )

        with patch("uniswap_api.requests.post", side_effect=[no_route, success]) as post:
            result = UniswapAPIClient(config).get_quote(
                sell_token="0x0000000000000000000000000000000000000001",
                buy_token="0x0000000000000000000000000000000000000002",
                sell_amount=100,
                taker_address="0x0000000000000000000000000000000000000003",
                preferred_protocol="V4",
                protocol_probe_limit=0,
            )

        self.assertTrue(result.success)
        payloads = [json.loads(call.kwargs["data"]) for call in post.call_args_list]
        self.assertEqual(payloads[0]["protocols"], ["V4"])
        self.assertNotIn("protocols", payloads[1])

    def test_failed_read_only_protocol_hint_retries_default_routing_when_budget_remains(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        success = SimpleNamespace(
            status_code=200, text="", headers={},
            json=lambda: {"quote": {"input": {"amount": "100"}, "output": {"amount": "95"}}, "tx": {}},
        )

        with patch("uniswap_api.requests.post", side_effect=[
            uniswap_api.requests.ConnectionError("transient"), success,
        ]) as post:
            result = UniswapAPIClient(config).get_quote(
                sell_token="0x0000000000000000000000000000000000000001",
                buy_token="0x0000000000000000000000000000000000000002",
                sell_amount=100,
                taker_address="0x0000000000000000000000000000000000000003",
                preferred_protocol="V4",
                quote_timeout_seconds=5,
            )

        self.assertTrue(result.success)
        payloads = [json.loads(call.kwargs["data"]) for call in post.call_args_list]
        self.assertEqual(payloads[0]["protocols"], ["V4"])
        self.assertNotIn("protocols", payloads[1])

    def test_slippage_is_normalized_to_two_decimal_places(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        response = SimpleNamespace(
            status_code=200,
            text="",
            headers={},
            json=lambda: {
                "quote": {
                    "input": {"amount": "100"},
                    "output": {"amount": "95"},
                },
                "tx": {},
            },
        )

        with patch("uniswap_api.requests.post", return_value=response) as post:
            result = UniswapAPIClient(config).get_quote(
                sell_token="0x0000000000000000000000000000000000000001",
                buy_token="0x0000000000000000000000000000000000000002",
                sell_amount=100,
                taker_address="0x0000000000000000000000000000000000000003",
                slippage_percentage=0.05 + 0.02,
            )

        self.assertTrue(result.success)
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(payload["slippageTolerance"], 7.0)
        self.assertEqual(len(str(payload["slippageTolerance"]).split(".")[1]), 1)

    def test_429_starts_shared_cooldown_and_skips_next_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                uniswap_api_key="test-key",
                uniswap_permit2_disabled=True,
                chain_id=4663,
                anti_mev_jitter=False,
                uniswap_rate_state_file=f"{directory}/rate.json",
                uniswap_rate_limit_rps=4,
                uniswap_cooldown_base_seconds=30,
                uniswap_cooldown_max_seconds=900,
            )
            response = SimpleNamespace(
                status_code=429,
                text='{"message":"Too Many Requests"}',
                headers={"Retry-After": "120"},
            )
            with patch("uniswap_api.requests.post", return_value=response) as post, patch(
                "shared_rate_limit.random.uniform", return_value=0
            ):
                client = UniswapAPIClient(config)
                first = client.get_quote("0xin", "0xout", sell_amount=100, taker_address="0xtaker")
                second = UniswapAPIClient(config).get_quote(
                    "0xin", "0xout", sell_amount=100, taker_address="0xtaker"
                )
            self.assertIn("status 429", first.error)
            self.assertIn("cooldown active", second.error)
            self.assertEqual(post.call_count, 1)

    def test_gateway_packet_409_retries_without_starting_shared_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                uniswap_api_key="test-key",
                uniswap_permit2_disabled=True,
                chain_id=4663,
                anti_mev_jitter=False,
                uniswap_rate_state_file=f"{directory}/rate.json",
                uniswap_rate_limit_rps=4,
                uniswap_cooldown_base_seconds=30,
                uniswap_cooldown_max_seconds=900,
            )
            response = SimpleNamespace(
                status_code=409,
                text='{"error":"client packet length exceeds 255 buffer"}',
                headers={},
            )
            with patch("uniswap_api.requests.post", return_value=response) as post, patch(
                "shared_rate_limit.random.uniform", return_value=0
            ):
                first = UniswapAPIClient(config).get_quote(
                    "0xin", "0xout", sell_amount=100, taker_address="0xtaker"
                )
                second = UniswapAPIClient(config).get_quote(
                    "0xin", "0xout", sell_amount=100, taker_address="0xtaker"
                )

            self.assertIn("status 409", first.error)
            self.assertIn("status 409", second.error)
            self.assertNotIn("cooldown active", second.error)
            self.assertEqual(post.call_count, 4)

    def test_gateway_packet_409_succeeds_on_one_fresh_retry(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        failed = SimpleNamespace(
            status_code=409,
            text='{"error":"client packet length exceeds 255 buffer"}',
            headers={},
        )
        recovered = SimpleNamespace(
            status_code=200,
            text="",
            headers={"x-request-id": "recovered"},
            json=lambda: {
                "quote": {
                    "input": {"amount": "100"},
                    "output": {"amount": "95"},
                },
                "tx": {},
            },
        )

        with patch("uniswap_api.requests.post", side_effect=[failed, recovered]) as post:
            result = UniswapAPIClient(config).get_quote(
                "0xin", "0xout", sell_amount=100, taker_address="0xtaker"
            )

        self.assertTrue(result.success)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["User-Agent"], "curl/8.0")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["User-Agent"], "curl/8.0")
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["Connection"], "close")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Connection"], "close")

    def test_post_json_does_not_extend_sub_50ms_deadline(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key", uniswap_permit2_disabled=True,
            chain_id=4663, anti_mev_jitter=False,
        )
        packet_failure = SimpleNamespace(
            status_code=409, text='{"error":"packet length exceeds buffer"}', headers={},
        )
        client = UniswapAPIClient(config)
        with patch("uniswap_api.requests.post", return_value=packet_failure) as post, \
             patch("uniswap_api.time.monotonic", side_effect=[0.0, 0.0, 0.0, 0.02, 0.02, 0.02, 0.02]):
            with self.assertRaises(uniswap_api.requests.Timeout):
                client._post_json("swap", {"quote": {}}, timeout_seconds=0.01)

        self.assertEqual(post.call_count, 1)

    def test_post_json_does_not_retry_past_supplied_deadline(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key", uniswap_permit2_disabled=True,
            chain_id=4663, anti_mev_jitter=False,
        )
        packet_failure = SimpleNamespace(
            status_code=409, text='{"error":"packet length exceeds buffer"}', headers={},
        )
        client = UniswapAPIClient(config)
        with patch("uniswap_api.requests.post", return_value=packet_failure) as post, \
             patch("uniswap_api.time.monotonic", side_effect=[0.0, 0.0, 0.0, 0.0, 0.4, 0.6]):
            with self.assertRaises(uniswap_api.requests.Timeout):
                client._post_json("swap", {"quote": {}}, timeout_seconds=0.5)

        self.assertEqual(post.call_count, 1)

    def test_swap_preparation_respects_supplied_deadline(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key", uniswap_permit2_disabled=True,
            chain_id=4663, anti_mev_jitter=False,
        )
        response = SimpleNamespace(
            status_code=200, text="", headers={},
            json=lambda: {
                "quote": {"input": {"amount": "100"}, "output": {"amount": "95"}},
                "swap": {"to": "0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a", "data": "0xdead", "value": "0x0"},
            },
        )
        client = UniswapAPIClient(config)
        with patch.object(client, "_post_json", return_value=response) as post:
            result = client.get_swap_transaction({"quote": {}}, quote_timeout_seconds=0.5)

        self.assertTrue(result.success)
        self.assertGreater(post.call_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(post.call_args.kwargs["timeout_seconds"], 0.5)

    def test_shadow_read_timeout_has_explicit_observation_deadline_error(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        with patch("uniswap_api.requests.post", side_effect=uniswap_api.requests.ReadTimeout("read timed out")):
            result = UniswapAPIClient(config).get_quote(
                "0xin", "0xout", sell_amount=100, taker_address="0xtaker",
                quote_timeout_seconds=0.5,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "shadow quote deadline elapsed")

    def test_no_route_discovers_v3_then_reuses_cached_protocol(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        no_route = SimpleNamespace(
            status_code=404,
            text='{"errorCode":"NoRouteFoundError","detail":"No route with sufficient liquidity"}',
            headers={},
        )
        recovered = SimpleNamespace(
            status_code=200,
            text="",
            headers={},
            json=lambda: {
                "quote": {"input": {"amount": "100"}, "output": {"amount": "95"}},
                "tx": {},
            },
        )

        client = UniswapAPIClient(config)
        with patch("uniswap_api.requests.post", side_effect=[no_route, no_route, recovered, recovered]) as post:
            first = client.get_quote("0xin", "0xout", sell_amount=100, taker_address="0xtaker")
            second = client.get_quote("0xin", "0xout", sell_amount=100, taker_address="0xtaker")

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(post.call_count, 4)
        payloads = [json.loads(call.kwargs["data"]) for call in post.call_args_list]
        self.assertNotIn("protocols", payloads[0])
        self.assertEqual(payloads[1]["protocols"], ["V4"])
        self.assertEqual(payloads[2]["protocols"], ["V3"])
        self.assertEqual(payloads[3]["protocols"], ["V3"])

    def test_no_route_discovers_first_available_protocol(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        no_route = SimpleNamespace(
            status_code=404,
            text='{"errorCode":"NoRouteFoundError","detail":"No route with sufficient liquidity"}',
            headers={},
        )
        recovered = SimpleNamespace(
            status_code=200,
            text="",
            headers={},
            json=lambda: {
                "quote": {
                    "input": {"amount": "100"},
                    "output": {"amount": "95"},
                },
                "tx": {},
            },
        )

        with patch("uniswap_api.requests.post", side_effect=[no_route, recovered]) as post:
            result = UniswapAPIClient(config).get_quote(
                "0xin", "0xout", sell_amount=100, taker_address="0xtaker"
            )

        self.assertTrue(result.success)
        self.assertEqual(post.call_count, 2)
        first_payload = json.loads(post.call_args_list[0].kwargs["data"])
        retry_payload = json.loads(post.call_args_list[1].kwargs["data"])
        self.assertNotIn("protocols", first_payload)
        self.assertEqual(retry_payload["protocols"], ["V4"])

    def test_non_route_404_does_not_trigger_amm_retry(self):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        response = SimpleNamespace(
            status_code=404,
            text='{"detail":"unknown token metadata"}',
            headers={},
        )

        with patch("uniswap_api.requests.post", return_value=response) as post:
            result = UniswapAPIClient(config).get_quote(
                "0xin", "0xout", sell_amount=100, taker_address="0xtaker"
            )

        self.assertFalse(result.success)
        self.assertEqual(post.call_count, 1)

    @patch("uniswap_api.time.sleep")
    def test_actionable_quote_retries_upstream_routing_timeout(self, sleep):
        config = SimpleNamespace(
            uniswap_api_key="test-key",
            uniswap_permit2_disabled=True,
            chain_id=4663,
            anti_mev_jitter=False,
        )
        timed_out = SimpleNamespace(
            status_code=404,
            text='{"errorCode":"UpstreamTimeoutError","detail":"A routing dependency timed out or failed; the request may succeed on retry."}',
            headers={},
        )
        recovered = SimpleNamespace(
            status_code=200,
            text="",
            headers={},
            json=lambda: {
                "quote": {
                    "input": {"amount": "100"},
                    "output": {"amount": "95"},
                },
                "tx": {},
            },
        )

        with patch("uniswap_api.requests.post", side_effect=[timed_out, recovered]) as post:
            result = UniswapAPIClient(config).build_swap_transaction(
                "0xin", "0xout", 100, "0xtaker"
            )

        self.assertTrue(result.success)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.75)

    def test_shared_gate_covers_approval_and_swap_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                uniswap_api_key="test-key",
                uniswap_permit2_disabled=True,
                chain_id=4663,
                anti_mev_jitter=False,
                uniswap_rate_state_file=f"{directory}/rate.json",
                uniswap_rate_limit_rps=4,
                uniswap_cooldown_base_seconds=30,
                uniswap_cooldown_max_seconds=900,
            )
            limited = SimpleNamespace(
                status_code=429,
                text='{"message":"Too Many Requests"}',
                headers={"Retry-After": "120"},
            )
            with patch("uniswap_api.requests.post", return_value=limited) as post, patch(
                "shared_rate_limit.random.uniform", return_value=0
            ):
                client = UniswapAPIClient(config)
                approval = client.check_approval("0xtoken", 100, "0xwallet")
                swap = client.get_swap_transaction({"quote": {}})
            self.assertIn("429", approval["error"])
            self.assertIn("cooldown active", swap.error)
            self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
