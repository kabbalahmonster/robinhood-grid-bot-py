"""Sushi v7 aggregator client.

Implements the bot's common quote/swap interface using Sushi's public API.
Exact-input quotes are used throughout the trading engine. An optional API key
can be supplied for higher service limits.

Docs: https://docs.sushi.com/api/examples/quote
      https://docs.sushi.com/api/examples/swap
"""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import logging
import random
import time
from typing import Optional

import requests

from config import BotConfig
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


class SushiAPIClient:
    """Quote and prepare exact-input swaps through Sushi's v7 API."""

    BASE_URL = "https://api.sushi.com"
    NATIVE_TOKEN_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
    RATE_LIMIT_BASE_SECONDS = 30
    RATE_LIMIT_MAX_SECONDS = 15 * 60

    def __init__(self, config: BotConfig):
        self.config = config
        self.chain_id = config.chain_id
        self.api_key = getattr(config, "sushi_api_key", "")
        self.logger = logging.getLogger("grid_bot.sushi_api")
        self._rate_limit_until = 0.0
        self._rate_limit_strikes = 0
        self.logger.info(f"Sushi API Client initialized for chain {self.chain_id}")

    @staticmethod
    def _parse_int(value, default=0):
        if value is None or value == "":
            return default
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)

    def _params(self, sell_token, buy_token, sell_amount, slippage_percentage):
        # The trading engine uses the zero address for native ETH because that
        # is Uniswap's convention. Sushi's API uses the Eeee... sentinel.
        sell_token = self._sushi_token_address(sell_token)
        buy_token = self._sushi_token_address(buy_token)
        params = {
            "tokenIn": sell_token,
            "tokenOut": buy_token,
            "amount": str(sell_amount),
            "maxSlippage": str(slippage_percentage if slippage_percentage is not None else 0.01),
        }
        if self.api_key:
            params["apiKey"] = self.api_key
        return params

    @classmethod
    def _sushi_token_address(cls, token_address):
        if str(token_address).lower() == "0x0000000000000000000000000000000000000000":
            return cls.NATIVE_TOKEN_ADDRESS
        return token_address

    def _request(self, endpoint, params):
        now = time.time()
        if now < self._rate_limit_until:
            remaining = max(1, int(self._rate_limit_until - now + 0.999))
            return 429, {"detail": f"Sushi rate-limit cooldown active; retry in {remaining}s"}

        try:
            response = requests.get(
                f"{self.BASE_URL}/{endpoint}/v7/{self.chain_id}",
                params=params,
                timeout=30,
            )
            try:
                data = response.json()
            except ValueError:
                data = {"detail": response.text[:500]}

            if response.status_code == 429:
                self._rate_limit_strikes += 1
                delay = self._retry_after_seconds(response.headers, now)
                if delay is None:
                    exponential = min(
                        self.RATE_LIMIT_BASE_SECONDS * (2 ** (self._rate_limit_strikes - 1)),
                        self.RATE_LIMIT_MAX_SECONDS,
                    )
                    delay = exponential * random.uniform(0.9, 1.1)
                self._rate_limit_until = max(self._rate_limit_until, now + max(1, delay))
                wait_seconds = max(1, int(self._rate_limit_until - now + 0.999))
                self.logger.warning(f"Sushi rate limited; pausing API requests for {wait_seconds}s")
                data = {"detail": f"Sushi rate limited; retry in {wait_seconds}s"}
            elif 200 <= response.status_code < 300:
                self._rate_limit_strikes = 0
                self._rate_limit_until = 0.0
            return response.status_code, data
        except requests.RequestException as exc:
            return None, {"detail": f"Request failed: {exc}"}

    @staticmethod
    def _retry_after_seconds(headers, now):
        raw = (headers or {}).get("Retry-After")
        if not raw:
            return None
        try:
            return max(0, float(raw))
        except (TypeError, ValueError):
            try:
                return max(0, parsedate_to_datetime(raw).timestamp() - now)
            except (TypeError, ValueError, OverflowError):
                return None

    def _quote_result(self, data, apply_jitter_to_price):
        status = data.get("status")
        if status != "Success":
            return QuoteResult(success=False, raw_response=data, error=f"Sushi route status: {status or 'unknown'}")

        sell_amount = self._parse_int(data.get("amountIn"))
        buy_amount = self._parse_int(data.get("assumedAmountOut"))
        price = sell_amount / buy_amount if buy_amount > 0 else 0
        if apply_jitter_to_price and self.config.anti_mev_jitter:
            price = apply_jitter(price, jitter_percent=0.05)
        return QuoteResult(
            success=True,
            price=price,
            buy_amount=buy_amount,
            sell_amount=sell_amount,
            gas=self._parse_int(data.get("gasSpent"), 0) or None,
            raw_response=data,
        )

    def get_quote(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: Optional[int] = None,
        buy_amount: Optional[int] = None,
        taker_address: Optional[str] = None,
        slippage_percentage: Optional[float] = None,
        apply_jitter_to_price: bool = True,
    ) -> QuoteResult:
        if not sell_amount:
            error = "Sushi provider supports exact-input sell_amount quotes only"
            return QuoteResult(success=False, error=error)

        params = self._params(sell_token, buy_token, sell_amount, slippage_percentage)
        status_code, data = self._request("quote", params)
        if status_code != 200:
            detail = data.get("detail") or data.get("title") or "unknown error"
            return QuoteResult(
                success=False,
                raw_response=data,
                error=f"Sushi API returned status {status_code}: {detail}",
            )
        return self._quote_result(data, apply_jitter_to_price)

    def get_price(self, sell_token: str, buy_token: str, sell_amount: int) -> Optional[float]:
        quote = self.get_quote(
            sell_token,
            buy_token,
            sell_amount=sell_amount,
            apply_jitter_to_price=False,
        )
        return quote.price if quote.success else None

    def build_swap_transaction(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        slippage_percentage: float = 0.01,
    ) -> QuoteResult:
        quote = self.get_quote(
            sell_token,
            buy_token,
            sell_amount=sell_amount,
            slippage_percentage=slippage_percentage,
            apply_jitter_to_price=False,
        )
        if not quote.success:
            return quote

        params = self._params(sell_token, buy_token, sell_amount, slippage_percentage)
        params.update({"sender": taker_address, "simulate": "true"})
        status_code, data = self._request("swap", params)

        # Sushi discovers its RouteProcessor spender during swap preparation.
        # Treat insufficient allowance as an approval handshake; the engine will
        # approve this spender and refresh the swap before execution.
        if status_code == 422 and data.get("code") == "422-04" and data.get("spender"):
            quote.allowance_target = data["spender"]
            quote.raw_response = data
            return quote

        if status_code != 200 or data.get("status") != "Success":
            detail = data.get("detail") or data.get("title") or data.get("status") or "unknown error"
            return QuoteResult(
                success=False,
                raw_response=data,
                error=f"Sushi swap API returned status {status_code}: {detail}",
            )

        tx = data.get("tx") or {}
        if not tx.get("to") or not tx.get("data"):
            return QuoteResult(success=False, raw_response=data, error="Sushi swap response omitted transaction data")

        buy_amount = self._parse_int(data.get("assumedAmountOut"), quote.buy_amount or 0)
        sell_amount = self._parse_int(data.get("amountIn"), quote.sell_amount or 0)
        return QuoteResult(
            success=True,
            price=sell_amount / buy_amount if buy_amount > 0 else 0,
            buy_amount=buy_amount,
            sell_amount=sell_amount,
            allowance_target=tx.get("to"),
            data=tx.get("data"),
            to=tx.get("to"),
            value=self._parse_int(tx.get("value")),
            gas=self._parse_int(tx.get("gas"), 300000),
            gas_price=self._parse_int(tx.get("gasPrice")) or None,
            raw_response=data,
        )

    def refresh_quote(self, sell_token, buy_token, sell_amount, taker_address, slippage_percentage=0.01):
        return self.build_swap_transaction(
            sell_token,
            buy_token,
            sell_amount,
            taker_address,
            slippage_percentage,
        )
