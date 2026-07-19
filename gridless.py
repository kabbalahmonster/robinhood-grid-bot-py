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
    """Calculate P&L: ((current_price - buy_price) / buy_price) * 100"""
    cost = position.get('cost', 0)
    balance = position.get('balance', 0)
    if balance <= 0 or cost <= 0:
        return 0.0
    cost_weth = cost / 1e9  # nano-WETH to WETH
    tokens = balance / 1e18  # wei to tokens
    buy_price = cost_weth / tokens
    if buy_price <= 0:
        return 0.0
    return ((current_price - buy_price) / buy_price) * 100


def get_buy_price(position: Dict[str, int]) -> float:
    """Calculate buy price in WETH per token."""
    cost = position.get('cost', 0)
    balance = position.get('balance', 0)
    if balance <= 0 or cost <= 0:
        return 0.0
    return (cost / 1e9) / (balance / 1e18)


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


def should_buy(positions: Dict[str, Dict], current_price: float, config: Any) -> bool:
    """Check buy rules: no positions OR (under max AND top_pnl <= threshold)."""
    max_active = getattr(config, 'max_active_positions', 10)
    buy_threshold = getattr(config, 'gridless_buy_threshold', -10.0)
    if len(positions) == 0 and max_active > 0:
        return True
    if len(positions) >= max_active:
        return False
    top = get_top_position(positions)
    if top is None:
        return True
    return calculate_pnl(top[1], current_price) <= buy_threshold


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
    """Validate: cost >= 0 and balance >= 0."""
    return position.get('cost', -1) >= 0 and position.get('balance', -1) >= 0


def load_positions() -> Dict[str, Dict[str, int]]:
    """Load and validate positions from JSON."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, 'r') as f:
            data = json.load(f)
        return {k: {'cost': int(v['cost']), 'balance': int(v['balance'])}
                for k, v in data.items() if validate_position(v)}
    except (json.JSONDecodeError, IOError):
        return {}


def save_positions(positions: Dict[str, Dict[str, int]]) -> None:
    """Atomic save: write to temp, then rename."""
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    temp_file = POSITIONS_FILE + '.tmp'
    with open(temp_file, 'w') as f:
        json.dump(positions, f, indent=2)
    os.replace(temp_file, POSITIONS_FILE)


def add_position(cost: int, balance: int) -> str:
    """Add new position with auto-incrementing ID."""
    positions = load_positions()
    next_id = max((int(k) for k in positions.keys()), default=-1) + 1
    positions[str(next_id)] = {'cost': cost, 'balance': balance}
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
