#!/usr/bin/env python3
"""
Migrate filled positions to a new grid.
Preserves holding positions while regenerating the grid around current price.
"""

import json
import sys
import os
import argparse
from typing import Dict, List, Tuple

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from zero_x import ZeroXClient
from web3 import Web3
from eth_account import Account

POSITIONS_FILE = "data/positions.json"
GAIN_FACTOR = 1.1  # 10% profit target
STOPLOSS_FACTOR = 1.2


def load_existing_positions(filepath: str) -> Dict:
    """Load existing positions from file."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ No existing positions file found: {filepath}")
        return {}


def get_holding_positions(positions: Dict) -> List[Dict]:
    """Extract positions with balance > 0."""
    holdings = []
    for pos_id, pos in positions.items():
        if pos.get('balance', 0) > 0:
            holdings.append({
                'id': pos_id,
                **pos
            })
    return holdings


def get_current_price(config) -> int:
    """Get current token price from 0x API."""
    zero_x = ZeroXClient(config)
    account = Account.from_key(config.private_key)
    taker = account.address
    
    quote = zero_x.get_quote(
        sell_token=config.weth_address,
        buy_token=config.token_address,
        sell_amount=10**15,  # 0.001 WETH
        taker_address=taker,
    )
    
    if quote.success and quote.buy_amount:
        price = (10**15 / quote.buy_amount) * 10**9
        return int(price)
    
    return None


def calculate_grid_levels(high: int, low: int, n: int) -> List[int]:
    """Calculate geometric grid levels from high to low."""
    step = (low / high) ** (1 / (n - 1))
    levels = [round(high * (step ** i)) for i in range(n)]
    levels.reverse()
    return levels


def generate_new_grid(levels: List[int], use_stoploss: bool = False) -> Dict:
    """Generate fresh positions with given levels."""
    positions = {}
    for i, level in enumerate(levels):
        position_id = f"{i + 1}"
        
        buy_min = levels[i - 1] if i > 0 else 0
        buy_max = level
        sell_min = round(buy_max * GAIN_FACTOR)
        
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


def find_best_position_match(holding: Dict, new_positions: Dict) -> str:
    """
    Find the best new position to migrate a holding to.
    Match by finding where holding's buy price falls within new position's range.
    """
    holding_buy_price = holding.get('buyMax', 0)  # Original buy price in nano-WETH
    
    best_match = None
    best_distance = float('inf')
    
    for pos_id, pos in new_positions.items():
        buy_min = pos['buyMin']
        buy_max = pos['buyMax']
        
        # Check if holding's buy price falls within this position's range
        if buy_min <= holding_buy_price <= buy_max:
            return pos_id  # Perfect match
        
        # Otherwise, calculate distance to closest edge
        if holding_buy_price < buy_min:
            distance = buy_min - holding_buy_price
        elif holding_buy_price > buy_max:
            distance = holding_buy_price - buy_max
        else:
            distance = 0
        
        if distance < best_distance:
            best_distance = distance
            best_match = pos_id
    
    return best_match


def merge_holdings_into_grid(holdings: List[Dict], new_positions: Dict) -> Tuple[Dict, Dict]:
    """
    Merge holding positions into the new grid.
    Returns (updated_positions, migration_log).
    """
    migration_log = {
        'merged': [],
        'placed': [],
        'errors': []
    }
    
    # Track which new positions have been filled
    filled_positions = {}  # pos_id -> holding data
    
    for holding in holdings:
        target_id = find_best_position_match(holding, new_positions)
        
        if not target_id:
            migration_log['errors'].append({
                'holding_id': holding['id'],
                'error': 'No suitable position found'
            })
            continue
        
        # If target already has a holding, merge them
        if target_id in filled_positions:
            existing = filled_positions[target_id]
            
            # Merge balances and costs
            merged_balance = existing['balance'] + holding['balance']
            merged_cost = existing['cost'] + holding['cost']
            
            # Use higher sellMin (more conservative = higher profit)
            merged_sell_min = max(existing['sellMin'], holding['sellMin'])
            
            filled_positions[target_id] = {
                'id': target_id,
                'balance': merged_balance,
                'cost': merged_cost,
                'sellMin': merged_sell_min,
                'source_ids': existing.get('source_ids', []) + [holding['id']]
            }
            
            migration_log['merged'].append({
                'target_id': target_id,
                'holding_ids': [existing.get('source_ids', [existing['id']]), holding['id']],
                'merged_balance': merged_balance,
                'merged_cost': merged_cost
            })
        else:
            # Place holding in empty position
            filled_positions[target_id] = {
                'id': target_id,
                'balance': holding['balance'],
                'cost': holding['cost'],
                'sellMin': max(holding['sellMin'], new_positions[target_id]['sellMin']),
                'source_ids': [holding['id']]
            }
            
            migration_log['placed'].append({
                'target_id': target_id,
                'holding_id': holding['id'],
                'balance': holding['balance'],
                'cost': holding['cost']
            })
    
    # Apply filled positions to new grid
    for pos_id, data in filled_positions.items():
        new_positions[pos_id]['balance'] = data['balance']
        new_positions[pos_id]['cost'] = data['cost']
        new_positions[pos_id]['sellMin'] = data['sellMin']
    
    return new_positions, migration_log


def save_positions(positions: Dict, filepath: str, backup: bool = True):
    """Save positions to file with optional backup."""
    # Create backup if file exists
    if backup and os.path.exists(filepath):
        backup_path = filepath + '.backup'
        with open(filepath, 'r') as f:
            old_data = f.read()
        with open(backup_path, 'w') as f:
            f.write(old_data)
        print(f"💾 Backup saved: {backup_path}")
    
    # Save new positions
    with open(filepath, 'w') as f:
        json.dump(positions, f, indent=2)
    print(f"✅ New grid saved: {filepath}")


def main():
    parser = argparse.ArgumentParser(
        description='Migrate filled positions to a new grid',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect current price, 10x range
  python migrate_grid.py --low 0.1 --high 10.0
  
  # Manual price, 24 positions
  python migrate_grid.py --price 15000 --positions 24
  
  # Narrow range around current price
  python migrate_grid.py --low 0.5 --high 2.0 --positions 12
        """
    )
    parser.add_argument('--low', type=float, default=0.1,
                        help='Low factor (e.g., 0.1 = 90%% below current price)')
    parser.add_argument('--high', type=float, default=10.0,
                        help='High factor (e.g., 10.0 = 10x above current price)')
    parser.add_argument('--positions', '-n', type=int, default=24,
                        help='Number of grid positions (default: 24)')
    parser.add_argument('--stoploss', action='store_true',
                        help='Enable stoploss')
    parser.add_argument('--price', type=int, default=None,
                        help='Manual price override (in nano-WETH)')
    parser.add_argument('--no-backup', action='store_true',
                        help='Skip creating backup file')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without saving')
    
    args = parser.parse_args()
    
    print("="*70)
    print("GRID MIGRATION TOOL")
    print("="*70)
    print(f"New grid range: {args.low}x to {args.high}x current price")
    print(f"Positions: {args.positions}")
    print()
    
    # Load existing positions
    print("📂 Loading existing positions...")
    existing = load_existing_positions(POSITIONS_FILE)
    if not existing:
        print("❌ No existing positions to migrate")
        return
    
    # Get holdings
    holdings = get_holding_positions(existing)
    print(f"💼 Found {len(holdings)} positions with balance:")
    total_tokens = sum(h['balance'] for h in holdings) / 10**18
    total_cost = sum(h['cost'] for h in holdings) / 10**9
    for h in holdings:
        tokens = h['balance'] / 10**18
        cost = h['cost'] / 10**9
        buy_price = cost / tokens if tokens > 0 else 0
        print(f"   Position {h['id']}: {tokens:.4f} tokens @ {buy_price:.10f} WETH")
    print(f"   Total: {total_tokens:.4f} tokens, {total_cost:.6f} WETH cost")
    print()
    
    # Get current price
    if args.price:
        current_price = args.price
        print(f"Using manual price: {current_price} nano-WETH")
    else:
        print("🔍 Fetching current price from 0x API...")
        try:
            config = load_config()
            current_price = get_current_price(config)
            if current_price is None:
                print("❌ Failed to get price from 0x")
                return
            print(f"✅ Current price: {current_price} nano-WETH")
            print(f"   = 0.{current_price:09d} WETH")
        except Exception as e:
            print(f"❌ Error fetching price: {e}")
            return
    
    print()
    
    # Calculate new grid
    low_price = round(current_price * args.low)
    high_price = round(current_price * args.high)
    
    print(f"📐 New grid range:")
    print(f"   Low:  {low_price} nano-WETH ({args.low}x)")
    print(f"   High: {high_price} nano-WETH ({args.high}x)")
    print()
    
    # Generate new grid
    print("🔄 Generating new grid...")
    levels = calculate_grid_levels(high_price, low_price, args.positions)
    new_positions = generate_new_grid(levels, use_stoploss=args.stoploss)
    
    print(f"   Generated {len(new_positions)} empty positions")
    print(f"   Position #1: Buy 0-{levels[0]}, Sell at {round(levels[0]*GAIN_FACTOR)}")
    print(f"   Position #{args.positions}: Buy {levels[-2]}-{levels[-1]}, Sell at {round(levels[-1]*GAIN_FACTOR)}")
    print()
    
    # Merge holdings
    if holdings:
        print("🔄 Migrating holdings to new grid...")
        new_positions, log = merge_holdings_into_grid(holdings, new_positions)
        
        if log['placed']:
            print(f"   ✅ Placed {len(log['placed'])} holdings:")
            for p in log['placed']:
                print(f"      Position {p['holding_id']} → new position {p['target_id']}")
        
        if log['merged']:
            print(f"   🔀 Merged {len(log['merged'])} positions:")
            for m in log['merged']:
                print(f"      Combined into position {m['target_id']}: {m['merged_balance']/10**18:.4f} tokens")
        
        if log['errors']:
            print(f"   ⚠️  Errors: {len(log['errors'])}")
            for e in log['errors']:
                print(f"      Position {e['holding_id']}: {e['error']}")
        
        print()
    
    # Show summary
    active_count = sum(1 for p in new_positions.values() if p['balance'] > 0)
    print("📊 Final grid summary:")
    print(f"   Total positions: {len(new_positions)}")
    print(f"   Active (with holdings): {active_count}")
    print(f"   Empty: {len(new_positions) - active_count}")
    print()
    
    # Show migrated positions
    if holdings:
        print("💼 Migrated positions:")
        for pos_id, pos in new_positions.items():
            if pos['balance'] > 0:
                tokens = pos['balance'] / 10**18
                cost = pos['cost'] / 10**9
                buy_price = cost / tokens if tokens > 0 else 0
                sell_price = pos['sellMin'] / 10**9
                print(f"   #{pos_id}: {tokens:.4f} tokens | Buy: {buy_price:.10f} | Sell@: {sell_price:.10f}")
        print()
    
    # Dry run or save
    if args.dry_run:
        print("🔍 DRY RUN - No changes saved")
        print("   Run without --dry-run to apply changes")
    else:
        confirm = input("⚠️  This will overwrite your positions.json. Continue? [y/N]: ")
        if confirm.lower() == 'y':
            save_positions(new_positions, POSITIONS_FILE, backup=not args.no_backup)
            print("\n✅ Migration complete!")
            print("   Restart your bot to use the new grid.")
        else:
            print("\n❌ Cancelled. No changes made.")


if __name__ == "__main__":
    main()
