"""
Utility functions for the Robinhood Chain Grid Trading Bot.

Provides logging setup, mathematical helpers, and common utilities
used across the bot modules.
"""

import os
import sys
import random
import logging
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP
from colorlog import ColoredFormatter


def setup_logging(log_level: str = "INFO", log_to_file: bool = True) -> logging.Logger:
    """
    Configure colored logging for the bot.
    
    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR).
        log_to_file: Whether to also log to a file.
        
    Returns:
        logging.Logger: Configured logger instance.
    """
    # Create logs directory if needed
    if log_to_file:
        os.makedirs("logs", exist_ok=True)
    
    # Get the logger
    logger = logging.getLogger("grid_bot")
    logger.setLevel(getattr(logging, log_level.upper()))
    
    # Remove existing handlers
    logger.handlers = []
    
    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper()))
    
    # Colored formatter
    color_formatter = ColoredFormatter(
        "%(log_color)s%(asctime)s | %(levelname)-8s | %(name)s | %(message)s%(reset)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
        secondary_log_colors={},
        style="%",
    )
    console_handler.setFormatter(color_formatter)
    logger.addHandler(console_handler)
    
    # File handler (without colors)
    if log_to_file:
        file_handler = logging.FileHandler("logs/bot.log")
        file_handler.setLevel(getattr(logging, log_level.upper()))
        file_formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    return logger


def format_token_amount(amount: int, decimals: int = 18) -> str:
    """
    Format a token amount from wei to human-readable.
    
    Args:
        amount: The amount in smallest unit (wei).
        decimals: Token decimals (default 18 for ETH).
        
    Returns:
        str: Formatted amount string.
    """
    decimal_amount = Decimal(amount) / Decimal(10 ** decimals)
    return f"{decimal_amount:.6f}"


def format_usd_amount(amount: float) -> str:
    """
    Format a USD amount with appropriate precision.
    
    Args:
        amount: The USD amount.
        
    Returns:
        str: Formatted USD string.
    """
    if amount >= 1000:
        return f"${amount:,.2f}"
    elif amount >= 1:
        return f"${amount:.2f}"
    else:
        return f"${amount:.4f}"


def calculate_percentage_change(current: float, previous: float) -> float:
    """
    Calculate percentage change between two values.
    
    Args:
        current: Current value.
        previous: Previous value.
        
    Returns:
        float: Percentage change (can be negative).
    """
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def apply_jitter(value: float, jitter_percent: float = 0.1) -> float:
    """
    Apply random jitter to a value for anti-MEV protection.
    
    Args:
        value: Original value.
        jitter_percent: Maximum jitter percentage.
        
    Returns:
        float: Value with jitter applied.
    """
    jitter_range = value * (jitter_percent / 100)
    jitter = random.uniform(-jitter_range, jitter_range)
    return value + jitter


def calculate_grid_levels(
    current_price: float,
    grid_spacing: float,
    num_levels: int,
) -> list[float]:
    """
    Calculate grid price levels below current price.
    
    Args:
        current_price: Current market price.
        grid_spacing: Grid spacing as decimal (e.g., 0.05 for 5%).
        num_levels: Number of grid levels to calculate.
        
    Returns:
        list[float]: List of grid price levels (descending).
    """
    levels = []
    for i in range(1, num_levels + 1):
        level_price = current_price * ((1 - grid_spacing) ** i)
        levels.append(level_price)
    return levels


def calculate_profit_margin(
    sell_price: float,
    buy_price: float,
) -> float:
    """
    Calculate profit margin percentage.
    
    Args:
        sell_price: Price at which token would be sold.
        buy_price: Price at which token was bought.
        
    Returns:
        float: Profit margin as percentage.
    """
    if buy_price == 0:
        return 0.0
    return ((sell_price - buy_price) / buy_price) * 100


def calculate_dynamic_buy_amount(
    available_balance: float,
    num_empty_positions: int,
    min_buy_amount: float = 0.001,
) -> float:
    """
    Calculate dynamic buy amount based on available balance and empty positions.
    
    Distributes available balance evenly across remaining empty positions
    while respecting a minimum buy amount.
    
    Args:
        available_balance: Available WETH balance.
        num_empty_positions: Number of empty grid positions.
        min_buy_amount: Minimum amount per buy.
        
    Returns:
        float: Calculated buy amount in WETH.
    """
    if num_empty_positions <= 0:
        return 0.0
    
    # Divide balance evenly across positions
    amount_per_position = available_balance / num_empty_positions
    
    # Ensure we don't go below minimum
    return max(amount_per_position, min_buy_amount)


def wei_to_eth(wei_amount: int) -> float:
    """
    Convert wei to ETH.
    
    Args:
        wei_amount: Amount in wei.
        
    Returns:
        float: Amount in ETH.
    """
    return wei_amount / 10**18


def eth_to_wei(eth_amount: float) -> int:
    """
    Convert ETH to wei.
    
    Args:
        eth_amount: Amount in ETH.
        
    Returns:
        int: Amount in wei.
    """
    return int(eth_amount * 10**18)


def format_address(address: str, length: int = 6) -> str:
    """
    Format an Ethereum address for display.
    
    Args:
        address: Full Ethereum address.
        length: Number of characters to show at start and end.
        
    Returns:
        str: Truncated address (e.g., 0x1234...5678).
    """
    if len(address) <= 2 + (length * 2):
        return address
    return f"{address[:2 + length]}...{address[-length:]}"


def truncate_decimal(value: Decimal, decimals: int = 18) -> Decimal:
    """
    Truncate a Decimal to specified decimal places.
    
    Args:
        value: Decimal value to truncate.
        decimals: Number of decimal places to keep.
        
    Returns:
        Decimal: Truncated value.
    """
    quantize_str = "0." + "0" * decimals
    return value.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)


class RateLimiter:
    """
    Simple rate limiter for API calls.
    
    Tracks last call time and enforces minimum intervals between calls.
    """
    
    def __init__(self, min_interval_seconds: float = 1.0):
        """
        Initialize rate limiter.
        
        Args:
            min_interval_seconds: Minimum seconds between calls.
        """
        self.min_interval = min_interval_seconds
        self._last_call_time: Optional[float] = None
    
    def can_call(self) -> bool:
        """
        Check if enough time has passed since last call.
        
        Returns:
            bool: True if call is allowed.
        """
        import time
        
        if self._last_call_time is None:
            return True
        
        elapsed = time.time() - self._last_call_time
        return elapsed >= self.min_interval
    
    def record_call(self) -> None:
        """Record that a call was made."""
        import time
        self._last_call_time = time.time()
    
    def wait_if_needed(self) -> None:
        """Wait if necessary before making next call."""
        import time
        
        if self._last_call_time is not None:
            elapsed = time.time() - self._last_call_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        
        self.record_call()


def calculate_gas_cost(gas_used: int, gas_price_gwei: float) -> float:
    """
    Calculate transaction cost in ETH.
    
    Args:
        gas_used: Gas units consumed.
        gas_price_gwei: Gas price in gwei.
        
    Returns:
        float: Transaction cost in ETH.
    """
    return (gas_used * gas_price_gwei) / 1e9


def is_profitable_after_gas(
    expected_profit_eth: float,
    gas_estimate: int,
    gas_price_gwei: float,
    min_profit_threshold_eth: float = 0.0001,
) -> bool:
    """
    Check if a trade would be profitable after gas costs.
    
    Args:
        expected_profit_eth: Expected profit in ETH.
        gas_estimate: Estimated gas units.
        gas_price_gwei: Current gas price in gwei.
        min_profit_threshold_eth: Minimum profit threshold.
        
    Returns:
        bool: True if trade is profitable after gas.
    """
    gas_cost = calculate_gas_cost(gas_estimate, gas_price_gwei)
    net_profit = expected_profit_eth - gas_cost
    return net_profit >= min_profit_threshold_eth