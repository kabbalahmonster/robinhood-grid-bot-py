#!/usr/bin/env python3
"""
Generate grid positions matching the original Solana bot logic.
"""

import json
import sys

# File path for the positions JSON file
POSITIONS_FILE = "data/positions.json"
STOPLOSS_FACTOR = 1.2
GAIN_FACTOR = 1.1

def calculate_positions(high, low, n=20):
    """Calculate geometric grid levels from high to low."""
    step = (low / high) ** (1 / (n - 1))
    levels = [round(high * (step ** i)) for i in range(n)]
    levels.reverse()
    print(f"Grid levels: {len(levels)} positions")
    print(f"Range: {levels[0]} to {levels[-1]}")
    return levels

def generate_positions(levels, stoploss_factor=1.2):
    """Generate positions with buyMin/buyMax/sellMin/stoploss."""
    positions = {}
    for i, level in enumerate(levels):
        position_id = f"{i + 1}"  # ID starts from 1
        
        # Previous level or 0 for the first position
        buy_min = levels[i - 1] if i > 0 else 0
        buy_max = level  # Current level
        
        # Calculate targets
        sell_min = round(buy_max * GAIN_FACTOR)  # 10% profit
        stoploss = round(buy_max / stoploss_factor)  # ~16.7% below
        
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

def save_positions_to_file(positions):
    """Save positions to JSON file."""
    with open(POSITIONS_FILE, "w") as file:
        json.dump(positions, file, indent=4)
    print(f"\nPositions saved to {POSITIONS_FILE}")

def main():
    # Default values for TENDIES (in nano-WETH, scale 10^9)
    # 6_000_000 = 0.006 WETH
    # 600_000 = 0.0006 WETH
    
    if len(sys.argv) >= 3:
        high = int(sys.argv[1])
        low = int(sys.argv[2])
    else:
        high = 6_000_000  # 0.006 WETH
        low = 600_000     # 0.0006 WETH
    
    n = 64  # Number of positions
    
    print(f"Generating grid:")
    print(f"  High: {high} (0.{high:09d} WETH)")
    print(f"  Low: {low} (0.{low:09d} WETH)")
    print(f"  Positions: {n}")
    print(f"  Gain factor: {GAIN_FACTOR}x ({(GAIN_FACTOR-1)*100:.0f}% profit)")
    print(f"  Stoploss factor: {STOPLOSS_FACTOR}x")
    print()
    
    # Calculate levels
    levels = calculate_positions(high, low, n)
    
    # Generate positions
    positions = generate_positions(levels)
    
    # Show first few
    print("\nFirst 3 positions:")
    for i in range(1, 4):
        p = positions[str(i)]
        print(f"  #{i}: Buy {p['buyMin']} - {p['buyMax']}, Sell at {p['sellMin']}, Stop at {p['stoploss']}")
    
    # Save
    save_positions_to_file(positions)

if __name__ == "__main__":
    main()
