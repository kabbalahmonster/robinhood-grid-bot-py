"""
LI.FI API integration module for the Robinhood Chain Grid Trading Bot.

Handles quote fetching and swap execution using LI.FI's cross-chain
swap aggregation API as an alternative to 0x Protocol.
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
    """LI.FI API quote result dataclass."""
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


class LiFiClient:
    """
    Client for interacting with the LI.FI API.
    
    Provides quote fetching and swap execution for token trades
    across multiple DEXs with optimal routing.
    """
    
    def __init__(self, config: BotConfig):
        """
        Initialize LI.FI client.
        
        Args:
            config: Bot configuration with API key and chain settings.
        """
        self.config = config
        self.logger = logging.getLogger("grid_bot.li_fi")
        
        # LI.FI API v1 base URL
        self.base_url = "https://li.quest/v1"
        self.api_key = getattr(config, 'li_fi_api_key', None)
        
        # Chain ID mapping for LI.FI
        self.chain_id = config.chain_id
        
        # Headers for API requests
        self.headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            self.headers["x-lifi-api-key"] = self.api_key
        
        lifi_chain = self._get_lifi_chain_id(self.chain_id)
        self.logger.info(f"LI.FI Client initialized for chain {self.chain_id} -> {lifi_chain}")
    
    def _get_lifi_chain_id(self, chain_id: int) -> int:
        """Get LI.FI chain ID. LI.FI uses numeric chain IDs, not string codes."""
        # LI.FI uses the actual chain ID numbers
        # See: https://docs.li.fi/ for supported chains
        supported_chains = [1, 8453, 4663]  # ETH, Base, Robinhood
        if chain_id not in supported_chains:
            self.logger.error(f"Chain {chain_id} not supported by LI.FI. Supported: {supported_chains}")
        return chain_id
    
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
        Get a quote from the LI.FI API.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            buy_amount: Amount to buy (in base units).
            taker_address: Address of the taker.
            slippage_percentage: Slippage tolerance percentage.
            apply_jitter_to_price: Whether to apply anti-MEV jitter.
            
        Returns:
            QuoteResult: Quote information or error.
        """
        # LI.FI uses different parameter format
        params = {
            "fromChain": self._get_lifi_chain_id(self.chain_id),
            "toChain": self._get_lifi_chain_id(self.chain_id),
            "fromToken": sell_token,
            "toToken": buy_token,
        }
        
        # Must specify either fromAmount or toAmount
        if sell_amount:
            params["fromAmount"] = str(sell_amount)
        elif buy_amount:
            params["toAmount"] = str(buy_amount)
        else:
            return QuoteResult(
                success=False,
                error="Must specify either sell_amount or buy_amount",
            )
        
        # Optional parameters
        if taker_address:
            params["fromAddress"] = taker_address
            params["toAddress"] = taker_address  # Same as fromAddress for single-chain swaps
        
        # Add integrator for proper API key attribution and dashboard tracking
        if getattr(self.config, 'li_fi_integrator', None):
            params["integrator"] = self.config.li_fi_integrator
        
        # Add anti-MEV jitter if enabled
        if apply_jitter_to_price and self.config.anti_mev_jitter:
            import random
            import time
            time.sleep(random.uniform(0.1, 0.5))
        
        try:
            # Use LI.FI quote endpoint
            url = f"{self.base_url}/quote"
            
            self.logger.debug(f"Fetching LI.FI quote: {params}")
            self.logger.debug(f"LI.FI API URL: {url}")
            # Debug: show headers (redact API key partially)
            debug_headers = self.headers.copy()
            if 'x-lifi-api-key' in debug_headers:
                key = debug_headers['x-lifi-api-key']
                debug_headers['x-lifi-api-key'] = key[:10] + '...' + key[-4:] if len(key) > 14 else '***'
            self.logger.debug(f"LI.FI headers: {debug_headers}")
            
            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            
            self.logger.debug(f"LI.FI API response status: {response.status_code}")
            
            # Log full response for debugging
            try:
                debug_data = response.json()
                self.logger.debug(f"LI.FI response: {json.dumps(debug_data, indent=2)[:1000]}")
            except:
                self.logger.debug(f"LI.FI raw response: {response.text[:500]}")
            
            if response.status_code != 200:
                self.logger.error(f"LI.FI API error: Status {response.status_code}")
                self.logger.error(f"Response: {response.text[:500]}")
                return QuoteResult(
                    success=False,
                    error=f"LI.FI API returned status {response.status_code}: {response.text[:200]}",
                )
            
            response.raise_for_status()
            data = response.json()
            
            # Extract quote information from LI.FI response
            # toAmount is in estimate, not action!
            action = data.get("action", {})
            estimate = data.get("estimate", {})
            
            # Parse amounts - fromAmount is in action, toAmount is in estimate
            from_amount_str = action.get("fromAmount", "0")
            to_amount_str = estimate.get("toAmount", "0")  # <-- FIXED: use estimate, not action
            
            self.logger.debug(f"LI.FI raw amounts - from: {from_amount_str}, to: {to_amount_str}")
            
            # Convert to int (handle both hex and decimal strings)
            if isinstance(from_amount_str, str) and from_amount_str.startswith("0x"):
                from_amount = int(from_amount_str, 16)
            else:
                from_amount = int(from_amount_str)
            
            if isinstance(to_amount_str, str) and to_amount_str.startswith("0x"):
                to_amount = int(to_amount_str, 16)
            else:
                to_amount = int(to_amount_str)
            
            # Check if quote is valid
            if to_amount == 0:
                self.logger.error(f"LI.FI returned zero tokens - check estimate.toAmount in response")
                return QuoteResult(
                    success=False,
                    error="LI.FI returned zero tokens",
                )
            
            # Calculate price (sell/buy ratio)
            price = from_amount / to_amount
            
            # Apply jitter to price if requested
            if apply_jitter_to_price and self.config.anti_mev_jitter:
                price = apply_jitter(price, jitter_percent=0.05)
            
            # Get transaction data if available
            transaction_request = data.get("transactionRequest", {})
            
            # IMPORTANT: Use estimate.approvalAddress for ERC20 approvals (not Permit2!)
            approval_address = estimate.get("approvalAddress")
            
            self.logger.debug(f"LI.FI quote: buy={to_amount}, sell={from_amount}")
            self.logger.debug(f"LI.FI approval address: {approval_address}")
            
            # Parse transaction values (may be hex strings)
            def parse_hex_or_int(val, default=0):
                if val is None:
                    return default
                if isinstance(val, str) and val.startswith("0x"):
                    return int(val, 16)
                return int(val)
            
            return QuoteResult(
                success=True,
                price=price,
                buy_amount=to_amount,
                sell_amount=from_amount,
                allowance_target=approval_address,  # Use estimate.approvalAddress
                data=transaction_request.get("data"),
                to=transaction_request.get("to"),
                value=parse_hex_or_int(transaction_request.get("value"), 0),
                gas=parse_hex_or_int(transaction_request.get("gasLimit"), 200000),
                gas_price=parse_hex_or_int(transaction_request.get("gasPrice"), 0),
                raw_response=data,
            )
        
        except requests.exceptions.RequestException as e:
            error_msg = f"LI.FI request failed: {e}"
            self.logger.error(error_msg)
            
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Status: {e.response.status_code}")
                try:
                    error_text = e.response.text[:500]
                    self.logger.error(f"Response: {error_text}")
                except:
                    pass
            
            return QuoteResult(success=False, error=error_msg)
        
        except Exception as e:
            error_msg = f"LI.FI unexpected error: {e}"
            self.logger.error(error_msg)
            return QuoteResult(success=False, error=error_msg)
    
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
        
        self.logger.debug(f"LI.FI price fetch failed: {result.error}")
        return None
    
    def build_swap_transaction(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        slippage_percentage: float = 0.02,
    ) -> QuoteResult:
        """
        Build a swap transaction using LI.FI API.
        
        This is a convenience wrapper around get_quote for transaction building.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            taker_address: Address executing the swap.
            slippage_percentage: Slippage tolerance (default 2%).
            
        Returns:
            QuoteResult: Transaction data or error.
        """
        return self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
            slippage_percentage=slippage_percentage,
            apply_jitter_to_price=True,
        )
    
    def refresh_quote(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker_address: str,
        slippage_percentage: float = 0.02,
    ) -> QuoteResult:
        """
        Refresh a quote after approval.
        
        IMPORTANT: Per LI.FI documentation, always refresh the quote after
        waiting for token approval. Gas prices, calldata, and routes may
        have changed while waiting for confirmation.
        
        Args:
            sell_token: Address of token to sell.
            buy_token: Address of token to buy.
            sell_amount: Amount to sell (in base units).
            taker_address: Address executing the swap.
            slippage_percentage: Slippage tolerance.
            
        Returns:
            QuoteResult: Fresh transaction data or error.
        """
        self.logger.info("Refreshing LI.FI quote after approval...")
        return self.get_quote(
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker_address=taker_address,
            slippage_percentage=slippage_percentage,
            apply_jitter_to_price=True,
        )
