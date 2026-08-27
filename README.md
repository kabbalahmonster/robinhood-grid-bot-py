# Robinhood Chain Grid Trading Bot (Python)

A production-grade grid trading bot for Robinhood Chain and other EVM networks, implemented in Python using web3.py. Supports SushiSwap, Uniswap API, LI.FI, and 0x swap providers with features like dynamic position sizing, moonbag retention, profit banking to stablecoins, session tracking, and optional fleet-dashboard reporting.

## Features

- **Two Trading Modes**:
  - **Classic Grid**: Fixed price levels with buy/sell ranges
  - **Gridless**: Dynamic position-based trading without fixed grid levels
- **Dynamic Grid Trading**: Automatically places buy orders at decreasing price levels
- **Cost Basis Tracking**: Each position tracks actual WETH spent for accurate P&L
- **Dynamic Token Decimals**: Reads and caches each configured ERC-20's on-chain `decimals()` value; non-18-decimal assets use correct prices, balances, P&L, moonbags, and dashboard valuation
- **Moonbag Support**: Retain a percentage of tokens after each sell
- **Profit Banking**: Automatically banks profits to USDG/USDC stablecoin
- **Session Statistics**: Track total buys, sells, and accumulated profit
- **Persistent Realized Profit**: Confirmed sell profit/loss survives restarts with transaction-hash deduplication and non-destructive baseline resets
- **Multiple Swap Providers**: SushiSwap, Uniswap API, LI.FI, or 0x selected through environment configuration
- **Anti-MEV Protection**: Jitter on timing to protect against front-running
- **Multi-Position Support**: Multiple active positions with individual tracking
- **Persistent State**: Survives restarts with position recovery
- **Multi-Chain Support**: Robinhood Chain (4663), Base (8453), Ethereum Mainnet (1)
- **Optional Live Dashboard**: Non-blocking authenticated status reporting, fleet metrics, Dexscreener charts, and explorer links
- **Persistent Trade History**: Latest 50 successful buys/sells saved locally and reported without additional RPC/API calls
- **Structured Events**: Latest 50 redacted successes/warnings/errors persist locally with repeat counts
- **Transient Sell Checks**: Reports when a sell target is being checked but quoted profit is below the configured minimum
- **Safe Maintenance CLI**: Read-only config checks plus independent profit, Event, and Trade History resets
- **USDG Monitoring**: Optional read-only stablecoin balance included in dashboard status

## Quick Start

### Prerequisites

- Python 3.9+
- pip
- A wallet with ETH/WETH for trading
- Credentials for the selected swap provider when required; Sushi's public v7 API works without a key
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
| `RPC_URLS` | No | empty | Comma-separated RPC rotation/failover list; overrides `RPC_URL` when set |
| `CHAIN_ID` | Yes | 4663 | Chain ID (4663=Robinhood, 8453=Base, 1=Mainnet) |
| `ZEROX_API_KEY` | Yes* | - | 0x API key from 0x.org (if using 0x) |
| `LI_FI_API_KEY` | Yes* | - | LI.FI API key from li.fi (if using LI.FI) |
| `LI_FI_INTEGRATOR` | No | empty | Optional LI.FI integrator identifier loaded for provider compatibility |
| `UNISWAP_API_KEY` | Yes* | - | Uniswap API key (if using Uniswap) |
| `UNISWAP_PERMIT2_DISABLED` | No | true | Request direct approval flow instead of Permit2 in the Uniswap API client |
| `SUSHI_API_KEY` | No | empty | Optional Sushi portal API key; the public v7 API works without one |
| `SWAP_PROVIDER` | No | empty | Explicit provider: `0x`, `lifi`, `uniswap`, or `sushiswap`; empty uses legacy flags |
| `SWAP_FALLBACK_PROVIDER` | No | sushiswap | Immediate per-operation fallback after retryable pre-broadcast failures; empty disables fallback |
| `USE_LI_FI` | No | false | Use LI.FI instead of 0x for swaps |
| `USE_UNISWAP_API` | No | true | Legacy Uniswap selection used when `SWAP_PROVIDER` is empty |
| **Token Configuration** ||||
| `TOKEN_ADDRESS` | Yes | - | Token address to trade |
| `TOKEN_SYMBOL` | No | TOKEN | Token symbol for logging |
| `WETH_ADDRESS` | Auto | Chain | WETH address (auto-set per chain) |
| `USDG_ADDRESS` | Yes | - | Stablecoin address for profit banking |
| `TREASURY_ALLOWED_RECIPIENTS` | No | empty | Comma-separated EVM recipient allowlist for the guarded treasury-transfer CLI |
| **Grid Parameters** ||||
| `GRID_SPACING_PERCENT` | No | 5.0 | Grid spacing percentage between levels |
| `MAX_POSITIONS` | No | Chain default | Total number of grid positions to create |
| `MAX_ACTIVE_POSITIONS` | No | `MAX_POSITIONS` | Maximum positions that can be active at once |
| **Trading Settings** ||||
| `MIN_PROFIT_PERCENT` | No | 5.0 | Minimum profit % before selling |
| `INITIAL_BUY_AMOUNT` | No | 0.01 | Initial ETH/WETH amount for first buys |
| `SLIPPAGE_TOLERANCE` | No | 2.0 | Slippage tolerance % for swaps |
| **Profit Distribution** ||||
| `BANK_PERCENTAGE` | No | 20 | % of profit to swap to stablecoin (0 to disable) |
| `MOONBAG_PERCENTAGE` | No | 1 | % of tokens to keep after sell (0 to disable) |
| `BANK_MIN_AMOUNT` | No | 0.2 | Minimum stablecoin output required before banking |
| `FAST_PROFIT` | No | true | Sell above minimum profit without waiting for the classic sell range |
| `TRADEABLE_BALANCE_PERCENT` | No | 100 | Percentage of ETH/WETH balance available for trading |
| `ETH_GAS_RESERVE` | No | 0.0005 | Native ETH retained for transaction gas and protected by guarded ETH treasury transfers |
| `USE_ETH_TRADING` | No | false | Trade native ETH rather than WETH; chain templates may override this to true |
| `GAS_LIMIT_MULTIPLIER` | No | 1.05 | Safety multiplier applied to estimated transaction gas limits; values below 1 are clamped |
| `GAS_PRICE_MULTIPLIER` | No | 1.05 | Safety multiplier applied to current/quoted gas price; values below 1 are clamped |
| **Bot Behavior** ||||
| `POLL_INTERVAL_SECONDS` | No | 6 | Price check interval in seconds |
| `ANTI_MEV_JITTER` | No | true | Enable anti-MEV timing jitter |
| `LOG_LEVEL` | No | INFO | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `STATE_FILE` | No | ./data/positions.json | Position state file path |
| `COMPACT_MODE` | No | false | Compact single-line output for tmux |
| `MINIMAL_LOGS` | No | false | Remove timestamps from console output |
| `MERCURY_EVOCATION` | No | true | Print the Mercury trading evocation once after successful startup |
| **Dashboard Reporting** ||||
| `DASHBOARD_URL` | No | empty | Full status endpoint URL; empty disables reporting |
| `DASHBOARD_API_KEY` | No* | empty | Shared dashboard key; required when reporting is enabled |
| `BOT_ID` | No | `TOKEN_SYMBOL` | Stable unique bot ID used by the dashboard |
| `DASHBOARD_NAME` | No | empty | Optional human-friendly display name |
| `DASHBOARD_GROUP` | No | empty | Optional fleet/group label used for filtering |
| **Gridless Mode** ||||
| `USE_GRIDLESS` | No | true | Enable gridless trading mode |
| `GRIDLESS_BUY_THRESHOLD` | No | -10.0 | Buy when top position P&L ≤ this % |
| `GRIDLESS_SELL_THRESHOLD` | No | 5.0 | Sell when position P&L ≥ this % |
| `GRIDLESS_LEADING_EDGE` | No | true | Buy into strength (single position climbing) |
| `GRIDLESS_STOPLOSS_ENABLED` | No | false | Enable stoploss in gridless mode |
| `GRIDLESS_STOPLOSS_THRESHOLD` | No | -25.0 | Stoploss trigger % |
| `GRIDLESS_BUY_COOLDOWN_SECONDS` | No | 0 | Cooldown between gridless buys (0 disables cooldown) |
| `GRIDLESS_BUY_EXECUTION_MARGIN` | No | 50 | Execution margin % - blocks buy if quote P&L recovered past threshold + (abs(threshold) * margin%) (e.g., -10% trigger + 50% = block above -5%) |

### Swap providers

Prefer explicit provider selection:

```dotenv
SWAP_PROVIDER=sushiswap  # sushiswap, uniswap, lifi, or 0x
```

Explicit `SWAP_PROVIDER` takes precedence. `sushi` is accepted as an alias for `sushiswap`. When the setting is empty, backward-compatible selection checks `USE_UNISWAP_API=true`, then `USE_LI_FI=true`, otherwise 0x. The normalized templates currently select Uniswap through the legacy flag and configure Sushi as the fallback. On a retryable primary failure (HTTP 404/408/425/429/5xx, timeout, or connection failure), the current pre-broadcast operation stops and immediately restarts from the beginning with the fallback. The next operation gives the configured primary the first opportunity again. This avoids mixing approvals and calldata from different routers inside one transaction flow. When Sushi is primary and the default Sushi fallback value is unchanged, the bot automatically uses Uniswap as the reverse fallback if `UNISWAP_API_KEY` is configured; set `SWAP_FALLBACK_PROVIDER` empty to disable fallback. Sushi uses its v7 quote/swap API and supports an optional `SUSHI_API_KEY`; the other providers require their matching credentials. When Sushi returns HTTP 429, that bot honors `Retry-After` when supplied and otherwise enters a jittered exponential cooldown (30 seconds up to 15 minutes). Requests are skipped locally during the cooldown and a successful response resets the backoff.

### Chain-Specific Configuration

Three template files are provided:

#### Robinhood Chain (4663) - `.env.robinhood`
```bash
CHAIN_ID=4663
RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73
USDG_ADDRESS=0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168
POLL_INTERVAL_SECONDS=6
MAX_POSITIONS=24             # More positions for volatile tokens
```

#### Base (8453) - `.env.base`
```bash
CHAIN_ID=8453
RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0x4200000000000000000000000000000000000006
USDG_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
POLL_INTERVAL_SECONDS=6
MAX_POSITIONS=10
```

#### Ethereum Mainnet (1) - `.env.mainnet`
```bash
CHAIN_ID=1
RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
USDG_ADDRESS=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
POLL_INTERVAL_SECONDS=6
MAX_POSITIONS=10
INITIAL_BUY_AMOUNT=0.01      # Higher amounts due to gas costs
```

## Usage

### Operating multiple bots with tmux

The repository includes aligned `start-fleet`, `stop-fleet`, `restart-fleet`,
`update-fleet`, guarded treasury-transfer and managed-asset liquidation tools,
read-only doctor/inventory/audit commands, shared `--only`/`--exclude`
targeting, and a reusable fleet variable updater under
[`ops/fleet`](ops/fleet/README.md). They run independently configured clones in
tiled tmux panes while preserving an interactive Bash shell, command history,
and normal job control beneath every bot. The fleet guide covers fresh-clone
setup, local configuration, virtual environments, command installation,
fleet health checks, balance/position inventory, receipt reconciliation,
updates, native/ERC-20 treasury safety, verified position-clearing liquidation,
fleet `.env` updates, tmux navigation, and the exact
`Ctrl+C`/`Ctrl+Z` behavior.

The gitignored `ops/fleet/fleet.conf` may also define a public
`FLEET_TREASURY_RECIPIENT`. Fleet USDG sweeps and generic treasury transfers
use it whenever `--recipient` is omitted; an explicit recipient overrides it,
and every bot's `.env` allowlist is still enforced.

Fleet membership defaults to recursive checkout discovery below
`FLEET_BOT_ROOT` (`$HOME/bot-farm/rh-bots` by default). A non-empty explicit
`FLEET_BOT_DIRS` array overrides discovery completely. The fleet guide explains
depth limits, exclusions, deterministic ordering, and the safety tradeoff.

Start with:

```bash
cp ops/fleet/fleet.conf.example ops/fleet/fleet.conf
nano ops/fleet/fleet.conf
ops/fleet/start-fleet
```

#### Fleet command map

All mutating financial commands preview by default and keep their explicit
execution guards. See [`ops/fleet/README.md`](ops/fleet/README.md) for every
option and safety invariant.

| Command | Purpose | Mutates/broadcasts? |
|---|---|---|
| `start-fleet` / `stop-fleet` / `restart-fleet` | Tmux lifecycle for the configured fleet | Processes only |
| `update-fleet` | Preflight and fast-forward clean checkouts; optional restart | Git/processes |
| `fleet-discover` | Print deterministic membership for review | No |
| `fleet-doctor` | Check config, Git, RPC, contracts, provider route, and dashboard | No |
| `fleet-inventory` | Read addresses, reserves, managed balances, positions, and audit ages | No |
| `fleet-audit` | Reconcile local treasury/liquidation receipts | No |
| `update-variable` | Preview/atomically change selected `.env` variables | Config only; `--apply` required |
| `backup-private-keys` | Validate every configured bot and write one sensitive key backup | Sensitive file output |
| `usdg-sweep` | Plan or execute fleet USDG transfers | Broadcast only with all guards |
| `treasury-transfer` | Plan or execute native/ERC-20 transfers | Broadcast only with all guards |
| `liquidate-assets` | Plan or sell verified bot-managed assets and clear matching positions | Broadcast only with all guards |
| `dashboard-remove` | Preview/remove permanently retired DoomDash cards/history | Network mutation only with both confirmations |

Common `--only name1,name2` and `--exclude name3` selectors are supported by
start, update, variable update, treasury/liquidation, doctor, inventory, and
audit operations. `stop-fleet` and `restart-fleet` remain whole-session actions.

### Production architecture and repository boundaries

Each fleet member is an independent clone with its own Git worktree, virtual
environment, `.env`, wallet, and `data/` directory. Shared fleet scripts only
coordinate those clones; they do not merge config or state.

```text
fleet.conf -> independent bot clones -> RPC + selected swap provider
                                     -> authenticated status reports to DoomDash
DoomDash -> browser snapshot/SSE + server-side Telegram monitoring
```

The trading repository is authoritative for execution logic and templates.
DoomDash is authoritative only for monitoring state; it is not a source-code,
secret, strategy-config, or wallet backup. Telegram monitoring lives in the
DoomDash repository and intentionally exposes no remote trading controls.

### Recreating one bot or the whole fleet

For an exact recoverable bot, back up the following outside Git and encrypt any
copy containing signing material:

- the bot's `.env` (strategy, chain/token/provider addresses, API credentials,
  dashboard identity, and `PRIVATE_KEY`)
- the entire `data/` directory, especially position state, persistent profit
  totals, dashboard trade/Event history, and treasury/liquidation receipts
- the Git remote, branch, and preferably the deployed commit SHA
- the fleet's gitignored `fleet.conf` and host service/tmux conventions

`backup-private-keys` validates and exports signing keys only. It does **not**
replace backups of `.env`, `data/`, or `fleet.conf`.

Rebuild procedure:

1. Clone the repository into the expected per-bot path and check out the saved
   commit/branch.
2. Create `.venv` and install `requirements.txt`.
3. Restore `.env` with mode `0600`; restore `data/` before starting the bot.
4. Run `python grid_bot.py --check-config`, then fleet `fleet-doctor` and
   `fleet-inventory` for read-only verification.
5. Confirm wallet, chain, token, provider, reserve, position count, and
   dashboard identity against the backup.
6. Start one bot and verify price/status reporting before admitting it to the
   full fleet session. DoomDash recreates a missing card on the next report.

Without old `data/`, the code and wallet can still be restored, but existing
position cost bases, persistent accounting, and audit continuity may not be
reconstructable safely. Do not generate a fresh grid over an unknown funded
wallet merely because the code starts.

### Safe maintenance commands

Stop the bot before resetting persisted history. None of these commands starts
the trading loop, and none modifies positions:

```bash
# Validate provider/key selection, RPC chain, wallet/token reads, and dashboard
# connectivity without requesting a quote or broadcasting a transaction.
python grid_bot.py --check-config

# Clear the dashboard Events history for this bot.
python grid_bot.py --reset-event-data

# Clear the dashboard Trade history for this bot.
python grid_bot.py --reset-trade-history

# Start a new displayed realized-profit accounting period.
python grid_bot.py --reset-profit-baseline
```

Each reset command is dispatched before Web3/provider imports, so it works with plain `python3` even when the bot's virtual environment is elsewhere. It exits without constructing or running `GridBot`: no provider initialization, quote, transaction, dashboard reporter, or trading loop. Event and Trade History resets atomically replace their files with empty lists. The profit reset starts a new displayed period at zero while preserving cumulative totals and recent transaction hashes for duplicate protection. `--check-config` still requires the normal bot environment because it deliberately checks live dependencies.

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
├── dashboard_reporter.py      # Non-blocking authenticated dashboard client
├── profit_tracker.py          # Persistent realized-profit accounting
├── wallet.py                  # Wallet & transaction handling
├── zero_x.py                  # 0x API integration
├── li_fi.py                   # LI.FI API integration
├── uniswap_api.py             # Uniswap Trading API integration
├── generate_grid_dynamic.py   # Grid position generator (dynamic from price)
├── generate_grid.py           # Grid position generator (legacy format)
├── generate_positions.py      # Grid position generator (simple)
├── migrate_grid.py            # Migrate holdings to new grid
├── generate_wallet.py         # Generate new trading wallet
├── data/                      # Data directory
│   ├── positions.json         # Position state file
│   ├── dashboard_trades.json  # Latest 50 successful trades for dashboard display
│   ├── dashboard_events.json  # Latest 50 structured operational events
│   └── profit_totals.json     # Realized totals, baseline, and transaction hashes
└── logs/                      # Log files (created at runtime)
```

### Module Descriptions

**config.py**: Loads and validates environment variables, provides chain-specific defaults

**wallet.py**: Handles Web3 connections, token approvals, transaction signing, balance checks

**zero_x.py**: Integrates with 0x API for price quotes and swap transactions

**li_fi.py / uniswap_api.py / sushi_api.py**: Alternative swap-provider integrations selected through `.env`. Sushi v7 uses exact-input routing, handles its insufficient-allowance response as a normal approve-and-refresh handshake, and applies per-bot rate-limit cooldowns without blocking the main loop.

**swap_provider.py**: Provider registry, factory, and capability adapter. It centralizes provider selection and differences such as taker-required pricing, quote refresh after approval, API-managed approvals, and separate swap-calldata preparation. `grid_bot.py` consumes these capabilities instead of checking provider classes directly.

### Swap provider selection

Prefer the explicit provider setting:

```dotenv
SWAP_PROVIDER=sushiswap  # 0x, lifi, uniswap, or sushiswap
```

The older `USE_UNISWAP_API` and `USE_LI_FI` flags remain backward compatible when `SWAP_PROVIDER` is empty. Explicit `SWAP_PROVIDER` takes precedence. Sushi currently supports exact-input swaps, which is the only execution mode used by this bot. Each provider keeps its own quote, approval, slippage, and transaction behavior behind the common capability layer.

For a Sushi bot, set:

```dotenv
SWAP_PROVIDER=sushiswap
SUSHI_API_KEY=your_key_here  # Optional, but recommended for a multi-bot rollout
```

Restart after changing `.env`. Roll out one or two bots first and confirm price
updates plus a small buy/sell before moving more of the fleet. Each process keeps
its own rate-limit state. On HTTP 429 it honors Sushi's `Retry-After` header or
uses a jittered exponential cooldown from 30 seconds to 15 minutes; calls are
skipped locally until the cooldown expires, and the first successful response
resets it. This prevents bots sharing one public IP from synchronizing retries.

**grid_bot.py**: Main trading logic - grid management, buy/sell decisions, profit tracking

**dashboard_reporter.py**: Bounded background queue for dashboard POSTs. Dashboard failures never propagate into the trading loop.

**profit_tracker.py**: Atomic integer-wei realized-profit accounting for confirmed sells. It includes gains and realized losses, deduplicates by transaction hash, and supports non-destructive displayed baselines.

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
python -m unittest discover -v

# Tests cover, among other behavior:
# - P&L calculations
# - Buy/sell decision logic
# - Stoploss triggers
# - Leading edge buys
# - Position validation
# - Provider selection and Sushi response mapping
# - Sushi rate-limit cooldown, skipped calls, and recovery
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

### Dashboard reporting

Enable reporting with the dashboard server's full ingestion endpoint and shared key:

```dotenv
DASHBOARD_URL=https://doomdash.ca/api/status
DASHBOARD_API_KEY=the-dashboard-server-API_KEY
BOT_ID=MERD
# Optional:
DASHBOARD_NAME=MERD Main
DASHBOARD_GROUP=Robinhood Farm
```

Restart the bot after changing `.env`. Reporting runs in a daemon thread with a bounded queue and a five-second HTTP timeout, so dashboard downtime does not block trading. Successful requests are logged only at `DEBUG`; failures remain warnings.

Each status payload includes schema version, chain/token/public-wallet metadata, ETH, USDG, and trading-token balances, positions, AVG P&L, session and persistent realized profit, buy/sell counts, capacity, the current round's optional `sell_attempt`, and up to 50 trades. It also includes `treasury_sent_usdg`: the all-time total of successful USDG sweep receipts in `data/treasury_transfers.json`. Dry runs, refused commands, failed broadcasts, and sweeps of other ERC-20s are excluded. Sell checks run before the status report so a blocked attempt is visible in the same round rather than one report late. The USDG balance is a read-only ERC-20 call made once per cycle when `USDG_ADDRESS` is configured; a failed read is omitted and never interrupts trading. Only the public wallet address is sent—never the private key.

Experimental builds also include a versioned `sigil` descriptor created once per process incarnation. One of exactly 23 curated positive, present-tense intentions in `sigil_intentions.json` is selected from cryptographic startup entropy, reduced to unique consonants, and bound with the bot ID and incarnation nonce into a SHA-256 visual seed. Only `{version, method, key, seed}` is reported; the readable intention and nonce are discarded. The dashboard can therefore render the symbol deterministically without an image service, while every restart produces a new working. A missing or malformed grimoire falls back to one built-in intention because dashboard ornamentation must never prevent trading from starting.

Successful trades are appended atomically to `data/dashboard_trades.json`, reloaded after restart, and capped at 50. This uses transaction results the bot already has and makes no extra RPC or third-party API calls. Existing on-chain history predating this feature is not reconstructed.

Operational Events are retained locally in `data/dashboard_events.json` and included in the existing dashboard status payload. The history is capped at 50 entries, consecutive identical events are count-badged instead of duplicated, and messages are truncated and redacted before reporting. Durable operational outcomes belong here; routine polling decisions are omitted. A successfully confirmed USDG banking swap records a green `usdg_banked` success Event with the source amount, USDG amount, and public transaction hash. Events carrying different transaction hashes are never collapsed together.

A sell target whose quoted profit is below the ETH minimum derived from the position cost and `MIN_PROFIT_PERCENT` is deliberately **not** stored as an Event. During that round, the bot reports `sell_attempt.status: "quote_below_minimum"` with the position ID, P&L, quoted profit, and minimum profit. `_sell_attempt` is cleared at the beginning of every cycle and must be re-established by that cycle's sell check, so the dashboard indication disappears on the next report where the condition no longer occurs. This is live attempt state, not historical warning state.

Confirmed sells update `data/profit_totals.json` atomically. Profit is stored as integer wei, includes both realized gains and realized stop-losses, and is deduplicated by transaction hash. It intentionally excludes unrealized P&L, gas, and trades completed before tracking began. Session profit still resets on restart; realized profit survives restarts. To begin a new displayed accounting period without deleting the all-time ledger, stop the bot and run `python grid_bot.py --reset-profit-baseline` once.

Common failures:

- `401`: `DASHBOARD_API_KEY` does not match the server `API_KEY`.
- Dashboard POST `429`: the dashboard's per-IP fleet rate limit is too low for the number/report interval of bots.
- `Sushi rate limited`: the provider returned HTTP 429. The bot automatically pauses Sushi calls, reports the warning as an Event, and resumes after `Retry-After` or its exponential cooldown. Do not restart repeatedly; that discards the in-memory cooldown.
- Bot absent: confirm `DASHBOARD_URL` ends with `/api/status`, restart the bot, and test connectivity from the bot host.

### "Failed to connect to RPC"
- Verify RPC URL in .env file
- Check network connectivity
- Try alternative RPC endpoint (Alchemy, QuickNode, etc.)

### "Insufficient allowance"
- The bot will auto-approve tokens on first use
- Check wallet has ETH for gas fees
- Verify token contract addresses are correct

### "Quote failed"
- Confirm `SWAP_PROVIDER` resolved to the intended provider in startup logs or run `python grid_bot.py --check-config`
- Check the selected provider's API key when it requires or recommends one
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
# Full unit suite
python -m unittest -v

# Read-only live configuration/RPC/wallet/token/dashboard check
python grid_bot.py --check-config
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

### Treasury sweeps

The guarded treasury CLI transfers banked USDG, a specified ERC-20, or an exact
native ETH amount from a bot wallet without exporting its private key to a
separate sweep script. It is intended for moving funds from an individual bot
to a known central treasury and is not part of the normal trading loop.

#### Before broadcasting

1. Stop the bot that owns the wallet and confirm its process is no longer
   running. A trading transaction and a sweep from the same wallet can race for
   the same account nonce.
2. Verify the destination address out of band. Do not paste an address from an
   untrusted chat message or browser prompt.
3. Keep enough native ETH in the bot wallet for gas. ERC-20 sweeps deliberately
   leave it untouched; native ETH transfers enforce the configured
   `ETH_GAS_RESERVE` after the amount and estimated maximum fee.
4. Run the dry run, review its wallet, token contract, balance, amount, and
   recipient, then run the identical command with the two execution guards.

Every invocation prints a transfer plan. It stays a dry run unless `--execute`
is present; a broadcast additionally requires `--confirm-bot-stopped`.

Use a shell variable containing the *actual checksummed central-wallet address*
so the same reviewed value is used in both commands:

```bash
TREASURY=0xYourActualCentralWalletAddress

# Sweep all USDG: dry run first
python grid_bot.py --sweep-usdg "$TREASURY"

# Broadcast only after the plan is correct and the bot is stopped
python grid_bot.py --sweep-usdg "$TREASURY" --execute --confirm-bot-stopped
```

Set `TREASURY_ALLOWED_RECIPIENTS` to a comma-separated list of central wallet
addresses. A recipient outside that list is allowed only when it is repeated
verbatim with `--confirm-recipient`; this prevents a typo or stale batch target
from silently becoming a transfer destination.

```bash
# .env: use real addresses, with no quotes required
TREASURY_ALLOWED_RECIPIENTS=0xCentralWalletAddress,0xBackupTreasuryAddress

# A one-off recipient needs an exact second acknowledgement.
RECIPIENT=0xOneOffRecipientAddress
python grid_bot.py --transfer-token USDG --recipient "$RECIPIENT" \
  --amount 25.50 --confirm-recipient "$RECIPIENT" \
  --execute --confirm-bot-stopped
```

`--sweep-usdg RECIPIENT` is shorthand for `--transfer-token USDG --recipient
RECIPIENT`. For another ERC-20, pass its contract address to `--transfer-token`.
`--amount all` (the default) sends the entire token balance; a positive decimal
sends that token amount, subject to its on-chain decimals and available balance.
The command refuses a self-transfer, malformed recipient, unsupported token
identifier, nonpositive amount, amount above balance, empty balance, or a
non-allowlisted recipient that was not repeated exactly.

For an exact native ETH transfer, use `--transfer-eth`:

```bash
python grid_bot.py \
  --transfer-eth \
  --recipient "$TREASURY" \
  --amount 0.0005

# Broadcast only after reviewing the balance, maximum gas, reserve, and
# minimum-remaining figures, and after stopping the bot.
python grid_bot.py \
  --transfer-eth \
  --recipient "$TREASURY" \
  --amount 0.0005 \
  --execute \
  --confirm-bot-stopped
```

Native transfers use current RPC gas estimates with the configured gas limit
and gas price multipliers. They are refused unless the wallet can send the
exact amount, cover the estimated maximum transaction fee, and still retain
`ETH_GAS_RESERVE`. Successful and failed broadcasts use the same local
`data/treasury_transfers.json` audit trail as ERC-20 transfers.

Native `--amount all` is an explicit liquidation mode. It requires
`--confirm-liquidate` even for planning, bypasses `ETH_GAS_RESERVE`, and sends
the current balance minus the buffered maximum fee calculated for the
transaction. It refuses contract recipients because their receive logic can
change the required gas:

```bash
# Dry-run liquidation plan
python grid_bot.py \
  --transfer-eth \
  --recipient "$TREASURY" \
  --amount all \
  --confirm-liquidate

# Broadcast after review and after stopping the bot
python grid_bot.py \
  --transfer-eth \
  --recipient "$TREASURY" \
  --amount all \
  --confirm-liquidate \
  --execute \
  --confirm-bot-stopped
```

Liquidation can leave the wallet with no usable ETH. A failed transaction may
still consume gas, and changing gas conditions can make a precomputed
whole-balance transaction fail. Review the freshly printed execution plan and
receipt; never retry a fleet liquidation blindly.

To convert this bot's configured trading-token, USDG, and WETH balances into
native ETH in the same wallet, use the separate managed-asset command. It does
not discover or touch unrelated tokens:

```bash
# Read-only balances and quote plan
python grid_bot.py --liquidate-assets --confirm-liquidate-assets

# Same plan, but preserve the configured USDG balance
python grid_bot.py --liquidate-assets --confirm-liquidate-assets --keep-usdg

# Broadcast only after review and after stopping this bot
python grid_bot.py \
  --liquidate-assets \
  --confirm-liquidate-assets \
  --execute \
  --confirm-bot-stopped
```

The full trading-token and USDG balances are swapped without profit, banking,
or moonbag rules; WETH is unwrapped directly. All managed-token balances must
verify at exactly zero after confirmed receipts. Only then are timestamped
backups created and both `data/positions.json` and
`data/gridless_positions.json` atomically cleared. Any partial failure leaves
position data intact. Execution events are durable in
`data/asset_liquidations.json`. See the fleet guide for the guarded batch form.
Add `--keep-usdg` to both the reviewed plan and execution command when USDG
must remain untouched; excluded USDG is not quoted, approved, swapped, or
included in final zero-balance verification.

#### Fleet batch sweep

For fleets configured through `ops/fleet/fleet.conf`, prefer the aligned
`ops/fleet/usdg-sweep` command documented in the
[tmux fleet operations guide](ops/fleet/README.md). It uses the same explicit
bot list as start/stop/restart/update, refuses broadcast while the configured
tmux session is running, and remains dry-run by default.

Use `ops/fleet/treasury-transfer` for native ETH or another ERC-20 across the
same explicit fleet, and `ops/fleet/liquidate-assets` for verified managed-asset
conversion plus position cleanup. The fleet guide also documents `ops/fleet/update-variable`
for previewed, backed-up, atomic `.env` changes such as
`ETH_GAS_RESERVE=0.0005`.

`scripts/sweep_fleet_usdg.sh` runs the USDG sweep command in every checkout
under a fleet root (each checkout is identified by a `grid_bot.py` file). It
uses each bot's `.venv/bin/python` when present, otherwise `python3`, and runs
from the bot directory so its own `.env` and receipt log are used. It never
stops bot processes itself.

Run the dry run first:

```bash
cd /path/to/robinhood-grid-bot-py
scripts/sweep_fleet_usdg.sh \
  --fleet-root "$HOME/bots" \
  --recipient 0xYourActualCentralWalletAddress
```

After reviewing every plan and stopping every bot, broadcast with the explicit
acknowledgement. The recipient must be allowed by each bot's
`TREASURY_ALLOWED_RECIPIENTS` setting (or that bot will refuse it):

```bash
scripts/sweep_fleet_usdg.sh \
  --fleet-root "$HOME/bots" \
  --recipient 0xYourActualCentralWalletAddress \
  --execute --confirm-fleet-stopped
```

The script continues through the fleet and exits nonzero if any bot command
fails, so its final summary identifies whether the batch needs attention.

The command waits for the transfer result. Successful and failed broadcast
results are appended, relative to the command's working directory, to
`data/treasury_transfers.json`, including the timestamp, wallet, token,
recipient, amount, status, and public transaction hash/error. Preserve that
file with the bot's operational records. A refused preflight and a dry run do
not write a receipt because no transaction was submitted.

On a successful sweep, the displayed transaction hash is a terminal hyperlink
to the configured chain's explorer (Robinhood Chain/Blockscout, BaseScan, or
Etherscan). The URL stays hidden so fleet output displays only the hash;
terminals without hyperlink support simply show the same readable hash.

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

### v1.3.0 - Latest
- Sushi v7 quote/swap compatibility with RouteProcessor approval refresh
- Fleet-safe Sushi HTTP 429 handling with `Retry-After`, jittered exponential cooldown, and automatic recovery
- Explicit Uniswap/LI.FI/0x provider abstraction with legacy compatibility
- Structured persistent dashboard Events and static position-capacity warnings
- Round-scoped dashboard indication for sell quotes below the minimum profit
- Persistent confirmed-sell realized profit with transaction deduplication and baseline resets
- Persistent Trade History plus independent Event/Trade reset commands
- Read-only `--check-config` diagnostics
- Per-cycle read-only USDG balance reporting
- Normalized 50-variable environment templates and shared operational defaults

### v1.2.0
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
