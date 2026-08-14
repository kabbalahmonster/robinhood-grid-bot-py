"""Sushi v7 aggregator client.

Implements the bot's common quote/swap interface using Sushi's public API.
Exact-input quotes are used throughout the trading engine. An optional API key
can be supplied for higher service limits.

Docs: https://docs.sushi.com/api/examples/quote
      https://docs.sushi.com/api/examples/swap
"""

from dataclasses import dataclass
import logging
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

    def __init__(self, config: BotConfig):
        self.config = config
        self.chain_id = config.chain_id
        self.api_key = getattr(config, "sushi_api_key", "")
        self.logger = logging.getLogger("grid_bot.sushi_api")
        self.logger.info(f"Sushi API Client initialized for chain {self.chain_id}")

    @staticmethod
    def _parse_int(value, default=0):
        if value is None or value == "":
            return default
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)

    def _params(self, sell_token, buy_token, sell_amount, slippage_percentage):
        params = {
            "tokenIn": sell_token,
            "tokenOut": buy_token,
            "amount": str(sell_amount),
            "maxSlippage": str(slippage_percentage if slippage_percentage is not None else 0.01),
        }
        if self.api_key:
            params["apiKey"] = self.api_key
        return params

    def _request(self, endpoint, params):
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
            return response.status_code, data
        except requests.RequestException as exc:
            return None, {"detail": f"Request failed: {exc}"}

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
