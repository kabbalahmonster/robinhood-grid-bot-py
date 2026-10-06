# Lightning managed rounds

This profile reduces reaction latency without overlapping wallet transactions or
weakening execution accounting. It is opt-in. A code update with every new
variable absent retains the legacy completion-delay loop, synchronous shadow
observer, one tournament per action, and all existing state formats.

## Latency model

Legacy polling is `round work + POLL_INTERVAL_SECONDS`. With
`POLL_CADENCE_MODE=fixed_rate`, the interval is measured start-to-start: a
three-second round with a four-second interval sleeps one second; a seven-second
round starts the next round immediately after completion. Rounds never overlap.

`ROUTE_TOURNAMENT_SHADOW_BACKGROUND=true` removes read-only shadow collection
from the execution thread. The observer starts after the complete trading pass,
uses one daemon worker, and retains only the latest queued snapshot per direction.
It cannot select a route, approve, sign, broadcast, or mutate positions. It does
still consume provider quota, so request and 429 telemetry remain part of the
canary decision.

`ROUTE_TOURNAMENT_ROUND_ROUTE_REUSE=true` applies only to guarded gate mode with
`GRIDLESS_MULTI_ACTION_ROUNDS=true`. The first action in each direction runs a
normal tournament. Later actions in that same polling round reuse only the
winning provider/settlement identity. Each action still obtains a fresh quote
for its exact amount, performs fresh local simulation and gas-price checks, and
passes the existing tax, slippage, gas-cap, profit-floor, reserve, approval,
broadcast, receipt, balance, and position-provenance guards. A failed fresh
revalidation invalidates the hint and falls back to the ordinary fresh baseline
path; it never replays a prior transaction. The hint is discarded at every new
round and on restart.

Speculative sell fallback never waits for a second gate-sized window. At the
gate deadline, a completed fresh overlap may be reused under its existing
three-second age limit; otherwise the bot immediately starts the ordinary fresh
baseline path. The configured gate deadline is now the full coordinator budget,
without the former hidden half-second grace.

Transactions remain receipt-serialized. Pending-nonce pipelining is intentionally
out of scope because it requires a durable intent/settlement queue to avoid
duplicate or phantom position transitions.

## Safe activation order

First canary the changes that do not alter route choice:

```bash
ops/fleet/update-variable --allow-add --only BOT \
  GRIDLESS_MULTI_ACTION_ROUNDS=true \
  POLL_CADENCE_MODE=fixed_rate \
  ROUTE_TOURNAMENT_MODE=shadow \
  ROUTE_TOURNAMENT_SHADOW_BACKGROUND=true
ops/fleet/update-variable --apply --allow-add --only BOT \
  GRIDLESS_MULTI_ACTION_ROUNDS=true \
  POLL_CADENCE_MODE=fixed_rate \
  ROUTE_TOURNAMENT_MODE=shadow \
  ROUTE_TOURNAMENT_SHADOW_BACKGROUND=true
ops/fleet/restart-bot BOT
```

Gate deadline and speculative-start values must come from a fresh 24-hour
counterfactual, not timeout count alone. For a measured one-bot gate canary,
add `ROUTE_TOURNAMENT_CANARY=true`, the chosen
`ROUTE_TOURNAMENT_GATE_TIMEOUT_SECONDS`, a strictly smaller
`ROUTE_TOURNAMENT_SPECULATIVE_FALLBACK_SECONDS`, and
`ROUTE_TOURNAMENT_ROUND_ROUTE_REUSE=true`.

## Acceptance and rollback

Compare equal-duration schema-capable windows. Require lower actionable
start-to-start and gate/fallback p95 latency, no increase in unresolved
settlement or receipt failures, no economic/safety regression, complete terminal
lifecycle records, and acceptable provider request/429 volume. For route reuse,
confirm one full tournament per direction per multi-action round and one fresh
execution quote/simulation per fill.

Roll route choice back immediately while retaining fixed cadence if desired:

```bash
ops/fleet/update-variable --apply --only BOT \
  ROUTE_TOURNAMENT_MODE=off \
  ROUTE_TOURNAMENT_CANARY=false \
  ROUTE_TOURNAMENT_ROUND_ROUTE_REUSE=false \
  ROUTE_TOURNAMENT_SPECULATIVE_FALLBACK_SECONDS=0
ops/fleet/restart-bot BOT
```

Restore all legacy timing with `POLL_CADENCE_MODE=completion_delay`,
`ROUTE_TOURNAMENT_SHADOW_BACKGROUND=false`, and
`GRIDLESS_MULTI_ACTION_ROUNDS=false`.
