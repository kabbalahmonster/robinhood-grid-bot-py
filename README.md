# Robinhood Chain Grid Trading Bot (Python)

A production-grade grid trading bot for Robinhood Chain and other EVM networks, implemented in Python using web3.py. Supports both 0x Protocol and LI.FI API for optimal DEX aggregation with features like dynamic position sizing, moonbag retention, profit banking to stablecoins, and session tracking.

## Features

- **Two Trading Modes**:
  - **Classic Grid**: Fixed price levels with buy/sell ranges
  - **Gridless**: Dynamic position-based trading without fixed grid levels
- **Dynamic Grid Trading**: Automatically places buy orders at decreasing price levels
- **Cost Basis Tracking**: Each position tracks actual WETH spent for accurate P&L
- **Moonbag Support**: Retain a percentage of tokens after each sell
- **Profit Banking**: Automatically banks profits to USDG/USDC stablecoin
- **Session Statistics**: Track total buys, sells, and accumulated profit
- **Multi-DEX Aggregation**: 0x Protocol OR LI.FI API for best price execution
- **Anti-MEV Protection**: Jitter on timing to protect against front-running
- **Multi-Position Support**: Multiple active positions with individual tracking
- **Persistent State**: Survives restarts with position recovery
- **Multi-Chain Support**: Robinhood Chain (4663), Base (8453), Ethereum Mainnet (1)

## Quick Start

### Prerequisites

- Python 3.9+
- pip
- A wallet with ETH/WETH for trading
- DEX Aggregator API key:
  - **0x** (free at [0x.org](https://0x.org/)) - default
  - **LI.FI** (free at [li.fi](https://li.fi/)) - alternative
- Alchemy or other RPC provider API key

### Installation

1. Clone the repository:
```bash
git clone https://github.com/kabbalahmonster/robinhood-grid-bot-py.git
cd robinhood-grid-bot-py
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. **Configure environment first** (required before grid generation):
```bash
# Copy the appropriate environment file for your chain
cp .env.robinhood .env

# Edit .env with your settings - MUST set TOKEN_ADDRESS and other required vars
nano .env
```

**Required .env settings for grid generation:**
- `TOKEN_ADDRESS` - The token you want to trade (needed to fetch current price)
- `PRIVATE_KEY` - Your wallet private key
- `RPC_URL` - RPC endpoint URL
- `ZEROX_API_KEY` - 0x API key

4. Generate grid positions (requires .env to be configured):
```bash
# For Robinhood Chain (recommended for testing)
python generate_grid_dynamic.py --low 0.2 --high 3.0 --positions 24

# For Base or Mainnet
python generate_grid_dynamic.py --low 0.5 --high 2.0 --positions 10
```

**Note:** The grid generator reads `TOKEN_ADDRESS` from your `.env` file to fetch the current price from 0x API. If `.env` is not configured, the generator will fail.

5. Run the bot:
```bash
python grid_bot.py
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| **Wallet & Connection** ||||
| `PRIVATE_KEY` | Yes | - | Wallet private key (with 0x prefix) |
| `RPC_URL` | Yes | - | RPC endpoint URL (Alchemy recommended) |
| `CHAIN_ID` | Yes | 4663 | Chain ID (4663=Robinhood, 8453=Base, 1=Mainnet) |
| `ZEROX_API_KEY` | Yes* | - | 0x API key from 0x.org (if using 0x) |
| `LI_FI_API_KEY` | Yes* | - | LI.FI API key from li.fi (if using LI.FI) |
| `USE_LI_FI` | No | false | Use LI.FI instead of 0x for swaps |
| **Token Configuration** ||||
| `TOKEN_ADDRESS` | Yes | - | Token address to trade |
| `TOKEN_SYMBOL` | No | TOKEN | Token symbol for logging |
| `WETH_ADDRESS` | Auto | Chain | WETH address (auto-set per chain) |
| `USDG_ADDRESS` | Yes | - | Stablecoin address for profit banking |
| **Grid Parameters** ||||
| `GRID_SPACING_PERCENT` | No | 6.0 | Grid spacing percentage between levels |
| `MAX_POSITIONS` | No | 24 | Total number of grid positions to create |
| `MAX_ACTIVE_POSITIONS` | No | 6 | Maximum positions that can be active at once |
| **Trading Settings** ||||
| `MIN_PROFIT_PERCENT` | No | 5.0 | Minimum profit % before selling (includes 1.5% slippage buffer) |
| `INITIAL_BUY_AMOUNT` | No | 0.001 | Initial WETH amount for first buys |
| `SLIPPAGE_TOLERANCE` | No | 2.0 | Slippage tolerance % for swaps |
| **Profit Distribution** ||||
| `BANK_PERCENTAGE` | No | 0.0 | % of profit to swap to stablecoin (0 to disable) |
| `MOONBAG_PERCENTAGE` | No | 0.0 | % of tokens to keep after sell (0 to disable) |
| **Bot Behavior** ||||
| `POLL_INTERVAL_SECONDS` | No | 1 | Price check interval in seconds |
| `ANTI_MEV_JITTER` | No | true | Enable anti-MEV timing jitter |
| `LOG_LEVEL` | No | INFO | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `STATE_FILE` | No | ./data/positions.json | Position state file path |
| `COMPACT_MODE` | No | false | Compact single-line output for tmux |
| `MINIMAL_LOGS` | No | false | Remove timestamps from console output |
| **Gridless Mode** ||||
| `USE_GRIDLESS` | No | false | Enable gridless trading mode |
| `GRIDLESS_BUY_THRESHOLD` | No | -10.0 | Buy when top position P&L ≤ this % |
| `GRIDLESS_SELL_THRESHOLD` | No | 5.0 | Sell when position P&L ≥ this % |
| `GRIDLESS_LEADING_EDGE` | No | false | Buy into strength (single position climbing) |
| `GRIDLESS_STOPLOSS_ENABLED` | No | false | Enable stoploss in gridless mode |
| `GRIDLESS_STOPLOSS_THRESHOLD` | No | -25.0 | Stoploss trigger % |
| `GRIDLESS_BUY_COOLDOWN_SECONDS` | No | 300 | Cooldown between gridless buys (default 5 min) |
| `GRIDLESS_BUY_EXECUTION_MARGIN` | No | 50 | Execution margin % - blocks buy if quote P&L recovered past threshold + (abs(threshold) * margin%) (e.g., -10% trigger + 50% = block above -5%) |

### DEX Aggregation (0x vs LI.FI)

The bot supports two DEX aggregators. Choose based on your needs:

#### 0x Protocol (Default)
- **Best for**: General use, widest liquidity
- **Setup**: Get API key at [0x.org](https://0x.org/)
- **Config**:
```bash
USE_LI_FI=false
ZEROX_API_KEY=your_0x_key_here
```

#### LI.FI API (Alternative)
- **Best for**: Multi-chain routing, when 0x is rate-limited
- **Setup**: Get API key at [li.fi](https://li.fi/)
- **Config**:
```bash
USE_LI_FI=true
LI_FI_API_KEY=your_li_fi_key_here
```

**Switching**: Just change `USE_LI_FI` in your `.env`. Same bot, same config, different aggregator.

### Chain-Specific Configuration

Three template files are provided:

#### Robinhood Chain (4663) - `.env.robinhood`
```bash
CHAIN_ID=4663
RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73
USDG_ADDRESS=0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168
POLL_INTERVAL_SECONDS=1      # Fast chain, can poll every 1s
MAX_POSITIONS=24             # More positions for volatile tokens
```

#### Base (8453) - `.env.base`
```bash
CHAIN_ID=8453
RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0x4200000000000000000000000000000000000006
USDG_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
POLL_INTERVAL_SECONDS=10     # Base block time ~2s
MAX_POSITIONS=10
```

#### Ethereum Mainnet (1) - `.env.mainnet`
```bash
CHAIN_ID=1
RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
USDG_ADDRESS=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
POLL_INTERVAL_SECONDS=15     # Mainnet block time ~12s
MAX_POSITIONS=10
INITIAL_BUY_AMOUNT=0.01      # Higher amounts due to gas costs
```

## Usage

### Generate Grid Positions

**Prerequisite:** You must configure your `.env` file first (see Installation step 3). The grid generator needs `TOKEN_ADDRESS` to fetch the current price from 0x API.

Before running the bot, generate your grid positions:

```bash
# Generate grid from current price (requires .env to be configured)
python generate_grid_dynamic.py --low 0.2 --high 3.0 --positions 24

# Options:
# --low: Lowest price multiplier (0.2 = 20% of current price)
# --high: Highest price multiplier (3.0 = 300% of current price)
# --positions: Number of grid levels to create
```

This creates `data/positions.json` with buy/sell ranges for each level.

**Troubleshooting:** If you get "Failed to get price from 0x", check that:
- `TOKEN_ADDRESS` is set correctly in `.env`
- `ZEROX_API_KEY` is valid
- `RPC_URL` is accessible

### Migrate Grid (Refocus Without Losing Positions)

If price moves outside your grid, you can regenerate it while preserving filled positions:

```bash
# Preview changes first (dry run)
python migrate_grid.py --dry-run --low 0.5 --high 2.0 --positions 24

# Apply migration
python migrate_grid.py --low 0.5 --high 2.0 --positions 24
```

**What it does:**
1. Extracts your current holdings (positions with balance > 0)
2. Generates a new grid around current price
3. Maps each holding to the best-matching new position
4. Merges holdings if multiple map to same position
5. Ensures sell prices never decrease (uses `max(old, new)`)
6. Creates `positions.json.backup` before overwriting

**Use cases:**
- Price moved above/below your grid range
- Want to tighten/expand grid spacing
- Changing profit targets

### Generate New Wallet

Create a dedicated trading wallet:

```bash
# Generate and save to file
python generate_wallet.py --output trading_wallet.txt

# Generate without saving (console only)
python generate_wallet.py --no-save
```

**Security features:**
- Uses Python's `secrets` module (cryptographically secure)
- Sets file permissions to 600 (owner read/write only)
- Includes security warnings in output

Then add the private key to your `.env`:
```bash
PRIVATE_KEY=0x...
```

### Run the Bot

```bash
# Using default .env file
python grid_bot.py

# The bot will:
# 1. Load positions from data/positions.json
# 2. Check current price
# 3. Execute buys when price enters grid levels
# 4. Execute sells when profit targets are met
# 5. Bank profits to stablecoin (if enabled)
# 6. Log session statistics
```

### Compact Mode (Tmux-Friendly)

For running multiple bots in tmux panes, enable compact output:

```bash
# Add to .env
COMPACT_MODE=true
MINIMAL_LOGS=true
```

**Compact output:**
```
01:58 R#123 | TENDIES | W:0.015 T:93.4 | 2/24 | B:9 S:9 P:0.0003
  #18: 40.82@7.76e-06 P&L:+13.8% Sell@9.51e-06 +7.7%
  #19: 23.01@7.73e-06 P&L:+11.7% Sell@1.07e-05 +41.9%
```

| Setting | Effect |
|---------|--------|
| `COMPACT_MODE=true` | Single-line status, top 3 positions only |
| `MINIMAL_LOGS=true` | Remove timestamps from console output |

File logs always retain full timestamps for debugging.

### Example Session Output

```
======================================================================
ROUND #506 | TENDIES | Elapsed: 1128s
======================================================================
💰 WETH Balance: 0.006016
🪙 Token Balance: 54.274311
📊 Price: 1 TENDIES = 0.0000088332 WETH
📈 Positions: 2 active / 22 empty (max active: 12)
📊 Session: 1 buys, 2 sells, 0.000029 WETH profit
🎯 Active Positions:
   #12: 31.2606 tokens | Buy: 0.0000077616 | Sell@: 0.0000095110 | P&L: +13.81% (need +7.7% more to sell)
   #13: 23.0137 tokens | Buy: 0.0000004204 | Sell@: 0.0000106990 | P&L: +2001.34% (need +21.1% more to sell)
----------------------------------------------------------------------
```

### Understanding the Output

- **ROUND #X**: Incrementing counter for each price check
- **Elapsed**: Seconds since bot started
- **WETH Balance**: Available WETH for buying
- **Token Balance**: Tokens in wallet (not in positions)
- **Price**: Current token price in WETH
- **Positions**: Active (have tokens) / Empty (available for buys)
- **Session**: Total buys, sells, and accumulated WETH profit
- **Active Positions**: Each shows tokens held, buy price, sell target, P&L, and % needed to reach sell target

## Gridless Mode

Gridless mode is an alternative trading strategy that doesn't use fixed price levels. Instead, it dynamically manages positions based on P&L thresholds.

### When to Use Gridless

| Use Classic Grid When | Use Gridless When |
|----------------------|-------------------|
| Price moves in predictable ranges | Price is highly volatile or trending |
| You want defined entry/exit points | You want P&L-based exits |
| Token has clear support/resistance levels | You want simpler position management |

### Enabling Gridless Mode

```bash
# Add to .env
USE_GRIDLESS=true
GRIDLESS_SELL_THRESHOLD=5.0      # Sell at +5% P&L
GRIDLESS_BUY_THRESHOLD=-10.0     # Buy more when top position at -10%
MAX_ACTIVE_POSITIONS=6           # Max positions to hold
```

### Gridless Buy Logic

Buys are triggered when:
1. **No positions exist** - Initial buy to start
2. **Top position P&L ≤ buy_threshold** - Buy the dip
3. **Leading edge** (optional) - Buy into strength when single position climbing

Buy amount: `available_WETH / available_slots`

### Gridless Sell Logic

Sells are triggered when:
1. **P&L ≥ sell_threshold** - Take profit
2. **Stoploss triggered** - Emergency exit (optional)

Each position is evaluated independently - a profitable position can sell even if others are underwater.

### Gridless vs Classic Grid

| Feature | Classic Grid | Gridless |
|---------|-------------|----------|
| Price levels | Fixed ranges | Dynamic |
| Buy trigger | Price enters range | Top position P&L threshold |
| Sell trigger | Price hits sellMin | P&L threshold |
| Stoploss | Per-position | Per-position |
| Configuration | Grid spacing % | P&L thresholds |

### Migrating Between Modes

Use `migrate_grid_mode.py` to switch between trading modes without losing positions:

```bash
# Check current status
python migrate_grid_mode.py status

# Migrate classic grid → gridless
python migrate_grid_mode.py to-gridless

# Migrate gridless → classic grid
python migrate_grid_mode.py to-grid
```

**What happens during migration:**
- Position data is converted between formats
- Balances and cost basis are preserved
- For grid migration, you'll be prompted for grid spacing %
- Original files are kept as backup

**After migration:**
- Update `USE_GRIDLESS` in your `.env` file
- Restart the bot

## How It Works

### Grid Strategy

1. **Grid Initialization**: Creates price levels below current market price
   - Spacing: `GRID_SPACING_PERCENT` between levels (default 6%)
   - Range: From `current_price * low_factor` to `current_price * high_factor`
   - Each position has: buyMin, buyMax, sellMin, stoploss

2. **Buy Execution**:
   - Monitors price for grid level triggers (buyMin ≤ price ≤ buyMax)
   - Calculates dynamic buy amount: `available_WETH / available_slots`
   - `available_slots = MAX_ACTIVE_POSITIONS - active_positions`
   - Executes swap via 0x AllowanceHolder API
   - Records position with actual WETH cost (nano-WETH)

3. **Sell Execution**:
   - Monitors positions for sell targets
   - Requires profit ≥ `MIN_PROFIT_PERCENT` + 1.5% slippage buffer
   - Applies moonbag: keeps X% of tokens, sells rest
   - Banks profit: swaps Y% of profit to stablecoin
   - Updates session statistics

4. **Dynamic Sizing**:
   - Buy amounts adjust based on available WETH and empty positions
   - Ensures even distribution across grid levels
   - Automatically compounds as positions fill/empty

### Position Tracking

```json
{
  "1": {
    "buyMin": 0,
    "buyMax": 2368000000,
    "sellMin": 2605000000,
    "stoploss": 1894000000,
    "balance": 28431726788596754770,
    "cost": 245094000
  }
}
```

- `buyMin/buyMax`: Price range to trigger buy (in nano-WETH)
- `sellMin`: Price target to trigger sell (in nano-WETH)
- `stoploss`: Price to emergency exit (optional)
- `balance`: Tokens held (in wei)
- `cost`: WETH spent (in nano-WETH)

### Key Features Explained

**Moonbag**: After selling, retains a percentage of tokens in the position
- Set `MOONBAG_PERCENTAGE=10` to keep 10% of tokens
- Cost basis is proportionally reduced
- Position remains "active" with remaining tokens

**Banking**: Swaps a percentage of WETH profit to stablecoin
- Set `BANK_PERCENTAGE=20` to bank 20% of each profit
- Happens immediately after successful sell
- Protects gains in volatile markets

**Session Stats**: Tracks performance across bot lifetime
- `session_buys`: Total buy transactions
- `session_sells`: Total sell transactions
- `session_profit_weth`: Accumulated WETH profit
- Resets when bot restarts

**Minimum Profit**: Prevents selling at a loss due to slippage
- Set `MIN_PROFIT_PERCENT=5` for 5% minimum
- Adds 1.5% buffer (so requires 6.5% actual profit)
- Blocks sells until price reaches threshold

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Grid Trading Bot                        │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │  Config  │  │  Wallet  │  │  0x API  │  │ Storage  │    │
│  │          │  │          │  │          │  │          │    │
│  │ - Env    │  │ - Web3   │  │ - Quotes │  │ - State  │    │
│  │ - Params │  │ - Txns   │  │ - Swaps  │  │ - Pos    │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘    │
│       └─────────────┴─────────────┴─────────────┘           │
│                         │                                    │
│                    ┌────┴────┐                               │
│                    │  Bot    │                               │
│                    │ - Grid  │                               │
│                    │ - Trade │                               │
│                    │ - Bank  │                               │
│                    └─────────┘                               │
└─────────────────────────────────────────────────────────────┘
```

### File Structure

```
robinhood-grid-bot-py/
├── README.md                   # This documentation
├── requirements.txt            # Python dependencies
├── .env.robinhood             # Robinhood Chain config template
├── .env.base                  # Base Chain config template
├── .env.mainnet               # Ethereum Mainnet config template
├── config.py                  # Configuration management
├── grid_bot.py                # Main bot logic
├── wallet.py                  # Wallet & transaction handling
├── zero_x.py                  # 0x API integration
├── generate_grid_dynamic.py   # Grid position generator (dynamic from price)
├── generate_grid.py           # Grid position generator (legacy format)
├── generate_positions.py      # Grid position generator (simple)
├── migrate_grid.py            # Migrate holdings to new grid
├── generate_wallet.py         # Generate new trading wallet
├── data/                      # Data directory
│   └── positions.json         # Position state file
└── logs/                      # Log files (created at runtime)
```

### Module Descriptions

**config.py**: Loads and validates environment variables, provides chain-specific defaults

**wallet.py**: Handles Web3 connections, token approvals, transaction signing, balance checks

**zero_x.py**: Integrates with 0x API for price quotes and swap transactions

**grid_bot.py**: Main trading logic - grid management, buy/sell decisions, profit tracking

**generate_grid_dynamic.py**: Creates grid positions based on current market price

**migrate_grid.py**: Regenerates grid while preserving filled positions and their sell prices

**generate_wallet.py**: Creates cryptographically secure Ethereum wallets for trading

## Utility Scripts Reference

### Generate Wallet (`generate_wallet.py`)

Create dedicated trading wallets with secure key generation:

```bash
# Generate wallet and save to file (appends if exists)
python generate_wallet.py --output my_wallet.txt

# Create new file (auto-increments: wallet_1.txt, wallet_2.txt, etc.)
python generate_wallet.py --output my_wallet.txt --new-file

# Generate without saving (console only)
python generate_wallet.py --no-save

# Skip setting file permissions
python generate_wallet.py --no-chmod
```

**Features:**
- Uses Python's `secrets` module (cryptographically secure randomness)
- Creates files with 600 permissions (owner read/write only)
- Appends to existing files (numbered wallets: #1, #2, #3...)
- Includes warnings and next steps in output

**Example output file:**
```
# ============================================================
# Ethereum Wallet #1 - Generated 2026-07-20T11:51:00Z
# ============================================================
Address:    0x...
PrivateKey: 0x...
```

### Migrate Grid (`migrate_grid.py`)

Regenerate grid while preserving filled positions:

```bash
# Preview changes first (dry run)
python migrate_grid.py --dry-run --low 0.5 --high 2.0 --positions 24

# Apply migration
python migrate_grid.py --low 0.5 --high 2.0 --positions 24
```

**What it does:**
1. Extracts current holdings (balance > 0)
2. Generates new grid around current price
3. Maps holdings to best-matching new positions
4. Merges if multiple map to same position
5. Ensures sell prices never decrease
6. Creates `positions.json.backup` before overwriting

**Use cases:**
- Price moved outside your grid range
- Want to tighten/expand grid spacing
- Changing profit targets

### Migrate Grid Mode (`migrate_grid_mode.py`)

Switch between classic grid and gridless modes:

```bash
# Check status of both modes
python migrate_grid_mode.py status

# Migrate classic grid → gridless
python migrate_grid_mode.py to-gridless

# Migrate gridless → classic grid
python migrate_grid_mode.py to-grid
```

**After migration:**
1. Update `USE_GRIDLESS` in your `.env`
2. Restart the bot

**Important:** Original files are kept as backup. You can switch back anytime.

### Generate Grid (`generate_grid_dynamic.py`)

Create grid positions based on current market price:

```bash
# Generate grid from current price
python generate_grid_dynamic.py --low 0.2 --high 3.0 --positions 24

# Options:
# --low: Lowest price multiplier (0.2 = 20% of current)
# --high: Highest price multiplier (3.0 = 300% of current)
# --positions: Number of grid levels
```

**Requirements:**
- `.env` must be configured with `TOKEN_ADDRESS`
- `ZEROX_API_KEY` required to fetch current price

### Test Gridless (`test_gridless_simple.py`)

Run unit tests for gridless trading logic:

```bash
# Run all tests
python test_gridless_simple.py

# Tests cover:
# - P&L calculations
# - Buy/sell decision logic
# - Stoploss triggers
# - Leading edge buys
# - Position validation
```

## Safety Features

1. **Profit Protection**: Only sells when profit ≥ `MIN_PROFIT_PERCENT` + slippage buffer
2. **Slippage Protection**: Configurable slippage tolerance on all swaps
3. **Gas Estimation**: 50% buffer on gas estimates for reliability
4. **Atomic State Saves**: Position state saved after every trade
5. **Approval Checks**: Verifies token approvals before trading
6. **Error Handling**: Graceful failures with detailed logging
7. **Session Tracking**: Monitors cumulative performance

## Troubleshooting

### "Failed to connect to RPC"
- Verify RPC URL in .env file
- Check network connectivity
- Try alternative RPC endpoint (Alchemy, QuickNode, etc.)

### "Insufficient allowance"
- The bot will auto-approve tokens on first use
- Check wallet has ETH for gas fees
- Verify token contract addresses are correct

### "Quote failed"
- Check 0x API key is valid and not rate-limited
- Verify token has liquidity on the chain
- Increase `SLIPPAGE_TOLERANCE` if token is volatile

### "Transaction failed"
- Check gas prices (may be too low during congestion)
- Verify sufficient ETH for gas
- Check token approvals haven't expired

### "Position cost seems wrong"
- Check the transaction on block explorer
- Verify the `cost` field in positions.json matches actual WETH spent
- The buy price is calculated as: `cost / (balance / 10^18)`

### Bot not buying/selling
- Check `MAX_ACTIVE_POSITIONS` hasn't been reached
- Verify price is within grid ranges
- Check `MIN_PROFIT_PERCENT` isn't blocking sells
- Review logs for specific error messages

## Development

### Running Tests

```bash
# Test imports
python -c "from grid_bot import GridBot; print('OK')"

# Test configuration
python -c "from config import load_config; c = load_config('.env'); print(f'Chain: {c.chain_name}')"

# Test wallet connection
python -c "from config import load_config; from wallet import Wallet; c = load_config('.env'); w = Wallet(c); print(f'Balance: {w.get_eth_balance()}')"
```

### Adding New Chains

1. Add chain config to `config.py`:
```python
CHAIN_CONFIG = {
    12345: {
        "name": "NewChain",
        "weth": "0x...",
        "permit2": "0x...",
        "zero_x_proxy": "0x...",
        "default_max_positions": 15,
    },
}
```

2. Create `.env.newchain` file with appropriate settings

3. Update README with chain information

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -am 'Add new feature'`
4. Push to the branch: `git push origin feature/my-feature`
5. Submit a pull request

## Security Considerations

⚠️ **Important**:
- **Never commit `.env` files with private keys**
- Use dedicated trading wallets (not your main wallet)
- Start with small amounts to test
- Monitor gas costs, especially on mainnet
- Review and understand the code before running with significant funds
- Keep your 0x API key private
- Use hardware wallets when possible for production

## API Reference

### BotConfig

Configuration dataclass loaded from environment.

```python
from config import load_config

config = load_config(".env")
print(config.chain_name)        # "Robinhood"
print(config.max_positions)     # 24
print(config.bank_percentage)   # 20.0
```

### Wallet

```python
from config import load_config
from wallet import Wallet

config = load_config(".env")
wallet = Wallet(config)

# Get balances
eth_balance = wallet.get_eth_balance()
weth_balance, weth_raw = wallet.get_token_balance(config.weth_address)

# Approve tokens
result = wallet.approve_token(token_address, spender, amount)

# Send transaction
result = wallet._send_transaction(tx_params)
```

### ZeroXClient

```python
from config import load_config
from zero_x import ZeroXClient

config = load_config(".env")
client = ZeroXClient(config)

# Get quote
quote = client.build_swap_transaction(
    sell_token=weth_address,
    buy_token=token_address,
    sell_amount=wei_amount,
    taker_address=wallet_address,
    slippage_percentage=0.02,
)
```

## Performance Tips

1. **Use Private RPCs**: Public RPCs have strict rate limits
   - Alchemy, Infura, QuickNode recommended
   - Set in `.env`: `RPC_URL=https://...`

2. **Optimize Polling**:
   - Robinhood: 1-5 seconds (fast chain)
   - Base: 5-10 seconds
   - Mainnet: 12-15 seconds (match block time)

3. **Grid Density**:
   - More positions = more opportunities but smaller sizes
   - Fewer positions = larger sizes but fewer trades
   - 10-24 positions is a good balance

4. **Profit Settings**:
   - Lower `MIN_PROFIT_PERCENT` = more frequent trades, smaller profits
   - Higher = fewer trades, larger profits
   - 5-10% is typical for volatile tokens

## Changelog

### v1.2.0 - Latest
- **Gridless Trading Mode**: Dynamic position-based trading without fixed grid levels
- **Grid Mode Migration**: `migrate_grid_mode.py` to switch between classic/gridless
- **Individual Position Quotes**: Gridless sells use per-position quotes (not aggregate)
- **Aligned Position Display**: Consistent column formatting for position output
- **Wallet Append**: Generate multiple wallets to same file (numbered)
- **Position Sorting**: Gridless positions display sorted by buy price ascending
- **LI.FI API Support**: Alternative DEX aggregator to 0x

### v1.1.0
- **Compact Mode**: Single-line output for tmux multi-pane view
- **Minimal Logs**: Option to remove timestamps from console output
- **Grid Migration Tool**: Regenerate grid while preserving positions
- **Wallet Generator**: Create secure trading wallets
- **Fixed Buy Calculation**: Now respects `MAX_ACTIVE_POSITIONS` properly
- **Auto-create data directory**: Grid generators work on fresh clones
- **Removed pydantic**: Cleaner dependency tree, Python 3.14 compatible

### v1.0.0 - Initial Release
- Dynamic grid generation from current price
- 0x AllowanceHolder API integration
- Multi-position support with cost tracking
- Session statistics (buys, sells, profit)
- Moonbag and banking features
- Multi-chain support (Robinhood, Base, Mainnet)

## License

MIT License - See LICENSE file

## Support

For issues and feature requests, please open a GitHub issue.

## Acknowledgments

- 0x Protocol for DEX aggregation API
- web3.py team for Ethereum integration
- Robinhood Chain team for L2 infrastructure
