# Drawdown Ladder Allocation

The drawdown ladder is an opt-in entry allocator for gridless mode. It freezes
a bounded principal budget when armed and distributes that budget across price
levels down to a configured terminal drawdown. Its purpose is to preserve buy
capacity through continued downside instead of spending the available balance
near one P&L threshold.

It is an allocation mechanism, not an asset-selection or risk-management
thesis. Existing sell thresholds, stop loss, moonbag handling, profit banking,
route selection, gas caps, slippage, transfer-tax handling, receipt
reconciliation, and circuit-breaker behavior remain authoritative.

## Enabling it

The legacy threshold allocator remains the default. A conservative paper or
isolated-wallet profile is:

```dotenv
USE_GRIDLESS=true
GRIDLESS_ALLOCATION_MODE=drawdown_ladder
MAX_ACTIVE_POSITIONS=50
TRADEABLE_BALANCE_PERCENT=50
GRIDLESS_MIN_POSITION_ETH=0.001
GRIDLESS_LADDER_TERMINAL_DRAWDOWN_PERCENT=90
GRIDLESS_LADDER_SPACING=log
GRIDLESS_LADDER_MAX_BUDGET_ETH=0
GRIDLESS_LADDER_EXPIRY_SECONDS=2592000
GRIDLESS_LADDER_REARM_POLICY=after_exit
GRIDLESS_LADDER_REARM_COOLDOWN_SECONDS=3600
GRIDLESS_LADDER_INCLUDE_REFERENCE_ENTRY=false
```

Do not switch an existing threshold ledger into ladder mode. The ladder refuses
to adopt open positions that lack ladder provenance. Start with an empty
`data/gridless_positions.json`, or close/migrate the positions deliberately.
Likewise, threshold mode refuses to trade while a persisted ladder file exists.
The position ledger and ladder state must be archived or restored as a pair.

## Budget and position count

At arm time, the bot reads the spendable trading balance. In native-ETH mode,
the existing `ETH_GAS_RESERVE` is removed first. The frozen principal is:

```text
uncapped_budget = spendable_balance × TRADEABLE_BALANCE_PERCENT / 100
budget = min(uncapped_budget, GRIDLESS_LADDER_MAX_BUDGET_ETH)
```

The second line applies only when the maximum budget is greater than zero.
Position count is:

```text
N = min(MAX_ACTIVE_POSITIONS, floor(budget / GRIDLESS_MIN_POSITION_ETH))
```

If `N` is zero, no ladder is armed. The budget is divided evenly across the
`N` levels; indivisible wei remainder is assigned to the deepest level. The
budget, count, reference, and levels are frozen. Later wallet deposits,
withdrawals, sales, or price changes do not resize or move an active ladder.

Example: 0.100 ETH spendable, 50% tradeable, 0.001 ETH minimum, and 50 maximum
positions creates 50 entries of 0.001 ETH across a 0.050 ETH frozen budget.

The reserve is logical rather than an on-chain escrow. If another process or
manual transaction spends the wallet balance, a crossed rung remains pending
until its exact principal is affordable again; the bot does not shrink it.

## Reference and level geometry

The reference is the observed token price when the ladder first arms. Let:

- `R` be reference price;
- `D` be terminal drawdown as a fraction, such as `0.90`;
- `N` be level count;
- `i` be a one-based level index.

With reference entry disabled (the default), linear spacing is:

```text
price[i] = R × (1 - D × i/N)
```

This spends capital steadily in absolute drawdown space. With 50 levels ending
at -90%, the levels are 1.8 percentage points apart.

Log spacing is:

```text
price[i] = R × (1 - D)^(i/N)
```

This makes adjacent price ratios equal. It deploys less capital during shallow
drawdowns and reserves more entries for deep declines. Both modes use the same
budget, count, and final price.

When `GRIDLESS_LADDER_INCLUDE_REFERENCE_ENTRY=true` and more than one position
exists, level zero is the reference and the last level remains the terminal
price. A one-position ladder always targets the terminal price; otherwise it
would not stretch capital downward at all.

## Polling and execution behavior

This bot executes swaps; it does not place resting exchange limit orders. Each
poll compares the observed price with the next unfilled level. If crossed, at
most one rung is attempted per cycle. Existing buy cooldowns continue to apply,
so a gap through multiple levels cannot burst-submit many transactions.

The rung advances only after all of the following succeed:

1. An exact-input route is quoted and remains within the configured buy
   execution recovery margin.
2. Existing route, slippage, tax, allowance, gas-cap, and gas-reserve checks
   pass.
3. The transaction confirms and the token balance increase is measured.
4. The position is atomically persisted with ladder ID, level index, and exact
   planned principal.
5. Ladder progress is atomically checkpointed.

A failed quote, rejected route, unaffordable rung, failed transaction, or
unreconciled receipt does not consume a level. Route price improvement is
allowed. A route whose effective execution price has recovered too far above
the crossed rung is rejected according to `GRIDLESS_BUY_EXECUTION_MARGIN`.

## Lifecycle

The persisted states are:

- `active`: one or more levels can still fill;
- `terminal`: every planned level filled; averaging down stops;
- `expired`: the lifetime elapsed; remaining levels are disabled.
- `closed`: every currently held ladder position exited; unfilled levels from
  that completed trade cycle are retired.

Exits do not rewind or recycle levels. Each confirmed sell records the latest
exit time. When the final open position exits, the cycle becomes `closed` so a
fall during the cooldown cannot refill an old rung. With `after_exit`, a new
ladder can arm only after every ladder position has exited and the re-arm
cooldown has elapsed. A terminal or expired plan becomes `closed` only when its
final held position has a confirmed exit. An expired ladder that never filled
cannot silently re-anchor to a lower market because it has no confirmed exit.
With `never`, replacement is always an operator decision.

## Persistence and crash recovery

Plans live at `data/gridless_ladder.json`. Position provenance lives in
`data/gridless_positions.json`. Both use atomic replacement writes.

There is an unavoidable cross-file boundary after a confirmed buy: the
position ledger is written before ladder progress. If the process stops in that
window, restart validation recognizes the uniquely proven next-level position
and advances the ladder once. Duplicate levels, foreign ladder IDs, missing
principal, unsupported state versions, chain/token mismatch, oversized plans,
or malformed accounting fail closed.

If the buy confirms but its position cannot be reconciled, the existing
unresolved-settlement safety mechanism halts trading. If the position is saved
but ladder checkpointing fails, trading halts and restart recovery uses the
position provenance. The system never intentionally advances a rung on an
unconfirmed buy.

## Dashboard telemetry

Status payloads add:

- `entry_allocation_mode`: `threshold` or `drawdown_ladder`;
- `drawdown_ladder.id` and `status`;
- spacing, reference, terminal drawdown, total/filled levels;
- next level price;
- frozen budget, spent principal, logical remaining reserve, and expiry.

Open-position P&L remains visible for exit decisions. In ladder mode, legacy
buy-threshold P&L is not advertised as entry-trigger authority.

## Operational safeguards

- Keep `GRIDLESS_LADDER_MAX_BUDGET_ETH` or
  `TRADEABLE_BALANCE_PERCENT` conservative during evaluation.
- `MAX_ACTIVE_POSITIONS` is both a risk cap and a ladder-density cap.
- The gas reserve is never included in the native-ETH snapshot.
- No leverage or borrowing is introduced.
- No buy happens below the configured minimum principal.
- No buy occurs after terminal or expiry.
- Configuration and persisted-state mismatches stop entry rather than guessing.
- Use separate wallets/state directories for simultaneous comparison runs.
- Back up both ladder and position files before changing allocation mode.

## Suggested evaluation

Run linear and log profiles against identical signal histories and compare:

- capital deployed at -10%, -30%, -50%, -70%, and -90%;
- weighted cost basis and rebound required to break even;
- gas as a percentage of each micro-position;
- unfilled capital at recovery;
- maximum drawdown and time to exit;
- failure behavior during gaps, provider outages, and restarts.

Do not judge the strategy only on survivors. Include assets that never recover,
lose liquidity, or become unsellable. A beautifully spaced descent into zero is
still a descent into zero, darling.
