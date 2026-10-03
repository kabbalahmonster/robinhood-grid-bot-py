# Adaptive Drawdown Allocation

The drawdown allocator is an opt-in gridless entry engine. It maintains a
stable field of reusable percentage-drawdown triggers below one reference
price. Capital can enter at any time: the engine first adds trigger density,
then raises rung size after maximum coverage is funded. A confirmed sell frees
its rung to trade again after price resets above that trigger.

It changes entry allocation only. Existing independent-position profit
thresholds, stop loss, moonbag handling, profit banking, gas reserves, route
selection, slippage, transfer-tax handling, receipt reconciliation, and circuit
breakers remain authoritative.

`GRIDLESS_ALLOCATION_MODE=drawdown_ladder` retains the original frozen
reference. `GRIDLESS_ALLOCATION_MODE=survivor` is the live variant. Its
reference is always the highest currently open measured purchase point, so a
confirmed higher buy moves the complete field upward and selling that leader
moves it down to the next-highest open position. With no open position, the
last confirmed reference remains until the next confirmed leading fill; market
price alone never invents an accounting anchor. Open positions keep their cost
basis, identity, principal, and sell rules while the rendered trigger geometry
is derived again each poll.

## Suggested isolated-wallet profile

```dotenv
USE_GRIDLESS=true
GRIDLESS_ALLOCATION_MODE=survivor
MAX_ACTIVE_POSITIONS=50
TRADEABLE_BALANCE_PERCENT=50
GRIDLESS_MIN_POSITION_ETH=0.001
GRIDLESS_LADDER_TERMINAL_DRAWDOWN_PERCENT=95
GRIDLESS_LADDER_SPACING=log
GRIDLESS_LEADING_EDGE=true
SURVIVOR_RAPID_POLL_SECONDS=1
SURVIVOR_RAPID_POLL_WINDOW_SECONDS=30
GRIDLESS_LADDER_MAX_BUDGET_ETH=0
GRIDLESS_LADDER_EXPIRY_SECONDS=0
GRIDLESS_LADDER_REARM_POLICY=after_exit
GRIDLESS_LADDER_REARM_COOLDOWN_SECONDS=0
GRIDLESS_LADDER_INCLUDE_REFERENCE_ENTRY=false
```

Use `GRIDLESS_ALLOCATION_MODE=survivor` for dynamic behavior and keep
`GRIDLESS_LEADING_EDGE=true`. Each leading entry counts inside
`MAX_ACTIVE_POSITIONS`, borrows one funded rung as its accounting slot even
though its entry is off the ladder, and exits through the same profit,
stop-loss, gas, quote, and receipt safeguards as every position. A failed,
rejected, or unreconciled buy cannot move the reference.

The mode can start fresh or adopt ordinary open gridless positions. On the
first fresh Survivor poll, it immediately attempts one ordinary guarded buy at
the live market. The confirmed measured economic entry—not the earlier market
observation—becomes the reference from which its ladder is formed. If that buy
fails or remains unresolved, the provisional geometry does not become an open
position and no confirmed purchase anchor is invented.

If positions exist but no ladder exists, the first ladder-mode poll maps those
positions into a compatible versioned plan and adds exact rung provenance.
Frozen-reference plans remain v2; live survivor plans use v4. Existing v3
Survivor state is explicitly migrated from its matching persisted open
positions on the first poll; no market estimate is used. Plans cannot be
silently reinterpreted between modes. The loader still refuses old v1 one-shot state,
partial/foreign ladder provenance,
malformed positions, or more open positions than `MAX_ACTIVE_POSITIONS`.
Archive `data/gridless_ladder.json` together with
`data/gridless_positions.json` before any manual migration; never delete only
one side of an active strategy.

## Adopting an existing gridless bot

An existing threshold-mode bot may be stopped, switched to
`GRIDLESS_ALLOCATION_MODE=drawdown_ladder`, funded, validated, and restarted
without selling its positions first. Adoption is deterministic:

1. Every position's exact entry price is derived from persisted cost and token
   balance. No market-value estimate is used.
2. The highest entry price—the position currently most underwater at a common
   market price—becomes the stable ladder reference.
3. The configured linear or logarithmic geometry is generated from that
   reference through the terminal drawdown. At 95%, the floor is 5% of the
   highest historical entry price.
4. Existing entries are assigned, in price order, to the nearest distinct
   ideal rungs. Their cost basis, token balance, position ID, and independent
   sell behavior do not change.
5. Existing deployed principal plus newly spendable liquid determines how many
   additional minimum-sized rungs can be funded.
6. New funding prioritizes empty triggers below the current market so migration
   cannot burst-buy missed historical levels. Funded levels above the market
   remain dormant until price is observed above them and crosses downward.

For example, if five legacy entries are presently 70% through 50% underwater,
the 70%-underwater entry has the highest entry price and becomes the reference.
Those five positions occupy the nearest unique points in the configured map.
If deposited liquid funds ten more minimum positions, ten missing triggers are
added with priority below today's price, extending and densifying coverage
toward the 95% floor. A bot with no free liquid can still be adopted; later
deposits grow the same frozen map.

Adoption checkpoints the ladder first and position provenance second. If the
process stops between those atomic writes, restart completes the provenance
write only when position ID and exact principal still match. Any mismatch fails
closed. This recovery path does not infer buys or sells.

## Trigger geometry

The allocator computes the maximum number of triggers allowed by
`MAX_ACTIVE_POSITIONS` from reference `R`. Frozen drawdown mode retains its
creation reference. Survivor derives `R` every poll from the highest open
measured purchase point. Let terminal drawdown `D` be a fraction, maximum rung
count `M`, and one-based rung index `i`.

Linear:

```text
price[i] = R × (1 - D × i/M)
```

Logarithmic:

```text
price[i] = R × (1 - D)^(i/M)
```

With a $100 reference and a 95% terminal drawdown, the deepest trigger is $5.
Linear spreads triggers evenly in absolute drawdown. Logarithmic spacing keeps
more triggers for deep declines.

Unfunded triggers already exist in the plan geometry. Funding activates them.
A fresh plan's initial funded rungs are
distributed evenly across the complete range and always include the terminal
floor. An adopted plan treats legacy entries as existing anchors and gives new
capital priority to missing levels below the current market. Later funding
splits uncovered intervals to make coverage progressively denser.

## Dynamic capital accounting

The engine derives controlled principal from free spendable settlement asset
plus principal currently deployed in open ladder positions:

```text
strategy_capital = spendable_balance + deployed_rung_principal
target_allocation = strategy_capital × TRADEABLE_BALANCE_PERCENT / 100
```

`GRIDLESS_LADDER_MAX_BUDGET_ETH`, when greater than zero, caps the target.
Including deployed principal prevents a buy from making the strategy appear
poorer. Counting only planned principal—not mark-to-market token value—prevents
price volatility from moving triggers or resizing the plan.

Returned principal is already represented once and is not mistaken for new
capital. In Survivor, a wallet deposit or realized net profit adds reserved
rungs or increases the next-buy amount on the next poll. A withdrawal, gas, or
realized loss removes or shrinks unfilled reservations on the next poll. Open
position principal is immutable: if deployed principal alone exceeds the new
target, every unfilled rung is defunded and no buy is available until liquid
capital returns. Frozen drawdown mode retains its historical add-only targets.

### Growth order

1. Create each newly affordable rung at `GRIDLESS_MIN_POSITION_ETH`.
2. Continue densifying until `MAX_ACTIVE_POSITIONS` rungs are funded.
3. Once every currently empty slot has coverage, distribute additional liquid
   evenly across those next-buy reservations.
4. An already-open position is never topped up or mutated. After it sells, its
   future reservation is derived from then-current wallet capital.

Example: five open 0.001 ETH rungs plus a new 0.010 ETH deposit represent 0.015
ETH of strategy capital at 100% tradeable. With a maximum of at least 15, the
engine funds ten additional 0.001 ETH rungs. If the maximum were five instead,
the same capital would raise all five future rung targets to 0.003 ETH.

## Reusable rung lifecycle

Each trigger has independent persistent state:

```text
inactive -> ready -> open -> waiting_reset -> ready -> ...
                                  |
                                  +-> retired
```

- `inactive`: stable trigger exists but has no assigned minimum principal.
- `ready`: funded and eligible when observed price is at or below its trigger.
- `open`: exactly one confirmed position owns the rung.
- `waiting_reset`: its position sold; the cooldown must elapse and price must
  first be observed above the trigger before another downward crossing can buy.
- `retired`: expiry, cancellation, or `GRIDLESS_LADDER_REARM_POLICY=never`
  disables further buys.

The above-trigger reset prevents an immediate rebuy after a stop loss executed
below the rung. With `after_exit`, normal profitable sells naturally occur
above their buy trigger, so the rung becomes ready on a subsequent poll and
waits for price to return downward.

Selling the last open position does not close the field. With the default zero
expiry it remains active indefinitely and retains the last confirmed reference
until another guarded leading buy confirms. Frozen drawdown mode can be
re-anchored only by an explicit migration; Survivor follows its current open
leader in either direction.

## Buy execution

Every poll reconciles state, observes liquid, activates or grows affordable
rungs, advances eligible resets, and selects the highest crossed ready rung.
Rungs are thresholds, not limit orders: a ready rung is eligible whenever the
observed price is equal to or below its trigger. A gap through several rungs
buys the highest missed funded rung first, then can buy the next crossed rung
on later polls, subject to capacity, cooldown, liquid, and every execution
guard. It does not wait for an exact-price match.

In Survivor, the current highest purchase point is also checked against 50% of
the configured sell trigger. If crossed, the next buy uses a funded ready rung
for principal accounting but executes at the live leading edge; confirmation
then moves the geometry to the measured new purchase point. Multiple leading
positions may accumulate while capacity remains. At most one buy is attempted
per poll, and the normal buy cooldown still applies.

A rung becomes `open` only after:

1. its exact-input route passes provider, tax, slippage, gas-cap, and reserve
   checks;
2. for a downward rung entry, the final route has not recovered too far above
   the crossed trigger under `GRIDLESS_BUY_EXECUTION_MARGIN`. The percentage
   is applied across the price gap from the target rung toward the nearest
   occupied rung above it; when none exists, the ladder reference is the
   fallback boundary. Thus a -60% target, -50% occupied rung, and margin 50
   allow execution through -55%. Execution below the trigger remains valid;
3. the transaction confirms and actual token receipt is measured;
4. the position is atomically persisted with ladder ID, rung index, and exact
   principal;
5. the rung checkpoint is atomically persisted.

Failed, rejected, unaffordable, or unreconciled buys do not consume a rung.

## Independent exits and compounding

Every open rung remains an ordinary gridless position. The configured
`GRIDLESS_SELL_THRESHOLD` wakes its independent percentage-P&L sell check. The
executable route must still preserve `MIN_PROFIT_PERCENT` after projected gas.
A profitable rung can sell while other rungs remain underwater.

Confirmed proceeds return to the wallet. Original principal supports the same
rung's next cycle; realized profit increases free strategy capital and will
eventually add coverage or increase all rung targets. This is bounded adaptive
compounding, not immediate reinvestment into the just-sold position.

After a confirmed Survivor sell, the main loop and sell-side P&L observation
temporarily accelerate to `SURVIVOR_RAPID_POLL_SECONDS` for
`SURVIVOR_RAPID_POLL_WINDOW_SECONDS`. Each confirmation extends the window.
This still executes at most one independently validated sell per cycle; it does
not batch transactions or bypass route, gas, profit, receipt, or unresolved-
broadcast safeguards. Set the window to `0` to disable acceleration.

Sell triggers have strict execution priority over buys. While an authorized
sell trigger is latched, its observation lane is refreshed ahead of any
simultaneous buy lane. If the exit route times out, is rejected, or otherwise
cannot execute, that cycle does not fall through to a buy tournament. A
successful sell also consumes the cycle so the next rapid poll can attempt the
next profitable position before Survivor commits capital to a new entry.

## Persistence and fail-closed recovery

`data/gridless_ladder.json` stores versioned identity and recovery facts:
current derived geometry/allocation, per-rung lifecycle and reset guards,
position linkage, adoption provenance, fill/exit counts, timestamps, and
realized rung profit. Survivor rewrites the derived geometry and unfilled
reservations atomically whenever live position or wallet state changes.
`data/gridless_positions.json` stores the exact open positions. Survivor
leading entries also persist their measured fill price so restart can finish a
confirmed position-first re-anchor without consulting the live market.

If a confirmed buy position is written before its rung checkpoint, restart can
recover the unique matching ready rung from provenance. Duplicate rung owners,
foreign ladder IDs, missing open positions, principal mismatches, malformed
state, unsupported versions, chain/token changes, or a changed hard maximum
fail closed. An ambiguous confirmed sell boundary also remains a safety halt;
the bot never invents a completed exit from an empty ledger.

## Dashboard telemetry

The status payload identifies the active strategy as `drawdown_ladder` or
`survivor` and
reports `strategy_spacing` as `linear` or `log`. DoomDash uses those values for
its compact mode badge. Both values are derived from the existing environment
configuration; no dashboard-only variable or trading behavior change is
introduced. The bounded ladder summary also includes:

- reference, spacing, terminal drawdown, status, and optional expiry;
- state version; maximum, funded, reserved, ready, open, and legacy-adopted
  rung counts;
- the live amount assigned to the next ladder buy;
- next highest ready trigger;
- allocated, deployed, and logically reserved principal;
- average target position size;
- completed buy/sell cycles and recorded realized rung profit.

## Treasury sweeps and future buys

Native `treasury-transfer --amount available` intentionally sends every liquid
wei above `ETH_GAS_RESERVE` and the estimated transfer fee when no position
reserve is requested. That leaves open token positions intact but prevents new
buys until more settlement asset arrives.

Use `--preserve-positions N` to retain principal for at most N currently
available future positions. When `TREASURY_POSITION_RESERVE_ETH=0`, adaptive
drawdown mode infers the protected amount per slot from
`GRIDLESS_MIN_POSITION_ETH`. Use `--position-reserve-eth ETH` when the reserve
should reflect a larger, already-grown rung target.

```bash
ops/fleet/treasury-transfer \
  --only BOT \
  --asset ETH \
  --amount available \
  --preserve-positions 10
```

The transfer remains dry-run by default and retains normal stopped-bot,
recipient, fee, and execution guards.

## Safety notes

- Use a dedicated wallet or a conservative `TRADEABLE_BALANCE_PERCENT`; wallet
  deposits are intentionally interpreted as capital available to the strategy.
- `MAX_ACTIVE_POSITIONS` caps both maximum trigger density and simultaneous
  ladder positions.
- `GRIDLESS_LADDER_MAX_BUDGET_ETH` is the hard strategy-principal cap.
- Native gas reserve is removed before adaptive capital accounting.
- No leverage or borrowing is introduced.
- In Survivor, a manual withdrawal immediately defunds or shrinks unfilled
  reservations; it never rewrites an open position or bypasses minimum size.
- Funding changes do not move Survivor geometry; confirmed changes to the
  highest currently open purchase point do.
- Evaluate gas as a percentage of `GRIDLESS_MIN_POSITION_ETH`; microscopic
  cycles can be mathematically profitable and economically stupid.

Paper-test linear and log profiles against identical histories, including
deposits, repeated oscillations, stop losses, provider failures, gaps, restart
boundaries, assets that never recover, and a fully funded 95% descent.
