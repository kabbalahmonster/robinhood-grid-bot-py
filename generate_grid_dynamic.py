#!/usr/bin/env python3
"""
Dynamic grid generator using current price from 0x API.
Generates grid from current_price * low_factor to current_price * high_factor.
"""

import json
import sys
import os

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from zero_x import ZeroXClient

POSITIONS_FILE = "data/positions.json"
GAIN_FACTOR = 1.1  # 10% profit target
STOPLOSS_FACTOR = 1.2  # ~16.7% stoploss (only used if stoploss enabled)


def get_current_price():
    """Get current token price from 0x API."""
    config = load_config()
    zero_x = ZeroXClient(config)
    
    # Get wallet address from private key
    from web3 import Web3
    from eth_account import Account
    account = Account.from_key(config.private_key)
    taker = account.address
    
    # Get quote for small amount to determine price
    quote = zero_x.get_quote(
        sell_token=config.weth_address,
        buy_token=config.token_address,
        sell_amount=10**15,  # 0.001 WETH
        taker_address=taker,
    )
    
    if quote.success and quote.buy_amount:
        # Price in nano-WETH per token
        price = (10**15 / quote.buy_amount) * 10**9
        return int(price)
    
    return None


def calculate_positions(high, low, n=24):
    """Calculate geometric grid levels from high to low."""
    step = (low / high) ** (1 / (n - 1))
    levels = [round(high * (step ** i)) for i in range(n)]
    levels.reverse()
    return levels


def generate_positions(levels, use_stoploss=False):
    """Generate positions with buyMin/buyMax/sellMin/stoploss."""
    positions = {}
    for i, level in enumerate(levels):
        position_id = f"{i + 1}"
        
        # Previous level or 0 for the first position
        buy_min = levels[i - 1] if i > 0 else 0
        buy_max = level
        
        # Calculate targets
        sell_min = round(buy_max * GAIN_FACTOR)
        
        # Stoploss only if enabled
        if use_stoploss:
            stoploss = round(buy_max / STOPLOSS_FACTOR)
        else:
            stoploss = 0
        
        positions[position_id] = {
            "id": position_id,
            "balance": 0,
            "buyMax": buy_max,
            "buyMin": buy_min,
            "sellMin": sell_min,
            "cost": 0,
            "stoploss": stoploss
        }
    return positions


def save_positions(positions):
    """Save positions to JSON file."""
    with open(POSITIONS_FILE, "w") as file:
        json.dump(positions, file, indent=4)
    print(f"\n✅ Saved {len(positions)} positions to {POSITIONS_FILE}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Generate dynamic grid from current price')
    parser.add_argument('--low', type=float, default=0.1, 
                        help='Low factor (e.g., 0.1 = 90%% below current price)')
    parser.add_argument('--high', type=float, default=4.0,
                        help='High factor (e.g., 4.0 = 4x above current price)')
    parser.add_argument('--positions', '-n', type=int, default=24,
                        help='Number of grid positions (default: 24)')
    parser.add_argument('--stoploss', action='store_true',
                        help='Enable stoploss (default: off)')
    parser.add_argument('--price', type=int, default=None,
                        help='Manual price override (in nano-WETH)')
    args = parser.parse_args()
    
    print("="*60)
    print("DYNAMIC GRID GENERATOR")
    print("="*60)
    print(f"Range: {args.low}x to {args.high}x current price")
    print(f"Positions: {args.positions}")
    print(f"Stoploss: {'ON' if args.stoploss else 'OFF'}")
    print()
    
    # Get current price
    if args.price:
        current_price = args.price
        print(f"Using manual price: {current_price} nano-WETH")
    else:
        print("Fetching current price from 0x API...")
        current_price = get_current_price()
        if current_price is None:
            print("❌ Failed to get price from 0x")
            print("Use --price to set manually")
            return
        print(f"✅ Current price: {current_price} nano-WETH")
        print(f"   = 0.{current_price:09d} WETH")
    
    # Calculate grid range
    low_price = round(current_price * args.low)
    high_price = round(current_price * args.high)
    
    print(f"\nGrid range:")
    print(f"  Low:  {low_price} nano-WETH ({args.low}x)")
    print(f"  High: {high_price} nano-WETH ({args.high}x)")
    
    # Generate grid
    levels = calculate_positions(high_price, low_price, args.positions)
    positions = generate_positions(levels, use_stoploss=args.stoploss)
    
    # Show summary
    print(f"\nGrid summary:")
    print(f"  Position #1: Buy 0-{levels[0]}, Sell at {round(levels[0]*GAIN_FACTOR)}")
    print(f"  Position #{args.positions}: Buy {levels[-2]}-{levels[-1]}, Sell at {round(levels[-1]*GAIN_FACTOR)}")
    
    # Show first 3
    print(f"\nFirst 3 positions:")
    for i in range(1, min(4, args.positions + 1)):
        p = positions[str(i)]
        print(f"  #{i}: Buy {p['buyMin']}-{p['buyMax']}, Sell {p['sellMin']}", end="")
        if args.stoploss:
            print(f", Stop {p['stoploss']}")
        else:
            print()
    
    # Save
    save_positions(positions)


if __name__ == "__main__":
    main()
