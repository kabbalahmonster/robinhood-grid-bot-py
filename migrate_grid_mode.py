#!/usr/bin/env python3
"""
Migration tool between grid and gridless modes.

Usage:
    python migrate_grid_mode.py to-gridless    # Migrate classic grid → gridless
    python migrate_grid_mode.py to-grid        # Migrate gridless → classic grid
    python migrate_grid_mode.py status         # Show status of both modes
"""

import sys
import os
import json
from gridless import (
    load_positions as load_gridless,
    save_positions as save_gridless,
    migrate_from_grid,
    migrate_to_grid,
    save_grid_positions
)

GRID_FILE = "data/positions.json"
GRIDLESS_FILE = "data/gridless_positions.json"


def load_grid():
    """Load classic grid positions."""
    if not os.path.exists(GRID_FILE):
        return {}
    try:
        with open(GRID_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def show_status():
    """Show status of both modes."""
    print("\n" + "=" * 60)
    print("MIGRATION STATUS")
    print("=" * 60)
    
    grid = load_grid()
    gridless = load_gridless()
    
    # Classic grid status
    print(f"\n📊 CLASSIC GRID ({GRID_FILE}):")
    if grid:
        active = sum(1 for p in grid.values() if p.get('balance', 0) > 0)
        total_balance = sum(p.get('balance', 0) for p in grid.values()) / 1e18
        print(f"   Positions: {len(grid)} ({active} active)")
        print(f"   Total balance: {total_balance:.4f} tokens")
    else:
        print("   No positions found")
    
    # Gridless status
    print(f"\n🎯 GRIDLESS ({GRIDLESS_FILE}):")
    if gridless:
        total_balance = sum(p.get('balance', 0) for p in gridless.values()) / 1e18
        print(f"   Positions: {len(gridless)}")
        print(f"   Total balance: {total_balance:.4f} tokens")
    else:
        print("   No positions found")
    
    print("\n" + "=" * 60)
    print("USAGE:")
    print("  python migrate_grid_mode.py to-gridless")
    print("  python migrate_grid_mode.py to-grid")
    print("=" * 60 + "\n")


def migrate_to_gridless():
    """Migrate classic grid to gridless."""
    print("\n🔄 Migrating CLASSIC GRID → GRIDLESS...")
    
    grid = load_grid()
    if not grid:
        print("❌ No classic grid positions found!")
        return False
    
    active = sum(1 for p in grid.values() if p.get('balance', 0) > 0)
    print(f"   Found {len(grid)} positions ({active} with balance)")
    
    # Check if gridless already has positions
    existing = load_gridless()
    if existing:
        print(f"⚠️  Gridless already has {len(existing)} positions!")
        response = input("   Overwrite? (yes/no): ")
        if response.lower() != 'yes':
            print("   Migration cancelled.")
            return False
    
    # Migrate
    gridless = migrate_from_grid(grid)
    save_gridless(gridless)
    
    print(f"✅ Migrated {len(gridless)} positions to gridless format")
    print(f"   Saved to: {GRIDLESS_FILE}")
    
    # Show migration summary
    total_balance = sum(p.get('balance', 0) for p in gridless.values()) / 1e18
    print(f"   Total balance: {total_balance:.4f} tokens")
    
    print("\n⚠️ IMPORTANT:")
    print("   - Set USE_GRIDLESS=true in your .env")
    print("   - Classic grid file still exists (backup)")
    print("   - You can switch back anytime with: to-grid")
    
    return True


def migrate_to_grid():
    """Migrate gridless to classic grid."""
    print("\n🔄 Migrating GRIDLESS → CLASSIC GRID...")
    
    gridless = load_gridless()
    if not gridless:
        print("❌ No gridless positions found!")
        return False
    
    print(f"   Found {len(gridless)} gridless positions")
    
    # Check if grid already has positions
    existing = load_grid()
    if existing:
        print(f"⚠️  Classic grid already has {len(existing)} positions!")
        response = input("   Overwrite? (yes/no): ")
        if response.lower() != 'yes':
            print("   Migration cancelled.")
            return False
    
    # Get grid spacing for migration
    spacing = input("   Grid spacing % (default 6.0): ").strip()
    spacing = float(spacing) if spacing else 6.0
    
    # Migrate
    grid = migrate_to_grid(gridless, spacing)
    save_grid_positions(grid)
    
    print(f"✅ Migrated {len(grid)} positions to classic grid format")
    print(f"   Saved to: {GRID_FILE}")
    print(f"   Grid spacing: {spacing}%")
    
    # Show migration summary
    total_balance = sum(p.get('balance', 0) for p in grid.values()) / 1e18
    print(f"   Total balance: {total_balance:.4f} tokens")
    
    print("\n⚠️ IMPORTANT:")
    print("   - Set USE_GRIDLESS=false in your .env")
    print("   - Gridless file still exists (backup)")
    print("   - You can switch back anytime with: to-gridless")
    
    return True


def main():
    if len(sys.argv) < 2:
        show_status()
        return
    
    cmd = sys.argv[1].lower()
    
    if cmd == 'status':
        show_status()
    elif cmd in ('to-gridless', 'togridless'):
        migrate_to_gridless()
    elif cmd in ('to-grid', 'togrid'):
        migrate_to_grid()
    else:
        print(f"Unknown command: {cmd}")
        print("\nUsage:")
        print("  python migrate_grid_mode.py status")
        print("  python migrate_grid_mode.py to-gridless")
        print("  python migrate_grid_mode.py to-grid")


if __name__ == "__main__":
    main()
