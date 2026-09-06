"""Read-only, bounded route observations. No quote can authorize execution.

Quotes deliberately remain quote_only: provider simulation flags and gas hints
are not proof of an exact local eth_call + eth_estimateGas. Where a provider
exposes its own ``gas`` estimate (Uniswap ``gasUseEstimate``, 0x gas budget,
Sushi gas hint), the tournament prefers that over the conservative 350k/300k
direction fallback so observed economics stay close to what execution would
actually pay. The fallback only applies when the provider's estimate is
missing or zero.

The tournament reads a **fresh** gas price per candidate via the injected
``gas_price_provider`` callable, so snapshot-time staleness cannot bias the
scoring. Existing ERC20 allowance is queried via ``allowance_probe``; when
the wallet already covers the trade amount, the approval budget is removed
from the cost stack rather than pessimistically assuming a reset.
"""

from decimal import Decimal, ROUND_CEILING
import logging
import re
import time

from swap_provider import PROVIDERS

LOG = logging.getLogger("grid_bot.route_tournament")
NATIVE = "0x" + "00" * 20

# Conservative direction-based fallback when the provider returns no gas hint.
# 350k is generous enough for a typical swap + multicall; 300k for sells.
_FALLBACK_SWAP_GAS = {"buy": 350000, "sell": 300000}
# Legacy approval budget when allowance is unknown or insufficient.
_RESET_AND_APPROVAL_GAS = 200000
_WRAP_UNWRAP_GAS = 60000


def _quote_failure_reason(provider, error):
    """Classify a provider failure without exposing response or credential data."""
    text = str(error or "").lower()
    if "shadow quote deadline" in text or "observation deadline" in text:
        return {"category": "observation_timeout", "retryable": True,
                "provider_error": "shadow_quote_deadline"}
    if "noroutefounderror" in text or "no route" in text or "no quotes available" in text:
        return {"category": "no_liquidity", "retryable": False,
                "provider_error": "NoRouteFoundError"}
    if "cooldown active" in text or "timed out" in text or "timeout" in text:
        return {"category": "transient", "retryable": True,
                "provider_error": "transient_provider_failure"}
    status = next((int(value) for value in re.findall(r"\b(\d{3})\b", text)
                   if 400 <= int(value) <= 599), None)
    if status is not None:
        retryable = status in {408, 409, 425, 429} or status >= 500
        return {"category": "transient" if retryable else "provider_rejected",
                "retryable": retryable, "provider_error": f"http_{status}"}
    if "not configured" in text:
        return {"category": "configuration", "retryable": False,
                "provider_error": "credentials_not_configured"}
    if "required" in text or "must specify" in text:
        return {"category": "invalid_request", "retryable": False,
                "provider_error": "invalid_quote_request"}
    return {"category": "provider_error", "retryable": False,
            "provider_error": f"{provider}_quote_failed"}


def _wei_to_eth(wei):
    return float(Decimal(str(wei)) / Decimal(10**18))


def _raw_to_human(raw, decimals):
    return float(Decimal(str(raw)) / Decimal(10 ** max(0, int(decimals))))


def snapshot(bot, direction, amount, sold_cost_wei=None):
    """Capture pre-operation economics without touching provider/router state.

    Note: this snapshot no longer captures ``gas_price`` for scoring — ``collect``
    reads a fresh gas price per candidate via the injected provider so that
    quote-time accuracy is preserved even if the queue ran seconds earlier.
    Snapshot still captures balances, slippage, tax, gas/price multipliers,
    reserve and cap so dashboard readers see the same economics the bot saw.
    """
    config = bot.config
    native_balance = int(bot.wallet.get_eth_balance_wei())
    result = {
        "direction": direction,
        "amount": int(amount),
        "sold_cost_wei": sold_cost_wei,
        "native_trading": bool(config.use_eth_trading),
        "native_balance": native_balance,
        "trade_balance": native_balance if config.use_eth_trading else int(bot._raw_trade_balance()),
        # Kept for dashboard payload backwards compatibility; collect() ignores
        # this and reads fresh gas price per candidate.
        "gas_price": int(bot.wallet.normal_gas_price()),
        # normal_gas_price() already incorporates the configured dynamic price
        # and freshness headroom.  Only the gas-limit multiplier remains for
        # the unit estimate below; never apply price headroom twice.
        "gas_multiplier": max(1.0, float(getattr(config, "gas_limit_multiplier", 1.05))),
        "reserve": int(Decimal(str(config.eth_gas_reserve)) * 10**18),
        "cap": int(Decimal(str(getattr(config, "max_" + direction + "_gas_eth",
                                       getattr(config, "max_swap_gas_eth", 0.00004)))) * 10**18),
        "slippage": bot._swap_slippage_fraction(),
        "tax": bot._effective_token_transfer_fee_percent() / 100 if bot._taxed_token_active() else 0,
        "min_profit": float(getattr(config, "min_profit_percent", 2.0)),
    }
    # Copy only a valid protocol family from the execution client's cache. The
    # observer gets no mutable cache reference, route, quote, or calldata.
    primary = getattr(getattr(bot, "provider", None), "primary", None)
    hint_reader = getattr(getattr(primary, "client", None), "protocol_hint_for", None)
    if callable(hint_reader):
        hints = {}
        for settlement, token in (("native", NATIVE), ("weth", config.weth_address)):
            sell_token, buy_token = ((token, config.token_address)
                                     if direction == "buy"
                                     else (config.token_address, token))
            try:
                hint = hint_reader(sell_token, buy_token)
            except Exception:
                hint = None
            if hint in {"V4", "V3", "V2"}:
                hints[settlement] = hint
        if hints:
            result["uniswap_protocol_hints"] = hints
    return result


def _probe_allowance(allowance_probe, token_address, spender_address, amount):
    """Return ``(allowance_int, error_str)``. Errors are swallowed and logged.

    ``allowance_probe`` is the test seam; production passes a callable of
    ``(token, spender) -> int`` that wraps ``wallet.check_allowance``. A
    failed probe falls back to the legacy conservative budget rather than
    blocking the tournament observation.
    """
    if allowance_probe is None or spender_address is None:
        return None, "no_probe"
    try:
        value = allowance_probe(token_address, spender_address)
        return int(value), None
    except Exception as exc:
        # Log without leaking the underlying provider/wallet error text.
        LOG.warning("Route shadow allowance probe failed; using legacy budget")
        return None, str(type(exc).__name__)


def _approval_units(direction, settlement, native_trading, allowance, amount,
                    execution_observation=None):
    """Return (gas_units, assumption_label).

    - For ``buy`` direction, no ERC20 is leaving the wallet, so no approval.
    - For sells: if allowance covers the amount, zero approval needed.
    - For sells with no probe or insufficient allowance: legacy budget.
    - The observed no-approval result from the exact live provider/settlement
      may remove the shadow-only estimate without querying or mutating state.
    """
    if execution_observation == "not_required":
        return 0, "execution_observed_no_approval"
    if direction == "buy":
        if settlement == "weth" and allowance is None:
            return _RESET_AND_APPROVAL_GAS, "reset_and_exact_approval_budget"
        return 0, "none"
    # Sells always go through an ERC20 transferFrom, so approval can apply.
    if allowance is not None and allowance >= amount:
        return 0, "existing_allowance_covers"
    return _RESET_AND_APPROVAL_GAS, "reset_and_exact_approval_budget"


def score_candidate(quote, provider, settlement, context, *, allowance_probe=None, gas_estimate=None):
    """Compare equal inputs using provider gas, fresh gas price, allowance lookup.

    ``allowance_probe`` is a callable ``(token_address, spender_address) -> int``
    used only when the provider exposes an ``allowance_target`` and the direction
    is ``sell``. For tests it may be a dict mapping ``{"value": N}`` or
    ``{"raise": Exception}`` to drive specific paths without a real wallet.
    """
    c = context
    row = {"provider": provider, "settlement": settlement,
           "validation_level": "rejected", "quoted_output_raw": None,
           "quoted_output_human": None, "gas_components_wei": {},
           "projected_total_gas_wei": None, "gas_total_eth": None,
           "output_floor_raw": None, "output_floor_human": None,
           "projected_net_score": None, "rejections": [], "execution_eligible": False}
    if not quote.success:
        failure_reason = _quote_failure_reason(provider, getattr(quote, "error", None))
        row["rejections"] = ["observation_timeout" if failure_reason["category"] == "observation_timeout"
                             else "provider_quote_failed"]
        row["failure_reason"] = failure_reason
        row["quote_failure_kind"] = {
            "no_liquidity": "no_route_or_liquidity",
            "invalid_request": "invalid_quote",
            "observation_timeout": "observation_timeout",
        }.get(failure_reason["category"], "provider_quote_failed")
        # No provider quote means there is no candidate-specific fresh gas read.
        row["gas_price_currentness"] = "unknown"
        return row
    output = int(quote.buy_amount or 0)
    row["quoted_output_raw"] = str(output)
    output_decimals = 18 if c["direction"] == "sell" else c.get("token_decimals", 18)
    row["quoted_output_human"] = _raw_to_human(output, output_decimals)
    if c["amount"] <= 0 or output <= 0 or int(quote.sell_amount or 0) != c["amount"]:
        row["rejections"] = ["invalid_quote_amounts"]
        return row
    if not (0 <= c["slippage"] < 1 and 0 <= c["tax"] < 1) or c["gas_price"] <= 0:
        row["rejections"] = ["invalid_economic_assumptions"]
        return row

    # A read-only RPC estimate of this exact quote is the freshest source. If
    # the quote is not locally estimable, retain the provider estimate and then
    # the conservative direction fallback.
    provider_gas = int(quote.gas or 0)
    local_gas = int(gas_estimate or 0)
    swap_gas = local_gas or provider_gas or _FALLBACK_SWAP_GAS[c["direction"]]

    # Allowance lookup (only meaningful for sells with a known spender).
    token_for_allowance = c.get("trade_token_address") if c["direction"] == "sell" else c.get("token_address")
    spender = getattr(quote, "allowance_target", None)
    # Wrap allowance_probe dict-shapes used by tests into callable semantics.
    def _probe_dict(token, sp):
        if isinstance(allowance_probe, dict):
            if "raise" in allowance_probe:
                raise allowance_probe["raise"]
            if "value" in allowance_probe:
                return allowance_probe["value"]
        return None
    probe = allowance_probe if callable(allowance_probe) else _probe_dict
    allowance, _probe_err = _probe_allowance(probe, token_for_allowance, spender, c["amount"])
    approval_observation = (c.get("approval_observations", {})
                             .get(f"{provider}:{settlement}"))
    approval_gas, approval_label = _approval_units(
        c["direction"], settlement, c["native_trading"], allowance, c["amount"],
        execution_observation=approval_observation,
    )

    # Wrap/unwrap accounting for native <-> WETH conversion.
    conversion = settlement == "weth" if c["native_trading"] else settlement == "native"
    wrap = conversion and ((c["direction"] == "buy") == c["native_trading"])
    unwrap = conversion and not wrap

    units = {"swap": swap_gas,
             "approval": approval_gas,
             "wrap": _WRAP_UNWRAP_GAS if wrap else 0,
             "unwrap": _WRAP_UNWRAP_GAS if unwrap else 0}
    # ``normal_gas_price`` is read immediately after each quote and already
    # includes price/freshness headroom. Provider gas-price hints can be stale
    # or use a different policy, so they must not override the live RPC value.
    gas_price = c["gas_price"]
    multiplier = Decimal(str(c["gas_multiplier"]))
    costs = {key: int((Decimal(value * gas_price) * multiplier).to_integral_value(rounding=ROUND_CEILING))
             for key, value in units.items()}
    total = sum(costs.values())
    # ``slippage`` is transaction tolerance, not a second quoted-output fee.
    # For taxed sells it already contains the transfer fee plus market buffer;
    # the live sell guard applies the transfer fee exactly once to a fresh quote.
    # Mirror that economic guard so shadow does not reject executable trades.
    if c["direction"] == "sell":
        floor = int(Decimal(output) * (1 - Decimal(str(c["tax"]))))
    else:
        floor = int(Decimal(output) * (1 - Decimal(str(c["slippage"]))) *
                    (1 - Decimal(str(c["tax"]))))
    row.update(validation_level="quote_only", preparation_dependent=True,
               gas_components_wei={key: str(value) for key, value in costs.items()},
               projected_total_gas_wei=str(total), gas_total_eth=_wei_to_eth(total),
               gas_basis=("local_estimate" if local_gas > 0 else "provider_estimate" if provider_gas > 0
                          else "conservative_direction_fallback"),
               output_floor_raw=str(floor),
               output_floor_human=_wei_to_eth(floor) if c["direction"] == "sell" else float(floor),
               slippage_fraction=c["slippage"], tax_fraction=c["tax"],
               approval_assumption=approval_label,
               provider_gas_estimate=provider_gas,
               effective_gas_price_wei=gas_price,
               gas_price_currentness="fresh", gas_price_age_seconds=0.0)
    if c["cap"] > 0 and total > c["cap"]:
        row["rejections"].append("total_gas_above_cap")
    # Native buy spend uses quote.value when present (ETH actually sent);
    # otherwise assume the full amount is sent.
    if c["direction"] == "buy" and c["native_trading"] and settlement == "native":
        spend = int(quote.value or 0) or c["amount"]
    else:
        spend = c["amount"] if c["direction"] == "buy" and c["native_trading"] else 0
    if c["native_balance"] - spend - total < c["reserve"]:
        row["rejections"].append("native_reserve")
    if c["direction"] == "buy":
        if c["trade_balance"] < c["amount"]:
            row["rejections"].append("input_balance")
        score = Decimal(floor) * 10**18 / (c["amount"] + total)
        row["score_unit"] = "output_raw_per_eth_total_cost"
    else:
        score = Decimal(floor - total)
        row["score_unit"] = "net_return_wei"
        cost = c["sold_cost_wei"]
        if cost is None or cost <= 0:
            row["rejections"].append("missing_sell_cost_basis")
        elif score < Decimal(cost) * (1 + Decimal(str(c["min_profit"])) / 100):
            row["rejections"].append("sell_profit_floor")
    row["projected_net_score"] = str(score)
    if row["rejections"]:
        row["validation_level"] = "rejected"
    return row


def collect(config, address, context, client_factory=None,
            gas_price_provider=None, allowance_probe=None, gas_estimate_provider=None,
            max_seconds=8, protocol_hints=None):
    """One get_quote per provider/settlement; never prepare, approve, or send.

    Independent client instances avoid mutating execution clients. Uniswap's
    shared limiter is intentionally respected. With routing_attempts=1, its
    explicit AMM fallback and gateway 409 retry allow at most four HTTP requests
    per candidate. Sushi makes one. No provider execution/preparation is used.

    ``gas_price_provider`` is a callable returning an int gas price (wei). When
    omitted, the snapshot's gas_price is used (less accurate — only acceptable
    for tests). Production callers should pass ``lambda: wallet.normal_gas_price()``.

    ``allowance_probe`` is a callable ``(token, spender) -> int``. When omitted,
    the legacy reset+approval budget is used for every sell candidate.
    """
    started = time.monotonic()
    deadline = started + max(0, float(max_seconds))
    rows = []
    provider_outputs = []
    candidate_index = 0
    enabled_provider_count = 1 + int(bool(getattr(config, "uniswap_api_key", "")))
    total_candidates = enabled_provider_count * 2
    for name in ("uniswap", "sushiswap"):
        if name == "uniswap" and not getattr(config, "uniswap_api_key", ""):
            continue
        try:
            client = client_factory(name) if client_factory else PROVIDERS[name].load_client_class()(config)
        except Exception:
            client = None
        for settlement, token in (("native", NATIVE), ("weth", config.weth_address)):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                rows.append({"provider": name, "settlement": settlement,
                             "validation_level": "rejected", "rejections": ["observation_deadline"],
                             "candidate_outcome": "not_sampled",
                             "quoted_output_raw": None, "quoted_output_human": None,
                             "gas_components_wei": {}, "projected_total_gas_wei": None,
                             "gas_total_eth": None, "output_floor_raw": None,
                             "output_floor_human": None, "projected_net_score": None,
                             "execution_eligible": False, "provider_gas_estimate": 0,
                             "effective_gas_price_wei": 0,
                             "approval_assumption": "none", "gas_basis": "skipped"})
                candidate_index += 1
                continue
            try:
                if client is None:
                    raise ValueError("unavailable client")
                args = dict(sell_token=token if context["direction"] == "buy" else config.token_address,
                            buy_token=config.token_address if context["direction"] == "buy" else token,
                            sell_amount=context["amount"], taker_address=address,
                            slippage_percentage=context["slippage"], apply_jitter_to_price=False)
                if name == "uniswap":
                    args["routing_attempts"] = 1
                    # Preserve one fallback probe per shadow candidate: this
                    # experiment must not consume unbounded shared API capacity.
                    args["protocol_probe_limit"] = 1
                    hint = (protocol_hints or context.get("uniswap_protocol_hints", {})).get(settlement)
                    if hint in {"V4", "V3", "V2"}:
                        args["preferred_protocol"] = hint
                # Each adapter receives its share of the remaining observation
                # budget, including any internal fallback request it makes.
                remaining_candidates = max(1, total_candidates - candidate_index)
                args["quote_timeout_seconds"] = remaining_seconds / remaining_candidates
                quote = client.get_quote(**args)
                # Read dynamic, already-normalized RPC gas after every quote.
                # A single oracle failure rejects only this candidate.
                fresh_gas_price = int(gas_price_provider()) if gas_price_provider else int(context["gas_price"])
                per_context = {**context, "gas_price": fresh_gas_price}
                try:
                    local_gas = int(gas_estimate_provider(quote)) if gas_estimate_provider else 0
                except Exception:
                    local_gas = 0
                row = score_candidate(quote, name, settlement, per_context,
                                      allowance_probe=allowance_probe, gas_estimate=local_gas)
                rows.append(row)
                # Emit one structured per-candidate log line for observability.
                result_label = "eligible" if row["validation_level"] == "quote_only" else "rejected"
                rejection = "+".join(row["rejections"]) if row["rejections"] else "-"
                LOG.info(
                    "Route tournament candidate provider=%s settlement=%s direction=%s "
                    "quoted_output=%s gas_estimate=%s gas_price_wei=%s approval_budget=%s "
                    "total_cost_wei=%s total_cost_eth=%.6f output_floor=%s score=%s result=%s reason=%s",
                    name, settlement, context["direction"],
                    row.get("quoted_output_raw", "-"),
                    row.get("provider_gas_estimate", 0),
                    row.get("effective_gas_price_wei", 0),
                    row["gas_components_wei"].get("approval", "0"),
                    row.get("projected_total_gas_wei", "0"),
                    row.get("gas_total_eth", 0.0) or 0.0,
                    row.get("output_floor_raw", "-"),
                    row.get("projected_net_score", "-"),
                    result_label, rejection,
                )
                if row["validation_level"] == "quote_only":
                    provider_outputs.append(row)
            except Exception:
                # Never publish exception text, raw provider responses, addresses,
                # calldata, request headers, or credentials in dashboard data.
                rows.append({"provider": name, "settlement": settlement,
                             "validation_level": "rejected", "rejections": ["candidate_failed"],
                             "quoted_output_raw": None, "quoted_output_human": None,
                             "gas_components_wei": {}, "projected_total_gas_wei": None,
                             "gas_total_eth": None, "output_floor_raw": None,
                             "output_floor_human": None, "projected_net_score": None,
                             "execution_eligible": False, "provider_gas_estimate": 0,
                             "effective_gas_price_wei": 0,
                             "approval_assumption": "none", "gas_basis": "skipped"})
                LOG.info(
                    "Route tournament candidate provider=%s settlement=%s direction=%s "
                    "quoted_output=- gas_estimate=0 gas_price_wei=0 approval_budget=0 "
                    "total_cost_wei=0 total_cost_eth=0.000000 output_floor=- score=- "
                    "result=rejected reason=candidate_failed",
                    name, settlement, context["direction"],
                )
            candidate_index += 1
    eligible = sorted((r for r in rows if r["validation_level"] == "quote_only"),
                      key=lambda r: Decimal(r["projected_net_score"]), reverse=True)
    winner = {key: eligible[0][key] for key in ("provider", "settlement")} if eligible else None
    runner_up_delta = None
    if len(eligible) > 1:
        runner_up_delta = str(Decimal(eligible[0]["projected_net_score"]) - Decimal(eligible[1]["projected_net_score"]))
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    result = {"mode": "shadow", "direction": context["direction"], "candidates": rows,
              "selected_hypothetical_winner": winner,
              "runner_up_delta": runner_up_delta,
              "elapsed_ms": elapsed_ms,
              "status": "hypothetical_only" if eligible else "no_eligible_candidate",
              "observation_timing": "after_execution_attempt_with_pre_operation_budget"}
    if winner:
        LOG.info(
            "Route tournament winner provider=%s settlement=%s direction=%s score=%s "
            "runner_up_delta=%s eligible=%d rejected=%d elapsed_ms=%s",
            winner["provider"], winner["settlement"], context["direction"],
            eligible[0]["projected_net_score"], runner_up_delta or "n/a",
            len(eligible), len(rows) - len(eligible), elapsed_ms,
        )
    else:
        LOG.info(
            "Route tournament winner provider=none settlement=none direction=%s "
            "score=- runner_up_delta=n/a eligible=0 rejected=%d elapsed_ms=%s",
            context["direction"], len(rows), elapsed_ms,
        )
    return result
