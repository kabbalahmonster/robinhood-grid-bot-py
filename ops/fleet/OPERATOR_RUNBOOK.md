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

Compare the baseline against alternative trigger geometry before changing live
configuration:

```bash
strategy-model --positions 8 --buy-triggers 10,15,20 \
  --sell-triggers 5,10,15 --min-profit 5 \
  --output reports/strategy-comparison.html
```

Open the HTML report and compare capacity-boundary coverage against the rebound
required for the newest position to exit. The matching CSV and JSON are created
beside it. Add `--fleet` to include current bot settings. This is a read-only
geometric model, not a backtest; do not treat its normalized prices as expected
returns.

## Update and verify

```bash
cd ~/bot-farm/fleet-command/robinhood-grid-bot-py
ops/fleet/update-this-checkout
ops/fleet/update-fleet
ops/fleet/fleet-doctor
```

For one canary, use the guarded single-bot updater. It conditionally restarts
only an already-running bot:

```bash
update-bot ROBINVAULT --list-branches
update-bot ROBINVAULT --branch nullfox/umbra-provider-integration --check
update-bot ROBINVAULT --branch nullfox/umbra-provider-integration
```

Rollback uses the same reviewed path; it is a fast-forward branch switch, not a
reset:

```bash
update-bot ROBINVAULT --branch main --check
update-bot ROBINVAULT --branch main
```

Use `--no-restart` to change only files/branch state. The command refuses
tracked modifications and divergence and preserves untracked `.env`/`data`
files. A stopped bot or absent fleet remains stopped.

## Reclaim log disk space

Cleanup is preview-first and safe while the fleet runs. A normal monthly
retention pass is:

```bash
cleanup-logs --older-than "1 month"
cleanup-logs --older-than "1 month" --apply --confirm-delete-logs
```

Use `--only A,B` or `--exclude A,B` for partial fleets. The newest log per bot
is retained unless `--keep-latest 0` is explicit. `--older-than all` means all
matching logs after newest-file retention; it does not mean all bot files. See
the fleet README for every accepted age format and live-process caveat.

## Bundle current fleet logs for analysis

Create one safely shareable file from every bot's newest log without stopping
the fleet:

```bash
bundle-logs --all --tournament-rounds-only --since 6h --output fleet-tournament.log

# Combined tournament plus bounded cycle/RPC/provenance evidence:
bundle-logs --all --analysis-sample-only --since 24h --output fleet-analysis.log
```

For a focused comparison, use `--only MANY,ROBINVAULT`. The default output is
grouped into labelled per-bot sections with source, status, record count, and
UTC time range; multiline errors stay intact and secret redaction is enabled.
Use `--chronological` when a shared-provider or RPC incident requires one
fleet-wide timeline. A nonzero exit with an output file means the
manifest identifies one or more missing/unreadable bot logs; the partial
evidence is usable, but its stated gaps matter.
An existing output is never silently overwritten: the command announces and
uses the next numbered name. Pass `--force` only when replacing the exact path
is intentional.
`--tournament-only` lists bots without a tournament in the filtered time window
as skipped in the manifest, while leaving them out of the merged records.
Use `--tournament-rounds-only` when the analysis needs only correlated
tournament lifecycle records, including each candidate's sanitized rejection
class and elapsed time. Interrupted final rounds are retained and clearly
labelled in the manifest.
Prefer bundles from bots running the current correlated telemetry. Their
manifest says `correlation=id`; `correlation=legacy_order` means the source log
predates stable round IDs and concurrent provider completion may make its
candidate-to-winner boundaries approximate.

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
`TREASURY_POSITION_RESERVE_ETH` multiplied by its available buy-slot count
(configured capacity minus filled positions). Use
`--position-reserve-eth ETH` to override the per-slot reserve for one run.
The footer shows each bot's planned contribution plus the exact fleet total;
execution uses the same layout but totals confirmed transfers only.

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
may still be safely skipped during normal final guards. If gate preflight itself
has no valid candidate, the dashboard records `baseline_fallback` and the bot
uses its normal configured route with all usual safeguards, rather than losing
an otherwise valid exit to tournament-only availability. Watch latency, 429s,
timeout rejections, gas, and successful buy/sell receipts before expanding the trial.
Rollback is `ROUTE_TOURNAMENT_MODE=off` and
`ROUTE_TOURNAMENT_CANARY=false` on that same bot. See the route-tournament
section of the main README and the fleet README for accounting and timeout
details.

For a larger rollout, begin with `POLL_INTERVAL_SECONDS=12` and the defaults
`ROUTE_TOURNAMENT_PROVIDERS=uniswap,sushiswap`,
`ROUTE_TOURNAMENT_SETTLEMENTS=native,weth`, shadow timeout `4`, and gate timeout
`12`. Increase polling toward 15-20 seconds if provider 429s or overlapping
rounds appear. Native-only settlement halves tournament candidates but removes
WETH fallback and should be an intentional liquidity tradeoff.
To canary Umbra, append it on one bot with
`ROUTE_TOURNAMENT_PROVIDERS=uniswap,sushiswap,umbra`. It adds one public-API
candidate per settlement. Watch 429s/timeouts before expanding; executable
builds pin UmbraRH and use local gas estimation.
Unapproved LI.FI/Umbra sell rows appear as `approval required`, not as a local
simulation failure. They are provisionally ranked with estimated approval gas;
only the provisional winner is approved, refreshed, and required to pass exact
local simulation. LI.FI approval is normally reusable; Umbra approval is exact.
A confirmed Umbra approval creates a durable one-approval fuse until its swap
settles: the existing allowance may finish the operation, but a second approval
is blocked and trading halts for review rather than entering an approval-gas
loop.
To canary LI.FI, set `LI_FI_API_KEY` and use
`ROUTE_TOURNAMENT_PROVIDERS=uniswap,sushiswap,lifi`, initially with
`ROUTE_TOURNAMENT_SETTLEMENTS=native` and `ROUTE_TOURNAMENT_MODE=shadow`.
`lofi` is accepted as an input alias, though documentation uses `lifi`.

## Position-balance reconciliation

When `fleet-doctor`, inventory, or bot logs show tracked managed-token balances
above the wallet's on-chain balance, preview the repair before stopping:

```bash
reconcile-position-balances --only BOTNAME
```

Review every proposed raw-unit haircut. Stop that bot, verify there is no
unresolved broadcast/settlement, then apply and inspect inventory before
restart:

```bash
stop-bot BOTNAME
reconcile-position-balances --only BOTNAME --apply --confirm-bot-stopped
fleet-inventory --only BOTNAME
restart-bot BOTNAME
```

`stop-bot` is durable. Guardian checks, fleet restarts, and `update-bot` keep
that bot stopped. Use `start-bot BOTNAME` or the explicit `restart-bot` above
to return it to desired-running state.

The tool proportionally reduces position balances to wallet reality, preserves
cost basis, backs up changed ledgers, and writes a reconciliation audit record.
When the haircut exactly matches the receipt-proven managed-token outflow of
the active unresolved-broadcast transaction, the matching guard is archived
automatically. Any ambiguous or nonmatching guard remains in force.
It never assigns wallet surplus or recovers an omitted buy. See the fleet README
for multi-bot partial-failure behavior and backup restoration guidance.

For an omitted gridless buy with a known transaction hash, use
`--reconcile-gridless-buy` from that bot checkout instead. After an applied,
receipt-verified reconciliation passes its ledger reload check, an
`unresolved_broadcast.json` record with the exact same hash is archived as
`unresolved_broadcast.json.reconciled.*`. Preview runs and hash mismatches leave
the trading halt in place. For a reconciliation already applied by an older
release, rerun the same applied command: it re-verifies the receipt and archives
the matching guard without duplicating the position.

## Thin-margin profit safety

Before operating at `MIN_PROFIT_PERCENT=0.1`, update and restart every bot so
all checkouts use the same audited arithmetic. A normal sell is authorized from
integer wei only when minimum executable proceeds cover upward-rounded sold
cost, upward-rounded target profit, confirmed setup gas, and the final signed
transaction's maximum gas. Provider quotes are not treated as guaranteed
proceeds: explicit minimum-output fields are preferred and otherwise configured
slippage is deducted. Successful setup gas from an aborted sell is carried into
the position for recovery on the next attempt. Unreconciled confirmed proceeds
halt the bot and never become reported realized profit.

Stoploss sells deliberately bypass profit protection. Thin-margin operation
therefore requires stoploss policy to be reviewed separately, accurate token
tax configuration, nonzero gas headroom, and healthy RPC receipt/balance reads.
See **Profit-accounting invariants** in the main README for the equations,
WETH-settlement behavior, and unavoidable on-chain risks.

## Tournament terminal and provider diagnostics

In gate mode every selected route now closes with exactly one terminal phase:
`completed`, `execution_aborted`, `execution_failed`, or
`settlement_unresolved`. The first terminal phase is immutable: a delayed
callback cannot replace it or emit a second terminal record. A confirmed trade always has a preceding
`transaction_submitted` lifecycle record; if the live callback was unavailable,
confirmation reconstructs the submission record and labels that timing as
observed-at-completion. `settlement_unresolved` is fail-closed: it means the
transaction confirmed but exact tokens/proceeds or required WETH settlement
could not be reconciled. Follow the unresolved-broadcast recovery procedure;
never treat that state as realized profit.

Failed tournament candidates expose only aggregation-safe fields:
`failure_category`, `provider_error`, `http_status`, `retry_after_seconds`,
`pair_fingerprint`, and `candidate_elapsed_ms`. The fingerprint is a stable,
non-reversible chain/direction/pair/settlement key. Provider response bodies,
request IDs, credentials, and calldata are not emitted. Use these fields to
compare failures by provider and pair before considering a scoped cooldown;
they do not themselves disable or penalize a provider.

Completed lifecycle payloads include receipt status, gas used, effective gas
price, measured token/proceeds base units, and reconciled realized profit when
available. Missing exact settlement produces `settlement_unresolved`, not
invented economics.

For performance investigations, `--analysis-sample-only` retains tournament
events plus `Bot runtime provenance` and `Bot cycle performance` records while
discarding ordinary log chatter. Verify `build_sha`, `build_dirty=false`, and a
post-deployment `process_started_utc` for every included bot before comparing
timings. Cycle phase and RPC figures are measurement data only; do not change a
deadline or provider policy until selected-route economics quantify the winners
that would be lost.
