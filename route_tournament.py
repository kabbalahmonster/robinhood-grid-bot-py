"""Read-only, bounded route observations. No quote can authorize execution.

Quotes deliberately remain quote_only: provider simulation flags and gas hints
are not proof of an exact local eth_call + eth_estimateGas. Setup gas is a
conservative allowance, not an estimate of executable setup calldata.
"""

from decimal import Decimal, ROUND_CEILING
import logging
import time

from swap_provider import PROVIDERS

LOG = logging.getLogger("grid_bot.route_tournament")
NATIVE = "0x" + "00" * 20


def snapshot(bot, direction, amount, sold_cost_wei=None):
    """Capture pre-operation economics without touching provider/router state."""
    config = bot.config
    native_balance = int(bot.wallet.get_eth_balance_wei())
    return {
        "direction": direction,
        "amount": int(amount),
        "sold_cost_wei": sold_cost_wei,
        "native_trading": bool(config.use_eth_trading),
        "native_balance": native_balance,
        "trade_balance": native_balance if config.use_eth_trading else int(bot._raw_trade_balance()),
        "gas_price": int(bot.wallet.normal_gas_price()),
        "gas_multiplier": max(1.0, float(getattr(config, "gas_limit_multiplier", 1.05))),
        "price_multiplier": max(1.0, float(getattr(config, "gas_price_multiplier", 1.0)),
                                float(getattr(config, "gas_price_freshness_multiplier", 1.0))),
        "reserve": int(Decimal(str(config.eth_gas_reserve)) * 10**18),
        "cap": int(Decimal(str(getattr(config, "max_" + direction + "_gas_eth",
                                       getattr(config, "max_swap_gas_eth", 0.00004)))) * 10**18),
        "slippage": bot._swap_slippage_fraction(),
        "tax": bot._effective_token_transfer_fee_percent() / 100 if bot._taxed_token_active() else 0,
        "min_profit": float(getattr(config, "min_profit_percent", 2.0)),
    }


def score_candidate(quote, provider, settlement, context):
    """Compare equal inputs using integer gas costs and conservative output floors."""
    c = context
    row = {"provider": provider, "settlement": settlement,
           "validation_level": "rejected", "quoted_output_raw": None,
           "gas_components_wei": {}, "projected_net_score": None,
           "rejections": [], "execution_eligible": False}
    if not quote.success:
        row["rejections"] = ["provider_quote_failed"]
        return row
    output = int(quote.buy_amount or 0)
    row["quoted_output_raw"] = str(output)
    if c["amount"] <= 0 or output <= 0 or int(quote.sell_amount or 0) != c["amount"]:
        row["rejections"] = ["invalid_quote_amounts"]
        return row
    if not (0 <= c["slippage"] < 1 and 0 <= c["tax"] < 1) or c["gas_price"] <= 0:
        row["rejections"] = ["invalid_economic_assumptions"]
        return row
    # Unknown allowance/spender: budget both reset-to-zero and exact approval.
    # No allowance or provider approval endpoint is called by this experiment.
    approval = 200000 if c["direction"] == "sell" or settlement == "weth" else 0
    conversion = settlement == "weth" if c["native_trading"] else settlement == "native"
    wrap = conversion and ((c["direction"] == "buy") == c["native_trading"])
    unwrap = conversion and not wrap
    units = {"swap": max(int(quote.gas or 0), 350000 if c["direction"] == "buy" else 300000),
             "approval": approval, "wrap": 60000 if wrap else 0, "unwrap": 60000 if unwrap else 0}
    gas_price = max(c["gas_price"], int(quote.gas_price or 0))
    multiplier = Decimal(str(c["gas_multiplier"])) * Decimal(str(c["price_multiplier"]))
    costs = {key: int((Decimal(value * gas_price) * multiplier).to_integral_value(rounding=ROUND_CEILING))
             for key, value in units.items()}
    total = sum(costs.values())
    floor = int(Decimal(output) * (1 - Decimal(str(c["slippage"]))) * (1 - Decimal(str(c["tax"]))))
    row.update(validation_level="quote_only", preparation_dependent=True,
               gas_components_wei={key: str(value) for key, value in costs.items()},
               projected_total_gas_wei=str(total), gas_basis="conservative_budget_not_simulated",
               output_floor_raw=str(floor), slippage_fraction=c["slippage"], tax_fraction=c["tax"],
               approval_assumption="reset_and_exact_approval_budget" if approval else "none")
    if c["cap"] > 0 and total > c["cap"]:
        row["rejections"].append("total_gas_above_cap")
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


def collect(config, address, context, client_factory=None):
    """One get_quote per provider/settlement; never prepare, approve, or send.

    Independent client instances avoid mutating execution clients. Uniswap's
    shared limiter is intentionally respected. With routing_attempts=1, its
    explicit AMM fallback and gateway 409 retry allow at most four HTTP requests
    per candidate. Sushi makes one. No provider execution/preparation is used.
    """
    started = time.monotonic()
    rows = []
    for name in ("uniswap", "sushiswap"):
        if name == "uniswap" and not getattr(config, "uniswap_api_key", ""):
            continue
        try:
            client = client_factory(name) if client_factory else PROVIDERS[name].load_client_class()(config)
        except Exception:
            client = None
        for settlement, token in (("native", NATIVE), ("weth", config.weth_address)):
            try:
                if client is None:
                    raise ValueError("unavailable client")
                args = dict(sell_token=token if context["direction"] == "buy" else config.token_address,
                            buy_token=config.token_address if context["direction"] == "buy" else token,
                            sell_amount=context["amount"], taker_address=address,
                            slippage_percentage=context["slippage"], apply_jitter_to_price=False)
                if name == "uniswap":
                    args["routing_attempts"] = 1
                quote = client.get_quote(**args)
                rows.append(score_candidate(quote, name, settlement, context))
            except Exception:
                # Never publish exception text, raw provider responses, addresses,
                # calldata, request headers, or credentials in dashboard data.
                rows.append({"provider": name, "settlement": settlement,
                             "validation_level": "rejected", "rejections": ["candidate_failed"],
                             "quoted_output_raw": None, "gas_components_wei": {},
                             "projected_net_score": None, "execution_eligible": False})
    eligible = sorted((r for r in rows if r["validation_level"] == "quote_only"),
                      key=lambda r: Decimal(r["projected_net_score"]), reverse=True)
    winner = {key: eligible[0][key] for key in ("provider", "settlement")} if eligible else None
    result = {"mode": "shadow", "direction": context["direction"], "candidates": rows,
              "selected_hypothetical_winner": winner,
              "runner_up_delta": str(Decimal(eligible[0]["projected_net_score"]) - Decimal(eligible[1]["projected_net_score"])) if len(eligible) > 1 else None,
              "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
              "status": "hypothetical_only" if eligible else "no_eligible_candidate",
              "observation_timing": "after_execution_attempt_with_pre_operation_budget"}
    LOG.info("Route shadow %s candidates=%d rejected=%d winner=%s delta=%s elapsed_ms=%s",
             context["direction"], len(rows), len(rows) - len(eligible), winner,
             result["runner_up_delta"], result["elapsed_ms"])
    return result
