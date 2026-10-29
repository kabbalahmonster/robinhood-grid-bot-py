"""Native Abyss DEX provider for Robinhood Chain.

Routes are discovered through Abyss's public candidate API, priced by the
deployed BatchQuoter, and encoded for the pinned Router V2.  The API can only
suggest pool keys: the on-chain quoter and the bot's normal local simulation
remain authoritative.
"""

from dataclasses import dataclass
import logging
import time
from typing import Optional

import requests
from web3 import Web3

from utils import apply_jitter


@dataclass
class QuoteResult:
    success: bool
    price: Optional[float] = None
    buy_amount: Optional[int] = None
    sell_amount: Optional[int] = None
    allowance_target: Optional[str] = None
    data: Optional[str] = None
    to: Optional[str] = None
    value: Optional[int] = None
    gas: Optional[int] = None
    gas_price: Optional[int] = None
    raw_response: Optional[dict] = None
    error: Optional[str] = None


_POOL_KEY_COMPONENTS = [
    {"name": "token0", "type": "address"},
    {"name": "token1", "type": "address"},
    {"name": "profile", "type": "uint8"},
    {"name": "fee", "type": "uint24"},
    {"name": "quoteIsToken0", "type": "bool"},
    {"name": "oracleConfigId", "type": "bytes32"},
]
_PATH_COMPONENTS = [
    {"name": "key", "type": "tuple", "components": _POOL_KEY_COMPONENTS},
    {"name": "tokenIn", "type": "address"},
    {"name": "sqrtPriceLimitX96", "type": "uint160"},
]
_EXACT_INPUT_COMPONENTS = [
    {"name": "path", "type": "tuple[]", "components": _PATH_COMPONENTS},
    {"name": "recipient", "type": "address"},
    {"name": "amountIn", "type": "uint256"},
    {"name": "amountOutMinimum", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]
_BATCH_QUOTER_ABI = [{
    "type": "function", "name": "quoteExactInputs", "stateMutability": "nonpayable",
    "inputs": [{
        "name": "requests", "type": "tuple[]", "components": [
            {"name": "path", "type": "tuple[]", "components": _PATH_COMPONENTS},
            {"name": "amountIn", "type": "uint256"},
        ],
    }],
    "outputs": [{
        "name": "results", "type": "tuple[]", "components": [
            {"name": "success", "type": "bool"},
            {"name": "amountOut", "type": "uint256"},
            {"name": "errorSelector", "type": "bytes4"},
        ],
    }],
}]
_ROUTER_ABI = [{
    "type": "function", "name": name, "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": _EXACT_INPUT_COMPONENTS}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
} for name in ("exactInput", "exactInputFromETH", "exactInputToETH")]


class AbyssAPIClient:
    """Quote and execute exact-input routes through Abyss Router V2."""

    API_URL = "https://api.abyss.trading"
    ROUTER = "0xF2a3Afb36768950eb2c7F04583328C09AEC0C366"
    BATCH_QUOTER = "0x233A672a26529849ed818017D350d97445616dB8"
    NATIVE = "0x0000000000000000000000000000000000000000"
    # Keep one quote bounded for tournament deadlines and public-RPC gas caps.
    # The API ranks direct/short routes first; 16 still captures alternatives
    # while avoiding the out-of-gas behavior seen with its full 1024-route set.
    MAX_CANDIDATES = 16
    MAX_RESPONSE_BYTES = 1_000_000

    def __init__(self, config):
        if int(config.chain_id) != 4663:
            raise ValueError("Abyss provider supports Robinhood Chain 4663 only")
        self.config = config
        self.chain_id = int(config.chain_id)
        self.weth = Web3.to_checksum_address(config.weth_address)
        self.api_url = str(getattr(config, "abyss_api_url", self.API_URL) or self.API_URL).rstrip("/")
        self.rpc_urls = list(getattr(config, "rpc_urls", None) or [config.rpc_url])
        self.logger = logging.getLogger("grid_bot.abyss_api")

    @classmethod
    def _is_native(cls, token):
        return str(token).lower() == cls.NATIVE

    def _pool_token(self, token):
        return self.weth if self._is_native(token) else Web3.to_checksum_address(token)

    @staticmethod
    def _remaining(deadline):
        return max(0.05, deadline - time.monotonic())

    def _candidate_paths(self, sell_token, buy_token, sell_amount, timeout):
        response = requests.get(
            f"{self.api_url}/api/v1/quote/candidates",
            params={
                "chainId": self.chain_id,
                "tokenIn": sell_token.lower(),
                "tokenOut": buy_token.lower(),
                "amountIn": str(sell_amount),
            },
            headers={"Accept": "application/json"}, timeout=max(0.05, timeout),
        )
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            raise ValueError("Abyss candidate response is too large")
        try:
            body = response.json()
        except ValueError as exc:
            raise ValueError("Abyss candidate API returned invalid JSON") from exc
        if response.status_code != 200:
            detail = body.get("error") if isinstance(body, dict) else None
            raise ValueError(f"Abyss candidate API returned status {response.status_code}: {detail or 'unknown error'}")
        if not isinstance(body, dict) or int(body.get("chainId", 0)) != self.chain_id:
            raise ValueError("Abyss candidate response has the wrong chain")
        if str(body.get("amountIn")) != str(sell_amount):
            raise ValueError("Abyss candidate response changed the input amount")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("Abyss found no connected route")

        paths = []
        expected_in = sell_token.lower()
        expected_out = buy_token.lower()
        for candidate in candidates[:self.MAX_CANDIDATES]:
            if not isinstance(candidate, list) or not 1 <= len(candidate) <= 4:
                continue
            path = []
            current = expected_in
            try:
                for hop in candidate:
                    key = hop["key"]
                    token0 = Web3.to_checksum_address(key["token0"])
                    token1 = Web3.to_checksum_address(key["token1"])
                    token_in = Web3.to_checksum_address(hop["tokenIn"])
                    token_out = Web3.to_checksum_address(hop["tokenOut"])
                    if token_in.lower() != current or {token_in.lower(), token_out.lower()} != {
                        token0.lower(), token1.lower()
                    }:
                        raise ValueError("disconnected path")
                    profile = int(key["profile"])
                    fee = int(key["fee"])
                    limit = int(hop["sqrtPriceLimitX96"])
                    oracle = str(key["oracleConfigId"])
                    if not 0 <= profile <= 3 or not 0 <= fee < 2**24 or not 0 < limit < 2**160:
                        raise ValueError("invalid pool key")
                    if len(oracle) != 66 or not oracle.startswith("0x"):
                        raise ValueError("invalid oracle id")
                    key_tuple = (token0, token1, profile, fee, bool(key["quoteIsToken0"]), bytes.fromhex(oracle[2:]))
                    path.append((key_tuple, token_in, limit))
                    current = token_out.lower()
                if current != expected_out:
                    raise ValueError("wrong output token")
            except (KeyError, TypeError, ValueError):
                continue
            paths.append(path)
        if not paths:
            raise ValueError("Abyss found no valid connected route")
        return paths

    def _quote_paths(self, paths, amount, timeout):
        last_error = None
        deadline = time.monotonic() + timeout
        for index, rpc_url in enumerate(self.rpc_urls):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Abyss quote deadline elapsed")
                per_rpc = max(0.05, remaining / max(1, len(self.rpc_urls) - index))
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": per_rpc}))
                quoter = w3.eth.contract(
                    address=Web3.to_checksum_address(self.BATCH_QUOTER), abi=_BATCH_QUOTER_ABI,
                )
                requests_ = [(path, amount) for path in paths]
                results = quoter.functions.quoteExactInputs(requests_).call({"from": Web3.to_checksum_address("0x0000000000000000000000000000000000000001")})
                successful = [
                    (int(result[1]), paths[i]) for i, result in enumerate(results)
                    if i < len(paths) and bool(result[0]) and int(result[1]) > 0
                ]
                if successful:
                    return max(successful, key=lambda item: (item[0], -len(item[1])))
                raise ValueError("Abyss quoter found no executable route")
            except Exception as exc:  # RPC providers expose several error classes.
                last_error = exc
        raise ValueError(f"Abyss on-chain quote failed: {last_error}")

    def _encode_swap(self, path, sell_token, buy_token, sell_amount, buy_amount,
                     taker_address, slippage_percentage):
        native_in = self._is_native(sell_token)
        native_out = self._is_native(buy_token)
        if native_in and native_out:
            raise ValueError("Abyss swap cannot use native ETH on both sides")
        method = "exactInputFromETH" if native_in else ("exactInputToETH" if native_out else "exactInput")
        slippage = float(slippage_percentage or 0)
        if not 0 <= slippage < 1:
            raise ValueError("Abyss slippage must be at least 0 and below 1")
        slippage_bps = min(9_999, max(0, int(round(slippage * 10_000))))
        minimum = int(buy_amount) * (10_000 - slippage_bps) // 10_000
        deadline = int(time.time()) + 120
        params = (
            path, Web3.to_checksum_address(taker_address), int(sell_amount),
            minimum, deadline,
        )
        contract = Web3().eth.contract(
            address=Web3.to_checksum_address(self.ROUTER), abi=_ROUTER_ABI,
        )
        data = contract.get_function_by_name(method)(params)._encode_transaction_data()
        return data, int(sell_amount) if native_in else 0, minimum, deadline

    def get_quote(self, sell_token, buy_token, sell_amount=None, buy_amount=None,
                  taker_address=None, slippage_percentage=0.01,
                  apply_jitter_to_price=True, quote_timeout_seconds=None, **_kwargs):
        if not sell_amount:
            return QuoteResult(False, error="Abyss supports exact-input sell_amount quotes only")
        if not taker_address:
            return QuoteResult(False, error="Abyss quotes require a taker address")
        timeout = 30.0 if quote_timeout_seconds is None else max(0.1, float(quote_timeout_seconds))
        deadline = time.monotonic() + timeout
        try:
            pool_sell = self._pool_token(sell_token)
            pool_buy = self._pool_token(buy_token)
            if pool_sell.lower() == pool_buy.lower():
                raise ValueError("Abyss route has identical input and output tokens")
            paths = self._candidate_paths(
                pool_sell, pool_buy, int(sell_amount), min(self._remaining(deadline), timeout * 0.45),
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Abyss quote deadline elapsed")
            amount_out, path = self._quote_paths(paths, int(sell_amount), remaining)
            data, value, minimum, expires = self._encode_swap(
                path, sell_token, buy_token, int(sell_amount), amount_out,
                taker_address, slippage_percentage,
            )
        except Exception as exc:
            return QuoteResult(False, error=str(exc))
        price = int(sell_amount) / amount_out
        if apply_jitter_to_price and self.config.anti_mev_jitter:
            price = apply_jitter(price, jitter_percent=0.05)
        return QuoteResult(
            True, price=price, buy_amount=amount_out, sell_amount=int(sell_amount),
            allowance_target=self.ROUTER, data=data, to=self.ROUTER, value=value,
            gas=None, raw_response={
                "path": path, "amountOut": amount_out,
                "amountOutMinimum": minimum, "deadline": expires,
            },
        )

    def get_swap_transaction(self, quote, **_kwargs):
        return quote

    def build_swap_transaction(self, sell_token, buy_token, sell_amount, taker_address,
                               slippage_percentage=0.01, quote_timeout_seconds=None):
        return self.get_quote(
            sell_token, buy_token, sell_amount=sell_amount,
            taker_address=taker_address, slippage_percentage=slippage_percentage,
            apply_jitter_to_price=False, quote_timeout_seconds=quote_timeout_seconds,
        )

    def refresh_quote(self, sell_token, buy_token, sell_amount, taker_address,
                      slippage_percentage=0.01):
        return self.build_swap_transaction(
            sell_token, buy_token, sell_amount, taker_address, slippage_percentage,
        )

    def get_price(self, sell_token, buy_token, sell_amount):
        # Price polling has no wallet context; use a harmless nonzero recipient.
        quote = self.get_quote(
            sell_token, buy_token, sell_amount=sell_amount,
            taker_address="0x0000000000000000000000000000000000000001",
            apply_jitter_to_price=False,
        )
        return quote.price if quote.success else None
