"""
Storage module for the Robinhood Chain Grid Trading Bot.

Handles persistence of trading positions, bot state, and trade history
to disk for recovery between restarts.
"""

import os
import json
import logging
from typing import Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path


@dataclass
class Position:
    """
    Represents a grid trading position.
    
    Tracks the buy price, amount, timestamp, and current status
    of a single grid position.
    """
    id: int
    buy_price: float
    buy_amount_token: float  # Amount of token bought
    buy_amount_eth: float  # Amount of ETH spent
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "open"  # open, closed, banking
    sell_price: Optional[float] = None
    sell_amount_eth: Optional[float] = None
    profit_eth: Optional[float] = None
    profit_percent: Optional[float] = None
    tx_hash_buy: Optional[str] = None
    tx_hash_sell: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert position to dictionary."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        """Create position from dictionary."""
        return cls(**data)
    
    @property
    def is_open(self) -> bool:
        """Check if position is open."""
        return self.status == "open"
    
    @property
    def cost_basis(self) -> float:
        """Calculate cost basis in ETH per token."""
        if self.buy_amount_token == 0:
            return 0.0
        return self.buy_amount_eth / self.buy_amount_token


@dataclass
class BotState:
    """
    Complete bot state for persistence.
    
    Contains all positions and metadata needed to restore
    bot operation after restart.
    """
    positions: list[Position] = field(default_factory=list)
    last_price: Optional[float] = None
    last_update: str = field(default_factory=lambda: datetime.now().isoformat())
    total_trades: int = 0
    total_profit_eth: float = 0.0
    version: str = "1.0.0"
    
    def to_dict(self) -> dict:
        """Convert state to dictionary."""
        return {
            "positions": [p.to_dict() for p in self.positions],
            "last_price": self.last_price,
            "last_update": self.last_update,
            "total_trades": self.total_trades,
            "total_profit_eth": self.total_profit_eth,
            "version": self.version,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "BotState":
        """Create state from dictionary."""
        positions = [Position.from_dict(p) for p in data.get("positions", [])]
        return cls(
            positions=positions,
            last_price=data.get("last_price"),
            last_update=data.get("last_update", datetime.now().isoformat()),
            total_trades=data.get("total_trades", 0),
            total_profit_eth=data.get("total_profit_eth", 0.0),
            version=data.get("version", "1.0.0"),
        )


class Storage:
    """
    Handles persistence of bot state to disk.
    
    Provides atomic save/load operations with backup and recovery.
    """
    
    def __init__(self, state_file: str):
        """
        Initialize storage.
        
        Args:
            state_file: Path to state file.
        """
        self.state_file = Path(state_file)
        self.backup_file = self.state_file.with_suffix(".json.bak")
        self.logger = logging.getLogger("grid_bot.storage")
        
        # Ensure directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
    
    def save_state(self, state: BotState) -> bool:
        """
        Save bot state to disk atomically.
        
        Args:
            state: BotState to save.
            
        Returns:
            bool: True if save was successful.
        """
        try:
            # Update timestamp
            state.last_update = datetime.now().isoformat()
            
            # Write to temp file first
            temp_file = self.state_file.with_suffix(".tmp")
            
            with open(temp_file, "w") as f:
                json.dump(state.to_dict(), f, indent=2)
            
            # Backup existing file if present
            if self.state_file.exists():
                os.replace(self.state_file, self.backup_file)
            
            # Move temp to final location
            os.replace(temp_file, self.state_file)
            
            self.logger.debug(f"State saved to {self.state_file}")
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")
            return False
    
    def load_state(self) -> BotState:
        """
        Load bot state from disk.
        
        Returns:
            BotState: Loaded state or new empty state if not found.
        """
        # Try main file first
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                
                state = BotState.from_dict(data)
                self.logger.info(f"State loaded from {self.state_file}")
                return state
            
            except Exception as e:
                self.logger.error(f"Failed to load main state file: {e}")
        
        # Try backup file
        if self.backup_file.exists():
            try:
                with open(self.backup_file, "r") as f:
                    data = json.load(f)
                
                state = BotState.from_dict(data)
                self.logger.warning(f"State loaded from backup file")
                
                # Restore main file from backup
                self.save_state(state)
                return state
            
            except Exception as e:
                self.logger.error(f"Failed to load backup state file: {e}")
        
        # Return empty state
        self.logger.info("No existing state found, starting fresh")
        return BotState()
    
    def add_position(self, position: Position) -> bool:
        """
        Add a new position to storage.
        
        Args:
            position: Position to add.
            
        Returns:
            bool: True if successful.
        """
        state = self.load_state()
        state.positions.append(position)
        return self.save_state(state)
    
    def update_position(self, position_id: int, updates: dict) -> bool:
        """
        Update an existing position.
        
        Args:
            position_id: ID of position to update.
            updates: Dictionary of fields to update.
            
        Returns:
            bool: True if successful.
        """
        state = self.load_state()
        
        for pos in state.positions:
            if pos.id == position_id:
                for key, value in updates.items():
                    if hasattr(pos, key):
                        setattr(pos, key, value)
                return self.save_state(state)
        
        self.logger.warning(f"Position {position_id} not found for update")
        return False
    
    def close_position(
        self,
        position_id: int,
        sell_price: float,
        sell_amount_eth: float,
        profit_eth: float,
        profit_percent: float,
        tx_hash: str,
    ) -> bool:
        """
        Close a position with sell details.
        
        Args:
            position_id: ID of position to close.
            sell_price: Price at which token was sold.
            sell_amount_eth: Amount of ETH received.
            profit_eth: Profit in ETH.
            profit_percent: Profit percentage.
            tx_hash: Sell transaction hash.
            
        Returns:
            bool: True if successful.
        """
        updates = {
            "status": "closed",
            "sell_price": sell_price,
            "sell_amount_eth": sell_amount_eth,
            "profit_eth": profit_eth,
            "profit_percent": profit_percent,
            "tx_hash_sell": tx_hash,
        }
        return self.update_position(position_id, updates)
    
    def get_open_positions(self) -> list[Position]:
        """
        Get all open positions.
        
        Returns:
            list[Position]: List of open positions.
        """
        state = self.load_state()
        return [p for p in state.positions if p.is_open]
    
    def get_all_positions(self) -> list[Position]:
        """
        Get all positions (open and closed).
        
        Returns:
            list[Position]: List of all positions.
        """
        state = self.load_state()
        return state.positions
    
    def get_next_position_id(self) -> int:
        """
        Get next available position ID.
        
        Returns:
            int: Next position ID.
        """
        state = self.load_state()
        if not state.positions:
            return 1
        return max(p.id for p in state.positions) + 1
    
    def record_trade_profit(self, profit_eth: float) -> bool:
        """
        Record cumulative trade profit.
        
        Args:
            profit_eth: Profit from trade.
            
        Returns:
            bool: True if successful.
        """
        state = self.load_state()
        state.total_trades += 1
        state.total_profit_eth += profit_eth
        return self.save_state(state)
    
    def update_last_price(self, price: float) -> bool:
        """
        Update last seen price.
        
        Args:
            price: Current price.
            
        Returns:
            bool: True if successful.
        """
        state = self.load_state()
        state.last_price = price
        return self.save_state(state)
    
    def get_stats(self) -> dict:
        """
        Get trading statistics.
        
        Returns:
            dict: Statistics dictionary.
        """
        state = self.load_state()
        open_positions = [p for p in state.positions if p.is_open]
        closed_positions = [p for p in state.positions if not p.is_open]
        
        return {
            "total_positions": len(state.positions),
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "total_trades": state.total_trades,
            "total_profit_eth": state.total_profit_eth,
            "last_price": state.last_price,
            "last_update": state.last_update,
        }
    
    def reset_state(self) -> bool:
        """
        Reset all state (use with caution).
        
        Returns:
            bool: True if successful.
        """
        try:
            if self.state_file.exists():
                os.rename(self.state_file, self.state_file.with_suffix(".json.old"))
            if self.backup_file.exists():
                os.remove(self.backup_file)
            
            self.logger.warning("State has been reset")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reset state: {e}")
            return False
    
    def export_to_csv(self, filepath: str) -> bool:
        """
        Export positions to CSV.
        
        Args:
            filepath: Path to CSV file.
            
        Returns:
            bool: True if successful.
        """
        try:
            import csv
            
            state = self.load_state()
            
            with open(filepath, "w", newline="") as f:
                if state.positions:
                    writer = csv.DictWriter(f, fieldnames=state.positions[0].to_dict().keys())
                    writer.writeheader()
                    for pos in state.positions:
                        writer.writerow(pos.to_dict())
            
            self.logger.info(f"Positions exported to {filepath}")
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to export to CSV: {e}")
            return False