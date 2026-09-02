# Fleet operator runbook

Financial commands preview by default. Stop the fleet before moving funds, and
never add `--execute` or `--apply` until the preview names the intended wallets.

## Current baseline

The September 2026 recovery baseline is gridless `-15% / +10%`, with a 5%
minimum realized profit after gas-inclusive position cost and projected
sell/setup gas. Position size is dynamic: 90% of unreserved wallet liquidity is
divided among available slots. Newly funded slots target about `0.003 ETH`.

```dotenv
GRIDLESS_BUY_THRESHOLD=-15
GRIDLESS_SELL_THRESHOLD=10
MIN_PROFIT_PERCENT=5
ETH_GAS_RESERVE=0.0006
MAX_SWAP_GAS_ETH=0.00015
MAX_BUY_GAS_ETH=0.00015
MAX_SELL_GAS_ETH=0.00015
MAX_FEE_TRANSFER_GAS_ETH=0.00002
BANK_PERCENTAGE=0
PROFIT_FEE_PERCENT=0
MIN_PROFIT_FEE_TRANSFER_ETH=0.0001
```

`MAX_SWAP_GAS_ETH` is the backward-compatible default; blank operation-specific
caps inherit it. The buy cap prevents uneconomic new inventory. The sell cap is
an independent ceiling and never overrides the minimum-profit check. The fee
cap covers a separate post-sale transfer. `ETH_GAS_RESERVE` is excluded from
position sizing and reserve-preserving sweeps.

## Update and verify

```bash
cd ~/bot-farm/fleet-command/robinhood-grid-bot-py
ops/fleet/update-this-checkout
ops/fleet/update-fleet
ops/fleet/fleet-doctor
```

## Freeze, consolidate, and redistribute

```bash
ops/fleet/stop-fleet
ops/fleet/adjust-positions --set-to-filled --all
ops/fleet/adjust-positions --set-to-filled --all --apply
ops/fleet/treasury-transfer --asset ETH --amount available
ops/fleet/treasury-transfer --asset ETH --amount available \
  --execute --confirm-fleet-stopped
```

The freeze preserves filled positions while preventing new buys. The sweep
retains each bot's configured reserve and live estimated transfer gas.

Top up selected wallets to a target total balance; existing ETH is credited:

```bash
ops/fleet/fund-bots --only bow,hookr,earn \
  --from-env ~/bot-farm/treasury.env --target-balance 0.007267
ops/fleet/fund-bots --only bow,hookr,earn \
  --from-env ~/bot-farm/treasury.env --target-balance 0.007267 \
  --confirm-source 0xExactTreasuryAddress \
  --execute --confirm-fleet-stopped
```

`0.007267 ETH` is approximately two new `0.003 ETH` slots plus the `0.0006 ETH`
reserve. Restore only funded capacity:

```bash
ops/fleet/adjust-positions bow,hookr,earn 2
ops/fleet/adjust-positions --apply bow,hookr,earn 2
ops/fleet/start-fleet --detach
tmux attach-session -t bot_farm
```

Expected safeguards include gas-cap rejections, gas-aware sell-profit checks,
one retry for Uniswap's packet 409 followed by Sushi fallback, and a shared
cooldown only for genuine 429 rate limits. Once a hash exists, never manually
repeat a transaction without checking its receipt and local audit history.
