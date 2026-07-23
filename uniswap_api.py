"""
Uniswap API integration module for the Robinhood Chain Grid Trading Bot.

Handles quote fetching and swap execution using Uniswap API v1.
Supports disabling Permit2 via x-permit2-disabled header.

Docs: https://developers.uniswap.org/docs/api-reference
"""

import json
import logging
from typing import Optional, Any
from dataclasses import dataclass
import requests
from web3 import Web3

from config import BotConfig
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
        """Get headers with fresh timestamp for each request."""
        headers = self.headers.copy()
        # Add anti-MEV jitter if enabled
        if self.config.anti_mev_jitter:
            import random
            import time
            time.sleep(random.uniform(0.1, 0.3))
        return headers
    
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
        if slippage_percentage is not None:
            payload["slippageTolerance"] = float(slippage_percentage * 100)  # e.g., 0.02 -> 2.0 (2%)
        
        try:
            url = f"{self.BASE_URL}/quote"
            
            self.logger.debug(f"Fetching Uniswap quote: {payload}")
            
            response = requests.post(
                url,
                headers=self._get_headers(),
                json=payload,
                timeout=30,
            )
            
            self.logger.debug(f"Uniswap API response status: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500]
                self.logger.error(f"Uniswap API error: Status {response.status_code}")
                self.logger.error(f"Response: {error_text}")
                return QuoteResult(
                    success=False,
                    error=f"Uniswap API returned status {response.status_code}: {error_text}",
                )
            
            data = response.json()
            
            # Extract amounts and calculate price
            # Uniswap API returns quote field with input/output amounts
            quote = data.get("quote", {})
            buy_amount = int(quote.get("output", {}).get("amount", 0)) if quote.get("output") else 0
            sell_amount = int(quote.get("input", {}).get("amount", 0)) if quote.get("input") else 0
            price = buy_amount / sell_amount if sell_amount > 0 else 0
            
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
        )
    
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
            
            response = requests.post(
                url,
                headers=self._get_headers(),
                json=payload,
                timeout=30,
            )
            
            self.logger.debug(f"Uniswap swap API response status: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500]
                self.logger.error(f"Uniswap swap API error: Status {response.status_code}")
                self.logger.error(f"Response: {error_text}")
                return QuoteResult(
                    success=False,
                    error=f"Uniswap swap API returned status {response.status_code}: {error_text}",
                )
            
            data = response.json()
            
            # Extract transaction data from "swap" field (not "tx")
            swap_data = data.get("swap", {})
            
            # Get quote data for amounts from swap response
            quote_info = data.get("quote", {})
            self.logger.info(f"Swap response has quote: {bool(quote_info)}, has swap: {bool(swap_data)}")
            
            if not quote_info:
                self.logger.warning(f"No quote in swap response! Keys: {list(data.keys())}")
                # Fallback: try to use amounts from original quote
                quote_info = payload.get("quote", {})
            
            output_info = quote_info.get("output", {}) if isinstance(quote_info, dict) else {}
            input_info = quote_info.get("input", {}) if isinstance(quote_info, dict) else {}
            
            self.logger.info(f"Output amount from swap: {output_info.get('amount')}")
            self.logger.info(f"Input amount from swap: {input_info.get('amount')}")
            
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
