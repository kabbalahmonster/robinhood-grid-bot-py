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
reference. `GRIDLESS_ALLOCATION_MODE=survivor` is the dynamic variant. The
highest open purchase point triggers an off-ladder leading buy when its P&L
reaches 50% of the configured sell trigger. Market price alone never moves the
ladder. Only a successfully confirmed leading fill above the prior reference
ratchets the complete trigger geometry upward to that measured economic entry
price. Open positions keep their cost basis, identity, principal, and sell
rules; their rung becomes the future reusable trigger at the repositioned
level.

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
first ladder-mode poll, if positions exist but no ladder exists, it maps those
positions into a compatible versioned plan and adds exact rung provenance.
Frozen-reference plans remain v2; survivor plans use v3 and cannot be silently
reinterpreted between modes. It still refuses old v1 one-shot state,
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

## Stable trigger geometry

At creation, the allocator freezes a reference `R` and precomputes the maximum
number of triggers allowed by `MAX_ACTIVE_POSITIONS`. Let terminal drawdown `D`
be a fraction, maximum rung count `M`, and one-based rung index `i`.

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

Unfunded triggers already exist in the persisted geometry. Funding activates
them without moving any existing price. A fresh plan's initial funded rungs are
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

Returned principal is already assigned to its rung and is not mistaken for new
capital. A wallet deposit or realized net profit increases strategy capital;
gas and realized losses reduce it. The allocator never shrinks existing target
sizes automatically. If actual liquid is below the logical ready-rung reserve,
buys simply defer until funded again.

### Growth order

1. Create each newly affordable rung at `GRIDLESS_MIN_POSITION_ETH`.
2. Continue densifying until `MAX_ACTIVE_POSITIONS` rungs are funded.
3. Once maximum coverage exists, distribute additional allocation evenly over
   every rung target.
4. An already-open position is never topped up or mutated. Its larger target
   applies on its next buy cycle after it sells.

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

Selling the last open position does not close or re-anchor the field. With the
default zero expiry it remains active indefinitely. Frozen drawdown mode can be
re-anchored only by an explicit migration; Survivor can ratchet upward only
from a confirmed higher leading-edge purchase point.

## Buy execution

Every poll reconciles state, observes liquid, activates or grows affordable
rungs, advances eligible resets, and selects the highest crossed ready rung.
In Survivor, the current highest purchase point is also checked against 50% of
the configured sell trigger. If crossed, the next buy uses a funded ready rung
for principal accounting but executes at the live leading edge; confirmation
then moves the geometry to the measured new purchase point. Multiple leading
positions may accumulate while capacity remains. At most one buy is attempted
per poll, and the normal buy cooldown still applies.

A rung becomes `open` only after:

1. its exact-input route passes provider, tax, slippage, gas-cap, and reserve
   checks;
2. the final route remains within `GRIDLESS_BUY_EXECUTION_MARGIN` of the rung;
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

## Persistence and fail-closed recovery

`data/gridless_ladder.json` stores versioned geometry, funding targets,
per-rung state and entry kind, position linkage, adoption provenance,
fill/exit counts, timestamps, and realized rung profit.
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
- maximum, funded, ready, open, and legacy-adopted rung counts;
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
- A manual withdrawal can make logically reserved rungs temporarily
  unaffordable; it cannot cause the bot to resize a buy downward.
- Geometry and open positions never move when funding changes.
- Evaluate gas as a percentage of `GRIDLESS_MIN_POSITION_ETH`; microscopic
  cycles can be mathematically profitable and economically stupid.

Paper-test linear and log profiles against identical histories, including
deposits, repeated oscillations, stop losses, provider failures, gaps, restart
boundaries, assets that never recover, and a fully funded 95% descent.
