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
TREASURY_POSITION_RESERVE_ETH=0.003
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

`stop-fleet` records durable stopped intent before terminating tmux, so the
guardian service can remain enabled during maintenance without resurrecting
the fleet. `start-fleet --detach` or `restart-fleet --detach` records running
intent only after the complete session starts successfully.

The freeze preserves filled positions while preventing new buys. The sweep
retains each bot's `ETH_GAS_RESERVE`, live estimated transfer gas, and
`TREASURY_POSITION_RESERVE_ETH` multiplied by its open-position count. Use
`--position-reserve-eth ETH` to override the per-position reserve for one run.

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
one retry for Uniswap's packet 409 followed by Sushi fallback, quote-provider
handoff confirmation and disagreement blocking, and a shared cooldown only for
genuine 429 rate limits. DoomDash exposes both active sell checks and buys
blocked by projected gas, including the actual quote source. Once a hash exists,
never manually repeat a transaction without checking its receipt and local
audit history.

## Tournament canary

Keep the fleet on `ROUTE_TOURNAMENT_MODE=off` by default. For a monitored
single-bot trial, preview and apply both guarded values together, then restart
only that bot:

```bash
update-variable --allow-add --only earn \
  ROUTE_TOURNAMENT_MODE=gate ROUTE_TOURNAMENT_CANARY=true
update-variable --apply --allow-add --only earn \
  ROUTE_TOURNAMENT_MODE=gate ROUTE_TOURNAMENT_CANARY=true
restart-bot EARN
```

`shadow` collects read-only comparison telemetry; `gate` gives route authority
only to a freshly re-quoted and locally simulated winner. A displayed winner
may still be safely skipped during revalidation. Watch latency, 429s, timeout
rejections, gas, and successful buy/sell receipts before expanding the trial.
Rollback is `ROUTE_TOURNAMENT_MODE=off` and
`ROUTE_TOURNAMENT_CANARY=false` on that same bot. See the route-tournament
section of the main README and the fleet README for accounting and timeout
details.
