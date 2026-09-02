import unittest
import tempfile
import json
from types import SimpleNamespace
from unittest.mock import patch

from uniswap_api import UniswapAPIClient


class TestUniswapAPIClient(unittest.TestCase):
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
