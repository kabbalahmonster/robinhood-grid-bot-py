"""Umbra best-execution API client for Robinhood Chain (chain 4663)."""

from dataclasses import dataclass
import logging
from typing import Optional

import requests

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


class UmbraAPIClient:
    """Quote and build exact-input swaps through UmbraRH.

    The returned router is pinned. Provider gas is intentionally ignored for
    economics because Robinhood builds currently advertise a flat 3M limit;
    execution and tournament gates use the bot's local gas simulation.
    """

    BASE_URL = "https://umbra.finance/api/rh"
    ROUTER = "0xfC830D7861C5ceBefF2272a03aacEf9baC8A7603"
    NATIVE = "0x0000000000000000000000000000000000000000"

    def __init__(self, config):
        if int(config.chain_id) != 4663:
            raise ValueError("Umbra provider supports Robinhood Chain 4663 only")
        self.config = config
        self.chain_id = config.chain_id
        self.logger = logging.getLogger("grid_bot.umbra_api")

    @classmethod
    def _token(cls, address):
        return "ETH" if str(address).lower() == cls.NATIVE else address

    @staticmethod
    def _int(value, default=0):
        try:
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _post(self, endpoint, payload, timeout_seconds=None):
        try:
            response = requests.post(
                f"{self.BASE_URL}/{endpoint}", json=payload,
                timeout=30 if timeout_seconds is None else max(0.05, float(timeout_seconds)),
            )
            try:
                data = response.json()
            except ValueError:
                data = {"error": response.text[:500]}
            return response.status_code, data
        except requests.RequestException as exc:
            return None, {"error": f"Request failed: {exc}"}

    def _payload(self, sell_token, buy_token, sell_amount):
        return {
            "tokenIn": self._token(sell_token),
            "tokenOut": self._token(buy_token),
            "amount": str(int(sell_amount)),
        }

    def get_quote(self, sell_token, buy_token, sell_amount=None, buy_amount=None,
                  taker_address=None, slippage_percentage=0.01,
                  apply_jitter_to_price=True, quote_timeout_seconds=None, **_kwargs):
        if not sell_amount:
            return QuoteResult(False, error="Umbra supports exact-input sell_amount quotes only")
        payload = self._payload(sell_token, buy_token, sell_amount)
        status, data = self._post("quote", payload, quote_timeout_seconds)
        if status != 200:
            detail = data.get("error") or data.get("detail") or "unknown error"
            return QuoteResult(False, raw_response=data,
                               error=f"Umbra API returned status {status}: {detail}")
        # Robinhood Umbra has no Pulse-style verification.status. netOut is the
        # documented post-protocol-fee and post-transfer-tax amount.
        output = self._int(data.get("netOut"))
        sold = self._int(data.get("amountIn"), int(sell_amount))
        if output <= 0 or sold != int(sell_amount):
            return QuoteResult(False, raw_response=data, error="Umbra quote amount mismatch or empty output")
        price = sold / output
        if apply_jitter_to_price:
            price = apply_jitter(price, jitter_percent=0.05)
        result = QuoteResult(True, price=price, buy_amount=output, sell_amount=sold,
                             allowance_target=self.ROUTER, gas=None, raw_response=data)
        result.output_includes_transfer_tax = True
        return result

    def get_swap_transaction(self, quote, *, sell_token, buy_token, sell_amount,
                             taker_address, slippage_percentage=0.01,
                             quote_timeout_seconds=None, **_kwargs):
        if not quote.success:
            return quote
        payload = self._payload(sell_token, buy_token, sell_amount)
        payload.update({
            "recipient": taker_address,
            "slippageBps": max(0, int(round(float(slippage_percentage) * 10000))),
        })
        status, data = self._post("build", payload, quote_timeout_seconds)
        if status != 200:
            detail = data.get("error") or data.get("detail") or "unknown error"
            return QuoteResult(False, raw_response=data,
                               error=f"Umbra build API returned status {status}: {detail}")
        router = str(data.get("router") or "")
        calldata = data.get("calldata")
        # minOut is the post-fee/post-tax on-chain floor and therefore the only
        # value safe enough to authorize execution economics.
        output = self._int(data.get("minOut"))
        if router.lower() != self.ROUTER.lower():
            return QuoteResult(False, raw_response=data, error="Umbra build returned an untrusted router")
        if not isinstance(calldata, str) or not calldata.startswith("0x") or len(calldata) < 10:
            return QuoteResult(False, raw_response=data, error="Umbra build omitted valid calldata")
        if output <= 0 or self._int(data.get("minOut")) <= 0:
            return QuoteResult(False, raw_response=data, error="Umbra build omitted output safeguards")
        if self._int(data.get("value")) != (int(sell_amount) if self._token(sell_token) == "ETH" else 0):
            return QuoteResult(False, raw_response=data, error="Umbra build returned an unexpected native value")
        result = QuoteResult(
            True, price=int(sell_amount) / output, buy_amount=output,
            sell_amount=int(sell_amount), allowance_target=self.ROUTER,
            data=calldata, to=router, value=self._int(data.get("value")),
            gas=None, raw_response=data,
        )
        result.output_includes_transfer_tax = True
        result.output_is_execution_floor = True
        return result

    def build_swap_transaction(self, sell_token, buy_token, sell_amount, taker_address,
                               slippage_percentage=0.01, quote_timeout_seconds=None):
        quote = self.get_quote(
            sell_token, buy_token, sell_amount=sell_amount,
            taker_address=taker_address, slippage_percentage=slippage_percentage,
            apply_jitter_to_price=False, quote_timeout_seconds=quote_timeout_seconds,
        )
        return self.get_swap_transaction(
            quote, sell_token=sell_token, buy_token=buy_token, sell_amount=sell_amount,
            taker_address=taker_address, slippage_percentage=slippage_percentage,
            quote_timeout_seconds=quote_timeout_seconds,
        )

    def get_price(self, sell_token, buy_token, sell_amount):
        quote = self.get_quote(sell_token, buy_token, sell_amount=sell_amount,
                               apply_jitter_to_price=False)
        return quote.price if quote.success else None

    def refresh_quote(self, sell_token, buy_token, sell_amount, taker_address,
                      slippage_percentage=0.01):
        return self.build_swap_transaction(
            sell_token, buy_token, sell_amount, taker_address, slippage_percentage,
        )
