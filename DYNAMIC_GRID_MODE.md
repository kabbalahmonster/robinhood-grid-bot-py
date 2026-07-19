# Dynamic Grid Mode (Python Robinhood Bot)

**Status**: ✅ Implemented

## Overview

Dynamic Grid Mode is a P&L-based trading strategy for the Python Robinhood Chain grid trading bot. Instead of fixed buy/sell price ranges, trades are triggered based on real-time profit/loss calculations.

## How It Works

### Buy Triggers
1. **Empty Grid**: Buys immediately when no positions exist
2. **Dip Buying**: Buys when the **TOP position** (lowest buy price) drops below `DYNAMIC_BUY_THRESHOLD`

Example: With `DYNAMIC_BUY_THRESHOLD=-10`, the bot buys when your best position is down 10% or more.

### Sell Triggers
1. **Profit Target**: Sells when any position reaches `DYNAMIC_SELL_THRESHOLD` profit
2. **Stop Loss**: Optional - Sells when position drops below `DYNAMIC_STOP_LOSS` (disabled by default)

Example: With `DYNAMIC_SELL_THRESHOLD=8`, the bot sells at 8% profit.

## Configuration

Add to your `.env` file:

```bash
# Enable dynamic grid mode
USE_DYNAMIC_GRID=true

# Buy when top position is down 10%
DYNAMIC_BUY_THRESHOLD=-10.0

# Sell at 8% profit
DYNAMIC_SELL_THRESHOLD=8.0

# Stop loss disabled (0 = off)
DYNAMIC_STOP_LOSS=0.0

# Minimum 30 seconds between buys
DYNAMIC_MIN_BUY_INTERVAL=30
```

## Example Trading Flow

```
1. Bot starts, no positions
   → BUY at current price (0.0001 WETH)
   Position 0: buy_price=0.0001, target_sell=0.000108 (+8%)

2. Price drops to 0.00009 (-10% from Position 0)
   Top position (Position 0) P&L = -10%
   P&L <= DYNAMIC_BUY_THRESHOLD (-10%) → BUY
   Position 1: buy_price=0.00009, target_sell=0.0000972 (+8%)

3. Price recovers to 0.000108
   Position 0 P&L = +8% → SELL ✓
   Position 1 P&L = +20% → SELL ✓

4. Both positions sold, grid empty → Wait for next buy signal
```

## Key Differences from Standard Mode

| Feature | Standard Grid | Dynamic Grid |
|---------|---------------|--------------|
| Buy trigger | Price in fixed range | Top position P&L < threshold |
| Sell trigger | Fixed sell price | P&L ≥ threshold |
| Grid range | Bounded | Unbounded |
| Position count | Fixed | Expands/contracts |

## Files Added

| File | Description |
|------|-------------|
| `dynamic_grid.py` | Core P&L calculation and position management |
| `config.py` | Updated with dynamic grid config options |
| `grid_bot.py` | Updated with dynamic mode integration |

## Data Storage

Dynamic grid state is stored in `data/dynamic_state.json`:
- Active positions with P&L tracking
- Position history (last 100 trades)
- Configuration and metadata

## Safety Features

1. **Rate Limiting**: `DYNAMIC_MIN_BUY_INTERVAL` prevents rapid-fire buying
2. **Max Positions**: Respects `MAX_ACTIVE_POSITIONS` limit
3. **Gas Reserve**: Always keeps 0.001 WETH for gas
4. **Balance Checks**: Won't buy if WETH balance is too low

## Migration from Standard Grid

You can switch between modes by changing `USE_DYNAMIC_GRID`:

```bash
# Standard grid (fixed price ranges)
USE_DYNAMIC_GRID=false

# Dynamic grid (P&L-based)
USE_DYNAMIC_GRID=true
```

Each mode maintains its own state file, so you won't lose position data when switching.

## Monitoring

When dynamic mode is enabled, the bot logs:

```
🎯 DYNAMIC GRID MODE ENABLED
   Buy threshold: -10%
   Sell threshold: 8%

🎯 Dynamic buy: Position 0 at 0.0001000000
   Reason: Empty grid - initial buy
   Top position P&L: +0.00%

💰 Dynamic sell: Position 0 at 0.0001080000
   Reason: Profit target: 8.00% >= 8%
   P&L: +8.00%
```

## Recommended Settings

For **volatile tokens**:
```bash
DYNAMIC_BUY_THRESHOLD=-15
DYNAMIC_SELL_THRESHOLD=10
```

For **stable tokens**:
```bash
DYNAMIC_BUY_THRESHOLD=-5
DYNAMIC_SELL_THRESHOLD=5
```

For **aggressive accumulation**:
```bash
DYNAMIC_BUY_THRESHOLD=-20
DYNAMIC_SELL_THRESHOLD=15
MAX_ACTIVE_POSITIONS=6
```

## Testing

Test with small amounts first:

```bash
INITIAL_BUY_AMOUNT=0.001
MAX_ACTIVE_POSITIONS=2
```

Monitor the logs to ensure buy/sell triggers work as expected before increasing position sizes.

## Troubleshooting

**Bot not buying:**
- Check `DYNAMIC_BUY_THRESHOLD` is negative (e.g., -10 not 10)
- Verify top position P&L is below threshold
- Check WETH balance > 0.001

**Bot not selling:**
- Check `DYNAMIC_SELL_THRESHOLD` is positive
- Verify position P&L is above threshold
- Ensure `MIN_PROFIT_PERCENT` isn't blocking sells

**Too many buys:**
- Increase `DYNAMIC_MIN_BUY_INTERVAL`
- Make `DYNAMIC_BUY_THRESHOLD` more negative (e.g., -15 instead of -10)

---

**Note**: Dynamic grid mode is designed for Robinhood Chain but can work on any EVM chain supported by the bot.
