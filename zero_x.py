"""
0x API integration module for the Robinhood Chain Grid Trading Bot.

Handles quote fetching, swap execution, and transaction construction
using the 0x Protocol API for optimal DEX aggregation.
"""

import json
import logging
from typing import Optional, Any
from dataclasses import dataclass
from urllib.parse import urlencode
import requests
from web3 import Web3

from config import BotConfig
from utils import apply_jitter


@dataclass
class QuoteResult:
    """0x API quote result dataclass."""
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


@dataclass
class SwapResult:
    """Swap execution result dataclass."""
    success: bool
    tx_hash: Optional[str] = None
    gas_used: Optional[int] = None
    effective_gas_price: Optional[int] = None
    block_number: Optional[int] = None
    error: Optional[str] = None


class ZeroXClient:
    """
    Client for interacting with the 0x Protocol API.
    
    Provides quote fetching and swap execution for token trades
    across multiple DEXs with optimal routing.
    """
    
    def __init__(self, config: BotConfig):
        """
        Initialize 0x client.
        
        Args:
            config: Bot configuration with API key and chain settings.
        """
        self.config = config
        self.logger = logging.getLogger("grid_bot.zero_x")
        
        self.base_url = config.zero_x_api_url
        self.api_key = config.zero_x_api_key
        self.chain_id = config.chain_id
        
        # Headers for API requests (0x API v2)
        self.headers = {
            "0x-api-key": self.api_key,
            "0x-version": "v2",
            "Content-Type": "application/json",
        }
        
        self.logger.info(f"0x Client initialized for chain {self.chain_id}")
    
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
        Get a quote from the 0x API.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units). Either this or buy_amount must be set.
            buy_amount: Amount to buy (in base units). Either this or sell_amount must be set.
            taker_address: Address of the taker (required for swaps).
            slippage_percentage: Slippage tolerance percentage (e.g., 0.01 for 1%).
            apply_jitter_to_price: Whether to apply anti-MEV jitter.
            
        Returns:
            QuoteResult: Quote information or error.
        """
        # Build query parameters
        params = {
            "chainId": self.chain_id,
            "sellToken": sell_token,
            "buyToken": buy_token,
        }
        
        # Must specify either sellAmount or buyAmount
        if sell_amount:
            params["sellAmount"] = str(sell_amount)
        elif buy_amount:
            params["buyAmount"] = str(buy_amount)
        else:
            return QuoteResult(
                success=False,
                error="Must specify either sell_amount or buy_amount",
            )
        
        # Optional parameters
        if taker_address:
            params["taker"] = taker_address
        
        if slippage_percentage:
            params["slippageBps"] = int(slippage_percentage * 100)  # Convert to basis points
        
        # Add anti-MEV jitter if enabled
        if apply_jitter_to_price and self.config.anti_mev_jitter:
            # Jitter doesn't apply to quotes directly, but we can add random delay
            import random
            import time
            time.sleep(random.uniform(0.1, 0.5))
        
        try:
            # Use 0x AllowanceHolder endpoint (no Permit2 signatures needed!)
            url = f"{self.base_url}/swap/allowance-holder/quote"
            
            self.logger.debug(f"Fetching quote: {params}")
            self.logger.debug(f"0x API URL: {url}")
            self.logger.debug(f"Chain ID: {self.chain_id}")
            
            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            
            # Log response status for debugging
            self.logger.debug(f"0x API response status: {response.status_code}")
            
            if response.status_code != 200:
                self.logger.error(f"0x API error: Status {response.status_code}")
                self.logger.error(f"Response: {response.text[:500]}")
                return QuoteResult(
                    success=False,
                    error=f"0x API returned status {response.status_code}: {response.text[:200]}",
                )
            
            response.raise_for_status()
            data = response.json()
            
            # Extract price and amounts (0x API v2 format)
            buy_amount = int(data.get("buyAmount", 0)) if data.get("buyAmount") else 0
            sell_amount = int(data.get("sellAmount", 0)) if data.get("sellAmount") else 0
            price = buy_amount / sell_amount if sell_amount > 0 else 0

            # Apply jitter to price if requested
            if apply_jitter_to_price and self.config.anti_mev_jitter:
                price = apply_jitter(price, jitter_percent=0.05)

            # 0x v2 may have transaction object OR we need to use the 0x Exchange Proxy
            transaction = data.get("transaction", {})

            # If no transaction object, use the permit2 allowanceTarget as to address
            # The 0x Exchange Proxy is the actual executor
            to_address = transaction.get("to") if transaction else self.config.zero_x_proxy
            tx_data = transaction.get("data") if transaction else None

            # Log what we got
            self.logger.debug(f"0x quote: buy={buy_amount}, sell={sell_amount}, to={to_address}")

            return QuoteResult(
                success=True,
                price=price,
                buy_amount=buy_amount,
                sell_amount=sell_amount,
                allowance_target=data.get("allowanceTarget"),
                data=tx_data,
                to=to_address,
                value=int(transaction.get("value", 0)) if transaction else 0,
                gas=int(transaction.get("gas", 0)) if transaction else 200000,
                gas_price=int(transaction.get("gasPrice", 0)) if transaction else 0,
                raw_response=data,
            )
        
        except requests.exceptions.RequestException as e:
            error_msg = f"Request failed: {e}"
            self.logger.error(error_msg)
            
            # Try to extract more error details from response
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response status: {e.response.status_code}")
                self.logger.error(f"Response headers: {dict(e.response.headers)}")
                try:
                    error_text = e.response.text[:500]
                    self.logger.error(f"Response body: {error_text}")
                    error_data = e.response.json()
                    if "reason" in error_data:
                        error_msg = f"0x API error: {error_data['reason']}"
                    elif "error" in error_data:
                        error_msg = f"0x API error: {error_data['error']}"
                except Exception as parse_err:
                    self.logger.error(f"Could not parse error response: {parse_err}")
            
            return QuoteResult(success=False, error=error_msg)
        
        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            self.logger.error(error_msg)
            return QuoteResult(success=False, error=error_msg)
    
    def get_price(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
    ) -> Optional[float]:
        """
        Get current price for a token pair using the lighter /price endpoint.
        
        This endpoint is for price discovery only and doesn't count against
        quote-to-trade conversion metrics.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell for price calculation.
            
        Returns:
            Optional[float]: Price ratio or None if failed.
        """
        # Build query parameters
        params = {
            "chainId": self.chain_id,
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": str(sell_amount),
        }
        
        try:
            # Use /price endpoint for price discovery (lighter weight than /quote)
            url = f"{self.base_url}/swap/allowance-holder/price"
            
            self.logger.debug(f"Fetching price: {params}")
            self.logger.debug(f"0x API URL: {url}")
            
            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            
            self.logger.debug(f"0x price API status: {response.status_code}")
            
            if response.status_code != 200:
                self.logger.error(f"0x price API error: Status {response.status_code}")
                self.logger.error(f"Response: {response.text[:500]}")
                return None
            
            response.raise_for_status()
            data = response.json()
            
            # Extract price from response
            buy_amount = int(data.get("buyAmount", 0)) if data.get("buyAmount") else 0
            sell_amount = int(data.get("sellAmount", 0)) if data.get("sellAmount") else 0
            
            if buy_amount > 0:
                # Price = sell_amount / buy_amount (WETH per token)
                # We sold sell_amount of WETH, got buy_amount of tokens
                price = sell_amount / buy_amount
                # Apply small jitter if enabled
                if self.config.anti_mev_jitter:
                    price = apply_jitter(price, jitter_percent=0.05)
                return price
            return None
        
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Price fetch failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Status: {e.response.status_code}")
                self.logger.error(f"Response: {e.response.text[:300]}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error fetching price: {e}")
            return None
    
    def build_swap_transaction(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        slippage_percentage: float = 0.01,
    ) -> QuoteResult:
        """
        Build a swap transaction using 0x API.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            taker_address: Address of the taker.
            slippage_percentage: Slippage tolerance percentage.
            
        Returns:
            QuoteResult: Transaction data or error.
        """
        return self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
            slippage_percentage=slippage_percentage,
            apply_jitter_to_price=False,  # Don't jitter actual swap quotes
        )
    
    def get_swap_transaction_params(
        self,
        quote: QuoteResult,
        from_address: str,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
    ) -> dict:
        """
        Build transaction parameters from a quote.
        
        Args:
            quote: QuoteResult from get_quote.
            from_address: Sender address.
            nonce: Transaction nonce.
            max_fee_per_gas: Maximum fee per gas.
            max_priority_fee_per_gas: Maximum priority fee per gas.
            
        Returns:
            dict: Transaction parameters.
        """
        if not quote.success or not quote.data:
            raise ValueError("Invalid quote")
        
        return {
            "from": Web3.to_checksum_address(from_address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": int(quote.gas * 1.2),  # Add 20% buffer
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
            "nonce": nonce,
            "chainId": self.chain_id,
            "type": 2,  # EIP-1559
        }
    
    def get_sources(self) -> list[dict]:
        """
        Get available liquidity sources for the current chain.
        
        Returns:
            list[dict]: List of liquidity sources.
        """
        try:
            url = f"{self.base_url}/swap/v1/sources"
            
            response = requests.get(
                url,
                headers=self.headers,
                params={"chainId": self.chain_id},
                timeout=10,
            )
            
            response.raise_for_status()
            return response.json().get("sources", [])
        
        except Exception as e:
            self.logger.error(f"Failed to get sources: {e}")
            return []
    
    def estimate_gas(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
    ) -> Optional[int]:
        """
        Estimate gas for a swap.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell.
            taker_address: Address of the taker.
            
        Returns:
            Optional[int]: Estimated gas or None.
        """
        result = self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
        )
        
        if result.success:
            return result.gas
        return None