import unittest
import tempfile
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
        payload = post.call_args.kwargs["json"]
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


if __name__ == "__main__":
    unittest.main()
