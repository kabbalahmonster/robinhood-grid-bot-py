"""
Main Grid Trading Bot implementation for Robinhood Chain.

Implements a dynamic grid trading strategy using 0x Protocol for swaps,
with automatic position management and profit banking to USDG.
"""

import time
import signal
import logging
from typing import Optional
from dataclasses import dataclass
from decimal import Decimal

from web3 import Web3

from config import BotConfig, load_config
from wallet import Wallet, TransactionResult
from zero_x import ZeroXClient, QuoteResult
from storage import Storage, Position, BotState
from utils import (
    setup_logging,
    calculate_grid_levels,
    calculate_profit_margin,
    calculate_dynamic_buy_amount,
    format_token_amount,
    format_address,
    wei_to_eth,
    eth_to_wei,
)


@dataclass
class GridLevel:
    """Represents a grid price level."""
    price: float
    position_id: Optional[int] = None
    filled: bool = False


class GridBot:
    """
    Grid Trading Bot for EVM chains using 0x Protocol.
    
    Implements a dynamic grid strategy where:
    - Buy orders are placed at decreasing price levels
    - Each position tracks cost basis for profit calculations
    - Profits are banked to USDG when positions close
    - Buy amounts are dynamically calculated based on available WETH
    """
    
    def __init__(self, config: BotConfig):
        """
        Initialize the grid bot.
        
        Args:
            config: Bot configuration.
        """
        self.config = config
        self.logger = setup_logging(config.log_level)
        
        self.logger.info(f"Initializing Grid Bot for {config.chain_name}")
        self.logger.info(f"Trading {config.token_symbol} against WETH")
        
        # Initialize components
        self.wallet = Wallet(config)
        self.zero_x = ZeroXClient(config)
        self.storage = Storage(config.state_file)
        
        # Load existing state
        self.state = self.storage.load_state()
        self.grid_levels: list[GridLevel] = []
        
        # Token addresses
        self.token_address = Web3.to_checksum_address(config.token_address)
        self.weth_address = Web3.to_checksum_address(config.weth_address)
        self.usdg_address = Web3.to_checksum_address(config.usdg_address)
        
        # Bot state
        self.running = False
        self.current_price: Optional[float] = None
        self.token_decimals: int = 18
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        self.logger.info("Grid Bot initialized successfully")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.logger.info("Shutdown signal received, stopping bot...")
        self.running = False
    
    def initialize(self) -> bool:
        """
        Initialize bot state and validate configuration.
        
        Returns:
            bool: True if initialization successful.
        """
        try:
            # Get token info
            token_info = self.wallet.get_token_info(self.token_address)
            self.token_decimals = token_info.decimals
            
            self.logger.info(f"Token: {token_info.symbol} ({token_info.decimals} decimals)")
            
            # Check wallet balances
            eth_balance = self.wallet.get_eth_balance()
            weth_balance, _ = self.wallet.get_token_balance(self.weth_address)
            token_balance, _ = self.wallet.get_token_balance(self.token_address)
            
            self.logger.info(f"ETH Balance: {eth_balance:.6f}")
            self.logger.info(f"WETH Balance: {weth_balance:.6f}")
            self.logger.info(f"Token Balance: {token_balance:.6f} {token_info.symbol}")
            
            # Verify we have WETH for trading
            if weth_balance < self.config.initial_buy_amount:
                self.logger.warning(
                    f"Low WETH balance ({weth_balance:.6f}). "
                    f"Recommended: at least {self.config.initial_buy_amount:.6f} WETH"
                )
            
            # Ensure approvals are set
            self.logger.info("Checking token approvals...")
            
            # Approve WETH for Permit2
            weth_approved = self.wallet.ensure_approval(
                self.weth_address,
                self.config.permit2_address,
                eth_to_wei(1000),  # Approve 1000 WETH
                use_permit2=False,  # Standard approval for Permit2 contract
            )
            
            # Approve token for Permit2
            token_approved = self.wallet.ensure_approval(
                self.token_address,
                self.config.permit2_address,
                eth_to_wei(1000000),  # Approve large amount
                use_permit2=False,
            )
            
            if not weth_approved or not token_approved:
                self.logger.error("Failed to set token approvals")
                return False
            
            # Get current price and initialize grid
            self._update_price()
            if self.current_price:
                self._initialize_grid_levels()
            
            self.logger.info("Initialization complete")
            return True
        
        except Exception as e:
            self.logger.error(f"Initialization failed: {e}")
            return False
    
    def _update_price(self) -> bool:
        """
        Update current token price via 0x API.
        
        Returns:
            bool: True if price updated successfully.
        """
        try:
            # Get price by quoting small WETH amount for token
            quote_amount = eth_to_wei(0.001)  # 0.001 WETH
            
            quote = self.zero_x.get_quote(
                sell_token=self.weth_address,
                buy_token=self.token_address,
                sell_amount=quote_amount,
            )
            
            if quote.success and quote.price:
                # Price is token per WETH, convert to ETH per token
                self.current_price = 1.0 / quote.price
                self.storage.update_last_price(self.current_price)
                return True
            else:
                self.logger.warning(f"Failed to get price: {quote.error}")
                return False
        
        except Exception as e:
            self.logger.error(f"Error updating price: {e}")
            return False
    
    def _initialize_grid_levels(self) -> None:
        """Initialize grid price levels based on current price."""
        if not self.current_price:
            return
        
        grid_spacing_decimal = self.config.grid_spacing_percent / 100.0
        
        # Calculate grid levels below current price
        prices = calculate_grid_levels(
            self.current_price,
            grid_spacing_decimal,
            self.config.max_positions,
        )
        
        # Create grid level objects
        self.grid_levels = [GridLevel(price=price) for price in prices]
        
        # Match existing open positions to grid levels
        open_positions = self.storage.get_open_positions()
        for position in open_positions:
            # Find closest grid level
            for level in self.grid_levels:
                # Allow 1% tolerance for matching
                tolerance = level.price * 0.01
                if abs(position.buy_price - level.price) <= tolerance:
                    level.position_id = position.id
                    level.filled = True
                    break
        
        self.logger.info(f"Initialized {len(self.grid_levels)} grid levels")
        self.logger.info(f"Price range: {prices[-1]:.6f} - {prices[0]:.6f} ETH")
    
    def _get_empty_grid_levels(self) -> list[GridLevel]:
        """Get grid levels that don't have positions."""
        return [level for level in self.grid_levels if not level.filled]
    
    def _get_open_positions(self) -> list[Position]:
        """Get all open positions from storage."""
        return self.storage.get_open_positions()
    
    def _calculate_buy_amount(self) -> float:
        """
        Calculate dynamic buy amount based on available WETH.
        
        Distributes WETH balance evenly across empty grid positions.
        
        Returns:
            float: Buy amount in WETH.
        """
        weth_balance, _ = self.wallet.get_token_balance(self.weth_address)
        empty_levels = self._get_empty_grid_levels()
        
        if not empty_levels:
            return 0.0
        
        # Reserve some WETH for gas (0.01 ETH worth)
        reserved = 0.01
        available = max(0, weth_balance - reserved)
        
        return calculate_dynamic_buy_amount(
            available,
            len(empty_levels),
            min_buy_amount=0.001,
        )
    
    def _should_buy(self, level: GridLevel) -> bool:
        """
        Check if we should execute a buy at this grid level.
        
        Args:
            level: Grid level to check.
            
        Returns:
            bool: True if buy should be executed.
        """
        if not self.current_price:
            return False
        
        # Check if level is already filled
        if level.filled:
            return False
        
        # Check if we have capacity for more positions
        open_positions = self._get_open_positions()
        if len(open_positions) >= self.config.max_positions:
            return False
        
        # Check if current price is at or below grid level
        # Allow small buffer (0.5%) for execution
        trigger_price = level.price * 1.005
        
        return self.current_price <= trigger_price
    
    def _should_sell(self, position: Position) -> bool:
        """
        Check if we should sell a position for profit.
        
        Args:
            position: Position to evaluate.
            
        Returns:
            bool: True if sell should be executed.
        """
        if not self.current_price or not position.is_open:
            return False
        
        # Calculate profit margin
        profit_percent = calculate_profit_margin(
            self.current_price,
            position.cost_basis,
        )
        
        # Check if profit meets minimum threshold
        return profit_percent >= self.config.min_profit_percent
    
    def _execute_buy(self, level: GridLevel) -> bool:
        """
        Execute a buy order at a grid level.
        
        Args:
            level: Grid level to buy at.
            
        Returns:
            bool: True if buy successful.
        """
        buy_amount_eth = self._calculate_buy_amount()
        
        if buy_amount_eth < 0.001:
            self.logger.warning("Buy amount too small, skipping")
            return False
        
        self.logger.info(
            f"Executing BUY at {level.price:.6f} ETH "
            f"with {buy_amount_eth:.6f} WETH"
        )
        
        try:
            # Get quote for swap
            sell_amount_wei = eth_to_wei(buy_amount_eth)
            
            quote = self.zero_x.build_swap_transaction(
                sell_token=self.weth_address,
                buy_token=self.token_address,
                sell_amount=sell_amount_wei,
                taker_address=self.wallet.address,
                slippage_percentage=self.config.slippage_tolerance / 100,
            )
            
            if not quote.success:
                self.logger.error(f"Failed to get quote: {quote.error}")
                return False
            
            # Build and send transaction
            tx_params = self.zero_x.get_swap_transaction_params(
                quote=quote,
                from_address=self.wallet.address,
                nonce=self.wallet.w3.eth.get_transaction_count(self.wallet.address),
                max_fee_per_gas=self.wallet.w3.eth.max_fee_per_gas,
                max_priority_fee_per_gas=self.wallet.w3.eth.max_priority_fee_per_gas,
            )
            
            # Send transaction
            result = self.wallet._send_transaction(tx_params)
            
            if result.success:
                # Calculate tokens received
                tokens_received = wei_to_eth(quote.buy_amount or 0)
                
                # Create position record
                position = Position(
                    id=self.storage.get_next_position_id(),
                    buy_price=level.price,
                    buy_amount_token=tokens_received,
                    buy_amount_eth=buy_amount_eth,
                    tx_hash_buy=result.tx_hash,
                )
                
                # Save to storage
                self.storage.add_position(position)
                
                # Update grid level
                level.position_id = position.id
                level.filled = True
                
                self.logger.info(
                    f"BUY successful: {format_token_amount(tokens_received, self.token_decimals)} "
                    f"tokens for {buy_amount_eth:.6f} WETH "
                    f"(tx: {format_address(result.tx_hash or '')})"
                )
                
                return True
            else:
                self.logger.error(f"BUY failed: {result.error}")
                return False
        
        except Exception as e:
            self.logger.error(f"Error executing buy: {e}")
            return False
    
    def _execute_sell(self, position: Position) -> bool:
        """
        Execute a sell order for a position.
        
        Args:
            position: Position to sell.
            
        Returns:
            bool: True if sell successful.
        """
        self.logger.info(
            f"Executing SELL for position {position.id} "
            f"(bought at {position.buy_price:.6f})"
        )
        
        try:
            # Calculate token amount to sell (convert to wei)
            token_amount_wei = int(position.buy_amount_token * (10 ** self.token_decimals))
            
            # Get quote for swap
            quote = self.zero_x.build_swap_transaction(
                sell_token=self.token_address,
                buy_token=self.weth_address,
                sell_amount=token_amount_wei,
                taker_address=self.wallet.address,
                slippage_percentage=self.config.slippage_tolerance / 100,
            )
            
            if not quote.success:
                self.logger.error(f"Failed to get sell quote: {quote.error}")
                return False
            
            # Calculate expected profit
            eth_received = wei_to_eth(quote.buy_amount or 0)
            profit_eth = eth_received - position.buy_amount_eth
            profit_percent = calculate_profit_margin(eth_received, position.buy_amount_eth)
            
            self.logger.info(
                f"Expected: {eth_received:.6f} WETH "
                f"(profit: {profit_eth:.6f} WETH / {profit_percent:.2f}%)"
            )
            
            # Double-check profit threshold
            if profit_percent < self.config.min_profit_percent:
                self.logger.warning(
                    f"Profit ({profit_percent:.2f}%) below threshold, aborting sell"
                )
                return False
            
            # Build and send transaction
            tx_params = self.zero_x.get_swap_transaction_params(
                quote=quote,
                from_address=self.wallet.address,
                nonce=self.wallet.w3.eth.get_transaction_count(self.wallet.address),
                max_fee_per_gas=self.wallet.w3.eth.max_fee_per_gas,
                max_priority_fee_per_gas=self.wallet.w3.eth.max_priority_fee_per_gas,
            )
            
            # Send transaction
            result = self.wallet._send_transaction(tx_params)
            
            if result.success:
                # Calculate bank amount
                bank_percentage = self.config.bank_percentage / 100
                bank_amount = profit_eth * bank_percentage
                trade_amount = eth_received - bank_amount
                
                # Close position in storage
                self.storage.close_position(
                    position_id=position.id,
                    sell_price=self.current_price or 0,
                    sell_amount_eth=eth_received,
                    profit_eth=profit_eth,
                    profit_percent=profit_percent,
                    tx_hash=result.tx_hash or "",
                )
                
                # Record profit
                self.storage.record_trade_profit(profit_eth)
                
                # Free up grid level
                for level in self.grid_levels:
                    if level.position_id == position.id:
                        level.filled = False
                        level.position_id = None
                        break
                
                self.logger.info(
                    f"SELL successful: Position {position.id} closed "
                    f"for {eth_received:.6f} WETH "
                    f"(profit: {profit_eth:.6f} WETH, {profit_percent:.2f}%) "
                    f"Bank: {bank_amount:.6f} WETH -> USDG"
                )
                
                # Bank profit to USDG
                if bank_amount > 0.001:
                    self._bank_profit(bank_amount)
                
                return True
            else:
                self.logger.error(f"SELL failed: {result.error}")
                return False
        
        except Exception as e:
            self.logger.error(f"Error executing sell: {e}")
            return False
    
    def _bank_profit(self, amount_eth: float) -> bool:
        """
        Bank profit to USDG.
        
        Args:
            amount_eth: Amount of WETH to convert to USDG.
            
        Returns:
            bool: True if banking successful.
        """
        self.logger.info(f"Banking {amount_eth:.6f} WETH to USDG")
        
        try:
            # Get quote for WETH -> USDG swap
            sell_amount_wei = eth_to_wei(amount_eth)
            
            quote = self.zero_x.build_swap_transaction(
                sell_token=self.weth_address,
                buy_token=self.usdg_address,
                sell_amount=sell_amount_wei,
                taker_address=self.wallet.address,
                slippage_percentage=self.config.slippage_tolerance / 100,
            )
            
            if not quote.success:
                self.logger.error(f"Failed to get banking quote: {quote.error}")
                return False
            
            # Build and send transaction
            tx_params = self.zero_x.get_swap_transaction_params(
                quote=quote,
                from_address=self.wallet.address,
                nonce=self.wallet.w3.eth.get_transaction_count(self.wallet.address),
                max_fee_per_gas=self.wallet.w3.eth.max_fee_per_gas,
                max_priority_fee_per_gas=self.wallet.w3.eth.max_priority_fee_per_gas,
            )
            
            result = self.wallet._send_transaction(tx_params)
            
            if result.success:
                self.logger.info(
                    f"Banking successful: {amount_eth:.6f} WETH -> USDG "
                    f"(tx: {format_address(result.tx_hash or '')})"
                )
                return True
            else:
                self.logger.error(f"Banking failed: {result.error}")
                return False
        
        except Exception as e:
            self.logger.error(f"Error banking profit: {e}")
            return False
    
    def _check_and_execute_trades(self) -> None:
        """Check for trade opportunities and execute."""
        # Check sells first (take profits)
        open_positions = self._get_open_positions()
        for position in open_positions:
            if self._should_sell(position):
                self._execute_sell(position)
                # Small delay between trades
                time.sleep(2)
        
        # Check buys
        empty_levels = self._get_empty_grid_levels()
        for level in empty_levels:
            if self._should_buy(level):
                self._execute_buy(level)
                # Small delay between trades
                time.sleep(2)
    
    def _log_status(self) -> None:
        """Log current bot status."""
        try:
            stats = self.storage.get_stats()
            weth_balance, _ = self.wallet.get_token_balance(self.weth_address)
            token_balance, _ = self.wallet.get_token_balance(self.token_address)
            
            self.logger.info(
                f"Status | Price: {self.current_price:.6f} ETH | "
                f"WETH: {weth_balance:.4f} | "
                f"{self.config.token_symbol}: {token_balance:.4f} | "
                f"Positions: {stats['open_positions']}/{self.config.max_positions} | "
                f"Profit: {stats['total_profit_eth']:.6f} ETH"
            )
        except Exception as e:
            self.logger.error(f"Error logging status: {e}")
    
    def run(self) -> None:
        """Main bot loop."""
        self.logger.info("Starting Grid Bot main loop")
        self.running = True
        
        cycle_count = 0
        
        while self.running:
            try:
                # Update price
                price_updated = self._update_price()
                
                if price_updated and self.current_price:
                    # Reinitialize grid if needed (price moved significantly)
                    if not self.grid_levels:
                        self._initialize_grid_levels()
                    
                    # Check and execute trades
                    self._check_and_execute_trades()
                
                # Log status periodically
                cycle_count += 1
                if cycle_count % 10 == 0:
                    self._log_status()
                
                # Wait for next poll
                time.sleep(self.config.poll_interval_seconds)
            
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}")
                time.sleep(self.config.poll_interval_seconds)
        
        self.logger.info("Grid Bot stopped")
    
    def run_once(self) -> None:
        """Run a single iteration (for testing)."""
        self.logger.info("Running single iteration")
        
        # Update price
        price_updated = self._update_price()
        
        if price_updated and self.current_price:
            if not self.grid_levels:
                self._initialize_grid_levels()
            
            # Check and execute trades
            self._check_and_execute_trades()
        
        self._log_status()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Grid Trading Bot for Robinhood Chain")
    parser.add_argument(
        "--env",
        type=str,
        default=".env",
        help="Path to environment file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run single iteration and exit",
    )
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config(args.env)
    
    # Create and initialize bot
    bot = GridBot(config)
    
    if not bot.initialize():
        print("Failed to initialize bot")
        return 1
    
    # Run bot
    if args.once:
        bot.run_once()
    else:
        bot.run()
    
    return 0


if __name__ == "__main__":
    exit(main())