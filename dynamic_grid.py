"""
Dynamic Grid Mode for Robinhood Chain Grid Trading Bot.

Implements P&L-based buy/sell triggers instead of fixed price ranges.
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from decimal import Decimal
import time


@dataclass
class DynamicPosition:
    """Extended position with P&L tracking for dynamic grid mode."""
    id: int
    buy_price: float  # Price when position was created
    sell_target: float  # Target sell price based on profit threshold
    stop_loss: float = 0.0  # Optional stop loss price (0 = disabled)
    balance: int = 0  # Token balance in wei
    cost: int = 0  # Cost in nano-WETH
    status: str = "EMPTY"  # EMPTY, HOLDING, SOLD
    created_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    
    # P&L tracking
    current_pnl_percent: float = 0.0
    peak_pnl_percent: float = 0.0
    lowest_pnl_percent: float = 0.0
    
    # Trading data
    buy_tx: Optional[str] = None
    sell_tx: Optional[str] = None
    sell_price: Optional[float] = None
    profit: Optional[float] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'buy_price': self.buy_price,
            'sell_target': self.sell_target,
            'stop_loss': self.stop_loss,
            'balance': self.balance,
            'cost': self.cost,
            'status': self.status,
            'created_at': self.created_at,
            'closed_at': self.closed_at,
            'current_pnl_percent': self.current_pnl_percent,
            'peak_pnl_percent': self.peak_pnl_percent,
            'lowest_pnl_percent': self.lowest_pnl_percent,
            'buy_tx': self.buy_tx,
            'sell_tx': self.sell_tx,
            'sell_price': self.sell_price,
            'profit': self.profit,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'DynamicPosition':
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DynamicGridConfig:
    """Configuration for dynamic grid mode."""
    enabled: bool = False
    buy_threshold_percent: float = -10.0  # Buy when top position down X%
    sell_threshold_percent: float = 8.0   # Sell at X% profit
    stop_loss_percent: float = 0.0        # 0 = disabled
    max_active_positions: int = 4
    min_buy_interval_seconds: int = 30    # Minimum time between buys
    use_trailing_stop: bool = False
    trailing_stop_percent: float = 5.0
    trailing_activation_percent: float = 3.0


class DynamicGridCalculator:
    """
    Calculates P&L and buy/sell triggers for dynamic grid mode.
    """
    
    @staticmethod
    def calculate_pnl_percent(buy_price: float, current_price: float) -> float:
        """
        Calculate P&L percentage.
        
        Returns:
            P&L as percentage (positive = profit, negative = loss)
        """
        # Validate inputs
        if buy_price <= 0:
            return 0.0
        if not isinstance(current_price, (int, float)):
            return 0.0
        if current_price < 0:
            return 0.0
        return ((current_price - buy_price) / buy_price) * 100
    
    @staticmethod
    def calculate_pnl_eth(cost_nano: int, balance_wei: int, 
                          buy_price: float, current_price: float) -> float:
        """
        Calculate P&L in ETH terms.
        
        Args:
            cost_nano: Cost in nano-WETH (10^-9)
            balance_wei: Token balance in wei (10^-18)
            buy_price: Price at purchase in WETH
            current_price: Current price in WETH
            
        Returns:
            P&L in WETH
        """
        if balance_wei == 0 or buy_price == 0:
            return 0.0
        
        # Current value = tokens * current_price
        current_value = (balance_wei / 1e18) * current_price
        cost = cost_nano / 1e9
        
        return current_value - cost
    
    @classmethod
    def update_position_pnl(cls, position: DynamicPosition, 
                           current_price: float) -> DynamicPosition:
        """
        Update position's current P&L and track peaks.
        
        Args:
            position: Position to update
            current_price: Current token price
            
        Returns:
            Updated position
        """
        if position.status != "HOLDING":
            return position
        
        pnl = cls.calculate_pnl_percent(position.buy_price, current_price)
        position.current_pnl_percent = pnl
        
        # Track peak and lowest P&L
        if pnl > position.peak_pnl_percent:
            position.peak_pnl_percent = pnl
        if pnl < position.lowest_pnl_percent:
            position.lowest_pnl_percent = pnl
        
        return position
    
    @classmethod
    def find_top_position(cls, positions: Dict[int, DynamicPosition]) -> Optional[DynamicPosition]:
        """
        Find the TOP position (lowest buy price) among HOLDING positions.
        
        In dynamic grid mode, the "top" position is the one bought at the 
        lowest price - this is the position we monitor for buy triggers.
        
        Args:
            positions: Dictionary of positions
            
        Returns:
            Top position or None if no holding positions
        """
        holding = [p for p in positions.values() if p.status == "HOLDING"]
        if not holding:
            return None
        
        # Find position with lowest buy price
        return min(holding, key=lambda p: p.buy_price)
    
    @classmethod
    def should_buy(cls, positions: Dict[int, DynamicPosition],
                   current_price: float,
                   config: DynamicGridConfig,
                   last_buy_time: float = 0) -> Tuple[bool, str, Optional[DynamicPosition]]:
        """
        Determine if we should buy based on dynamic grid rules.
        
        Buy Rules:
        1. Buy if no positions exist (empty grid)
        2. Buy if TOP position (lowest buy price) has P&L below threshold
        
        Args:
            positions: Current positions
            current_price: Current token price
            config: Dynamic grid configuration
            last_buy_time: Timestamp of last buy (for rate limiting)
            
        Returns:
            Tuple of (should_buy, reason, top_position)
        """
        if not config.enabled:
            return False, "Dynamic mode disabled", None
        
        # Check rate limiting
        time_since_last = time.time() - last_buy_time
        if time_since_last < config.min_buy_interval_seconds:
            return False, f"Rate limited ({time_since_last:.0f}s < {config.min_buy_interval_seconds}s)", None
        
        # Count active positions
        holding_count = sum(1 for p in positions.values() if p.status == "HOLDING")
        
        # Rule 1: Buy if no positions
        if holding_count == 0:
            return True, "Empty grid - initial buy", None
        
        # Check max positions
        if holding_count >= config.max_active_positions:
            return False, f"Max positions reached ({holding_count}/{config.max_active_positions})", None
        
        # Rule 2: Buy if TOP position P&L is below threshold
        top_position = cls.find_top_position(positions)
        if not top_position:
            return True, "No holding positions found", None
        
        # Calculate TOP position's current P&L
        top_pnl = cls.calculate_pnl_percent(top_position.buy_price, current_price)
        
        if top_pnl <= config.buy_threshold_percent:
            return True, f"Top position P&L {top_pnl:.2f}% <= threshold {config.buy_threshold_percent}%", top_position
        
        return False, f"Top position P&L {top_pnl:.2f}% > threshold {config.buy_threshold_percent}%", top_position
    
    @classmethod
    def should_sell(cls, position: DynamicPosition,
                   current_price: float,
                   config: DynamicGridConfig) -> Tuple[bool, str]:
        """
        Determine if a position should be sold.
        
        Sell Rules:
        1. Sell if P&L >= sell_threshold_percent
        2. Sell if stop loss enabled and P&L <= -stop_loss_percent
        3. Sell if trailing stop triggered
        
        Args:
            position: Position to check
            current_price: Current token price
            config: Dynamic grid configuration
            
        Returns:
            Tuple of (should_sell, reason)
        """
        if position.status != "HOLDING":
            return False, "Position not holding"
        
        if not config.enabled:
            return False, "Dynamic mode disabled"
        
        pnl = cls.calculate_pnl_percent(position.buy_price, current_price)
        
        # Check stop loss first (if enabled)
        if config.stop_loss_percent > 0 and pnl <= -config.stop_loss_percent:
            return True, f"Stop loss triggered: {pnl:.2f}% <= -{config.stop_loss_percent}%"
        
        # Check trailing stop
        if config.use_trailing_stop and position.peak_pnl_percent >= config.trailing_activation_percent:
            stop_level = position.peak_pnl_percent - config.trailing_stop_percent
            if pnl <= stop_level:
                return True, f"Trailing stop: {pnl:.2f}% <= {stop_level:.2f}% (peak: {position.peak_pnl_percent:.2f}%)"
        
        # Check profit target
        if pnl >= config.sell_threshold_percent:
            return True, f"Profit target: {pnl:.2f}% >= {config.sell_threshold_percent}%"
        
        return False, f"P&L {pnl:.2f}% within range"
    
    @classmethod
    def find_sellable_positions(cls, positions: Dict[int, DynamicPosition],
                               current_price: float,
                               config: DynamicGridConfig) -> List[Tuple[int, str]]:
        """
        Find all positions that should be sold.
        
        Args:
            positions: All positions
            current_price: Current token price
            config: Dynamic grid configuration
            
        Returns:
            List of (position_id, reason) tuples
        """
        sellable = []
        
        for pos_id, position in positions.items():
            should_sell, reason = cls.should_sell(position, current_price, config)
            if should_sell:
                sellable.append((pos_id, reason))
        
        return sellable
    
    @classmethod
    def create_position(cls, position_id: int, current_price: float,
                       config: DynamicGridConfig) -> DynamicPosition:
        """
        Create a new dynamic position at current price.
        
        Args:
            position_id: ID for the new position
            current_price: Current market price
            config: Dynamic grid configuration
            
        Returns:
            New position
        """
        # Calculate sell target based on profit threshold
        sell_target = current_price * (1 + config.sell_threshold_percent / 100)
        
        # Calculate stop loss if enabled
        stop_loss = 0.0
        if config.stop_loss_percent > 0:
            stop_loss = current_price * (1 - config.stop_loss_percent / 100)
        
        return DynamicPosition(
            id=position_id,
            buy_price=current_price,
            sell_target=sell_target,
            stop_loss=stop_loss,
            status="EMPTY",
            created_at=time.time(),
            current_pnl_percent=0.0,
            peak_pnl_percent=0.0,
            lowest_pnl_percent=0.0,
        )
    
    @classmethod
    def validate_config(cls, config: DynamicGridConfig) -> Tuple[bool, List[str]]:
        """
        Validate dynamic grid configuration.
        
        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []
        
        if not config.enabled:
            return True, []  # Not in dynamic mode, no validation needed
        
        # Buy threshold should be negative
        if config.buy_threshold_percent >= 0:
            errors.append(f"buy_threshold_percent ({config.buy_threshold_percent}) should be negative")
        
        # Sell threshold should be positive
        if config.sell_threshold_percent <= 0:
            errors.append(f"sell_threshold_percent ({config.sell_threshold_percent}) should be positive")
        
        # Sell should be higher than absolute buy threshold
        if config.sell_threshold_percent <= abs(config.buy_threshold_percent):
            errors.append(f"sell_threshold_percent ({config.sell_threshold_percent}) should be > abs(buy_threshold_percent) ({abs(config.buy_threshold_percent)})")
        
        # Max positions check
        if config.max_active_positions < 1:
            errors.append(f"max_active_positions ({config.max_active_positions}) must be >= 1")
        
        # Stop loss check
        if config.stop_loss_percent < 0:
            errors.append(f"stop_loss_percent ({config.stop_loss_percent}) cannot be negative")
        
        return len(errors) == 0, errors
    
    @staticmethod
    def format_pnl(pnl_percent: float) -> str:
        """Format P&L percentage for display."""
        sign = "+" if pnl_percent >= 0 else ""
        return f"{sign}{pnl_percent:.2f}%"
    
    @classmethod
    def calculate_grid_stats(cls, positions: Dict[int, DynamicPosition],
                            current_price: float) -> dict:
        """
        Calculate statistics for the dynamic grid.
        
        Returns:
            Dictionary with grid statistics
        """
        holding = [p for p in positions.values() if p.status == "HOLDING"]
        sold = [p for p in positions.values() if p.status == "SOLD"]
        
        # Calculate P&L for all holding positions
        pnls = []
        total_cost = 0
        total_current_value = 0
        
        for pos in holding:
            pnl = cls.calculate_pnl_percent(pos.buy_price, current_price)
            pnls.append(pnl)
            total_cost += pos.cost / 1e9  # Convert to WETH
            tokens = pos.balance / 1e18
            total_current_value += tokens * current_price
        
        stats = {
            "total_positions": len(positions),
            "holding_count": len(holding),
            "sold_count": len(sold),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "best_pnl": max(pnls) if pnls else 0.0,
            "worst_pnl": min(pnls) if pnls else 0.0,
            "total_invested_eth": total_cost,
            "total_current_value_eth": total_current_value,
            "unrealized_pnl_eth": total_current_value - total_cost,
        }
        
        return stats


class DynamicGridState:
    """
    Manages state for dynamic grid mode including position history.
    """
    
    def __init__(self, config: DynamicGridConfig):
        self.config = config
        self.positions: Dict[int, DynamicPosition] = {}
        self.position_history: List[DynamicPosition] = []
        self.next_position_id = 0
        self.last_buy_time = 0.0
        self.calculator = DynamicGridCalculator()
    
    def add_position(self, position: DynamicPosition):
        """Add a new position."""
        self.positions[position.id] = position
        if position.id >= self.next_position_id:
            self.next_position_id = position.id + 1
    
    def create_new_position(self, current_price: float) -> DynamicPosition:
        """Create and add a new position."""
        position = self.calculator.create_position(
            self.next_position_id, 
            current_price, 
            self.config
        )
        self.add_position(position)
        self.last_buy_time = time.time()
        return position
    
    def close_position(self, position_id: int, sell_price: float, 
                      profit: float, sell_tx: str):
        """Close a position and move to history."""
        if position_id not in self.positions:
            return
        
        position = self.positions[position_id]
        position.status = "SOLD"
        position.sell_price = sell_price
        position.profit = profit
        position.sell_tx = sell_tx
        position.closed_at = time.time()
        
        # Move to history
        self.position_history.append(position)
        
        # Limit history size
        max_history = 100
        if len(self.position_history) > max_history:
            self.position_history = self.position_history[-max_history:]
    
    def update_all_pnl(self, current_price: float):
        """Update P&L for all holding positions."""
        for position in self.positions.values():
            if position.status == "HOLDING":
                self.calculator.update_position_pnl(position, current_price)
    
    def get_active_count(self) -> int:
        """Get number of active (holding) positions."""
        return sum(1 for p in self.positions.values() if p.status == "HOLDING")
    
    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "position_history": [p.to_dict() for p in self.position_history],
            "next_position_id": self.next_position_id,
            "last_buy_time": self.last_buy_time,
            "config": {
                "enabled": self.config.enabled,
                "buy_threshold_percent": self.config.buy_threshold_percent,
                "sell_threshold_percent": self.config.sell_threshold_percent,
                "stop_loss_percent": self.config.stop_loss_percent,
            }
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'DynamicGridState':
        """Deserialize from dictionary."""
        config_data = data.get("config", {})
        config = DynamicGridConfig(
            enabled=config_data.get("enabled", False),
            buy_threshold_percent=config_data.get("buy_threshold_percent", -10.0),
            sell_threshold_percent=config_data.get("sell_threshold_percent", 8.0),
            stop_loss_percent=config_data.get("stop_loss_percent", 0.0),
        )
        
        state = cls(config)
        state.next_position_id = data.get("next_position_id", 0)
        state.last_buy_time = data.get("last_buy_time", 0.0)
        
        # Load positions
        for pos_id, pos_data in data.get("positions", {}).items():
            state.positions[int(pos_id)] = DynamicPosition.from_dict(pos_data)
        
        # Load history
        for hist_data in data.get("position_history", []):
            state.position_history.append(DynamicPosition.from_dict(hist_data))
        
        return state
