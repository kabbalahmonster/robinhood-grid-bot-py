"""Tests for Sushi v7 response handling."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sushi_api import SushiAPIClient


def config():
    return SimpleNamespace(chain_id=4663, sushi_api_key="", anti_mev_jitter=False)


def response(status_code, payload, headers=None):
    item = Mock(status_code=status_code, headers=headers or {})
    item.json.return_value = payload
    return item


SUCCESS_QUOTE = {
    "status": "Success",
    "amountIn": "1000000000000000",
    "assumedAmountOut": "2000000",
    "gasSpent": 28082,
}


class TestSushiAPI(unittest.TestCase):
    def test_native_eth_zero_address_uses_sushi_sentinel(self):
        client = SushiAPIClient(config())
        params = client._params(
            "0x0000000000000000000000000000000000000000",
            "0xout",
            10**15,
            0.01,
        )
        self.assertEqual(params["tokenIn"], client.NATIVE_TOKEN_ADDRESS)

    @patch("sushi_api.requests.get")
    def test_quote_maps_exact_input_amounts(self, get):
        get.return_value = response(200, SUCCESS_QUOTE)
        result = SushiAPIClient(config()).get_quote("0xin", "0xout", sell_amount=10**15)
        self.assertTrue(result.success)
        self.assertEqual(result.sell_amount, 10**15)
        self.assertEqual(result.buy_amount, 2_000_000)
        self.assertEqual(result.price, 500_000_000)

    @patch("sushi_api.requests.get")
    def test_swap_maps_executable_transaction(self, get):
        swap = dict(SUCCESS_QUOTE, tx={
            "to": "0xrouter", "data": "0x1234", "value": "0",
            "gas": "250000", "gasPrice": 100,
        })
        get.side_effect = [response(200, SUCCESS_QUOTE), response(200, swap)]
        result = SushiAPIClient(config()).build_swap_transaction("0xin", "0xout", 10**15, "0xsender")
        self.assertTrue(result.success)
        self.assertEqual(result.allowance_target, "0xrouter")
        self.assertEqual(result.data, "0x1234")
        self.assertEqual(result.gas, 250000)

    @patch("sushi_api.requests.get")
    def test_insufficient_allowance_returns_approval_handshake(self, get):
        allowance = {"code": "422-04", "detail": "Insufficient allowance", "spender": "0xspender"}
        get.side_effect = [response(200, SUCCESS_QUOTE), response(422, allowance)]
        result = SushiAPIClient(config()).build_swap_transaction("0xin", "0xout", 10**15, "0xsender")
        self.assertTrue(result.success)
        self.assertEqual(result.allowance_target, "0xspender")
        self.assertIsNone(result.data)

    def test_exact_output_is_rejected_cleanly(self):
        result = SushiAPIClient(config()).get_quote("0xin", "0xout", buy_amount=100)
        self.assertFalse(result.success)
        self.assertIn("exact-input", result.error)

    @patch("sushi_api.time.time", return_value=1000)
    @patch("sushi_api.requests.get")
    def test_rate_limit_respects_retry_after_and_skips_requests(self, get, now):
        get.return_value = response(429, {"detail": "slow down"}, {"Retry-After": "120"})
        client = SushiAPIClient(config())

        first = client.get_quote("0xin", "0xout", sell_amount=10**15)
        second = client.get_quote("0xin", "0xout", sell_amount=10**15)

        self.assertFalse(first.success)
        self.assertIn("retry in 120s", first.error)
        self.assertFalse(second.success)
        self.assertIn("cooldown active", second.error)
        self.assertEqual(get.call_count, 1)

    @patch("sushi_api.random.uniform", return_value=1.0)
    @patch("sushi_api.time.time", return_value=1000)
    @patch("sushi_api.requests.get")
    def test_rate_limit_uses_exponential_fallback(self, get, now, uniform):
        get.return_value = response(429, {"detail": "slow down"})
        client = SushiAPIClient(config())
        client.get_quote("0xin", "0xout", sell_amount=10**15)
        self.assertEqual(client._rate_limit_until, 1030)

    @patch("sushi_api.time.time", side_effect=[1000, 1031])
    @patch("sushi_api.requests.get")
    def test_success_after_cooldown_resets_backoff(self, get, now):
        get.side_effect = [
            response(429, {"detail": "slow down"}, {"Retry-After": "30"}),
            response(200, SUCCESS_QUOTE),
        ]
        client = SushiAPIClient(config())
        client.get_quote("0xin", "0xout", sell_amount=10**15)
        recovered = client.get_quote("0xin", "0xout", sell_amount=10**15)
        self.assertTrue(recovered.success)
        self.assertEqual(client._rate_limit_strikes, 0)
        self.assertEqual(client._rate_limit_until, 0)


if __name__ == "__main__":
    unittest.main()
