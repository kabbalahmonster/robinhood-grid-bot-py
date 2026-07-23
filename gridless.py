"""
Gridless Trading Mode - Simple position-based trading without fixed grid levels.
Buy: No positions OR (count < max AND top_pnl <= buy_threshold)
Sell: pnl >= sell_threshold (check quote) OR stoploss triggered
"""

import json
import os
from typing import Dict, Optional, Tuple, Any

POSITIONS_FILE = "data/gridless_positions.json"


def calculate_pnl(position: Dict[str, int], current_price: float) -> float:
    """Calculate P&L using wei units throughout for precision.
    
    Args:
        position: Dict with 'cost_wei' (wei spent) and 'balance' (wei tokens received)
        current_price: Current price in ETH per token (for display comparison only)
    
    Returns:
        P&L percentage
    """
    cost_wei = position.get('cost_wei', 0)
    balance = position.get('balance', 0)
    if balance <= 0 or cost_wei <= 0:
        return 0.0
    
    # Calculate buy price in wei per wei-token for precise comparison
    # buy_price_wei_per_token_wei = cost_wei / balance
    # This gives us the ratio to compare against current market
    buy_price_eth_per_token = (cost_wei / 1e18) / (balance / 1e18)
    
    if buy_price_eth_per_token <= 0:
        return 0.0
    
    # Debug
    import logging
    logger = logging.getLogger('gridless')
    logger.debug(f"P&L calc: cost_wei={cost_wei}, balance={balance}, buy_price={buy_price_eth_per_token}, current_price={current_price}")
    
    return ((current_price - buy_price_eth_per_token) / buy_price_eth_per_token) * 100


def get_buy_price(position: Dict[str, int]) -> float:
    """Calculate buy price in WETH per token."""
    cost_wei = position.get('cost_wei', position.get('cost', 0) * 10**9)  # Migrate from nano-ETH
    balance = position.get('balance', 0)
    if balance <= 0 or cost_wei <= 0:
        return 0.0
    return (cost_wei / 1e18) / (balance / 1e18)


def get_top_position(positions: Dict[str, Dict]) -> Optional[Tuple[str, Dict]]:
    """Get position with lowest buy price (best position)."""
    if not positions:
        return None
    top_id, top_pos, top_price = None, None, float('inf')
    for pos_id, pos in positions.items():
        buy_price = get_buy_price(pos)
        if buy_price > 0 and buy_price < top_price:
            top_price, top_id, top_pos = buy_price, pos_id, pos
    return (top_id, top_pos) if top_id else None


def should_buy(positions: Dict[str, Dict], current_price: float, config: Any) -> Tuple[bool, str]:
    """Check buy rules with leading edge support.
    
    Standard buy: no positions OR (under max AND top_pnl <= buy_threshold)
    Leading edge: only 1 position AND room for more AND pnl >= 50% of sell_threshold
    """
    max_active = getattr(config, 'max_active_positions', 10)
    buy_threshold = getattr(config, 'gridless_buy_threshold', -10.0)
    sell_threshold = getattr(config, 'gridless_sell_threshold', 5.0)
    leading_edge_enabled = getattr(config, 'gridless_leading_edge', False)
    
    # No positions - initial buy
    if len(positions) == 0 and max_active > 0:
        return (True, "Initial buy (no positions)")
    
    # Max positions reached
    if len(positions) >= max_active:
        return (False, f"Max positions reached ({len(positions)}/{max_active})")
    
    # Standard dip-buying logic
    top = get_top_position(positions)
    if top is None:
        return (True, "No holding positions found")
    
    top_pnl = calculate_pnl(top[1], current_price)
    if top_pnl <= buy_threshold:
        return (True, f"Top position P&L {top_pnl:.2f}% <= threshold {buy_threshold}%")
    
    # Leading edge: buy into strength when single position is climbing
    # Trigger at 50% of sell threshold (e.g., if sell=5%, buy at +2.5%)
    if leading_edge_enabled and len(positions) == 1:
        leading_edge_trigger = sell_threshold * 0.5
        if top_pnl >= leading_edge_trigger:
            return (True, f"Leading edge: P&L {top_pnl:.2f}% >= 50% of sell ({leading_edge_trigger}%)")
    
    return (False, f"Top position P&L {top_pnl:.2f}% > threshold {buy_threshold}%")


def should_sell(position: Dict[str, int], current_price: float, config: Any,
                quote_profit_eth: float = 0.0) -> Tuple[bool, str]:
    """Check sell rules: profit target OR stoploss."""
    sell_threshold = getattr(config, 'gridless_sell_threshold', 5.0)
    stoploss_threshold = getattr(config, 'gridless_stoploss_threshold', -25.0)
    stoploss_enabled = getattr(config, 'gridless_stoploss_enabled', False)
    min_profit = getattr(config, 'min_profit_percent', 1.5)
    pnl = calculate_pnl(position, current_price)
    # Stoploss check (highest priority)
    if stoploss_enabled and pnl <= stoploss_threshold:
        if quote_profit_eth < 0:
            return (False, f"STOPLOSS ({pnl:.1f}%) but quote loss")
        return (True, f"STOPLOSS: {pnl:.1f}%")
    # Profit target check
    if pnl >= sell_threshold:
        if quote_profit_eth <= 0:
            return (False, f"Target met ({pnl:.1f}%) but no quote profit")
        cost_weth = position.get('cost', 0) / 1e9
        min_profit_eth = cost_weth * (min_profit / 100)
        if quote_profit_eth < min_profit_eth:
            return (False, f"Target met ({pnl:.1f}%) but quote < min")
        return (True, f"PROFIT: {pnl:.1f}%")
    return (False, f"No sell (P&L: {pnl:.1f}%)")


def find_sell_candidate(positions: Dict[str, Dict], current_price: float,
                        config: Any, quote_profit_eth: float = 0.0) -> Optional[Tuple[str, Dict, str]]:
    """Find best position to sell (stoploss first, then highest P&L)."""
    stoploss_enabled = getattr(config, 'gridless_stoploss_enabled', False)
    stoploss_threshold = getattr(config, 'gridless_stoploss_threshold', -25.0)
    candidates = []
    for pos_id, pos in positions.items():
        pnl = calculate_pnl(pos, current_price)
        should_sell_flag, reason = should_sell(pos, current_price, config, quote_profit_eth)
        if should_sell_flag:
            priority = 0 if (stoploss_enabled and pnl <= stoploss_threshold) else 1
            candidates.append((priority, -pnl, pos_id, pos, reason))
    if not candidates:
        return None
    candidates.sort()
    return (candidates[0][2], candidates[0][3], candidates[0][4])


def validate_position(position: Dict[str, int]) -> bool:
    """Validate: cost_wei or cost >= 0 and balance >= 0."""
    has_cost = position.get('cost_wei', -1) >= 0 or position.get('cost', -1) >= 0
    has_balance = position.get('balance', -1) >= 0
    return has_cost and has_balance


def load_positions() -> Dict[str, Dict[str, int]]:
    """Load and validate positions from JSON."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, 'r') as f:
            data = json.load(f)
        positions = {}
        for k, v in data.items():
            if validate_position(v):
                # Migrate from cost (nano-ETH) to cost_wei if needed
                if 'cost' in v and 'cost_wei' not in v:
                    positions[k] = {
                        'cost_wei': int(v['cost']) * 10**9,
                        'balance': int(v['balance'])
                    }
                else:
                    positions[k] = {
                        'cost_wei': int(v.get('cost_wei', 0)),
                        'balance': int(v['balance'])
                    }
        return positions
    except (json.JSONDecodeError, IOError):
        return {}


def save_positions(positions: Dict[str, Dict[str, int]]) -> None:
    """Atomic save: write to temp, then rename."""
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    temp_file = POSITIONS_FILE + '.tmp'
    with open(temp_file, 'w') as f:
        json.dump(positions, f, indent=2)
    os.replace(temp_file, POSITIONS_FILE)


def add_position(cost_wei: int, balance: int) -> str:
    """Add new position with lowest available ID (fills gaps).
    
    Args:
        cost_wei: Cost in wei (not nano-ETH)
        balance: Token balance in wei
    """
    positions = load_positions()
    
    # Find the lowest unused ID
    existing_ids = {int(k) for k in positions.keys()}
    next_id = 0
    while next_id in existing_ids:
        next_id += 1
    
    positions[str(next_id)] = {'cost_wei': cost_wei, 'balance': balance}
    save_positions(positions)
    return str(next_id)


def remove_position(position_id: str) -> bool:
    """Remove position by ID."""
    positions = load_positions()
    if position_id not in positions:
        return False
    del positions[position_id]
    save_positions(positions)
    return True


def migrate_from_grid(grid_positions: Dict[str, Dict]) -> Dict[str, Dict[str, int]]:
    """Migrate grid positions to gridless format."""
    gridless, next_id = {}, 0
    for pos in grid_positions.values():
        if pos.get('balance', 0) > 0:
            gridless[str(next_id)] = {'cost': int(pos.get('cost', 0)), 'balance': int(pos['balance'])}
            next_id += 1
    return gridless


def migrate_to_grid(gridless_positions: Dict[str, Dict[str, int]], 
                    grid_spacing_percent: float = 6.0) -> Dict[str, Dict]:
    """Migrate gridless positions back to grid format.
    
    Args:
        gridless_positions: Gridless position dict {id: {cost, balance}}
        grid_spacing_percent: Grid spacing % for calculating ranges
        
    Returns:
        Grid format positions dict
    """
    import math
    grid_positions = {}
    
    for pos_id, pos in gridless_positions.items():
        balance = pos.get('balance', 0)
        cost = pos.get('cost', 0)
        if balance <= 0:
            continue
        
        # Calculate buy price
        buy_price = (cost / 1e9) / (balance / 1e18)
        
        # Calculate grid range based on buy price
        # Position covers range around its buy price
        range_pct = grid_spacing_percent / 100
        
        # Scale to nano-WETH (10^9)
        buy_price_nano = int(buy_price * 1e9)
        
        # Create grid position
        grid_pos = {
            'buyMin': int(buy_price_nano * (1 - range_pct)),
            'buyMax': int(buy_price_nano * (1 + range_pct)),
            'sellMin': int(buy_price_nano * (1 + range_pct * 2)),  # ~2x spacing for sell
            'cost': cost,
            'balance': balance
        }
        
        grid_positions[pos_id] = grid_pos
    
    return grid_positions


def save_grid_positions(grid_positions: Dict[str, Dict], filepath: str = "data/positions.json") -> None:
    """Save grid positions to JSON file (atomic write)."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    temp_file = filepath + '.tmp'
    with open(temp_file, 'w') as f:
        json.dump(grid_positions, f, indent=2)
    os.replace(temp_file, filepath)
