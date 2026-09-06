import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ops.fleet.probe_uniswap_route_matrix import build_variants, baseline_series, run_matrix


class ProbeUniswapRouteMatrixTests(unittest.TestCase):
    def setUp(self):
        self.body = {
            "tokenInChainId": 4663,
            "tokenOutChainId": 4663,
            "tokenIn": "0x0000000000000000000000000000000000000001",
            "tokenOut": "0x0000000000000000000000000000000000000002",
            "swapper": "0x0000000000000000000000000000000000000003",
            "amount": "100",
            "type": "EXACT_INPUT",
        }
        self.headers = {
            "x-universal-router-version": "2.1.1",
            "x-erc20eth-enabled": "true",
            "x-permit2-disabled": "true",
            "User-Agent": "curl/8.0",
            "Connection": "close",
            "Accept": "application/json",
        }

    def test_variants_change_only_one_baseline_dimension(self):
        variants = build_variants(self.body, self.headers, include_slippage=True)

        self.assertEqual([variant["name"] for variant in variants], [
            "baseline", "amm_protocols", "v2_only", "v3_only", "v4_only", "erc20eth_false", "erc20eth_omitted",
            "connection_omitted", "user_agent_omitted", "slippage_explicit",
        ])
        baseline = variants[0]
        for variant in variants[1:]:
            body_changes = {
                key for key in set(baseline["body"]) | set(variant["body"])
                if baseline["body"].get(key) != variant["body"].get(key)
            }
            header_changes = {
                key for key in set(baseline["headers"]) | set(variant["headers"])
                if baseline["headers"].get(key) != variant["headers"].get(key)
            }
            self.assertEqual(len(body_changes) + len(header_changes), 1, variant["name"])

    def test_baseline_series_keeps_one_exact_request_shape(self):
        series = baseline_series(build_variants(self.body, self.headers), rounds=4)

        self.assertEqual([item["name"] for item in series], [
            "baseline_round_1", "baseline_round_2", "baseline_round_3", "baseline_round_4",
        ])
        self.assertEqual({item["body"]["amount"] for item in series}, {"100"})
        self.assertEqual({item["headers"]["x-universal-router-version"] for item in series}, {"2.1.1"})

    def test_slippage_variant_omits_existing_baseline_slippage(self):
        body = {**self.body, "slippageTolerance": 2.0}

        variants = build_variants(body, self.headers, include_slippage=True)

        self.assertEqual(variants[-1]["name"], "slippage_omitted")
        self.assertNotIn("slippageTolerance", variants[-1]["body"])

    def test_matrix_output_redacts_addresses_and_amounts_in_error_detail(self):
        response = SimpleNamespace(
            status_code=400,
            headers={},
            json=lambda: {"errorCode": "Invalid", "detail": "token 0x0000000000000000000000000000000000000001 amount 1000000"},
            text="ignored",
        )
        output = io.StringIO()

        run_matrix(build_variants(self.body, self.headers)[:1], api_key="key", post=Mock(return_value=response),
                   sleep=lambda _: None, output=output, timeout_seconds=2)

        detail = json.loads(output.getvalue())["detail"]
        self.assertNotIn("0x0000000000000000000000000000000000000001", detail)
        self.assertNotIn("1000000", detail)

    def test_matrix_output_is_sanitized_and_stops_on_rate_limit(self):
        response = SimpleNamespace(
            status_code=429,
            headers={"x-request-id": "request-1", "Retry-After": "120"},
            json=lambda: {"errorCode": "RateLimited", "detail": "slow\n down"},
            text="ignored",
        )
        post = Mock(return_value=response)
        output = io.StringIO()

        count = run_matrix(
            build_variants(self.body, self.headers), api_key="super-secret-key",
            post=post, sleep=lambda _: None, output=output, timeout_seconds=2,
        )

        self.assertEqual(count, 1)
        self.assertEqual(post.call_count, 1)
        record = json.loads(output.getvalue())
        self.assertEqual(record["status"], 429)
        self.assertEqual(record["error_code"], "RateLimited")
        self.assertEqual(record["detail"], "slow down")
        self.assertNotIn("super-secret-key", output.getvalue())
        self.assertNotIn(self.body["tokenIn"], output.getvalue())
        self.assertNotIn(self.body["swapper"], output.getvalue())
        self.assertTrue(record["payload_fingerprint"].startswith("sha256:"))
        self.assertIsNone(record["routing"])

    def test_matrix_records_allowlisted_success_routing(self):
        response = SimpleNamespace(
            status_code=200, headers={}, json=lambda: {"routing": "CLASSIC"}, text="ignored",
        )
        output = io.StringIO()

        run_matrix(build_variants(self.body, self.headers)[:1], api_key="key", post=Mock(return_value=response),
                   sleep=lambda _: None, output=output, timeout_seconds=2)

        self.assertEqual(json.loads(output.getvalue())["routing"], "CLASSIC")


if __name__ == "__main__":
    unittest.main()
