# Robinhood Chain Grid Trading Bot (Python)

A sophisticated grid trading bot for Robinhood Chain and other EVM networks, implemented in Python using web3.py. Uses the 0x Protocol for optimal DEX aggregation and supports dynamic position sizing with profit banking to USDG.

## Features

- **Dynamic Grid Trading**: Automatically places buy orders at decreasing price levels
- **Cost Basis Tracking**: Each position tracks its own cost basis for accurate profit calculations
- **Profit Banking**: Automatically banks profits to USDG (stablecoin)
- **0x Protocol Integration**: Best price execution across multiple DEXs
- **Anti-MEV Protection**: Jitter on quotes to protect against front-running
- **Permit2 Approvals**: Efficient token approvals using Uniswap's Permit2
- **Persistent State**: Survives restarts with position recovery
- **Multi-Chain Support**: Robinhood Chain (4663), Base (8453), Ethereum Mainnet (1)

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

## Installation

### Prerequisites

- Python 3.9+
- pip
- A wallet with ETH/WETH for trading
- 0x API key (free at [0x.org](https://0x.org/))

### Setup

1. Clone the repository:
```bash
git clone https://github.com/kabbalahmonster/robinhood-grid-bot-py.git
cd robinhood-grid-bot-py
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment:
```bash
# Copy the appropriate environment file for your chain
cp .env.robinhood .env

# Edit .env with your settings
nano .env
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PRIVATE_KEY` | Yes | - | Wallet private key (with 0x prefix) |
| `RPC_URL` | Yes | - | RPC endpoint URL |
| `CHAIN_ID` | Yes | 4663 | Chain ID (4663/8453/1) |
| `ZEROX_API_KEY` | Yes | - | 0x API key |
| `TOKEN_ADDRESS` | Yes | - | Token address to trade |
| `TOKEN_SYMBOL` | No | TOKEN | Token symbol for logging |
| `USDG_ADDRESS` | Yes | - | USDG/stablecoin address for profit banking |
| `GRID_SPACING_PERCENT` | No | 5.0 | Grid spacing percentage |
| `MAX_POSITIONS` | No | 20 | Maximum number of grid positions |
| `MIN_PROFIT_PERCENT` | No | 1.5 | Minimum profit % before selling |
| `INITIAL_BUY_AMOUNT` | No | 0.01 | Initial WETH buy amount |
| `SLIPPAGE_TOLERANCE` | No | 1.0 | Slippage tolerance % |
| `BANK_PERCENTAGE` | No | 50.0 | % of profits to bank to USDG |
| `POLL_INTERVAL_SECONDS` | No | 30 | Price check interval |
| `ANTI_MEV_JITTER` | No | true | Enable anti-MEV quote jitter |
| `LOG_LEVEL` | No | INFO | Logging level |
| `STATE_FILE` | No | ./data/positions.json | State file path |

### Chain-Specific Configuration

#### Robinhood Chain (4663)
```bash
CHAIN_ID=4663
RPC_URL=https://robinhood.robinhoodchain.com
WETH_ADDRESS=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73
MAX_POSITIONS=20
```

#### Base (8453)
```bash
CHAIN_ID=8453
RPC_URL=https://mainnet.base.org
WETH_ADDRESS=0x4200000000000000000000000000000000000006
MAX_POSITIONS=10
```

#### Ethereum Mainnet (1)
```bash
CHAIN_ID=1
RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
WETH_ADDRESS=0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2
MAX_POSITIONS=10
```

## Usage

### Start the Bot

```bash
# Using default .env file
python bot.py

# Using specific environment file
python bot.py --env .env.robinhood

# Run single iteration (for testing)
python bot.py --once
```

### Example Session

```
2024-01-15 10:30:00 | INFO     | grid_bot | Initializing Grid Bot for Robinhood
2024-01-15 10:30:00 | INFO     | grid_bot | Trading TOKEN against WETH
2024-01-15 10:30:01 | INFO     | grid_bot.wallet | Wallet initialized: 0x1234...5678
2024-01-15 10:30:02 | INFO     | grid_bot | Token: TOKEN (18 decimals)
2024-01-15 10:30:02 | INFO     | grid_bot | WETH Balance: 1.500000
2024-01-15 10:30:02 | INFO     | grid_bot | Initialized 20 grid levels
2024-01-15 10:30:02 | INFO     | grid_bot | Price range: 0.000100 - 0.000400 ETH
2024-01-15 10:30:02 | INFO     | grid_bot | Initialization complete
2024-01-15 10:30:02 | INFO     | grid_bot | Starting Grid Bot main loop
2024-01-15 10:30:35 | INFO     | grid_bot | Executing BUY at 0.000380 ETH with 0.075000 WETH
2024-01-15 10:31:12 | INFO     | grid_bot | BUY successful: 197.368421 tokens for 0.075000 WETH
2024-01-15 10:31:45 | INFO     | grid_bot | Executing BUY at 0.000361 ETH with 0.075000 WETH
...
2024-01-15 14:22:10 | INFO     | grid_bot | Executing SELL for position 1 (bought at 0.000380)
2024-01-15 14:22:45 | INFO     | grid_bot | SELL successful: Position 1 closed for 0.077500 WETH (profit: 0.002500 WETH, 3.33%)
2024-01-15 14:22:46 | INFO     | grid_bot | Banking 0.001250 WETH to USDG
```

## How It Works

### Grid Strategy

1. **Grid Initialization**: Creates price levels below current market price
   - Default spacing: 5% between levels
   - Example levels at $100: $95, $90.25, $85.74, ...

2. **Buy Execution**:
   - Monitors price for grid level triggers
   - Calculates dynamic buy amount (WETH balance / empty positions)
   - Executes swap via 0x Protocol
   - Records position with cost basis

3. **Sell Execution**:
   - Monitors positions for profit targets
   - Sells when profit ≥ `MIN_PROFIT_PERCENT`
   - Banks portion of profit to USDG
   - Frees up grid level for rebuy

4. **Profit Banking**:
   - Configurable percentage of profits
   - Automatic swap to USDG/stablecoin
   - Preserves capital in stable asset

### Position Tracking

```python
Position {
    id: 1,
    buy_price: 0.000380,      # Price when bought
    buy_amount_token: 197.37,  # Tokens acquired
    buy_amount_eth: 0.075,     # WETH spent
    status: "open",            # open/closed/banking
    cost_basis: 0.000380       # ETH per token
}
```

### Dynamic Sizing

Buy amounts are calculated dynamically:
```python
buy_amount = available_weth / empty_positions
```

This ensures:
- Even distribution across grid levels
- Automatic adjustment as positions fill
- No manual rebalancing needed

## File Structure

```
robinhood-grid-bot-py/
├── README.md              # This file
├── requirements.txt       # Python dependencies
├── .env.example          # Example environment variables
├── .env.robinhood        # Robinhood Chain config
├── .env.base             # Base Chain config
├── .env.mainnet          # Ethereum Mainnet config
├── config.py             # Configuration management
├── bot.py                # Main bot logic
├── wallet.py             # Wallet & transaction handling
├── zero_x.py             # 0x API integration
├── storage.py            # Position persistence
├── utils.py              # Utility functions
└── logs/                 # Log files
```

## API Reference

### BotConfig

Configuration dataclass loaded from environment.

```python
config = load_config(".env")
print(config.chain_name)  # "Robinhood"
print(config.max_positions)  # 20
```

### Wallet

```python
wallet = Wallet(config)

# Get balances
eth = wallet.get_eth_balance()
weth = wallet.get_token_balance(config.weth_address)

# Ensure approvals
wallet.ensure_approval(token, spender, amount)

# Send transaction
result = wallet._send_transaction(tx_params)
```

### ZeroXClient

```python
client = ZeroXClient(config)

# Get quote
quote = client.get_quote(
    sell_token=weth,
    buy_token=token,
    sell_amount=wei_amount,
)

# Build swap
tx_params = client.get_swap_transaction_params(
    quote, from_address, nonce, gas_params
)
```

### Storage

```python
storage = Storage("./data/positions.json")

# Save position
storage.add_position(position)

# Get open positions
open_pos = storage.get_open_positions()

# Get stats
stats = storage.get_stats()
```

## Safety Features

1. **Profit Thresholds**: Only sells when profit > `MIN_PROFIT_PERCENT`
2. **Slippage Protection**: Configurable slippage tolerance
3. **Gas Estimation**: 20% buffer on gas estimates
4. **Atomic State Saves**: Prevents data corruption
5. **Backup Files**: Automatic backup before state updates
6. **Approval Checks**: Verifies token approvals before trading

## Troubleshooting

### "Failed to connect to RPC"
- Verify RPC URL in .env file
- Check network connectivity
- Try alternative RPC endpoint

### "Insufficient allowance"
- The bot will auto-approve tokens
- Check wallet has ETH for gas
- Verify token contract addresses

### "Quote failed"
- Check 0x API key is valid
- Verify token has liquidity
- Increase slippage tolerance if volatile

### "Transaction failed"
- Check gas prices (may be too low)
- Verify sufficient ETH for gas
- Check token approvals

## Development

### Running Tests

```bash
# Test imports
python -c "from bot import GridBot; print('OK')"

# Test configuration
python -c "from config import load_config; c = load_config('.env'); print(c.chain_name)"

# Single iteration
python bot.py --env .env --once
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

2. Create `.env.newchain` file
3. Update documentation

## Security Considerations

⚠️ **Important**:
- Never commit `.env` files with private keys
- Use dedicated trading wallets
- Start with small amounts
- Monitor gas costs on mainnet
- Review and understand the code before running

## License

MIT License - See LICENSE file

## Support

For issues and feature requests, please open a GitHub issue.

## Acknowledgments

- 0x Protocol for DEX aggregation
- Uniswap for Permit2
- web3.py team for Ethereum integration