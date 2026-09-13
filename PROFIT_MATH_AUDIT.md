# Buy and sell profit-math audit

Date: 2026-09-13

## Scope and conclusion

This audit traced the active `grid_bot.py` grid and gridless paths through all
configured providers, native/WETH settlement, approval and wrap/unwrap setup,
stale-fee retry, taxed-token handling, partial moonbag sales, position storage,
and realized-profit reporting.

The pre-audit implementation correctly included confirmed buy gas in completed
position cost and charged confirmed sell gas in realized profit. It also used
the final signed swap gas plan immediately before broadcast. However, it did
not satisfy a strict thin-margin proof in four places:

1. Normal sell authorization used expected `buy_amount` (tax-adjusted where
   applicable), rather than the minimum output enforced after slippage.
2. Percentage and partial cost calculations passed through binary floats and
   could round in the trader's favor by a small number of wei.
3. Successful approval/cancel gas could be forgotten when the later swap
   aborted, understating the cost that a future sale needed to recover.
4. When exact post-sell proceeds could not be reconciled, the validated quote
   floor was substituted and recorded as though it were measured realized
   proceeds.

All four are corrected. Normal sells now have a conservative, integer proof at
the final broadcast boundary. The proof supports `MIN_PROFIT_PERCENT=0.1`.

## Audited invariants

### Completed buy cost

For native settlement, exact-input principal is the transaction value/requested
input. For WETH fallback, it is the exact wrapped input. The recorded position
cost is:

`principal + confirmed wrap gas + confirmed approval gas + confirmed swap gas`

Received token quantity comes from the wallet's raw integer balance delta. A
receipt transfer log is accepted only as stale-RPC recovery. A confirmed buy
with no positive measured or receipt-proven token quantity does not create a
position.

### Sold cost basis

Full sales use the full position cost. Partial moonbag sales use:

`ceil((position cost + deferred sell setup gas) * sold raw tokens / position raw tokens)`

Rounding upward prevents the sold slice from being assigned too little cost.
The retained moonbag remains a deliberately untracked asset after the position
closes; that policy is separate from the sold slice's profit calculation.

### Minimum proceeds and profit authorization

The engine first validates that executable `sell_amount` exactly equals the
requested raw input and that output is positive. It then selects the lowest
enforceable proceeds:

- Umbra: verified build-response `minOut` (`output_is_execution_floor`).
- 0x/LI.FI: explicit provider minimum when present.
- Other executable responses: `floor(quoted output * (1 - configured slippage))`.

0x slippage conversion is corrected to basis points (`fraction * 10,000`), and
LI.FI now receives the configured slippage parameter. Taxed-token slippage is
the configured/detected transfer fee plus market buffer and is deducted once.

At the last point before signing, a non-stoploss sell requires:

`minimum proceeds >= sold cost + ceil(sold cost * profit percent / 100) + confirmed setup gas + gas limit * signed gas price`

The exact same test is repeated before the one permitted stale-base-fee retry
using its higher gas price. `gas limit * gas price` is the maximum legacy signed
swap fee; confirmed realized profit later uses `gasUsed * effectiveGasPrice`.

### Setup and WETH settlement

Approval/cancel receipts in the current sell attempt are included as confirmed
setup gas. They are also persisted on the position as
`deferred_sell_gas_wei`. If the swap succeeds, current realized accounting
charges them and the position disappears. If the swap aborts, the next attempt
adds them to cost basis, so they cannot silently vanish.

A WETH-output sell reserves estimated unwrap gas before swap authorization.
After the swap confirms, the unwrap is built with a fresh gas plan and is
allowed only if its maximum fee still fits the remaining target-profit budget.
Otherwise the WETH is left intact and the durable unresolved-settlement guard
blocks further trading for operator recovery.

### Realized profit

Realized profit uses exact raw proceeds and confirmed receipt fees:

`confirmed proceeds - sold cost - confirmed setup gas - confirmed swap gas`

The bot no longer converts a quote floor into claimed realized proceeds. If
wallet balance and receipt logs cannot prove exact proceeds, it retains the
position state, writes an unresolved-broadcast guard, and records no sale
profit. This may require reconciliation, but it cannot manufacture accuracy.

## Explicit exceptions and residual risks

Stoploss execution intentionally bypasses the minimum-profit requirement. It
is a loss-containment feature and cannot simultaneously guarantee profit.

The normal-sell authorization invariant does not mean every attempted on-chain
operation is incapable of economic loss. Gas can be spent by a reverted swap,
an approval followed by permanent abandonment, or recovery activity. Chain
reorganizations, compromised/malicious routers, incorrect declared token tax,
nonstandard token behavior, and RPC dishonesty are outside a mathematical
preflight guarantee. Price improvement is harmless; output below the encoded
minimum should revert and spend gas rather than execute an under-floor sale.

For 0.1% operation, the configured slippage plus all maximum gas must fit below
the expected gross edge. A 0.1% target is not a promise that every market will
offer a viable transaction; the correct behavior is frequently to skip.

## Verification requirements

Regression coverage includes:

- 0.1% profit rounding upward at wei boundaries;
- slippage-derived and provider-explicit minimum proceeds;
- exact-input mismatch rejection;
- upward-rounded partial/moonbag cost basis;
- deferred confirmed setup gas;
- WETH unwrap refusal when it would consume guaranteed profit;
- exact proceeds required for realized accounting;
- provider/tournament parity and taxed-token combined tolerance;
- configuration acceptance at 0.1% and rejection below it.

Any future provider must expose a trustworthy encoded minimum or use a verified
configured-slippage contract. Any new sell setup transaction must be included
both in the final current-attempt inequality and deferred recovery accounting.
