# AI strategy capability reference

This document describes what the Robinhood Chain Grid Trading Bot can actually
do. It is intended to be supplied to an AI model that is designing, reviewing,
or tuning trading strategies. It is a capability contract, not a claim that any
configuration is profitable.

Implementation reference: commit `fe6342d57e5b824dcfa470d2c1481eaea54959d4`.
If the bot changes, verify this document against `config.py`, `grid_bot.py`,
`gridless.py`, `drawdown_ladder.py`, and `route_tournament.py` before relying on
it.

## Executive model

The bot is a long-running, single-wallet, single-token, spot trading process on
an EVM chain. It exchanges native ETH or WETH for one configured ERC-20 token
and later exchanges that token back to ETH or WETH. It maintains multiple
independent long positions, exact cost basis, persistent state, and realized
profit records.

The bot is not a general strategy scripting engine. A strategy model can
recommend supported configuration changes, compare existing modes, or propose a
code change. It cannot assume the bot already supports arbitrary indicators,
external signals, order books, limit orders, shorts, leverage, derivatives,
cross-token portfolios, cross-chain execution, or discretionary transaction
construction.

The bot has two top-level trading engines:

1. **Classic grid**: pre-generated fixed price ranges decide entries and exits.
2. **Gridless**: persistent positions are entered and exited using P&L marks.
   Gridless has three entry-allocation modes: `threshold`, `drawdown_ladder`,
   and `survivor`.

Strategy logic decides when a trade becomes eligible and how much settlement
asset is assigned. The execution layer retains final authority. Every action
must still pass route, exact-input, allowance, slippage/tax, gas, balance,
reserve, local simulation, receipt, settlement, and accounting guards.

## Supported market and position model

- Supported chains: Robinhood Chain (`4663`), Base (`8453`), and Ethereum
  Mainnet (`1`).
- One process trades one configured token address against native ETH or WETH.
- Positions are long-only and spot-only.
- Buys and sells are exact-input swaps, not resting orders.
- Multiple positions may be open, each with its own measured token balance and
  all-in cost basis.
- Token decimals are read on-chain and used in price, balance, P&L, and
  moonbag calculations.
- By default, actions are receipt-serialized: no overlapping wallet
  transactions and no pending-nonce pipeline.
- A trigger is permission to attempt a trade, not a guarantee of execution.
  The bot may skip an eligible trade when the route or safety checks fail.

Unsupported unless new code is added:

- short selling, leverage, borrowing, lending, futures, or options;
- simultaneous multi-token portfolio allocation in one process;
- order-book limit/stop orders or on-chain resting orders;
- market-cap, volume, social, news, technical-indicator, or oracle-driven
  signals beyond the bot's own quote-derived price/P&L observations;
- automatic token discovery or rotation;
- splitting one position across several providers in one atomic action;
- aggregating several position exits into one swap;
- allowing an AI model to sign, broadcast, or bypass a guard directly.

## Main operating loop

At startup the bot validates configuration, initializes the wallet, RPC pool,
provider graph, persistent trackers, and dashboard reporter, then optionally
waits for randomized startup jitter.

Each non-overlapping polling round performs this sequence:

1. Refuse to trade if a durable unresolved-broadcast or accounting guard is
   active. Optional automatic reconciliation may resume only from exact
   receipt-proven state.
2. Read settlement, traded-token, and optional stablecoin balances.
3. Load persistent positions and halt if tracked position tokens exceed the
   wallet's actual token balance.
4. Obtain a market observation. Classic mode normally requests a simple price.
   Gridless mode can request one quote-derived P&L side per cycle and retain
   other fresh sides in a bounded cache.
5. Evaluate sells before buys. Sell priority prevents new capital deployment
   while an eligible exit is unresolved.
6. Report status, positions, balances, attempts, events, trade history, route
   telemetry, and warnings to the optional dashboard.
7. Evaluate buys. With gridless multi-action rounds, the bot may complete a
   full guarded sell pass and then a guarded buy pass before reporting.
8. Emit bounded cycle/RPC performance telemetry and dispatch optional
   background shadow route observation.
9. Sleep according to `POLL_CADENCE_MODE` and repeat.

`completion_delay` sleeps the full poll interval after work. `fixed_rate`
targets start-to-start cadence by subtracting round work from the sleep. An
overrun starts the next round immediately, but rounds still never overlap.

## Classic grid engine

Classic mode is selected with `USE_GRIDLESS=false` and requires a generated
`data/positions.json` grid.

Each empty grid position has a fixed `buyMin`/`buyMax` price range. The first
empty position whose range contains the observed price is eligible to buy,
subject to available balance and `MAX_ACTIVE_POSITIONS`. At most one buy is
attempted per cycle.

Each filled position has a fixed `sellMin`. The engine computes its observed
gross price P&L from recorded cost and balance:

- With `FAST_PROFIT=true`, it may sell once observed P&L reaches
  `MIN_PROFIT_PERCENT + 1.5 percentage points`, even below `sellMin`.
- Otherwise the required observed price is the higher of `sellMin` and the
  same buffered profit threshold.
- At most one sell is attempted per cycle.

The observed trigger is only the first gate. Executable proceeds must still
clear the gas-aware minimum-profit guard before a normal sale is signed.

Classic strategy controls:

- `GRID_SPACING_PERCENT`: used when generating or migrating grid geometry.
- `MAX_POSITIONS`: total generated grid slots.
- `MAX_ACTIVE_POSITIONS`: maximum simultaneously filled slots.
- `TRADEABLE_BALANCE_PERCENT`: controls the share of spendable balance divided
  across currently available slots for each classic-grid buy.
- `FAST_PROFIT`: permit a profit-triggered exit before the fixed sell range.
- `MIN_PROFIT_PERCENT`: minimum normal-sale profit requirement.

`INITIAL_BUY_AMOUNT` is loaded into configuration and used by the older
`bot.py` balance preflight, but the active `grid_bot.py` classic execution path
does not use it for order sizing. A strategy model must not assume that changing
this variable changes live classic buy size.

Grid geometry is state, not continuously regenerated configuration. Changing
grid-generation parameters alone does not rewrite an existing positions file;
use the supplied generator or migration flow.

## Gridless engine

Gridless mode is selected with `USE_GRIDLESS=true`. Positions live in
`data/gridless_positions.json` and are evaluated independently.

### Quote-derived P&L observations

Gross market price may be misleading because executable buy and sell routes can
have different tax, slippage, liquidity, approval, and gas economics. The
gridless engine therefore supports directional net P&L observations:

- `legacy`: gross market-price mark;
- `buy`: exact-input buy-side mark, appropriate for entry triggers;
- `sell`: exact-input sell-side mark, appropriate for exits;
- `bidirectional`: alternate buy and sell marks, one request per cycle;
- `trilateral`: rotate buy, sell, and legacy marks, one request per cycle.

`PNL_POLLING_MODE` selects the behavior. `BIDIRECTIONAL_PNL_MAX_AGE_SECONDS`
bounds how long a cached mark may influence a trigger. Near-trigger focus can
refresh an approaching side every other cycle and a crossed side every cycle.
`PNL_LEGACY_TRIGGERS` optionally lets the gross legacy mark independently wake
buy and sell attempts; it defaults on only in `legacy` mode.

These observations authorize strategy checks only. Execution always obtains a
fresh exact-amount quote and reruns the final guards.

### Shared gridless exit behavior

Every gridless position exits independently:

- Normal exit: its authorized P&L mark reaches
  `GRIDLESS_SELL_THRESHOLD`, or `MIN_PROFIT_PERCENT` when
  `PNL_TRIGGER_BY_MIN_PROFIT=true`.
- The fresh executable route must still return at least the position's sold
  cost plus `MIN_PROFIT_PERCENT` and all maximum/confirmed applicable gas.
- Optional stop loss: if `GRIDLESS_STOPLOSS_ENABLED=true`, a position at or
  below `GRIDLESS_STOPLOSS_THRESHOLD` becomes an exit candidate.
- Stop-loss candidates have priority; otherwise the highest-P&L eligible
  position is selected first.
- `MOONBAG_PERCENTAGE` retains that percentage of raw tokens and sells the
  remainder. The sold cost basis is allocated proportionally and rounded up.

Current implementation caveat: `_execute_sell_gridless()` contains an intended
stop-loss exemption from the positive-profit floor, but
`_attempt_gridless_sell_candidate()` first applies an unconditional projected
net-profit check. A normally losing stop-loss candidate is therefore expected
to be skipped before it reaches that exemption. Treat gridless stop loss as
non-functional for ordinary loss exits at this referenced commit; do not rely
on it for risk control until the precheck is fixed and regression-tested.

With `GRIDLESS_MULTI_ACTION_ROUNDS=false`, the bot attempts at most one selected
sell per round and preserves strict sell-before-buy priority. With it enabled,
the opening eligibility snapshot may authorize separate guarded attempts for
every candidate. Each attempt reloads and matches durable position identity,
gets its own exact quote, transaction, receipt, and reconciliation. A confirmed
sell pass may then permit buys in the same round.

### Threshold allocation

Set `GRIDLESS_ALLOCATION_MODE=threshold`.

Entry behavior:

- With no positions, make an initial guarded market buy.
- Otherwise identify the lowest purchase-price position (the current "top"
  dip reference). Buy when its authorized buy-side P&L is at or below
  `GRIDLESS_BUY_THRESHOLD`.
- If `GRIDLESS_LEADING_EDGE=true` and exactly one position exists, also buy
  when that position's authorized buy-side P&L reaches 50% of the configured
  sell trigger. With multiple positions, the nearest confirmed purchase below
  market can trigger at the same spacing only while market is inside a bounded
  gap and another half-sell interval fits before the next purchase above it.
  Each confirmed fill becomes the next lower anchor, so capacity is filled
  progressively as price rises rather than all at once.
- Do not buy at or above `MAX_ACTIVE_POSITIONS`.
- Enforce `GRIDLESS_BUY_COOLDOWN_SECONDS` after every confirmed buy.

Sizing:

```text
principal = spendable settlement balance
          × TRADEABLE_BALANCE_PERCENT / 100
          ÷ currently available position slots
```

Native-ETH spendable balance excludes `ETH_GAS_RESERVE` during sizing. The
minimum ordinary threshold trade is 0.001 ETH/WETH.

After a dip trigger, `GRIDLESS_BUY_EXECUTION_MARGIN` limits how far price may
recover toward zero before execution. For example, with a -10% trigger and a
50% margin, execution is blocked after recovery above -5%. Leading-edge buys
do not use this dip-recovery check.

### Frozen drawdown ladder allocation

Set `GRIDLESS_ALLOCATION_MODE=drawdown_ladder`.

This mode builds a persistent field of percentage-drawdown triggers below a
frozen reference purchase price. It can start fresh or deterministically adopt
existing gridless positions without altering their balances or cost basis.

- `GRIDLESS_LADDER_TERMINAL_DRAWDOWN_PERCENT` sets the deepest trigger.
- `GRIDLESS_LADDER_SPACING=linear` uses equal absolute drawdown intervals.
- `GRIDLESS_LADDER_SPACING=log` uses equal price ratios and concentrates more
  rungs in deep declines.
- `GRIDLESS_MIN_POSITION_ETH` is the minimum fundable rung principal.
- `GRIDLESS_LADDER_MAX_BUDGET_ETH` optionally caps total assigned principal;
  zero disables the additional cap.
- Controlled capital includes spendable liquid plus principal deployed in
  open ladder positions, multiplied by `TRADEABLE_BALANCE_PERCENT`.
- New capital first increases the count of minimum-sized funded rungs, then
  raises rung size after maximum coverage is funded.
- A crossed ready rung is eligible at or below its trigger; it is not a limit
  order and does not require an exact-price match.
- By default only the highest crossed ready rung is attempted in a poll.

`GRIDLESS_BUY_EXECUTION_MARGIN` limits recovery from the crossed trigger toward
the nearest occupied rung above it, or toward the ladder reference when none is
occupied. A fill below the trigger remains valid.

### Survivor allocation

Set `GRIDLESS_ALLOCATION_MODE=survivor`.

Survivor is the dynamic ladder variant:

- A fresh strategy begins with one guarded live-market buy. Its confirmed,
  measured economic purchase point becomes the reference.
- The highest currently open purchase price is the live reference.
- The lowest currently open purchase price is the upper boundary for future
  downward rungs.
- Only current spendable liquid funds the future grid; open principal does not
  inflate or dilute that allocation.
- On every poll, affordable future reservations are rebuilt below the lowest
  open entry through the terminal floor. Deposits, withdrawals, gas use,
  realized profits, and realized losses can change density and rung size.
- Existing open positions keep their identities, balances, costs, and exit
  rules when geometry changes.
- With `GRIDLESS_LEADING_EDGE=true`, strength at 50% of the sell trigger can
  create repeated guarded entries from the current highest purchase point.
  A confirmed leading fill re-anchors the field; a failed or unresolved buy
  cannot move it.
- When all positions exit, the last confirmed reference remains until the next
  confirmed leading purchase point.

After a confirmed Survivor sell, polling temporarily accelerates to
`SURVIVOR_RAPID_POLL_SECONDS` for
`SURVIVOR_RAPID_POLL_WINDOW_SECONDS`. Setting the window to zero disables this
behavior.

### Ladder rung lifecycle

Persistent rung states are:

```text
inactive -> ready -> open -> waiting_reset -> ready -> ...
                                  |
                                  +-> retired
```

- `inactive`: geometry exists but no minimum principal is assigned.
- `ready`: funded and eligible on a qualifying crossing.
- `open`: exactly one confirmed position owns the rung.
- `waiting_reset`: the position exited; cooldown and an observation above the
  trigger are required before another downward entry.
- `retired`: no future entries due to expiry/cancellation. Older builds could
  incorrectly strand active Survivor slots here under a `never` rearm policy;
  current builds recover them.

Survivor always recycles a rung after reset. For frozen drawdown ladders,
`GRIDLESS_LADDER_REARM_POLICY=after_exit` recycles and `never` allows one
completed trade only.
`GRIDLESS_LADDER_REARM_COOLDOWN_SECONDS` adds a post-exit delay.
`GRIDLESS_LADDER_EXPIRY_SECONDS` stops new fills after a duration; zero means no
expiry. `GRIDLESS_LADDER_INCLUDE_REFERENCE_ENTRY` optionally makes the first
rung equal to the reference instead of strictly below it.

## Route and settlement behavior

Primary swap providers are `uniswap`, `sushiswap`, `lifi`, `0x`, and `umbra`.
Umbra is Robinhood Chain-only. Provider credentials and chain restrictions are
validated at startup.

`SWAP_PROVIDER` selects the primary. `SWAP_FALLBACK_PROVIDER` optionally retries
an operation from the beginning on the paired provider after a retryable
pre-broadcast failure. Each new operation returns to primary-first behavior.
Fallback is sealed after a setup transaction confirms so an approval, wrap, or
other partial action cannot be replayed through another provider.

With `USE_ETH_TRADING=true`, native settlement is preferred. If native routing
is unavailable, a verified direct-WETH path may be used:

- buys wrap exactly the intended principal, approve if needed, and swap;
- sells receive WETH, measure it, and unwrap exactly that receipt;
- wrap, approval, swap, and unwrap costs are included in authorization and
  realized accounting;
- partial settlement creates a durable halt rather than replaying the trade.

With `USE_ETH_TRADING=false`, the strategy spends and receives WETH directly.

### Route tournament

`ROUTE_TOURNAMENT_MODE` controls optional multi-route comparison:

- `off`: no tournament work; execute through configured primary/fallback.
- `shadow`: compare configured provider/settlement candidates for telemetry
  only. It never changes execution. Background mode can remove this observation
  from the trading thread, but it still consumes API/RPC quota.
- `gate`: guarded one-bot canary execution. Requires
  `ROUTE_TOURNAMENT_CANARY=true`. Candidates are ranked by output floor after
  all projected gas and must be freshly re-quoted, locally simulated, and
  revalidated before execution. When no valid winner exists, the bot falls
  back to the ordinary fresh primary/fallback path.

`execute` is intentionally invalid.

Tournament dimensions:

- `ROUTE_TOURNAMENT_PROVIDERS`: subset of
  `uniswap,sushiswap,umbra,lifi`.
- `ROUTE_TOURNAMENT_SETTLEMENTS`: subset of `native,weth`.
- shadow and gate deadlines are independently bounded from 1 to 15 seconds.
- buy score is output units per total principal-plus-gas cost.
- sell score is conservative output floor minus all projected gas and must
  clear sold cost plus the profit floor.
- only the winner may receive required setup; losing candidates are never
  approved.

`ROUTE_TOURNAMENT_SPECULATIVE_FALLBACK_SECONDS` is an opt-in sell-only gate
canary. It may start a non-broadcasting baseline quote in parallel, but it does
not approve, sign, or weaken later checks.

With multi-action rounds,
`ROUTE_TOURNAMENT_ROUND_ROUTE_REUSE=true` may reuse only a winning
provider/settlement identity within the same direction and round. Every fill
still gets a fresh exact-amount quote, simulation, and guard stack. The hint
cannot cross a round or restart.

## Execution and accounting invariants

These constraints are authoritative and should not be proposed away as
"strategy optimization":

- Exact input amount and positive output are required.
- Final calldata is locally checked with `eth_call` and `eth_estimateGas`
  immediately before signing.
- `MAX_BUY_GAS_ETH` and `MAX_SELL_GAS_ETH` enforce direction-specific projected
  fee caps; `MAX_SWAP_GAS_ETH` is their default. Zero disables a cap.
- `GAS_LIMIT_MULTIPLIER`, `GAS_PRICE_MULTIPLIER`, and
  `GAS_PRICE_FRESHNESS_MULTIPLIER` add bounded transaction headroom.
- Native buys must cover principal plus maximum transaction cost. Gas-reserve
  policy is applied while sizing and in final funding checks.
- Recorded buy cost is principal plus confirmed wrap, approval, and swap gas.
- Recorded token quantity is the measured wallet balance increase, with
  receipt evidence used for stale-RPC recovery.
- Normal sell authorization uses the lowest enforceable proceeds and requires:

```text
minimum proceeds >= sold cost
                  + configured minimum profit
                  + confirmed setup gas
                  + maximum signed swap gas
```

- Realized profit uses exact reconciled proceeds and confirmed receipt fees,
  not a quote or optimistic estimate.
- An uncertain confirmed broadcast creates a durable guard and halts trading.
- Position state is not removed until settlement is confirmed and reconciled.
- A stale-base-fee rejection may be rebuilt once with the rejecting node's
  higher base fee, but all economic and gas guards run again.
- Persistent writes are atomic where strategy/accounting state requires it.
- Position/ladder provenance mismatches, unsupported state versions, ambiguous
  settlement, or wallet balance deficits fail closed.

## Fee-on-transfer token behavior

Manual taxed-token mode requires `TAXED_TOKEN=true` and a verified
`TOKEN_TRANSFER_FEE_PERCENT`. The configured fee plus
`TAXED_TOKEN_SLIPPAGE_BUFFER_PERCENT` is capped at 15%.

On Robinhood Chain, automatic detection defaults on. It requires two consistent
tax-specific simulation failures before persisting runtime protection. It does
not learn from generic route, timeout, 404, or 429 failures and never edits
`.env`. `AUTO_DETECT_TOKEN_TRANSFER_FEE_MAX_PERCENT` bounds classification.

Measured post-trade wallet deltas remain accounting authority. This behavior
reduces accounting errors for bounded transfer taxes; it does not make
honeypots, blacklists, mutable fees, or malicious tokens safe.

## Profit disposition

After a confirmed profitable sell, optional policies can divide positive
realized profit:

- `BANK_PERCENTAGE`: swap this percentage of profit to the configured USDG/USDC
  stablecoin; zero disables banking.
- `BANK_MIN_AMOUNT`: minimum stablecoin output required before banking.
- `PROFIT_FEE_PERCENT`: accrue this percentage for
  `PROFIT_FEE_WALLET`; zero disables it.
- `MIN_PROFIT_FEE_TRANSFER_ETH`: minimum accrued fee before transfer.
- Banking plus profit-fee percentages may not exceed 100%.
- `MOONBAG_PERCENTAGE`: retain a percentage of sold-position tokens as an
  untracked wallet moonbag; zero disables retention.

Banking, fee transfer, and moonbag handling occur after trade authorization and
do not relax the sale's minimum-profit accounting.

## Behavioral option matrix

| Concern | Supported options | Primary controls |
|---|---|---|
| Trading engine | classic grid, gridless | `USE_GRIDLESS` |
| Gridless allocation | threshold, frozen drawdown ladder, Survivor | `GRIDLESS_ALLOCATION_MODE` |
| Entry direction | dips; optional strength/leading-edge entries | `GRIDLESS_BUY_THRESHOLD`, `GRIDLESS_LEADING_EDGE` |
| Exit direction | profit target; optional stop loss | `GRIDLESS_SELL_THRESHOLD`, `GRIDLESS_STOPLOSS_*` |
| Position sizing | classic/threshold balance divided across slots; adaptive ladder reservations | `TRADEABLE_BALANCE_PERCENT`, ladder controls |
| Capacity | fixed maximum concurrent positions | `MAX_ACTIVE_POSITIONS` |
| Entry timing | cooldown, recovery margin, ladder crossings | `GRIDLESS_BUY_COOLDOWN_SECONDS`, `GRIDLESS_BUY_EXECUTION_MARGIN` |
| P&L signal | legacy, buy, sell, alternating bidirectional, trilateral | `PNL_POLLING_MODE`, `PNL_LEGACY_TRIGGERS` |
| Round actions | one prioritized action; multiple separately guarded gridless actions | `GRIDLESS_MULTI_ACTION_ROUNDS` |
| Cadence | completion delay; fixed rate; Survivor rapid sell window | `POLL_CADENCE_MODE`, polling controls |
| Settlement asset | native ETH; WETH | `USE_ETH_TRADING` |
| Routing | primary plus retry fallback; shadow comparison; guarded gate canary | provider and tournament controls |
| Transfer tax | declared bounded fee; guarded auto-detection; off | taxed-token controls |
| Token remainder | full sale; retained moonbag | `MOONBAG_PERCENTAGE` |
| Profit disposition | retain, bank to stablecoin, charge configured fee | banking/fee controls |
| Recovery | operator reconciliation; narrow receipt-proven automatic reconciliation | `AUTO_RECONCILE_UNRESOLVED_BROADCAST` |

## State and telemetry available to strategy analysis

Persistent local state includes:

- classic or gridless open positions and exact cost basis;
- adaptive ladder identity, geometry, funding, ownership, reset state, fills,
  exits, and realized rung profit;
- realized profit history and reset baseline;
- recent successful trade history;
- recent structured warnings/errors with repeat counts;
- token-tax detection state;
- unresolved-broadcast and exact-approval guards;
- profit-fee accrual and treasury receipts;
- route incident state.

Dashboard/status telemetry can include:

- token price, wallet balances, gas reserve, stablecoin balance, and treasury
  sent totals;
- open positions, cost basis, gross and directional P&L marks;
- session and persistent realized profit windows;
- active strategy/allocation/spacing, capacity and funding warnings;
- buy and sell attempt state;
- provider, tax, effective slippage, poll mode, and trigger mode;
- bounded ladder state and next trigger;
- route-tournament candidates, rejection codes, selected identity, lifecycle,
  and exact final economics;
- sanitized cycle phase timings and aggregate RPC attempts/failures/latency.

Telemetry does not include private keys, raw provider response bodies, calldata,
RPC URLs, credentials, or reversible trading-pair fingerprints. A model should
request schema/build/coverage validation before comparing telemetry windows.

## Strategy-development rules for an AI model

When proposing a strategy or optimization:

1. State whether it is achievable by configuration alone or requires code.
2. Name the compatible engine and allocation mode.
3. Separate entry eligibility, sizing, exit eligibility, routing, and execution
   safety. Do not treat them as one decision.
4. Preserve exact-input, simulation, gas, receipt, settlement, cost-basis, and
   unresolved-broadcast invariants.
5. Use net executable economics for exits. A gross price threshold is not proof
   that a sale is profitable.
6. Treat stop loss as a known implementation gap at this commit: the candidate
   trigger exists, but an earlier unconditional profit precheck can block the
   intended loss exit.
7. Account for provider/API quota and RPC load when changing cadence, P&L
   polling, tournaments, or fleet-wide behavior.
8. Do not infer that a missed action is caused by the strategy trigger until
   route, gas-cap, reserve, capacity, cooldown, stale-mark, and safety-halt
   reasons have been checked.
9. Prefer one isolated bot canary and an equal-duration pre/post comparison.
10. Specify rollback as exact configuration reversions.

A useful proposal format is:

```yaml
objective: <measurable economic or operational objective>
scope: <bot/token/chain and mode>
evidence:
  window: <duration and timestamps>
  build_and_schema: <validated versions>
  observations: <trigger, route, gas, latency, failure evidence>
hypothesis: <causal claim>
implementation:
  type: configuration | code_change
  changes:
    ENV_NAME: old -> new
preserved_invariants:
  - <execution/accounting guard>
expected_effects:
  benefit: <measurable prediction>
  costs: <quota, latency, gas, drawdown, inventory, or opportunity cost>
canary:
  bot: <one isolated bot>
  duration_or_event_count: <measurement requirement>
  acceptance: <metrics and thresholds>
  abort: <safety/economic regression conditions>
rollback:
  ENV_NAME: <original value>
confidence: low | medium | high
unknowns:
  - <missing evidence>
```

## High-risk misconceptions to reject

- "The sell threshold guarantees profit." It only wakes an attempt; the fresh
  gas-aware route decides whether execution is authorized.
- "The configured stop loss currently guarantees a loss exit." It does not:
  the candidate trigger exists, but the current pre-execution profit check can
  block the trade before the later stop-loss exemption.
- "Shadow tournament results are executable quotes from the same instant."
  They are observational subsequent-market comparisons.
- "More frequent polling always improves fills." It can increase provider
  throttling, RPC contention, round overlap pressure, and fleet latency.
- "A ladder rung is a limit order." It is a crossing threshold checked during
  polling, followed by a fresh market-route attempt.
- "Survivor compounds all marked position value." It allocates the future grid
  from liquid settlement balance; open positions mainly define geometry.
- "Multi-action rounds batch trades." They execute distinct serialized swaps.
- "Provider fallback can replay after approval or wrap." Fallback is sealed
  once setup confirms.
- "Changing `.env` safely migrates persistent strategy state." Some mode,
  identity, geometry, or capacity changes require validated migration and can
  fail closed.
- "An unresolved receipt means the trade failed." Its outcome is unknown;
  replaying it could duplicate the trade, so the bot halts.

## Source map

- Configuration fields, defaults, and validation: `config.py`
- Main loop, triggers, execution, settlement, and reporting: `grid_bot.py`
- Gridless threshold decisions and persistent positions: `gridless.py`
- Adaptive ladder and Survivor state machine: `drawdown_ladder.py`
- Route scoring and gate/shadow behavior: `route_tournament.py`
- Provider selection and fallback: `swap_provider.py`
- Wallet broadcast and receipt guards: `wallet.py`
- Realized profit persistence: `profit_tracker.py`
- Detailed adaptive allocation semantics: `docs/DRAWDOWN_LADDER.md`
- Managed-round latency options: `docs/LIGHTNING_ROUNDS.md`
- Profit invariants and residual risk: `PROFIT_MATH_AUDIT.md`
