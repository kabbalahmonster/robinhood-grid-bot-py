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

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from swap_provider import PROVIDERS
from zero_x import QuoteResult

LOG = logging.getLogger("grid_bot.route_tournament")
NATIVE = "0x" + "00" * 20

# Conservative direction-based fallback when the provider returns no gas hint.
# 350k is generous enough for a typical swap + multicall; 300k for sells.
_FALLBACK_SWAP_GAS = {"buy": 350000, "sell": 300000}
# Legacy approval budget when allowance is unknown or insufficient.
_RESET_AND_APPROVAL_GAS = 200000
_WRAP_UNWRAP_GAS = 60000
_DEFAULT_EXECUTION_CANDIDATES = frozenset({
    ("uniswap", "native"),
    ("uniswap", "weth"),
    ("sushiswap", "native"),
    ("sushiswap", "weth"),
})
_SUPPORTED_EXECUTION_CANDIDATES = _DEFAULT_EXECUTION_CANDIDATES | frozenset({
    ("umbra", "native"), ("umbra", "weth"),
    ("lifi", "native"), ("lifi", "weth"),
})


def _configured_identities(config):
    providers = tuple(getattr(config, "route_tournament_providers", ("uniswap", "sushiswap")))
    settlements = tuple(getattr(config, "route_tournament_settlements", ("native", "weth")))
    return [(provider, settlement) for provider in providers for settlement in settlements]


def _comparison_identities(comparison):
    configured = comparison.get("expected_candidates") if isinstance(comparison, dict) else None
    if not isinstance(configured, list) or not configured:
        return _DEFAULT_EXECUTION_CANDIDATES
    identities = set()
    for item in configured:
        if not isinstance(item, dict):
            return frozenset()
        identity = (item.get("provider"), item.get("settlement"))
        if identity not in _SUPPORTED_EXECUTION_CANDIDATES or identity in identities:
            return frozenset()
        identities.add(identity)
    return frozenset(identities)


def select_execution_candidate(comparison, direction):
    """Return the best complete pre-execution candidate, or ``None``.

    This intentionally consumes only already-collected, sanitized economics.
    It does not quote, prepare, probe allowance, approve, sign, or broadcast.
    A future execution gate must re-quote and revalidate the returned identity
    before any state-changing step.
    """
    if (not isinstance(comparison, dict)
            or direction not in {"buy", "sell"}
            or comparison.get("mode") != "execution_preflight"
            or comparison.get("direction") != direction
            or comparison.get("candidate_accounting_complete") is not True):
        return None
    expected_identities = _comparison_identities(comparison)
    rows = comparison.get("candidates")
    if not expected_identities or not isinstance(rows, list) or len(rows) != len(expected_identities):
        return None
    identities = set()
    eligible = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        identity = (row.get("provider"), row.get("settlement"))
        if identity not in expected_identities or identity in identities:
            return None
        identities.add(identity)
        rejections = row.get("rejections")
        if not isinstance(rejections, list):
            return None
        validation_level = row.get("validation_level")
        if validation_level == "rejected":
            # Every losing path must have an explicit reason, but does not
            # disqualify a separately eligible route.
            if not rejections:
                return None
            continue
        if validation_level != "quote_only" or rejections:
            return None
        # A quote-only row is timely because collect() converts every late
        # quote/economic result into a rejected observation_timeout row.
        try:
            score = Decimal(str(row["projected_net_score"]))
        except (KeyError, InvalidOperation, ValueError):
            return None
        if not score.is_finite():
            return None
        eligible[identity] = (score, row)
    if identities != expected_identities or not eligible:
        return None
    provider, settlement = max(eligible, key=lambda identity: eligible[identity][0])
    selection = {"provider": provider, "settlement": settlement}
    protocol = eligible[(provider, settlement)][1].get("protocol")
    if provider == "uniswap" and protocol in {"V4", "V3", "V2"}:
        selection["protocol"] = protocol
    if eligible[(provider, settlement)][1].get("staged_weth_buy") is True:
        selection["staged_weth_buy"] = True
        target = eligible[(provider, settlement)][1].get("allowance_target")
        if target:
            selection["allowance_target"] = target
    if eligible[(provider, settlement)][1].get("staged_approval_required") is True:
        selection["staged_approval_required"] = True
        target = eligible[(provider, settlement)][1].get("allowance_target")
        if target:
            selection["allowance_target"] = target
    return selection


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
        if value is None:
            return None, "unknown"
        return int(value), None
    except Exception as exc:
        # Log without leaking the underlying provider/wallet error text.
        LOG.warning("Route shadow allowance probe failed; using legacy budget")
        return None, str(type(exc).__name__)


def _approval_units(direction, settlement, native_trading, allowance, amount):
    """Return (gas_units, assumption_label).

    - For ``buy`` direction, no ERC20 is leaving the wallet, so no approval.
    - For sells: if allowance covers the amount, zero approval needed.
    - For sells with no probe or insufficient allowance: legacy budget.
    - The original tournament also budgeted approval for ``buy + weth``
      (WETH being spent on-router); when the probe is unavailable, keep
      that conservative legacy default.
    """
    if direction == "buy":
        if settlement == "weth" and (allowance is None or allowance < amount):
            return _RESET_AND_APPROVAL_GAS, "reset_and_exact_approval_budget"
        return 0, "none"
    # Sells always go through an ERC20 transferFrom, so approval can apply.
    if allowance is not None and allowance >= amount:
        return 0, "existing_allowance_covers"
    return _RESET_AND_APPROVAL_GAS, "reset_and_exact_approval_budget"


def score_candidate(quote, provider, settlement, context, *, allowance_probe=None,
                    gas_estimate=None, conversion_gas_limit=None,
                    approval_gas_limit=None, deadline=None,
                    require_local_gas=False, require_dynamic_setup_gas=False,
                    staged_weth_buy=False):
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
    if staged_weth_buy and provider_gas <= 0:
        row["rejections"] = ["provider_swap_gas_estimate_missing"]
        row["gas_basis"] = "staged_setup_requires_provider_swap_estimate"
        return row
    # Allowance lookup is for the actual asset being sold. Settlement only
    # determines wrap/unwrap economics; it is never the sell-token approval.
    token_for_allowance = c.get("token_address") if c["direction"] == "sell" else c.get("token_address")
    spender = getattr(quote, "allowance_target", None)
    if deadline is not None and time.monotonic() >= deadline:
        return score_candidate(QuoteResult(success=False, error="shadow quote deadline elapsed"),
                               provider, settlement, c)
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
    if deadline is not None and time.monotonic() >= deadline:
        return score_candidate(QuoteResult(success=False, error="shadow quote deadline elapsed"),
                               provider, settlement, c)
    approval_gas, approval_label = _approval_units(c["direction"], settlement,
                                                    c["native_trading"], allowance, c["amount"])
    if approval_gas and approval_gas_limit:
        approval_gas = int(approval_gas_limit)
        approval_label = "dynamic_local_approval_estimate"
    # LI.FI and Umbra sell calldata necessarily reverts during eth_estimateGas
    # until their spender can transfer the token. Keep such a route in the
    # tournament only as an explicitly staged candidate: its provisional score
    # uses the provider/conservative swap budget plus a *locally estimated*
    # approval transaction. Provider gas can rank it, but can never authorize
    # the eventual swap; the winner is approved, rebuilt and locally simulated
    # at the execution boundary.
    staged_approval = bool(
        require_local_gas and c["direction"] == "sell"
        and provider in {"lifi", "umbra"} and spender
        and allowance is not None and allowance < c["amount"]
        and approval_gas > 0
        and local_gas <= 0 and approval_gas_limit
    )
    if (require_dynamic_setup_gas and c["direction"] == "sell"
            and provider in {"lifi", "umbra"} and spender
            and allowance is not None and allowance < c["amount"]
            and approval_gas and not approval_gas_limit):
        row["rejections"] = ["approval_required_before_local_simulation"]
        row["approval_assumption"] = approval_label
        row["gas_basis"] = "dynamic_setup_required"
        return row
    if require_local_gas and local_gas <= 0 and not staged_weth_buy and not staged_approval:
        row["rejections"] = ["local_gas_simulation_failed"]
        row["provider_gas_estimate"] = provider_gas
        row["gas_basis"] = "local_simulation_required"
        return row
    if require_dynamic_setup_gas and approval_gas and not approval_gas_limit:
        row["rejections"] = ["approval_required_before_local_simulation"]
        row["approval_assumption"] = approval_label
        row["gas_basis"] = "dynamic_setup_required"
        return row
    swap_gas = local_gas or provider_gas or _FALLBACK_SWAP_GAS[c["direction"]]

    # Wrap/unwrap accounting for native <-> WETH conversion.
    conversion = settlement == "weth" if c["native_trading"] else settlement == "native"
    wrap = conversion and ((c["direction"] == "buy") == c["native_trading"])
    unwrap = conversion and not wrap

    dynamic_conversion_gas = int(conversion_gas_limit or 0)
    if require_dynamic_setup_gas and conversion and dynamic_conversion_gas <= 0:
        row["rejections"] = ["local_conversion_gas_simulation_failed"]
        row["gas_basis"] = "dynamic_setup_required"
        return row

    units = {"swap": swap_gas,
             "approval": approval_gas,
             "wrap": (dynamic_conversion_gas or _WRAP_UNWRAP_GAS) if wrap else 0,
             "unwrap": (dynamic_conversion_gas or _WRAP_UNWRAP_GAS) if unwrap else 0}
    # ``normal_gas_price`` is read immediately after each quote and already
    # includes price/freshness headroom. Provider gas-price hints can be stale
    # or use a different policy, so they must not override the live RPC value.
    gas_price = c["gas_price"]
    multiplier = Decimal(str(c["gas_multiplier"]))
    if c.get("execution_preflight") is True:
        # Match _swap_gas_fields(): normal execution uses Python's float
        # multiplication and truncates gas-limit headroom before gas pricing.
        normal_multiplier = float(c["gas_multiplier"])
        costs = {
            key: int(value * normal_multiplier) * gas_price
            for key, value in units.items()
        }
        # The wallet's wrap/unwrap builder already applied gas-limit headroom
        # to its fresh local estimate. Do not multiply that final limit twice.
        if dynamic_conversion_gas > 0:
            costs["wrap"] = units["wrap"] * gas_price
            costs["unwrap"] = units["unwrap"] * gas_price
    else:
        costs = {key: int((Decimal(value * gas_price) * multiplier).to_integral_value(rounding=ROUND_CEILING))
                 for key, value in units.items()}
    total = sum(costs.values())
    # A normal sell applies its profit and hard-cap guard to the executable swap
    # before it knows whether an approval transaction is actually needed. It
    # then charges the confirmed setup fee in the final post-approval guard.
    # Execution preflight must mirror that two-stage standard: retaining an
    # unknown allowance budget in telemetry is conservative, but using it to
    # veto route authority would reject a sell normal execution is allowed to
    # prepare and validate. Shadow remains fully hypothetical and retains its
    # all-in approval estimate for comparative reporting.
    preapproval_total = total
    if c["direction"] == "sell" and c.get("execution_preflight") is True:
        preapproval_total -= costs["approval"]
    # ``slippage`` is transaction tolerance, not a second quoted-output fee.
    # For taxed sells it already contains the transfer fee plus market buffer;
    # the live sell guard applies the transfer fee exactly once to a fresh quote.
    # Mirror that economic guard so shadow does not reject executable trades.
    effective_tax = 0 if getattr(quote, "output_includes_transfer_tax", False) else c["tax"]
    if c["direction"] == "sell":
        if c.get("execution_preflight") is True:
            # Match _taxed_quote_return_wei() exactly at the authorization
            # boundary, including its established float-to-int rounding.
            floor = int(output * (1.0 - float(effective_tax)))
        else:
            floor = int(Decimal(output) * (1 - Decimal(str(effective_tax))))
    else:
        effective_slippage = 0 if getattr(quote, "output_is_execution_floor", False) else c["slippage"]
        floor = int(Decimal(output) * (1 - Decimal(str(effective_slippage))) *
                    (1 - Decimal(str(effective_tax))))
    row.update(validation_level="quote_only", preparation_dependent=True,
               gas_components_wei={key: str(value) for key, value in costs.items()},
               approval_budget_wei=str(costs["approval"]),
               projected_total_gas_wei=str(total),
               preapproval_total_gas_wei=str(preapproval_total),
               gas_total_eth=_wei_to_eth(total),
               gas_basis=("local_estimate" if local_gas > 0 else "provider_estimate" if provider_gas > 0
                          else "conservative_direction_fallback"),
               output_floor_raw=str(floor),
               output_floor_human=(
                   _wei_to_eth(floor) if c["direction"] == "sell"
                   else _raw_to_human(floor, output_decimals)
               ),
               slippage_fraction=c["slippage"], tax_fraction=effective_tax,
               approval_assumption=approval_label,
               provider_gas_estimate=provider_gas,
               effective_gas_price_wei=gas_price,
               gas_price_currentness="fresh", gas_price_age_seconds=0.0)
    if staged_weth_buy:
        row["staged_weth_buy"] = True
        row["allowance_target"] = getattr(quote, "allowance_target", None)
        row["gas_basis"] = "provider_estimate_pending_post_setup_local_simulation"
    if staged_approval:
        row["staged_approval_required"] = True
        row["allowance_target"] = spender
        row["candidate_state"] = "approval_required"
        row["gas_basis"] = (
            "provisional_provider_gas_pending_post_approval_local_simulation"
            if provider_gas > 0
            else "provisional_conservative_gas_pending_post_approval_local_simulation"
        )
    protocol = getattr(quote, "protocol_hint", None)
    if provider == "uniswap" and protocol in {"V4", "V3", "V2"}:
        row["protocol"] = protocol
    # WETH fallback sells skip the sell hard-cap check in normal execution; the
    # subsequent unwrap has its own native-reserve guard. Match that authority
    # boundary during execution preflight rather than rejecting either the swap
    # or unwrap cost against the ordinary sell cap.
    cap_total = preapproval_total
    skip_sell_cap = (c["direction"] == "sell" and c.get("execution_preflight") is True
                     and settlement == "weth")
    if c["cap"] > 0 and not skip_sell_cap and cap_total > c["cap"]:
        row["rejections"].append("total_gas_above_cap")
    # Native buy spend uses quote.value when present (ETH actually sent);
    # otherwise assume the full amount is sent.
    if c["direction"] == "buy" and c["native_trading"] and settlement == "native":
        spend = int(quote.value or 0) or c["amount"]
    else:
        spend = c["amount"] if c["direction"] == "buy" and c["native_trading"] else 0
    # ETH_GAS_RESERVE is excluded while sizing a buy, not an untouchable floor
    # afterwards: an authorized buy may consume it for gas.  It still must fund
    # native input plus all projected transaction gas.  Passive sell observation
    # retains its reserve diagnostic; live sell execution has its own guard.
    if c["direction"] == "buy":
        if c["native_balance"] - spend - total < 0:
            row["rejections"].append("native_reserve")
    elif c.get("execution_preflight") is not True and c["native_balance"] - total < c["reserve"]:
        row["rejections"].append("native_reserve")
    if c["direction"] == "buy":
        if c["trade_balance"] < c["amount"]:
            row["rejections"].append("input_balance")
        score = Decimal(floor) * 10**18 / (c["amount"] + total)
        row["score_unit"] = "output_raw_per_eth_total_cost"
    else:
        # Tournament ranking is all-in: setup/conversion costs cannot be
        # omitted merely because normal execution validates them in stages.
        # The winner must be the route with the highest output after every
        # projected gas component, not just the cheapest swap transaction.
        score = Decimal(floor - total)
        row["score_unit"] = "net_return_after_all_projected_gas_wei"
        cost = c["sold_cost_wei"]
        if cost is None or cost <= 0:
            row["rejections"].append("missing_sell_cost_basis")
        else:
            minimum = Decimal(cost) * (1 + Decimal(str(c["min_profit"])) / 100)
            profit = score - Decimal(cost)
            row.update(
                sold_cost_wei=str(cost),
                projected_profit_wei=str(int(profit)),
                projected_profit_eth=_wei_to_eth(profit),
                projected_profit_percent=float(profit * 100 / Decimal(cost)),
                minimum_return_wei=str(int(minimum)),
                minimum_return_eth=_wei_to_eth(minimum),
                minimum_profit_percent=float(c["min_profit"]),
            )
            if score < minimum:
                row["rejections"].append("sell_profit_floor")
    row["projected_net_score"] = str(score)
    if row["rejections"]:
        row["validation_level"] = "rejected"
    return row


def collect(config, address, context, client_factory=None,
            gas_price_provider=None, allowance_probe=None, gas_estimate_provider=None,
            conversion_gas_estimate_provider=None, approval_gas_estimate_provider=None,
            max_seconds=8,
            protocol_hints=None, mode="shadow", _candidate_filter=None,
            _suppress_summary=False):
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
    configured_identities = _configured_identities(config)
    total_candidates = 1 if _candidate_filter else len(configured_identities)

    def deadline_row(name, settlement):
        return score_candidate(
            QuoteResult(success=False, error="shadow quote deadline elapsed"),
            name, settlement, context,
        )

    configured_providers = tuple(dict.fromkeys(provider for provider, _ in configured_identities))
    configured_settlements = tuple(dict.fromkeys(settlement for _, settlement in configured_identities))
    for name in configured_providers:
        if name == "uniswap" and not getattr(config, "uniswap_api_key", ""):
            continue
        # A parallel execution-preflight worker owns exactly one identity.
        # Do not construct/log clients for the other provider before skipping.
        if _candidate_filter and name != _candidate_filter[0]:
            continue
        try:
            client = client_factory(name) if client_factory else PROVIDERS[name].load_client_class()(config)
        except Exception:
            client = None
        for settlement in configured_settlements:
            token = NATIVE if settlement == "native" else config.weth_address
            if _candidate_filter and (name, settlement) != _candidate_filter:
                continue
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
                # Execution preflight needs a second bounded read-only /swap
                # request for both providers. A price quote or provider gas hint
                # cannot authorize or accurately rank an executable route.
                request_slots = remaining_candidates + int(
                    mode == "execution_preflight"
                )
                args["quote_timeout_seconds"] = remaining_seconds / request_slots
                quote = client.get_quote(**args)
                staged_weth_buy = bool(
                    mode == "execution_preflight"
                    and context["direction"] == "buy"
                    and settlement == "weth"
                )
                # Provider /quote responses can be indicative and their gas
                # hints are not eligible for execution-gate economics. Prepare
                # a read-only /swap artifact inside the absolute deadline so
                # every candidate can be locally simulated and compared.
                if (mode == "execution_preflight" and quote.success
                        and (not getattr(quote, "to", None) or not getattr(quote, "data", None))):
                    remaining_preparation = deadline - time.monotonic()
                    if remaining_preparation <= 0:
                        row = deadline_row(name, settlement)
                    else:
                        # Reserve an equal share of the absolute budget for every
                        # remaining candidate. Preparation must not consume the
                        # time needed to account for the other three routes.
                        preparation_budget = min(
                            remaining_preparation / remaining_candidates,
                            max(0.05, float(max_seconds) / 2),
                        )
                        if name == "uniswap":
                            quote = client.get_swap_transaction(
                                quote.raw_response,
                                quote_timeout_seconds=preparation_budget,
                                **({"simulate_transaction": False} if staged_weth_buy else {}),
                            )
                        else:
                            quote = client.get_swap_transaction(
                                quote,
                                sell_token=args["sell_token"],
                                buy_token=args["buy_token"],
                                sell_amount=args["sell_amount"],
                                taker_address=args["taker_address"],
                                slippage_percentage=args["slippage_percentage"],
                                quote_timeout_seconds=preparation_budget,
                                **({"simulate_transaction": False} if staged_weth_buy else {}),
                            )
                        if name == "uniswap":
                            protocol_reader = getattr(client, "protocol_hint_for", None)
                            protocol = protocol_reader(
                                args["sell_token"], args["buy_token"]
                            ) if callable(protocol_reader) else None
                            if protocol in {"V4", "V3", "V2"}:
                                setattr(quote, "protocol_hint", protocol)
                        row = None
                else:
                    row = None
                # A provider can return after its requested socket timeout.
                # Late economics must never become a tournament winner or trigger
                # post-deadline RPC gas/allowance work.
                if row is not None:
                    pass
                elif time.monotonic() >= deadline:
                    row = deadline_row(name, settlement)
                else:
                    # Read dynamic, already-normalized RPC gas after every quote.
                    # A single oracle failure rejects only this candidate.
                    fresh_gas_price = int(gas_price_provider()) if gas_price_provider else int(context["gas_price"])
                    if time.monotonic() >= deadline:
                        row = deadline_row(name, settlement)
                    else:
                        per_context = {**context, "gas_price": fresh_gas_price}
                        try:
                            local_gas = int(gas_estimate_provider(quote, settlement)) if gas_estimate_provider else 0
                        except Exception:
                            local_gas = 0
                        try:
                            conversion_gas = int(conversion_gas_estimate_provider(
                                quote, settlement
                            )) if conversion_gas_estimate_provider else 0
                        except Exception:
                            conversion_gas = 0
                        try:
                            # Internal-only identity used to build the same
                            # approval amount execution will send (Umbra exact,
                            # LI.FI reusable). It is never exposed in telemetry.
                            setattr(quote, "_tournament_provider", name)
                            approval_gas = int(approval_gas_estimate_provider(
                                quote, settlement
                            )) if approval_gas_estimate_provider else 0
                        except Exception:
                            approval_gas = 0
                        if time.monotonic() >= deadline:
                            row = deadline_row(name, settlement)
                        else:
                            row = score_candidate(
                                quote, name, settlement, per_context,
                                allowance_probe=allowance_probe, gas_estimate=local_gas,
                                conversion_gas_limit=conversion_gas,
                                approval_gas_limit=approval_gas,
                                deadline=deadline,
                                require_local_gas=(mode == "execution_preflight"),
                                require_dynamic_setup_gas=(mode == "execution_preflight"),
                                staged_weth_buy=staged_weth_buy,
                            )
                            if time.monotonic() >= deadline:
                                row = deadline_row(name, settlement)
                rows.append(row)
                # Emit one structured per-candidate log line for observability.
                result_label = "eligible" if row["validation_level"] == "quote_only" else "rejected"
                rejection = "+".join(row["rejections"]) if row["rejections"] else "-"
                if context["direction"] == "sell" and row.get("projected_profit_percent") is not None:
                    LOG.info(
                        "Route tournament candidate ⚔️ %s/%s: net %.6f ETH (%+.2f%%) · minimum %.6f ETH (%.2f%%) · gas %.6f ETH · %s%s",
                        name, settlement, Decimal(row["projected_net_score"]) / Decimal(10**18),
                        row["projected_profit_percent"], row["minimum_return_eth"],
                        row["minimum_profit_percent"], row.get("gas_total_eth", 0.0) or 0.0,
                        result_label, " · " + rejection if rejection != "-" else "",
                    )
                else:
                    LOG.info(
                        "Route tournament candidate ⚔️ %s/%s: output %s · gas %.6f ETH · %s%s",
                        name, settlement, row.get("quoted_output_human", "-"),
                        row.get("gas_total_eth", 0.0) or 0.0, result_label,
                        " · " + rejection if rejection != "-" else "",
                    )
                if row["validation_level"] == "quote_only":
                    provider_outputs.append(row)
            except Exception:
                # Never publish exception text, raw provider responses, addresses,
                # calldata, request headers, or credentials in dashboard data.
                if time.monotonic() >= deadline:
                    rows.append(deadline_row(name, settlement))
                    failure = "observation_timeout"
                else:
                    rows.append({"provider": name, "settlement": settlement,
                                 "validation_level": "rejected", "rejections": ["candidate_failed"],
                                 "quoted_output_raw": None, "quoted_output_human": None,
                                 "gas_components_wei": {}, "projected_total_gas_wei": None,
                                 "gas_total_eth": None, "output_floor_raw": None,
                                 "output_floor_human": None, "projected_net_score": None,
                                 "execution_eligible": False, "provider_gas_estimate": 0,
                                 "effective_gas_price_wei": 0,
                                 "approval_assumption": "none", "gas_basis": "skipped"})
                    failure = "candidate_failed"
                LOG.info(
                    "Route tournament candidate provider=%s settlement=%s direction=%s "
                    "quoted_output=- gas_estimate=0 gas_price_wei=0 approval_budget=0 "
                    "total_cost_wei=0 total_cost_eth=0.000000 output_floor=- score=- "
                    "result=rejected reason=%s",
                    name, settlement, context["direction"], failure,
                )
            candidate_index += 1
    eligible = sorted((r for r in rows if r["validation_level"] == "quote_only"),
                      key=lambda r: Decimal(r["projected_net_score"]), reverse=True)
    winner = {key: eligible[0][key] for key in ("provider", "settlement")} if eligible else None
    runner_up_delta = None
    if len(eligible) > 1:
        runner_up_delta = str(Decimal(eligible[0]["projected_net_score"]) - Decimal(eligible[1]["projected_net_score"]))
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    expected_identities = {
        identity for identity in configured_identities
        if identity[0] != "uniswap" or getattr(config, "uniswap_api_key", "")
        if not _candidate_filter or identity == _candidate_filter
    }
    observed_identities = {
        (row.get("provider"), row.get("settlement"))
        for row in rows if isinstance(row, dict)
    }
    candidate_accounting_complete = (
        len(rows) == len(expected_identities)
        and observed_identities == expected_identities
    )
    deadline_met = time.monotonic() < deadline
    result = {"mode": mode, "direction": context["direction"], "candidates": rows,
              "expected_candidates": [
                  {"provider": provider, "settlement": settlement}
                  for provider, settlement in sorted(expected_identities)
              ],
              "observed_candidates": [
                  {"provider": provider, "settlement": settlement}
                  for provider, settlement in sorted(observed_identities)
              ],
              "candidate_accounting_complete": candidate_accounting_complete,
              "deadline_met": deadline_met,
              "selected_hypothetical_winner": winner,
              "runner_up_delta": runner_up_delta,
              "elapsed_ms": elapsed_ms,
              "status": "hypothetical_only" if eligible else "no_eligible_candidate",
              "observation_timing": "after_execution_attempt_with_pre_operation_budget"}
    if winner and not _suppress_summary:
        LOG.info(
            "Route tournament winner provider=%s settlement=%s direction=%s score=%s "
            "runner_up_delta=%s eligible=%d rejected=%d elapsed_ms=%s",
            winner["provider"], winner["settlement"], context["direction"],
            eligible[0]["projected_net_score"], runner_up_delta or "n/a",
            len(eligible), len(rows) - len(eligible), elapsed_ms,
        )
    elif not _suppress_summary:
        LOG.info(
            "Route tournament winner provider=none settlement=none direction=%s "
            "score=- runner_up_delta=n/a eligible=0 rejected=%d elapsed_ms=%s",
            context["direction"], len(rows), elapsed_ms,
        )
    return result


def collect_execution_preflight(config, address, context, client_factory=None,
                                gas_price_provider=None, allowance_probe=None,
                                gas_estimate_provider=None,
                                conversion_gas_estimate_provider=None, max_seconds=4,
                                approval_gas_estimate_provider=None,
                                protocol_hints=None):
    """Collect a complete, bounded read-only comparison for a future gate.

    Unlike shadow telemetry, execution preflight has no useful partial result:
    both providers must be configured so all four provider/settlement candidates
    can be collected. This helper remains non-signing and non-broadcasting. In
    execution-preflight only, an indicative Uniswap quote may be read-only
    prepared with ``simulateTransaction`` to obtain calldata for local gas
    estimation; approval and all state-changing work remain outside it.
    """
    identities = _configured_identities(config)
    configured_providers = {provider for provider, _ in identities}
    if "uniswap" in configured_providers and not getattr(config, "uniswap_api_key", ""):
        return {
            "mode": "execution_preflight", "direction": context.get("direction"),
            "candidates": [], "expected_candidates": [
                {"provider": provider, "settlement": settlement}
                for provider, settlement in sorted(_DEFAULT_EXECUTION_CANDIDATES)
            ],
            "observed_candidates": [], "candidate_accounting_complete": False,
            "deadline_met": False, "selected_hypothetical_winner": None,
            "selected_execution_candidate": None,
            "runner_up_delta": None, "status": "required_provider_unavailable",
            "failures": ["uniswap_unavailable"],
        }
    if not callable(gas_estimate_provider):
        return {
            "mode": "execution_preflight", "direction": context.get("direction"),
            "candidates": [], "expected_candidates": [
                {"provider": provider, "settlement": settlement}
                for provider, settlement in sorted(_DEFAULT_EXECUTION_CANDIDATES)
            ],
            "observed_candidates": [], "candidate_accounting_complete": False,
            "deadline_met": False, "selected_hypothetical_winner": None,
            "selected_execution_candidate": None,
            "runner_up_delta": None,
            "status": "required_local_gas_estimator_unavailable",
            "failures": ["local_gas_estimator_unavailable"],
        }
    # Run each provider/settlement identity in its own worker with the same
    # absolute-sized budget. A slow Uniswap preparation can therefore never
    # consume Sushi's opportunity to produce an executable candidate.
    started = time.monotonic()

    def collect_one(identity):
        return collect(
            config, address, context, client_factory=client_factory,
            gas_price_provider=gas_price_provider, allowance_probe=allowance_probe,
            gas_estimate_provider=gas_estimate_provider, max_seconds=max_seconds,
            conversion_gas_estimate_provider=conversion_gas_estimate_provider,
            approval_gas_estimate_provider=approval_gas_estimate_provider,
            protocol_hints=protocol_hints, mode="execution_preflight",
            _candidate_filter=identity, _suppress_summary=True,
        )

    results = {}
    pool = ThreadPoolExecutor(max_workers=len(identities), thread_name_prefix="route-preflight")
    futures = {pool.submit(collect_one, identity): identity for identity in identities}
    try:
        for future in as_completed(futures, timeout=max(0.1, float(max_seconds) + 0.5)):
            identity = futures[future]
            try:
                partial = future.result()
                if partial.get("candidates"):
                    results[identity] = partial["candidates"][0]
            except Exception:
                pass
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    rows = []
    for provider, settlement in identities:
        row = results.get((provider, settlement))
        if row is None:
            row = score_candidate(
                QuoteResult(success=False, error="observation deadline elapsed"),
                provider, settlement, context,
            )
        rows.append(row)
    eligible = sorted(
        (row for row in rows if row.get("validation_level") == "quote_only"),
        key=lambda row: Decimal(row["projected_net_score"]), reverse=True,
    )
    runner_up_delta = None
    if len(eligible) > 1:
        runner_up_delta = str(
            Decimal(eligible[0]["projected_net_score"])
            - Decimal(eligible[1]["projected_net_score"])
        )
    comparison = {
        "mode": "execution_preflight", "direction": context.get("direction"),
        "candidates": rows,
        "expected_candidates": [
            {"provider": provider, "settlement": settlement}
            for provider, settlement in identities
        ],
        "observed_candidates": [
            {"provider": provider, "settlement": settlement}
            for provider, settlement in identities
        ],
        "candidate_accounting_complete": True,
        "deadline_met": len(results) == len(identities),
        "selected_hypothetical_winner": (
            {key: eligible[0][key] for key in ("provider", "settlement")}
            if eligible else None
        ),
        "runner_up_delta": runner_up_delta,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "status": "hypothetical_only" if eligible else "no_eligible_candidate",
        "observation_timing": "parallel_pre_execution",
    }
    if eligible:
        LOG.info(
            "Route tournament winner provider=%s settlement=%s direction=%s score=%s "
            "runner_up_delta=%s eligible=%d rejected=%d elapsed_ms=%s",
            eligible[0]["provider"], eligible[0]["settlement"], context.get("direction"),
            eligible[0]["projected_net_score"], runner_up_delta or "n/a",
            len(eligible), len(rows) - len(eligible), comparison["elapsed_ms"],
        )
    else:
        LOG.info(
            "Route tournament winner provider=none settlement=none direction=%s "
            "score=- runner_up_delta=n/a eligible=0 rejected=%d elapsed_ms=%s",
            context.get("direction"), len(rows), comparison["elapsed_ms"],
        )
    selection = select_execution_candidate(comparison, context.get("direction"))
    comparison["selected_execution_candidate"] = selection
    if selection is None:
        # A partial collection or all-rejected set has no preflight winner.
        comparison["selected_hypothetical_winner"] = None
        comparison["status"] = "preflight_no_authorized_candidate"
    else:
        comparison["status"] = "preflight_candidate_selected"
    return comparison
