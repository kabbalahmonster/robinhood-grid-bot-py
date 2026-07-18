#!/usr/bin/env python3
"""
Generate grid positions in the original format.
Run this first to create positions.json
"""

import json
import sys

def generate_grid_positions(
    start_price: float,  # In WETH (e.g., 0.00000333)
    num_positions: int = 64,
    grid_spacing: float = 0.06,  # 6%
    profit_target: float = 0.10,  # 10%
    stoploss: float = 0.20,  # 20% below buy
):
    """Generate positions in the original format with contiguous bands."""
    
    positions = {}
    current_buy_max = start_price
    
    for i in range(1, num_positions + 1):
        # Calculate bands
        buy_max = current_buy_max
        # buyMin is the previous position's buyMin (or 0 for first)
        if i == 1:
            buy_min = 0  # First position buys from 0 to buyMax
        else:
            buy_min = buy_max / (1 + grid_spacing)
        
        sell_min = buy_max * (1 + profit_target)
        stoploss_price = buy_min * (1 - stoploss) if buy_min > 0 else buy_max * (1 - stoploss)
        
        # Store in format matching user's example (scale: 10^12 for micro-WETH)
        # Looking at user's data: 600000 = 0.0006 WETH, so scale is 10^9
        scale = 10**9
        positions[str(i)] = {
            "id": str(i),
            "balance": 0,
            "buyMax": int(buy_max * scale),
            "buyMin": int(buy_min * scale),
            "sellMin": int(sell_min * scale),
            "cost": 0,
            "stoploss": int(stoploss_price * scale),
        }
        
        # Next position's buyMax is this position's buyMin
        current_buy_max = buy_min
    
    return positions

if __name__ == "__main__":
    # Get current price from user or use default
    if len(sys.argv) > 1:
        start_price = float(sys.argv[1])
    else:
        # Default for TENDIES based on 0x quote
        start_price = 0.00000333  # WETH per TENDIES
    
    print(f"Generating grid with start price: {start_price} WETH")
    print(f"Grid spacing: 6%")
    print(f"Profit target: 10%")
    print(f"Stop loss: 20% below buy")
    print()
    
    positions = generate_grid_positions(start_price)
    
    # Save to file
    import os
    filename = "data/positions.json"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w") as f:
        json.dump(positions, f, indent=2)
    
    print(f"Generated {len(positions)} positions")
    print(f"Price range: {positions[str(len(positions))]['buyMin']/10**9:.10f} - {positions['1']['buyMax']/10**9:.10f} WETH")
    print(f"Saved to: {filename}")
    print()
    print("First 3 positions:")
    for i in range(1, 4):
        p = positions[str(i)]
        print(f"  Position {i}: Buy {p['buyMin']/10**9:.10f} - {p['buyMax']/10**9:.10f}, Sell at {p['sellMin']/10**9:.10f}")
