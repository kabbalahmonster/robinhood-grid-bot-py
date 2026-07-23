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
        
        # Headers for API requests
        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        
        # Disable Permit2 if configured
        if self.permit2_disabled:
            self.headers["x-permit2-disabled"] = "true"
            self.logger.info("Permit2 disabled via header")
        
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
            taker_address: Address of the taker.
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
        
        # Build query parameters
        params = {
            "chainId": self.chain_id,
            "tokenIn": sell_token,
            "tokenOut": buy_token,
        }
        
        # Must specify either sellAmount or buyAmount
        if sell_amount:
            params["amount"] = str(sell_amount)
            params["type"] = "exactIn"
        elif buy_amount:
            params["amount"] = str(buy_amount)
            params["type"] = "exactOut"
        else:
            return QuoteResult(
                success=False,
                error="Must specify either sell_amount or buy_amount",
            )
        
        # Optional parameters
        if taker_address:
            params["recipient"] = taker_address
        
        if slippage_percentage:
            params["slippageTolerance"] = str(int(slippage_percentage * 10000))  # Basis points
        
        try:
            url = f"{self.BASE_URL}/quote"
            
            self.logger.debug(f"Fetching Uniswap quote: {params}")
            
            response = requests.get(
                url,
                headers=self._get_headers(),
                params=params,
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
            buy_amount = int(data.get("outputAmount", 0)) if data.get("outputAmount") else 0
            sell_amount = int(data.get("inputAmount", 0)) if data.get("inputAmount") else 0
            price = buy_amount / sell_amount if sell_amount > 0 else 0
            
            # Apply jitter if requested
            if apply_jitter_to_price and self.config.anti_mev_jitter:
                price = apply_jitter(price, jitter_percent=0.05)
            
            # Extract transaction data if available
            route = data.get("route", [])
            method_parameters = data.get("methodParameters", {})
            
            gas = data.get("gasUseEstimate")
            gas_price = data.get("gasPriceWei")
            
            return QuoteResult(
                success=True,
                price=price,
                buy_amount=buy_amount,
                sell_amount=sell_amount,
                allowance_target=method_parameters.get("to"),  # Router address
                data=method_parameters.get("calldata"),
                to=method_parameters.get("to"),
                value=int(method_parameters.get("value", 0)) if method_parameters.get("value") else 0,
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
