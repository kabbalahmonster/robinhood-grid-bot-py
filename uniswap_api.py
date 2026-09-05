"""
Uniswap API integration module for the Robinhood Chain Grid Trading Bot.

Handles quote fetching and swap execution using Uniswap API v1.
Supports disabling Permit2 via x-permit2-disabled header.

Docs: https://developers.uniswap.org/docs/api-reference
"""

import json
import hashlib
import logging
import shlex
import time
from typing import Optional, Any
from dataclasses import dataclass
import requests
from web3 import Web3

from config import BotConfig
from shared_rate_limit import SharedRateLimiter
from utils import apply_jitter


@dataclass
class QuoteResult:
    """Uniswap API quote result dataclass."""
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


class UniswapAPIClient:
    """
    Client for interacting with the Uniswap API.
    
    Provides quote fetching and swap execution for token trades
    with optional Permit2 bypass support.
    """
    
    BASE_URL = "https://trade-api.gateway.uniswap.org/v1"
    
    def __init__(self, config: BotConfig):
        """
        Initialize Uniswap API client.
        
        Args:
            config: Bot configuration with API key and settings.
        """
        self.config = config
        self.logger = logging.getLogger("grid_bot.uniswap_api")
        
        self.api_key = getattr(config, 'uniswap_api_key', '')
        self.permit2_disabled = getattr(config, 'uniswap_permit2_disabled', True)
        self.chain_id = config.chain_id
        self.rate_limiter = SharedRateLimiter(
            namespace="uniswap",
            credential=self.api_key,
            requests_per_second=float(getattr(config, "uniswap_rate_limit_rps", 4.0)),
            cooldown_base_seconds=float(getattr(config, "uniswap_cooldown_base_seconds", 30)),
            cooldown_max_seconds=float(getattr(config, "uniswap_cooldown_max_seconds", 900)),
            state_file=getattr(config, "uniswap_rate_state_file", ""),
        )
        
        # Headers for API requests (matching working curl)
        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "x-universal-router-version": "2.1.1",
            "x-erc20eth-enabled": "true",
        }
        
        # Disable Permit2 if configured
        if self.permit2_disabled:
            self.headers["x-permit2-disabled"] = "true"
            self.logger.info("Permit2 disabled via header")
        else:
            self.headers["x-permit2-disabled"] = "false"
        
        if not self.api_key:
            self.logger.warning("Uniswap API key not set")
        else:
            self.logger.info(f"Uniswap API Client initialized for chain {self.chain_id}")
    
    def _get_headers(self) -> dict:
        """Return a fresh copy of the request headers."""
        headers = self.headers.copy()
        # Cloudflare's Uniswap gateway intermittently rejects otherwise
        # identical requests carrying Requests' default Python user agent with
        # a bogus 409 ("client packet length exceeds 255 buffer").  A curl
        # user agent was verified against the same payload and transport
        # headers, while zstd encoding and keep-alive were independently
        # cleared by the diagnostic matrix.
        headers["User-Agent"] = "curl/8.0"
        # Be explicit: the gateway packet 409 is a transport/proxy failure and
        # must never be coupled to a potentially stale pooled connection.
        headers["Connection"] = "close"
        headers["Accept"] = "application/json"
        return headers

    @staticmethod
    def _is_gateway_packet_failure(response) -> bool:
        error_text = response.text[:500].lower() if response.status_code != 200 else ""
        return (
            response.status_code == 409
            and "packet length exceeds" in error_text
            and "buffer" in error_text
        )

    def _post_json(self, endpoint: str, payload: dict):
        """POST once, retrying only the known transient gateway packet 409.

        ``requests.post`` already creates a short-lived Session, so there was
        no persistent client pool to reset. ``Connection: close`` plus a second
        one-shot request nevertheless guarantees a fresh TCP/TLS connection.
        A packet 409 is not recorded as a rate limit and therefore cannot open
        the fleet-wide cooldown; a second failure is returned to the provider
        adapter, which may safely restart the complete operation on Sushi.
        """
        url = f"{self.BASE_URL}/{endpoint}"
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_id = hashlib.sha256(encoded).hexdigest()[:12]
        safe_headers = {
            key: value for key, value in self._get_headers().items()
            if key.lower() not in {"x-api-key", "authorization"}
        }

        for attempt in (1, 2):
            started = time.monotonic()
            response = requests.post(
                url,
                headers=self._get_headers(),
                data=encoded,
                timeout=30,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            self.logger.info(
                "Uniswap HTTP endpoint=%s status=%s attempt=%s payload_bytes=%s "
                "payload_id=%s elapsed_ms=%s request_id=%s headers=%s",
                endpoint,
                response.status_code,
                attempt,
                len(encoded),
                payload_id,
                elapsed_ms,
                getattr(response, "headers", {}).get("x-request-id")
                or getattr(response, "headers", {}).get("request-id") or "",
                safe_headers,
            )
            if not self._is_gateway_packet_failure(response):
                return response
            if attempt == 2:
                replay_headers = " ".join(
                    f"--header {shlex.quote(f'{key}: {value}')}"
                    for key, value in self._get_headers().items()
                    if key.lower() != "x-api-key"
                )
                replay_headers += ' --header "x-api-key: ${UNISWAP_API_KEY}"'
                self.logger.error(
                    "Exact failed Uniswap request (API key omitted): "
                    "curl --request POST --url %s "
                    "%s --data-binary %s",
                    url,
                    replay_headers,
                    shlex.quote(encoded.decode("utf-8")),
                )
                self.logger.warning(
                    "Uniswap Robinhood gateway packet failure confirmed twice; "
                    "returning the failure without opening the fleet-wide rate-limit cooldown",
                )
                return response
            self.logger.warning(
                "Uniswap transient gateway packet 409; retrying once on a fresh "
                "connection endpoint=%s payload_bytes=%s payload_id=%s",
                endpoint, len(encoded), payload_id,
            )

    def _cooldown_error(self) -> Optional[str]:
        # Jitter before reserving the shared slot. Applying it afterward can
        # compress two adjacent reservations into a much smaller wire-level
        # gap and defeat the coordinator's strict request spacing.
        if self.config.anti_mev_jitter:
            import random
            import time
            time.sleep(random.uniform(0.1, 0.3))
        cooldown = self.rate_limiter.acquire()
        if cooldown is None:
            return None
        return f"Uniswap shared provider cooldown active; retry in {cooldown}s"

    def _record_response_limit(self, response) -> None:
        if response.status_code == 429:
            wait_seconds = self.rate_limiter.record_rate_limit(
                response.headers.get("Retry-After", "")
            )
            self.logger.warning(
                "Uniswap rate limited; pausing shared API key for %ss",
                wait_seconds,
            )
        elif response.status_code == 200:
            self.rate_limiter.record_success()

    @staticmethod
    def _is_no_route_failure(response) -> bool:
        """Return whether the gateway failed to discover executable liquidity."""
        if response.status_code != 404:
            return False
        error_text = (response.text or "")[:1000].lower()
        return "noroutefounderror" in error_text or "no route" in error_text or "no quotes available" in error_text

    @classmethod
    def _is_retryable_routing_failure(cls, response) -> bool:
        """Failures where rerunning discovery is explicitly safe/useful."""
        if cls._is_no_route_failure(response):
            return True
        if response.status_code != 404:
            return False
        error_text = (response.text or "")[:1000].lower()
        return "upstreamtimeouterror" in error_text or "routing dependency timed out" in error_text
    
    def get_quote(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: Optional[int] = None,
        buy_amount: Optional[int] = None,
        taker_address: Optional[str] = None,
        slippage_percentage: Optional[float] = None,
        apply_jitter_to_price: bool = True,
        routing_attempts: int = 1,
    ) -> QuoteResult:
        """
        Get a quote from the Uniswap API.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            buy_amount: Amount to buy (in base units).
            taker_address: Address of the taker (required).
            slippage_percentage: Slippage tolerance (e.g., 0.01 for 1%).
            apply_jitter_to_price: Whether to apply anti-MEV jitter.
            
        Returns:
            QuoteResult: Quote information or error.
        """
        if not self.api_key:
            return QuoteResult(
                success=False,
                error="Uniswap API key not configured",
            )
        
        if not taker_address:
            return QuoteResult(
                success=False,
                error="swapper (taker_address) is required",
            )
        
        # Build JSON payload (POST request)
        payload = {
            "tokenInChainId": self.chain_id,
            "tokenOutChainId": self.chain_id,
            "tokenIn": sell_token,
            "tokenOut": buy_token,
            "swapper": taker_address,
        }
        
        # Must specify either sellAmount or buyAmount
        if sell_amount:
            payload["amount"] = str(sell_amount)
            payload["type"] = "EXACT_INPUT"
        elif buy_amount:
            payload["amount"] = str(buy_amount)
            payload["type"] = "EXACT_OUTPUT"
        else:
            return QuoteResult(
                success=False,
                error="Must specify either sell_amount or buy_amount",
            )
        
        # Optional parameters - slippageTolerance in percent (e.g., 0.5 = 0.5%)
        # Convert from fraction (0.02 = 2%) to percent value (2.0 = 2%)
        # and normalize to the API's maximum of two decimal places. Adding a
        # token fee and market buffer can otherwise produce values such as
        # 7.000000000000001, which Uniswap rejects as invalid.
        if slippage_percentage is not None:
            payload["slippageTolerance"] = round(slippage_percentage * 100, 2)
        
        try:
            url = f"{self.BASE_URL}/quote"

            self.logger.debug(f"Fetching Uniswap quote: {payload}")
            
            routing_attempts = min(3, max(1, int(routing_attempts)))
            response = None
            for routing_attempt in range(1, routing_attempts + 1):
                cooldown_error = self._cooldown_error()
                if cooldown_error is not None:
                    return QuoteResult(success=False, error=cooldown_error)
                response = self._post_json("quote", payload)

                # BEST_PRICE/default routing may involve UniswapX discovery.
                # Retry the same attempt against canonical AMM liquidity.
                if self._is_no_route_failure(response):
                    amm_payload = payload.copy()
                    amm_payload["protocols"] = ["V2", "V3", "V4"]
                    self.logger.warning(
                        "Uniswap default routing found no route; retrying quote "
                        "against explicit V2/V3/V4 AMM liquidity"
                    )
                    cooldown_error = self._cooldown_error()
                    if cooldown_error is not None:
                        return QuoteResult(success=False, error=cooldown_error)
                    response = self._post_json("quote", amm_payload)

                if response.status_code == 200 or routing_attempt == routing_attempts:
                    break
                if not self._is_retryable_routing_failure(response):
                    break
                delay = 0.75 * routing_attempt
                self.logger.warning(
                    "Uniswap actionable routing failed transiently; retrying "
                    "fresh discovery (%s/%s) after %.2fs",
                    routing_attempt + 1, routing_attempts, delay,
                )
                time.sleep(delay)
            
            self.logger.debug(f"Uniswap API response status: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500]
                self.logger.error(f"Uniswap API error: Status {response.status_code}")
                self.logger.error(f"Response: {error_text}")
                self._record_response_limit(response)
                return QuoteResult(
                    success=False,
                    error=f"Uniswap API returned status {response.status_code}: {error_text}",
                )
            
            self._record_response_limit(response)
            data = response.json()
            
            # Extract amounts and calculate price
            # Uniswap API returns quote field with input/output amounts
            quote = data.get("quote", {})
            buy_amount = int(quote.get("output", {}).get("amount", 0)) if quote.get("output") else 0
            sell_amount = int(quote.get("input", {}).get("amount", 0)) if quote.get("input") else 0
            # Price in ETH per token (sell_amount is wei, buy_amount is wei-tokens)
            # ETH/token = (wei / 10^18) / (wei-tokens / 10^18) = wei / wei-tokens
            price = sell_amount / buy_amount if buy_amount > 0 else 0
            
            # Apply jitter if requested
            if apply_jitter_to_price and self.config.anti_mev_jitter:
                price = apply_jitter(price, jitter_percent=0.05)
            
            # Extract transaction data if available
            tx_data = data.get("tx", {})
            
            gas = data.get("gasUseEstimate")
            gas_price = tx_data.get("gasPrice")
            
            return QuoteResult(
                success=True,
                price=price,
                buy_amount=buy_amount,
                sell_amount=sell_amount,
                allowance_target=tx_data.get("to"),  # Router address
                data=tx_data.get("data"),
                to=tx_data.get("to"),
                value=int(tx_data.get("value", 0)) if tx_data.get("value") else 0,
                gas=int(gas) if gas else 300000,
                gas_price=int(gas_price) if gas_price else None,
                raw_response=data,
            )
        
        except requests.exceptions.RequestException as e:
            error_msg = f"Request failed: {e}"
            self.logger.error(error_msg)
            return QuoteResult(success=False, error=error_msg)
        
        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            self.logger.error(error_msg)
            return QuoteResult(success=False, error=error_msg)
    
    def build_swap_transaction(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        slippage_percentage: float = 0.01,
    ) -> QuoteResult:
        """
        Build a swap transaction using Uniswap API.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            taker_address: Address of the taker.
            slippage_percentage: Slippage tolerance.
            
        Returns:
            QuoteResult: Transaction data or error.
        """
        return self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
            slippage_percentage=slippage_percentage,
            apply_jitter_to_price=False,
            routing_attempts=3,
        )
    
    def get_price(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
    ) -> Optional[float]:
        """
        Get current price for a token pair.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell for price calculation.
            
        Returns:
            Optional[float]: Price ratio or None if failed.
        """
        result = self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            apply_jitter_to_price=False,
        )
        
        if result.success and result.price:
            return result.price
        return None
    
    def refresh_quote(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        slippage_percentage: float = 0.01,
    ) -> QuoteResult:
        """
        Refresh a quote (called after token approval).
        Same as build_swap_transaction - gets fresh quote.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            taker_address: Address of the taker.
            slippage_percentage: Slippage tolerance.
            
        Returns:
            QuoteResult: Fresh transaction data.
        """
        return self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
            slippage_percentage=slippage_percentage,
            apply_jitter_to_price=False,
            routing_attempts=3,
        )
    
    def check_approval(
        self,
        token: str,
        amount: int,
        wallet: str,
    ) -> dict:
        """
        Check and get approval transaction from Uniswap API.
        
        Args:
            token: Token address to approve.
            amount: Amount to approve (in base units).
            wallet: Wallet address.
            
        Returns:
            dict: Approval response with transaction data if needed.
        """
        if not self.api_key:
            return {"error": "Uniswap API key not configured"}
        
        try:
            url = f"{self.BASE_URL}/check_approval"

            cooldown_error = self._cooldown_error()
            if cooldown_error is not None:
                return {"error": cooldown_error}
            
            payload = {
                "walletAddress": wallet,
                "token": token,
                "amount": str(amount),
                "chainId": self.chain_id,
            }
            
            self.logger.debug(f"Checking Uniswap approval: token={token}, amount={amount}, wallet={wallet}")
            
            response = self._post_json("check_approval", payload)
            
            self.logger.debug(f"Uniswap check_approval response status: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500]
                self.logger.error(f"Uniswap check_approval error: Status {response.status_code}")
                self.logger.error(f"Response: {error_text}")
                self._record_response_limit(response)
                return {"error": f"check_approval failed: {response.status_code}", "detail": error_text}
            
            self._record_response_limit(response)
            data = response.json()
            self.logger.debug(f"Uniswap check_approval response: {data}")
            return data
            
        except requests.exceptions.RequestException as e:
            error_msg = f"check_approval request failed: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}
        except Exception as e:
            error_msg = f"check_approval unexpected error: {e}"
            self.logger.error(error_msg)
            return {"error": error_msg}
    
    def get_swap_transaction(
        self,
        quote_data: dict,
    ) -> QuoteResult:
        """
        Get swap transaction calldata from quote.
        
        Uniswap API requires a separate call to /swap to get the
        actual transaction calldata from a quote.
        
        Args:
            quote_data: The full quote object from get_quote response.
            
        Returns:
            QuoteResult: Transaction data with calldata.
        """
        if not self.api_key:
            return QuoteResult(
                success=False,
                error="Uniswap API key not configured",
            )
        
        try:
            url = f"{self.BASE_URL}/swap"

            cooldown_error = self._cooldown_error()
            if cooldown_error is not None:
                return QuoteResult(success=False, error=cooldown_error)
            
            # Build payload according to Uniswap API spec
            # Extract ONLY the nested quote object from the /quote response
            # The nested quote contains: chainId, swapper, tradeType, route, input, output, etc.
            if isinstance(quote_data, dict) and 'quote' in quote_data:
                nested_quote = quote_data.get('quote', {})
                self.logger.debug(f"Extracted nested quote, keys: {list(nested_quote.keys()) if isinstance(nested_quote, dict) else 'not dict'}")
            else:
                nested_quote = quote_data
                self.logger.debug(f"Using quote_data directly, keys: {list(nested_quote.keys()) if isinstance(nested_quote, dict) else 'not dict'}")
            
            payload = {
                "quote": nested_quote,
                "refreshGasPrice": True,
                "simulateTransaction": True,
                "safetyMode": "SAFE",
            }
            
            self.logger.debug(f"Fetching Uniswap swap transaction")
            self.logger.debug(f"Swap payload quote keys: {list(quote_data.keys()) if isinstance(quote_data, dict) else 'not dict'}")
            # Log the nested quote structure
            if isinstance(quote_data, dict) and 'quote' in quote_data:
                nested_quote = quote_data.get('quote', {})
                self.logger.debug(f"Nested quote keys: {list(nested_quote.keys()) if isinstance(nested_quote, dict) else 'not dict'}")
            
            response = self._post_json("swap", payload)
            
            self.logger.debug(f"Uniswap swap API response status: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500]
                self.logger.error(f"Uniswap swap API error: Status {response.status_code}")
                self.logger.error(f"Response: {error_text}")
                self._record_response_limit(response)
                return QuoteResult(
                    success=False,
                    error=f"Uniswap swap API returned status {response.status_code}: {error_text}",
                )
            
            self._record_response_limit(response)
            data = response.json()
            
            # Extract transaction data from "swap" field (not "tx")
            swap_data = data.get("swap", {})
            
            # Get quote data for amounts from swap response
            quote_info = data.get("quote", {})
            self.logger.debug(f"Swap response has quote: {bool(quote_info)}, has swap: {bool(swap_data)}")
            
            if not quote_info:
                self.logger.warning("Uniswap swap response contained no quote")
                # Fallback: try to use amounts from original quote
                quote_info = payload.get("quote", {})
            
            output_info = quote_info.get("output", {}) if isinstance(quote_info, dict) else {}
            input_info = quote_info.get("input", {}) if isinstance(quote_info, dict) else {}
            
            self.logger.debug(f"Output amount from swap: {output_info.get('amount')}")
            self.logger.debug(f"Input amount from swap: {input_info.get('amount')}")
            
            buy_amount = int(output_info.get("amount", 0)) if output_info else 0
            sell_amount = int(input_info.get("amount", 0)) if input_info else 0
            
            # Parse values that may be strings
            def parse_int(value):
                if value is None:
                    return 0
                if isinstance(value, int):
                    return value
                if isinstance(value, str):
                    if value.startswith("0x"):
                        return int(value, 16)
                    return int(value, 10)
                return 0
            
            return QuoteResult(
                success=True,
                price=buy_amount / sell_amount if sell_amount > 0 else 0,
                buy_amount=buy_amount,
                sell_amount=sell_amount,
                allowance_target=swap_data.get("to"),
                data=swap_data.get("data"),
                to=swap_data.get("to"),
                value=parse_int(swap_data.get("value")),
                gas=parse_int(swap_data.get("gasLimit")) or 300000,
                gas_price=parse_int(swap_data.get("gasPrice")) or None,
                raw_response=data,
            )
        
        except requests.exceptions.RequestException as e:
            error_msg = f"Swap request failed: {e}"
            self.logger.error(error_msg)
            return QuoteResult(success=False, error=error_msg)
        
        except Exception as e:
            error_msg = f"Unexpected swap error: {e}"
            self.logger.error(error_msg)
            return QuoteResult(success=False, error=error_msg)
    
    def execute_sell_with_approval(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        wallet,
        slippage_percentage: float = 0.02,
    ) -> QuoteResult:
        """
        Execute a sell with full approval handling.
        
        This is the high-level method that handles:
        1. Getting quote
        2. Checking/executing approval via Uniswap API
        3. Getting swap transaction
        
        Args:
            sell_token: Token to sell
            buy_token: Token to buy
            sell_amount: Amount to sell
            taker_address: Taker address
            wallet: Wallet instance for sending transactions
            slippage_percentage: Slippage tolerance
            
        Returns:
            QuoteResult with transaction data ready to execute
        """
        # Step 1: Get initial quote
        quote = self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
            slippage_percentage=slippage_percentage,
        )
        if not quote.success:
            return quote
        
        # Step 2: Check approval via Uniswap API
        approval_result = self.check_approval(
            token=sell_token,
            amount=sell_amount,
            wallet=taker_address,
        )
        
        if "error" in approval_result:
            return QuoteResult(success=False, error=f"Approval check failed: {approval_result.get('error')}")
        
        # Step 3: Execute approval transactions if needed
        cancel_tx = approval_result.get("cancel")
        approval_tx = approval_result.get("approval")
        
        if cancel_tx is not None:
            self.logger.info("Approval cancel transaction required")
            result = self._send_api_transaction(cancel_tx, wallet)
            if not result.success:
                return QuoteResult(success=False, error=f"Cancel failed: {result.error}")
            self.logger.info(f"Cancel confirmed: {result.tx_hash}")
            import time
            time.sleep(3)
        
        if approval_tx is not None:
            self.logger.info("ERC20 approval transaction required")
            result = self._send_api_transaction(approval_tx, wallet)
            if not result.success:
                return QuoteResult(success=False, error=f"Approval failed: {result.error}")
            self.logger.info(f"Approval confirmed: {result.tx_hash}")
            import time
            time.sleep(3)
        
        # Step 4: Get swap transaction
        swap_result = self.get_swap_transaction(quote.raw_response)
        if not swap_result.success:
            return swap_result
        
        return swap_result
    
    def _send_api_transaction(self, api_tx: dict, wallet) -> Any:
        """Send a transaction from Uniswap API response with fresh EIP-1559 fees."""
        from web3 import Web3
        
        # Get fresh block data
        latest_block = wallet.w3.eth.get_block("latest")
        base_fee = int(latest_block.get("baseFeePerGas", 0))
        
        # Get priority fee
        try:
            priority_fee = int(wallet.w3.eth.max_priority_fee)
        except Exception:
            priority_fee = 1_000_000
        priority_fee = max(priority_fee, 1_000_000)
        
        # Calculate max fee with headroom
        max_fee = base_fee * 2 + priority_fee
        
        tx = {
            "from": Web3.to_checksum_address(api_tx.get("from", wallet.address)),
            "to": Web3.to_checksum_address(api_tx.get("to")),
            "data": api_tx.get("data"),
            "value": int(api_tx.get("value", "0x0"), 16) if isinstance(api_tx.get("value"), str) else int(api_tx.get("value", 0)),
            "chainId": int(api_tx.get("chainId", self.chain_id)),
            "nonce": wallet.w3.eth.get_transaction_count(wallet.address, "pending"),
            "maxPriorityFeePerGas": priority_fee,
            "maxFeePerGas": max_fee,
            "type": 2,
        }
        
        # Estimate gas
        try:
            estimated = wallet.w3.eth.estimate_gas(tx)
            tx["gas"] = int(estimated * 1.2)
        except Exception as e:
            self.logger.warning(f"Gas estimation failed: {e}")
            tx["gas"] = int(api_tx.get("gas", 100000))
        
        self.logger.info(f"Tx fees: base={base_fee} priority={priority_fee} max={max_fee} gas={tx['gas']}")
        return wallet._send_transaction(tx)
