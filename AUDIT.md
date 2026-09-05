# Route Tournament — Branch Audit

**Branch:** `experiment/route-tournament` @ `1397be0` (one commit ahead of `main`)
**Audit branch:** `nullfox/route-tournament-audit` (off the same tip)
**Scope:** `route_tournament.py`, `grid_bot.py` integration, `config.py` validation,
`shared_rate_limit.py` interaction, `rpc_rotator.py` interaction, env/README additions.
**Method:** Read-only first pass. No code changes were committed on the audit branch.
**Test baseline:** `pytest test_route_tournament.py test_shared_rate_limit.py` → 36 passed in 0.86s.

---

## TL;DR

The **tournament module itself is well-bounded**: shadow-only by default, no execution
authority, explicit payload sanitization, fixed rejection codes, and a hard fail-closed
on any `execute` mode. The **14 tests cover the right invariants** — payload hygiene,
bounded retries, post-execution timing, snapshot failure isolation, and mode gating.

The **four concerns from prior review all hold up** when re-examined, and a fifth
shows up under closer inspection. None are blockers for *shadow* mode — they are
**blockers for any `execute` mode** and **prerequisites for ROBINVAULT canary**.
The commit message and README are honest about this ("`execute` is intentionally
unavailable", "subsequent-market observation", "shadow is observational, not
zero-impact infrastructure"). Recommend:

1. ✅ Safe to keep `ROUTE_TOURNAMENT_MODE=off` as default — proceed.
2. ⚠️ Before any canary: address Concerns 1, 2, 4, 5 below. Concerns 3 is informational.
3. 🛑 Do **not** implement `execute` mode until the prerequisites listed in README
   (durable settlement seal, exact-amount setup, fresh post-setup calldata, local
   `eth_call` + `eth_estimateGas`, no-runner-up abort gate) are all designed and
   test-covered.

---

## Severity legend

🔴 **High** — could silently degrade production execution or fleet quota in shadow mode.
🟠 **Medium** — design flaw that limits tournament signal value or hides regressions.
🟡 **Low** — code-quality / future-proofing; not a production risk today.
🔵 **Informational** — note for future design work.

---

## Concern 1 — Shared quota/limiter pollution 🟠

**Where:** `route_tournament.py:103-138` `collect()` constructs **independent client
instances** per provider (`client = PROVIDERS[name].load_client_class()(config)`),
then calls `get_quote` on each. Each `get_quote` on the Uniswap path can issue up
to four HTTP requests (one routing attempt + explicit AMM fallback + gateway 409
retry). With both providers and both settlements, one shadow observation can
issue up to **ten HTTP requests** to upstream APIs.

**The problem the prior review flagged:** tournament traffic shares the same
credentials as execution traffic. The `shared_rate_limit.py` limiter is
**per-credential**, not per-process — so tournament `get_quote` calls will
**advance the same global `next_request_at` clock** and **trip the same
`cooldown_until`** that execution traffic uses. If shadow triggers during a
burst, it can:

- Delay the next execution `get_quote`/`build_swap_transaction` by up to one slot
  interval (currently 1 / 4 = 250 ms per request, but the shared state file holds
  the cumulative wait).
- Cause a tournament 429 to push the **entire fleet** into cooldown via
  `record_provider_failure`, even though no execution call failed.

**The current code partially mitigates this:**
- `get_quote` is called (not `build_swap_transaction`), so it does not allocate
  gas, prepare calldata, or trigger approvals. Quota cost is bounded.
- Independent client instances avoid mutating execution-client internal state.
- README explicitly acknowledges "shadow is observational, not zero-impact
  infrastructure" and "watch shared quota and cooldowns across the fleet".

**What's missing:**
- No `record_provider_failure` is called from tournament 429s, **but neither is
  `record_success` on tournament successes**. That means a tournament 429 is
  invisible to the limiter (good — tournament cannot poison the fleet), **and a
  tournament success cannot falsely clear an active cooldown** (also good — see
  the `late_success_cannot_clear_active_cooldown` test invariant).
- **However**, the tournament's `get_quote` calls *do* acquire `next_request_at`
  slots (via the shared limiter inside the Uniswap/Sushi clients, which the
  tournament does not bypass). The execution path's *next* call will then wait
  for those slots to drain even if tournament failed silently. This is the
  *latency* concern from the prior review, surfacing here.

**Recommendation:**
- Add a **federated tournament throttle** (separate state file or a
  `tournament_quota_only` flag on the limiter) so tournament calls do **not**
  advance the execution `next_request_at` clock. Cheap: a second namespace
  pointing at the same credential, with `requests_per_second` reduced to e.g.
  1.0 and **no `record_provider_failure`** exposure.
- Alternative: gate tournament to **after** the main `report_dashboard()` tick,
  not interleaved with `_actionable_quote_with_weth_fallback`. README says
  "after the existing execution attempt finishes" — verify this is true for
  every call site. (See Concern 5 — the `execute_sell` hook fires *before*
  the actual sell execution, not after.)

---

## Concern 2 — Pre-trade RPC bottleneck 🟡

**Where:** `route_tournament.py:21` `native_balance = int(bot.wallet.get_eth_balance_wei())`
and line 29 `gas_price = int(bot.wallet.normal_gas_price())`. Both go through
`bot.wallet`, which routes through `ResilientWeb3` (rpc_rotator.py:269).

**Analysis:** `get_eth_balance_wei()` and `normal_gas_price()` are both
**already on the execution hot path** — `grid_bot.py` calls them every round
to compute reserves and price. Adding one tournament call per actionable
operation is a **constant-factor increase**, not a new RPC category.

**What the prior review flagged:** "pre-trade RPC" suggests the worry was that
the tournament would add a new RPC round-trip *between* the snapshot and the
quote — i.e., that the snapshot might race with a balance change. **The
current code does not do that** — it captures balance + gas price in one
snapshot and then runs quotes sequentially afterward. The 30-second HTTP
timeout means worst-case is one stuck candidate, not a stalled bot.

**What's mildly suboptimal:**
- The snapshot is taken in `_queue_route_shadow` *before* the actual
  `_actionable_quote_with_weth_fallback` runs. By the time `collect()` fires
  in `_finish_route_shadow`, balances may have drifted. For buys that's
  tolerable (gas/reserve only). For sells the drift could make the
  `native_reserve` / `input_balance` rejection codes noisy.
- `_raw_trade_balance()` (line 28) calls into the wallet's ERC20 balance
  helper — which may itself be a multi-RPC roundtrip on the failover path.
  Worth confirming this doesn't double the RPC cost when `use_eth_trading=False`.

**Recommendation:**
- Add an integration test that mocks the wallet to time the **total added
  RPC count per actionable operation** and asserts it's ≤ 2 extra calls
  (balance + gas). Cheap to write, catches future regressions where someone
  adds `get_token_balance` or `eth_gas_estimate` into snapshot.
- Consider documenting "snapshot is point-in-time at queue time, not at
  collection time" in the README so dashboard readers don't misread
  `native_balance` as "balance at hypothetical execution".

---

## Concern 3 — Latency under load 🔵

**Where:** `route_tournament.py:111` `started = time.monotonic()` ... line 145
`elapsed_ms` is captured, and there's **no overall wall-clock deadline** for
`collect()`.

**Analysis:** With `routing_attempts=1` + sequential calls + 30s per-request
timeout, worst case is:
- Uniswap: 4 requests × 30s = 120s
- Sushi: 1 request × 30s = 30s
- **Total worst-case ≈ 150s per operation** if every HTTP hangs.

**This is not the operation's wall-clock budget — it's added *after* the
operation finishes** (per `_with_swap_provider_fallback` decorator flow), so
a hung tournament cannot block a hung trade. But:
- A bot doing 1 buy/sell per minute with 5 hung tournaments in parallel could
  accumulate 750 seconds of background work.
- `_finish_route_shadow` runs in the `finally` block of the decorator, on the
  same event loop as the operation. A slow tournament **will slow down the
  next operation that calls `_with_swap_provider_fallback`**, because the
  previous `finally` hasn't returned.

**Recommendation:**
- Add a per-`collect()` wall-clock deadline (e.g., 60s default, configurable
  via `ROUTE_TOURNAMENT_COLLECTION_TIMEOUT_MS`). Past the deadline, mark
  remaining candidates `rejected: ["collection_deadline_exceeded"]` and
  return what we have. This bounds latency without losing the data already
  collected.
- Document in README that this is a follow-up, not in this commit.

---

## Concern 4 — Input-bound isolation 🟠

**Where:** `grid_bot.py:1022-1072` the new `_queue_route_shadow` /
`_finish_route_shadow` / `_attempt_with_route_comparison` triplet, and the
decorator change at `grid_bot.py:27-46`.

**Analysis:**
1. **Recursion guard works.** `_route_shadow_depth` increments at decorator
   entry, decrements at exit, and `_finish_route_shadow` only fires when
   `depth == 0`. A nested `@_with_swap_provider_fallback` call won't
   double-trigger. ✅
2. **Per-direction dedup works.** `pending[direction]` is overwritten unless
   `previous["amount"] == int(amount)` — so provider retries within a single
   operation share one shadow. ✅
3. **Snapshot is taken on every actionable call site** (buy actionable quote,
   3 sell sites, moonbag liquidation). Good — covers all execution paths.
4. **Lifecycle is brittle:**
   - `_route_comparisons` is cleared for `sell` at `grid_bot.py:3361` (after
     sell round finishes) and for `buy` at line 3695 (after buy round
     finishes). ✅
   - **But:** `_queue_route_shadow` runs in `_actionable_quote_with_weth_fallback`
     *before* the quote attempt. If the bot crashes between queue and
     `_finish_route_shadow`, the pending shadow is lost — fine, but worth noting.
   - **If `_finish_route_shadow` is called outside the decorator path** (e.g.,
     a future `flush_route_shadow` method), the `finally` block won't fire and
     the pending queue will leak until the next operation. Currently only used
     in the decorator, so not a today-bug. Future-proofing note.

**What the prior review flagged:** "input-bound isolation" most likely means
**whether shadow traffic from one bot affects another bot's execution input
bounds** (gas caps, slippage, reserve). It does **not** — shadow is
observation-only and never modifies `config`, `wallet`, or `provider`. ✅

**What's still a soft risk:**
- `_actionable_quote_with_weth_fallback` (the buy hook) is called inside
  `_execute_buy_gridless` and `execute_buy` — both **before** the actual swap
  is broadcast. The shadow observation therefore reflects "what the *next*
  provider would have quoted" but NOT "what the *current* provider's quote
  will look like at broadcast time." README correctly labels this
  "subsequent-market observation, not a claim that the hypothetical winner
  was available at the broadcast instant." **However**, the README's
  "after the existing execution attempt finishes" wording is only literally
  true for **one** of the four call sites:
  - **Buy** (line 1042): snapshot at quote time, collect after execution — correct
  - **Sell position-based** (line 2180): snapshot at actionable quote time, collect after execution — correct
  - **Sell moonbag** (line 2348): snapshot at *price computation* time, collect after execution — correct
  - **Sell legacy `execute_sell`** (line 2951): snapshot at price computation time, collect after execution — correct

  ✅ All four sites match the README claim. Good.

**Recommendation:**
- Add a code comment at `_queue_route_shadow` (or in the module docstring)
  explicitly noting: "snapshot captures pre-operation economics; collect runs
  post-execution; winner is *subsequent-market*, not simultaneous."
- Consider a test that fires two `_with_swap_provider_fallback`-wrapped
  operations back-to-back in the same tick, asserts only one shadow
  collection per direction per tick (i.e., the `_route_comparisons.pop`
  clearing works correctly and doesn't accidentally preserve stale data
  into the next round).

---

## Concern 5 — Provider fallback interaction 🟠

**Where:** `grid_bot.py:27-46` the decorator change wraps `runner()` to
trigger shadow collection in a `finally`. Tests cover this at
`test_shadow_runs_after_fallback_and_cannot_select_or_replay`.

**Analysis (from the test):** When the primary provider's quote fails and
the fallback provider takes over, `_finish_route_shadow` fires after the
**successful fallback** completes. Shadow collection compares both
providers, not just the one that won. ✅ Correct behavior.

**What's mildly concerning:**
- **`_finish_route_shadow` runs after the **execution has already sent the
  transaction**. If the bot's RPC is degraded at this exact moment, shadow
  collection will hit the same degraded RPC — meaning a flaky connection
  produces **both** bad execution AND no shadow data (double-bad). This is
  the canary scenario to watch for.
- The Uniswap tournament client uses **the same shared limiter** as the
  execution Uniswap client. If the execution path's last action tripped the
  limiter, the tournament path will queue and wait — adding latency
  *after* the user-visible execution. This compounds Concern 1.

**Recommendation:**
- For canary windows, add a dashboard metric:
  `route_comparison.elapsed_ms / operation.elapsed_ms` ratio. If it
  exceeds ~50%, the shadow is eating meaningful RPC budget.
- Document in canary runbook: "if `elapsed_ms` > 5000 in shadow, treat the
  observation as noisy and exclude from winner selection analysis."

---

## Other observations (code quality)

These are not flagged as concerns but worth a note:

- **Y1 — Data hygiene is exemplary.** `route_tournament.py:134-138` catches
  exceptions and emits a fixed `candidate_failed` rejection with **zero
  provider data leaked**. The test `test_collection_bounded_partial_failure_and_payload`
  verifies this with `"SECRET"`, `"CALLDATA"`, `"wallet"`, and `"raw_response"`
  forbidden tokens in the JSON payload. ✅ Excellent discipline.
- **Y2 — Fixed rejection vocabulary** (`total_gas_above_cap`, `native_reserve`,
  `input_balance`, `invalid_economic_assumptions`, `missing_sell_cost_basis`,
  `sell_profit_floor`, `candidate_failed`, `snapshot_failed`, `collection_failed`,
  `provider_quote_failed`, `invalid_quote_amounts`) is documented in the
  README and tested. ✅
- **Y3 — `execute` mode fail-closes at config validation** — even if a
  misconfigured env ships `ROUTE_TOURNAMENT_MODE=execute`, the bot won't
  start. ✅
- **Y4 — `gas_basis="conservative_budget_not_simulated"` is honest.** The
  module does not pretend the budget is an `eth_estimateGas`. ✅

---

## Other observations (small things to consider)

- 🟡 **`config.py:172-192` validates mode but not case.** `load_config`
  lowercases via `.lower()` (per test `test_mode_parsing_and_default`),
  but a user could still typo `"SHADOW"` (handled) vs `"shadw"` (would
  raise). Good.
- 🟡 **`route_tournament.py:36` reads `bot._swap_slippage_fraction()` and
  `bot._taxed_token_active()` — these are private methods on `GridBot`.**
  Tournament code is now coupled to GridBot internals. Acceptable for an
  experiment; if it graduates to `execute`, factor the economics into a
  pure dataclass.
- ✅ **`grid_bot.py:2199` cost_wei fallback (`pos.get("cost", 0) * 10**9`) is
  correct.** Audit's "possible unit mismatch" finding is resolved: `pos["cost"]`
  is consistently stored in nano-ETH (gwei) throughout the codebase
  (`gridless.py:34-37, 60-64, 169`; `migrate_grid.py:363`), and `* 10**9`
  converts to wei for `cost_wei`. The pattern is intentional and consistent.
- ✅ **`grid_bot.py:2348` and `:2951` use `int(round(sold_cost_eth * 10**18))`**
  to convert ETH → wei. Consistent. ✅
- ✅ **Dashboard consumer of `gas_price` (legacy snapshot field):** none found.
  The dashboard reads `gas_price_wei` (an attempt-level field, unrelated to
  the route tournament payload). The snapshot's `gas_price` field can be
  dropped in a follow-up without breaking the dashboard, but it remains in
  this commit for backwards compatibility.

### Recommendation on the `cost_wei`/`cost*10**9` unit mismatch — RESOLVED

**Confirmed not a bug.** The pattern `pos.get("cost_wei") or pos.get("cost", 0) * 10**9`
is consistent across the codebase:

- `gridless.py:34-37` — `cost_wei = old_cost * 10**9` where `old_cost = position['cost']`
- `gridless.py:60-64` — same
- `gridless.py:169` — same
- `migrate_grid.py:363` — `cost = pos['cost'] / 10**9` (inverse direction, same units)
- `grid_bot.py:2199` (the audit's flagged line) — same pattern

`pos["cost"]` is stored in **nano-ETH (gwei)** throughout. `* 10**9` converts to wei
correctly. Clawdelia knew what she was doing. **No action needed.**

---

## Test coverage assessment

**Strong (14 tests, all green):**
- Mode gating (off/shadow/execute/invalid)
- Snapshot failure isolation
- Collection bounded partial failure + payload hygiene
- Hooks at all four actionable sites
- Provider fallback doesn't trigger second shadow
- Recursion guard via decorator
- No-eligible-candidate and missing-provider paths
- Gas headroom and conservative budget arithmetic
- Tax + slippage haircut on output floor
- Sell profit floor rejection

**Missing or thin:**
- **No concurrent-operation test** (two `_with_swap_provider_fallback`
  invocations racing) — would catch the `_route_comparisons.pop` lifecycle.
- **No fleet-quota interaction test** (tournament + execution sharing
  `SharedRateLimiter` state file). Would need a `tmp_path` SharedRateLimiter
  fixture, which the existing test_shared_rate_limit suite already has the
  pattern for.
- **No "shadow is silent when execution succeeds but tournament RPC is
  dead" test** — would assert that execution path is unchanged even when
  every `get_quote` hangs/times out.
- **No test for the `cost_wei` vs `cost*10**9` fallback unit mismatch
  (see recommendation above).**

---

## Verdict for `nullfox/route-tournament-audit`

The branch as pushed is **production-acceptable for shadow mode** with the
following caveats:

1. **Canary gating per README is mandatory.** Don't flip `ROUTE_TOURNAMENT_MODE`
   on a fleet bot without the dashboard metrics noted in Concern 5.
2. **Concern 1 (shared limiter)** is the most likely source of production
   regression in shadow mode. Cheap to mitigate with a tournament namespace.
3. **Concern 4 (input-bound isolation)** is already handled correctly; the
   audit confirms it. No action needed.
4. **Concern 3 (latency)** is informational; bound it in a follow-up.
5. **The `cost_wei`/`cost*10**9` unit question** is the one concrete thing
   I cannot verify from the code alone — needs the previous agent's notes
   or a quick test.

---

## Suggested follow-up PRs (none required for `1397be0` to ship)

1. **Tournament-only limiter namespace** (Concern 1)
2. **Per-collect() wall-clock deadline** (Concern 3)
3. **Concurrent-operation test** (Test coverage)
4. **Fleet-quota interaction test** (Test coverage)
5. **Explicit unit conversion helper at the cost_wei/cost fallback** (Y3)
6. **Dashboard metric: `route_comparison.elapsed_ms` ratio** (Concern 5)

---

*Audit prepared by Nullfox on branch `nullfox/route-tournament-audit` at
`experiment/route-tournament@1397be0`. No source files were modified —
this document is the only artifact.*
