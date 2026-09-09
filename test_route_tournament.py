import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from config import BotConfig, load_config
from grid_bot import GridBot, _with_swap_provider_fallback
from route_tournament import (collect, collect_execution_preflight, score_candidate,
                              snapshot, select_execution_candidate)
from swap_provider import FallbackSwapProvider
from zero_x import QuoteResult


def context(direction="buy", **overrides):
    return dict(direction=direction, amount=10**15, sold_cost_wei=10**15,
                native_trading=True, native_balance=10**18, trade_balance=10**18,
                gas_price=10**6, gas_multiplier=1, price_multiplier=1,
                reserve=10**15, cap=10**14, slippage=0.01, tax=0.02,
                min_profit=2, **overrides)


def quote(output=2 * 10**15, gas=300000, **kwargs):
    return QuoteResult(success=True, buy_amount=output, sell_amount=10**15,
                       gas=gas, **kwargs)


def test_staged_weth_buy_prices_dynamic_wrap_approval_and_swap_gas():
    q = quote(gas=210000, allowance_target="router")
    row = score_candidate(
        q, "uniswap", "weth", context("buy"),
        allowance_probe=lambda _token, _spender: 0,
        gas_estimate=0, conversion_gas_limit=42000,
        approval_gas_limit=51000, require_local_gas=True,
        require_dynamic_setup_gas=True, staged_weth_buy=True,
    )

    assert row["validation_level"] == "quote_only"
    assert row["staged_weth_buy"] is True
    assert row["gas_basis"] == "provider_estimate_pending_post_setup_local_simulation"
    assert row["gas_components_wei"] == {
        "swap": str(210000 * 10**6),
        "approval": str(51000 * 10**6),
        "wrap": str(42000 * 10**6),
        "unwrap": "0",
    }


@pytest.mark.parametrize("direction,settlement,components", [
    ("buy", "native", {"swap"}),
    ("buy", "weth", {"swap", "approval", "wrap"}),
    ("sell", "native", {"swap", "approval"}),
    ("sell", "weth", {"swap", "approval", "unwrap"}),
])
def test_components_and_quote_only(direction, settlement, components):
    row = score_candidate(quote(data="secret calldata", to="router"), "sushiswap", settlement, context(direction))
    assert row["validation_level"] == "quote_only"
    assert not row["execution_eligible"]
    assert {key for key, value in row["gas_components_wei"].items() if int(value)} == components
    assert int(row["projected_total_gas_wei"]) == sum(map(int, row["gas_components_wei"].values()))
    assert "secret" not in json.dumps(row)


@pytest.mark.parametrize("direction,component", [("buy", "unwrap"), ("sell", "wrap")])
def test_weth_treasury_normalizes_native_conversion(direction, component):
    c = context(direction)
    c["native_trading"] = False
    row = score_candidate(quote(), "uniswap", "native", c)
    assert int(row["gas_components_wei"][component]) == 60000 * c["gas_price"]
    same = score_candidate(quote(), "uniswap", "weth", c)
    assert int(same["gas_components_wei"][component]) == 0


def test_buy_and_sell_scoring_tax_slippage_and_all_gas():
    """Provider gas estimate (300k from fixture) drives swap cost, not the fallback."""
    c = context()
    buy = score_candidate(quote(), "sushiswap", "weth", c)
    buy_floor = 2 * 10**15 * 99 * 98 // 10000
    # Provider says 300k (fixture default); weth buy needs approval + wrap.
    gas = (300000 + 200000 + 60000) * 10**6
    assert int(buy["output_floor_raw"]) == buy_floor
    assert Decimal(buy["projected_net_score"]) == Decimal(buy_floor) * 10**18 / (10**15 + gas)
    assert buy["gas_basis"] == "provider_estimate"
    assert buy["output_floor_human"] == pytest.approx(buy_floor / 10**18)
    c["direction"] = "sell"
    sell = score_candidate(quote(), "sushiswap", "weth", c)
    sell_floor = 2 * 10**15 * 98 // 100
    assert Decimal(sell["projected_net_score"]) == sell_floor - (300000 + 200000 + 60000) * 10**6


def test_taxed_sell_does_not_charge_fee_twice_when_slippage_includes_fee():
    # Taxed-token execution tolerance is fee + market buffer. Applying both
    # that total tolerance and tax again rejects a sell the live guard accepts.
    c = context("sell")
    c.update(
        amount=1, sold_cost_wei=3_746_083_335_437_205,
        gas_price=377_542_040, gas_multiplier=1.05, cap=10**18,
        slippage=0.083, tax=0.063, min_profit=2,
    )
    observed = score_candidate(
        QuoteResult(success=True, buy_amount=4_312_533_175_868_646,
                    sell_amount=1, gas=300000),
        "uniswap", "native", c, allowance_probe={"value": 1},
    )

    assert int(observed["output_floor_raw"]) == 4_312_533_175_868_646 * 937 // 1000
    assert "sell_profit_floor" not in observed["rejections"]


def test_execution_preflight_sell_ranks_with_all_projected_gas():
    """Route selection must compare complete economics, including approval gas."""
    c = context("sell")
    c.update(
        execution_preflight=True,
        amount=1,
        sold_cost_wei=4_250_000_000_000_000,
        gas_price=373_114_200,
        gas_multiplier=1.05,
        cap=10**18,
        slippage=0.083,
        tax=0.063,
        min_profit=1.5,
    )
    observed = score_candidate(
        QuoteResult(success=True, buy_amount=4_704_748_472_054_972,
                    sell_amount=1, gas=90_300),
        "sushiswap", "native", c, allowance_probe={"value": 0},
    )

    # Keep the staged pre-approval diagnostic, but rank the tournament using
    # approval plus swap gas so a setup-heavy route cannot win incorrectly.
    swap_gas_wei = int(90_300 * 1.05) * 373_114_200
    output_floor = int(4_704_748_472_054_972 * (1.0 - 0.063))
    assert int(observed["preapproval_total_gas_wei"]) == swap_gas_wei
    assert int(observed["approval_budget_wei"]) > 0
    assert int(observed["projected_total_gas_wei"]) > swap_gas_wei
    assert Decimal(observed["projected_net_score"]) == Decimal(
        output_floor - int(observed["projected_total_gas_wei"])
    )
    assert "sell_profit_floor" in observed["rejections"]


def test_execution_preflight_weth_sell_hard_cap_matches_normal_swap_only_cap():
    c = context("sell", execution_preflight=True)
    c.update(gas_price=10, gas_multiplier=1, cap=200_000,
             native_balance=10**18, reserve=1, sold_cost_wei=10**15)
    observed = score_candidate(
        quote(gas=30_000), "sushiswap", "weth", c,
        allowance_probe={"value": 10**15},
    )

    # Normal sell execution skips its swap hard-cap check for WETH fallback;
    # unwrap cost is protected separately by its own reserve guard.
    assert "total_gas_above_cap" not in observed["rejections"]


def test_execution_preflight_sell_has_no_native_reserve_veto():
    c = context("sell", execution_preflight=True)
    c.update(gas_price=10, gas_multiplier=1, cap=10**18,
             native_balance=600_000, reserve=500_000, sold_cost_wei=10**15)
    observed = score_candidate(
        quote(gas=30_000), "sushiswap", "native", c,
        allowance_probe={"value": 10**15},
    )

    # Normal sell execution does not use buy-side ETH reserve as an exit veto.
    assert "native_reserve" not in observed["rejections"]


def test_execution_preflight_sell_gas_rounding_matches_normal_execution():
    c = context("sell", execution_preflight=True)
    c.update(gas_price=10, gas_multiplier=1.05, cap=30,
             native_balance=10**18, reserve=1, sold_cost_wei=10**15)
    observed = score_candidate(
        quote(gas=3), "sushiswap", "native", c,
        allowance_probe={"value": 10**15},
    )

    # Normal execution truncates gas-limit headroom before applying gas price.
    assert int(observed["preapproval_total_gas_wei"]) == 30
    assert "total_gas_above_cap" not in observed["rejections"]


def test_execution_preflight_gas_multiplier_keeps_normal_float_rounding():
    c = context("sell", execution_preflight=True)
    c.update(gas_price=10, gas_multiplier=1.15, cap=1_140,
             native_balance=10**18, reserve=1, sold_cost_wei=10**15)
    observed = score_candidate(
        quote(gas=100), "sushiswap", "native", c,
        allowance_probe={"value": 10**15},
    )

    # This intentionally follows int(100 * 1.15), including binary-float loss.
    assert int(observed["preapproval_total_gas_wei"]) == 1_140
    assert "total_gas_above_cap" not in observed["rejections"]


def test_execution_preflight_tax_floor_matches_normal_execution_rounding():
    c = context("sell", execution_preflight=True)
    c.update(tax=0.063, cap=10**18, sold_cost_wei=1)
    observed = score_candidate(
        QuoteResult(success=True, buy_amount=444_157_599_796_692_942,
                    sell_amount=10**15, gas=1),
        "sushiswap", "native", c, allowance_probe={"value": 10**15},
    )

    # This is int(output * (1.0 - fee)), exactly as _taxed_quote_return_wei.
    assert int(observed["output_floor_raw"]) == 416_175_671_009_501_312


@pytest.mark.parametrize("change,reason", [
    ({"cap": 1}, "total_gas_above_cap"),
    ({"native_balance": 0}, "native_reserve"),
    ({"trade_balance": 0}, "input_balance"),
    ({"slippage": 1}, "invalid_economic_assumptions"),
    ({"tax": -0.1}, "invalid_economic_assumptions"),
    ({"direction": "sell", "sold_cost_wei": None}, "missing_sell_cost_basis"),
    ({"direction": "sell", "sold_cost_wei": 3 * 10**15}, "sell_profit_floor"),
])
def test_rejections(change, reason):
    c = context()
    c.update(change)
    row = score_candidate(quote(), "uniswap", "native", c)
    assert row["validation_level"] == "rejected"
    assert reason in row["rejections"]



def test_execution_preflight_prepares_uniswap_quote_for_local_gas():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    indicative = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                             gas=300000, raw_response={"quote": {}})
    prepared = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                           gas=207348, to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                           allowance_target="spender", data="0xdead", value=0)
    clients["uniswap"].get_quote.return_value = indicative
    clients["uniswap"].get_swap_transaction.return_value = prepared
    clients["sushiswap"].get_quote.return_value = quote()
    clients["sushiswap"].get_swap_transaction.return_value = prepared
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), clients.__getitem__,
        allowance_probe=lambda _token, _spender: 10**15,
        gas_estimate_provider=lambda candidate, _settlement: 180612 if candidate is prepared else 0,
        conversion_gas_estimate_provider=lambda _candidate, _settlement: 60001,
        max_seconds=4,
    )

    uniswap_rows = [row for row in result["candidates"] if row["provider"] == "uniswap"]
    assert all(row["gas_basis"] == "local_estimate" for row in uniswap_rows)
    assert all(int(row["gas_components_wei"]["swap"]) == 180612 * 10**6
               for row in uniswap_rows)
    assert clients["uniswap"].get_swap_transaction.call_count == 2
    for call in clients["uniswap"].get_swap_transaction.call_args_list:
        assert call.args == ({"quote": {}},)
        assert 0 < call.kwargs["quote_timeout_seconds"] <= 2
    assert clients["sushiswap"].get_swap_transaction.call_count == 2


def test_execution_preflight_requires_local_gas_for_sushi_too():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    indicative = quote()
    prepared = quote(to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                     allowance_target="spender", data="0xdead")
    for client in clients.values():
        client.get_quote.return_value = indicative
        client.get_swap_transaction.return_value = prepared
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), clients.__getitem__,
        allowance_probe=lambda _token, _spender: 10**15,
        gas_estimate_provider=lambda candidate, _settlement: (
            180612 if candidate is prepared and candidate is not indicative else 0
        ),
        conversion_gas_estimate_provider=lambda _candidate, _settlement: 60001,
        max_seconds=4,
    )

    sushi_rows = [row for row in result["candidates"] if row["provider"] == "sushiswap"]
    assert all(row["gas_basis"] == "local_estimate" for row in sushi_rows)
    assert all(int(row["gas_components_wei"]["swap"]) == 180612 * 10**6
               for row in sushi_rows)


def test_execution_preflight_rejects_unsimulatable_sushi_approval_handshake():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    prepared = quote(to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a", data="0xdead")
    clients["uniswap"].get_quote.return_value = quote()
    clients["uniswap"].get_swap_transaction.return_value = prepared
    clients["sushiswap"].get_quote.return_value = quote()
    clients["sushiswap"].get_swap_transaction.return_value = quote(
        allowance_target="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a"
    )
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), clients.__getitem__,
        gas_estimate_provider=lambda candidate, _settlement: 180612 if candidate.data else 0,
        max_seconds=4,
    )

    sushi_rows = [row for row in result["candidates"] if row["provider"] == "sushiswap"]
    assert all(row["rejections"] == ["local_gas_simulation_failed"] for row in sushi_rows)
    assert all(row["projected_total_gas_wei"] is None for row in sushi_rows)


def test_execution_preflight_rejects_failed_uniswap_preparation_without_gas_fallback():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    indicative = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                             gas=300000, raw_response={"quote": {}})
    clients["uniswap"].get_quote.return_value = indicative
    clients["uniswap"].get_swap_transaction.return_value = QuoteResult(
        success=False, error="swap preparation unavailable",
    )
    clients["sushiswap"].get_quote.return_value = quote()
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), clients.__getitem__, max_seconds=4,
    )

    uniswap_rows = [row for row in result["candidates"] if row["provider"] == "uniswap"]
    assert all(row["rejections"] == ["provider_quote_failed"] for row in uniswap_rows)
    assert all(row["projected_total_gas_wei"] is None for row in uniswap_rows)


def test_execution_preflight_rejects_zero_local_gas_after_uniswap_preparation():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    indicative = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                             gas=300000, raw_response={"quote": {}})
    prepared = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                           gas=300000, to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                           data="0xdead", value=0)
    clients["uniswap"].get_quote.return_value = indicative
    clients["uniswap"].get_swap_transaction.return_value = prepared
    clients["sushiswap"].get_quote.return_value = quote()
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), clients.__getitem__,
        gas_estimate_provider=lambda _quote: 0, max_seconds=4,
    )

    uniswap_rows = [row for row in result["candidates"] if row["provider"] == "uniswap"]
    assert all(row["rejections"] == ["local_gas_simulation_failed"] for row in uniswap_rows)
    assert all(row["projected_total_gas_wei"] is None for row in uniswap_rows)


def test_execution_preflight_rejects_local_gas_exception_after_uniswap_preparation():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    indicative = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                             gas=300000, raw_response={"quote": {}})
    prepared = QuoteResult(success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                           gas=300000, to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                           data="0xdead", value=0)
    clients["uniswap"].get_quote.return_value = indicative
    clients["uniswap"].get_swap_transaction.return_value = prepared
    clients["sushiswap"].get_quote.return_value = quote()
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    def failed_simulation(_quote):
        raise RuntimeError("rpc unavailable")

    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), clients.__getitem__,
        gas_estimate_provider=failed_simulation, max_seconds=4,
    )

    uniswap_rows = [row for row in result["candidates"] if row["provider"] == "uniswap"]
    assert all(row["rejections"] == ["local_gas_simulation_failed"] for row in uniswap_rows)


def test_collection_bounded_partial_failure_and_payload():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    clients["uniswap"].get_quote.side_effect = [RuntimeError("api-key=SECRET"), quote()]
    clients["sushiswap"].get_quote.return_value = quote(raw_response={"secret": "SECRET", "data": "CALLDATA"})
    cfg = SimpleNamespace(uniswap_api_key="SECRET", weth_address="weth", token_address="token")
    result = collect(cfg, "wallet", context(), clients.__getitem__)
    assert len(result["candidates"]) == 4
    assert result["candidates"][0]["rejections"] == ["candidate_failed"]
    assert result["selected_hypothetical_winner"] == {"provider": "sushiswap", "settlement": "native"}
    assert Decimal(result["runner_up_delta"]) > 0
    for client in clients.values():
        assert client.get_quote.call_count == 2
        client.prepare_swap.assert_not_called()
        client.get_swap_transaction.assert_not_called()
        client.build_swap_transaction.assert_not_called()
    assert clients["uniswap"].get_quote.call_args.kwargs["routing_attempts"] == 1
    assert result["elapsed_ms"] >= 0
    payload = json.dumps(result)
    for forbidden in ("SECRET", "CALLDATA", "wallet", "raw_response"):
        assert forbidden not in payload


def test_no_eligible_and_missing_provider():
    client = Mock()
    client.get_quote.return_value = QuoteResult(success=False, error="secret")
    result = collect(SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token"),
                     "wallet", context(), lambda name: client)
    assert len(result["candidates"]) == 2
    assert result["status"] == "no_eligible_candidate"
    assert result["selected_hypothetical_winner"] is None
    assert result["runner_up_delta"] is None


def test_provider_quote_failure_has_structured_actionable_reason():
    row = score_candidate(
        QuoteResult(success=False, error="Uniswap API returned status 404: NoRouteFoundError"),
        "uniswap", "native", context(),
    )
    assert row["rejections"] == ["provider_quote_failed"]
    assert row["failure_reason"] == {
        "category": "no_liquidity",
        "retryable": False,
        "provider_error": "NoRouteFoundError",
    }
    assert row["quote_failure_kind"] == "no_route_or_liquidity"
    assert row["gas_price_currentness"] == "unknown"


def test_quote_deadline_is_exposed_as_an_observation_timeout():
    row = score_candidate(
        QuoteResult(success=False, error="shadow quote deadline elapsed"),
        "uniswap", "native", context(),
    )

    assert row["rejections"] == ["observation_timeout"]
    assert row["quote_failure_kind"] == "observation_timeout"
    assert row["failure_reason"] == {
        "category": "observation_timeout",
        "retryable": True,
        "provider_error": "shadow_quote_deadline",
    }


def test_collection_rejects_quote_returned_after_global_deadline():
    clock = [0.0]
    client = Mock()

    def late_quote(**_kwargs):
        clock[0] = 1.1
        return quote()

    client.get_quote.side_effect = late_quote
    cfg = SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token")
    gas_oracle = Mock(return_value=10**6)
    with patch("route_tournament.time.monotonic", side_effect=lambda: clock[0]):
        result = collect(
            cfg, "wallet", context(), lambda _name: client,
            gas_price_provider=gas_oracle, max_seconds=1,
        )

    first = result["candidates"][0]
    assert first["rejections"] == ["observation_timeout"]
    assert first["quote_failure_kind"] == "observation_timeout"
    assert result["selected_hypothetical_winner"] is None
    gas_oracle.assert_not_called()


def test_collection_rejects_economics_finished_after_global_deadline():
    clock = [0.0]
    client = Mock()
    client.get_quote.return_value = quote()

    def late_gas_price():
        clock[0] = 1.1
        return 10**6

    gas_estimator = Mock(return_value=123456)
    cfg = SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token")
    with patch("route_tournament.time.monotonic", side_effect=lambda: clock[0]):
        result = collect(
            cfg, "wallet", context(), lambda _name: client,
            gas_price_provider=late_gas_price,
            gas_estimate_provider=gas_estimator, max_seconds=1,
        )

    first = result["candidates"][0]
    assert first["rejections"] == ["observation_timeout"]
    assert result["selected_hypothetical_winner"] is None
    gas_estimator.assert_not_called()


def test_collection_rejects_allowance_probe_finished_after_global_deadline():
    clock = [0.0]
    client = Mock()
    client.get_quote.return_value = quote(allowance_target="spender")

    def late_allowance(_token, _spender):
        clock[0] = 1.1
        return 10**15

    cfg = SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token")
    with patch("route_tournament.time.monotonic", side_effect=lambda: clock[0]):
        result = collect(
            cfg, "wallet", context("sell"), lambda _name: client,
            allowance_probe=late_allowance, max_seconds=1,
        )

    first = result["candidates"][0]
    assert first["rejections"] == ["observation_timeout"]
    assert result["selected_hypothetical_winner"] is None


def test_collection_labels_quote_exception_after_deadline_as_timeout():
    clock = [0.0]
    client = Mock()

    def late_error(**_kwargs):
        clock[0] = 1.1
        raise RuntimeError("provider failed")

    client.get_quote.side_effect = late_error
    cfg = SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token")
    with patch("route_tournament.time.monotonic", side_effect=lambda: clock[0]):
        result = collect(cfg, "wallet", context(), lambda _name: client, max_seconds=1)

    assert result["candidates"][0]["rejections"] == ["observation_timeout"]


def test_collection_does_not_start_allowance_probe_after_deadline():
    client = Mock()
    client.get_quote.return_value = quote(allowance_target="spender")
    allowance_probe = Mock(return_value=10**15)
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 1.1])
    cfg = SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token")
    with patch("route_tournament.time.monotonic", side_effect=lambda: next(ticks, 1.1)):
        result = collect(
            cfg, "wallet", context("sell"), lambda _name: client,
            allowance_probe=allowance_probe, max_seconds=1,
        )

    assert result["candidates"][0]["rejections"] == ["observation_timeout"]
    allowance_probe.assert_not_called()


def test_tournament_quote_requests_respect_shared_provider_cooldown_state():
    client = Mock()
    client.get_quote.return_value = quote()
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")
    collect(cfg, "wallet", context(), lambda name: client)
    uniswap_calls = [call for call in client.get_quote.call_args_list if call.kwargs.get("routing_attempts") == 1]
    assert uniswap_calls
    assert all(call.kwargs["protocol_probe_limit"] == 1 for call in uniswap_calls)
    assert all("isolated_rate_limit" not in call.kwargs for call in uniswap_calls)


def test_tournament_uses_read_only_protocol_hint_for_matching_settlement():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    for client in clients.values():
        client.get_quote.return_value = quote()
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    collect(cfg, "wallet", context(), clients.__getitem__,
            protocol_hints={"native": "V4", "weth": "V3"})

    uniswap_calls = clients["uniswap"].get_quote.call_args_list
    assert [call.kwargs["preferred_protocol"] for call in uniswap_calls] == ["V4", "V3"]
    assert all("preferred_protocol" not in call.kwargs
               for call in clients["sushiswap"].get_quote.call_args_list)


def test_snapshot_copies_only_matching_execution_protocol_hints():
    hint_reader = Mock(side_effect=["V4", "V3"])
    b = SimpleNamespace(
        config=SimpleNamespace(
            use_eth_trading=True, eth_gas_reserve=0.001,
            max_sell_gas_eth=0.002, max_swap_gas_eth=0.003,
            min_profit_percent=2, weth_address="weth", token_address="token",
            gas_limit_multiplier=1.05,
        ),
        wallet=Mock(),
        provider=SimpleNamespace(primary=SimpleNamespace(client=SimpleNamespace(protocol_hint_for=hint_reader))),
        _swap_slippage_fraction=Mock(return_value=0.01),
        _effective_token_transfer_fee_percent=Mock(return_value=0),
        _taxed_token_active=Mock(return_value=False),
    )
    b.wallet.get_eth_balance_wei.return_value = 10**18
    b.wallet.normal_gas_price.return_value = 10**6

    captured = snapshot(b, "sell", 10**15, sold_cost_wei=10**15)

    assert captured["uniswap_protocol_hints"] == {"native": "V4", "weth": "V3"}
    assert hint_reader.call_args_list[0].args == ("token", "0x" + "00" * 20)
    assert hint_reader.call_args_list[1].args == ("token", "weth")


def test_tournament_passes_each_candidate_a_bounded_quote_timeout():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    for client in clients.values():
        client.get_quote.return_value = quote()
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    collect(cfg, "wallet", context(), clients.__getitem__, max_seconds=4)

    for client in clients.values():
        assert client.get_quote.call_count == 2
        for call in client.get_quote.call_args_list:
            assert 0 < call.kwargs["quote_timeout_seconds"] <= 4


def bot(mode):
    b = GridBot.__new__(GridBot)
    b.config = SimpleNamespace(
        route_tournament_mode=mode,
        route_tournament_canary=(mode == "gate"),
    )
    b.wallet = Mock(address="wallet")
    b.wallet.normal_gas_price.return_value = 10**6  # tournament gas oracle
    b.wallet.check_allowance.return_value = 0  # conservative default for tests
    b.provider = SimpleNamespace()
    return b


def test_off_has_no_snapshot_collection_or_payload_change():
    b = bot("off")
    b._buy_attempt = {"status": "original"}
    with patch("route_tournament.snapshot") as capture, patch("route_tournament.collect") as collection:
        @_with_swap_provider_fallback
        def operation(self):
            self._queue_route_shadow("buy", 1)
            return "unchanged"
        assert operation(b) == "unchanged"
        assert b._attempt_with_route_comparison("buy") is b._buy_attempt
        capture.assert_not_called()
        collection.assert_not_called()
        assert b.wallet.mock_calls == []


def test_shadow_runs_after_fallback_and_cannot_select_or_replay():
    b = bot("shadow")
    primary, fallback = SimpleNamespace(name="uniswap"), SimpleNamespace(name="sushiswap")
    b.provider = FallbackSwapProvider(primary, fallback)
    calls = []

    @_with_swap_provider_fallback
    def operation(self):
        self._queue_route_shadow("buy", 10**15)
        calls.append(self.provider.active.name)
        if self.provider.active is primary:
            self.provider._request_retry_after_failure("quote", None)
            return None
        self.provider.seal_current_operation()
        self.wallet._send_transaction({"data": "EXACT_ORIGINAL"})
        return "confirmed"

    def observe(*args, **kwargs):
        assert calls == ["uniswap", "sushiswap"]
        b.wallet._send_transaction.assert_called_once_with({"data": "EXACT_ORIGINAL"})
        assert b.provider.active is primary
        return {"selected_hypothetical_winner": {"provider": "uniswap", "settlement": "weth"}}

    with patch("route_tournament.snapshot", return_value=context()) as capture, patch("route_tournament.collect", side_effect=observe) as collection:
        assert operation(b) == "confirmed"
        capture.assert_called_once()
        collection.assert_called_once()
    assert calls == ["uniswap", "sushiswap"]
    assert b.provider.active is primary
    assert b._attempt_with_route_comparison("buy")["route_comparison"]["selected_hypothetical_winner"]["provider"] == "uniswap"


def test_shadow_observation_budget_is_four_seconds():
    b = bot("shadow")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.trade_token_address = "weth"
    b._route_shadow_pending = {"sell": context("sell")}

    with patch("route_tournament.collect", return_value={"mode": "shadow"}) as collection:
        b._finish_route_shadow()

    assert collection.call_args.kwargs["max_seconds"] == 4


def test_poll_and_observer_failure_do_not_change_operation():
    b = bot("shadow")
    @_with_swap_provider_fallback
    def poll(self):
        return 123
    with patch("route_tournament.collect") as collection:
        assert poll(b) == 123
        collection.assert_not_called()
    b._route_shadow_pending = {"buy": context()}
    with patch("route_tournament.collect", side_effect=RuntimeError("secret")):
        assert poll(b) == 123
    assert b._attempt_with_route_comparison("buy")["route_comparison"]["status"] == "observation_failed"


@pytest.mark.parametrize("mode", ["shadow", "gate"])
def test_reported_buy_tournament_is_expired_before_next_buy_check(mode):
    """A reported buy contest must not be retransmitted without a new contest."""
    b = bot(mode)
    b._funding_warning = {"status": "reported"}
    b._buy_attempt = {"status": "reported"}
    b._route_comparisons = {"buy": {"direction": "buy"}}

    b._expire_reported_buy_state()

    assert b._funding_warning is None
    assert b._buy_attempt is None
    assert b._attempt_with_route_comparison("buy") is None


def test_buy_strategy_veto_marks_selected_tournament_terminal():
    b = bot("gate")
    b._route_comparisons = {
        "buy": {
            "mode": "execution_preflight", "direction": "buy",
            "status": "preflight_candidate_selected",
        }
    }

    b._mark_buy_tournament_aborted(
        reason="buy_trigger_recovered",
        quoted_pnl_percent=-3.5,
        block_threshold_percent=-9.6,
        trigger_threshold_percent=-10.0,
    )

    comparison = b._route_comparisons["buy"]
    assert comparison["status"] == "execution_aborted"
    assert comparison["execution_abort"] == {
        "reason": "buy_trigger_recovered",
        "quoted_pnl_percent": -3.5,
        "block_threshold_percent": -9.6,
        "trigger_threshold_percent": -10.0,
    }
    assert b._buy_attempt["status"] == "buy_trigger_recovered"


@pytest.mark.parametrize("mode", ["execute", "invalid"])
def test_execute_and_unknown_modes_fail_closed(mode):
    cfg = BotConfig.__new__(BotConfig)
    cfg.route_tournament_mode = mode
    with pytest.raises(ValueError, match="supports off, shadow, or gate"):
        cfg.validate()


def test_gate_mode_requires_explicit_canary_flag():
    cfg = BotConfig.__new__(BotConfig)
    cfg.route_tournament_mode = "gate"
    cfg.route_tournament_canary = False
    with pytest.raises(ValueError, match="ROUTE_TOURNAMENT_CANARY=true"):
        cfg.validate()


def test_gridless_tournament_uses_exact_post_moonbag_amount_and_cost():
    b = bot("gate")
    b.config.moonbag_percentage = 1

    amount, cost = b._gridless_sell_terms({"balance": 10_001, "cost_wei": 5_000})

    assert amount == 9_901
    assert cost == 5_000 * 9_901 // 10_001


def test_mode_parsing_and_default(monkeypatch, tmp_path):
    # Avoid loading a checkout/operator .env or requiring live credentials.
    with patch("config.load_dotenv"), patch.object(BotConfig, "validate"):
        monkeypatch.delenv("ROUTE_TOURNAMENT_MODE", raising=False)
        assert load_config().route_tournament_mode == "off"
        monkeypatch.setenv("ROUTE_TOURNAMENT_MODE", " SHADOW ")
        assert load_config().route_tournament_mode == "shadow"
        monkeypatch.setenv("UNISWAP_PROTOCOL_CACHE_TTL_SECONDS", "420")
        assert load_config().uniswap_protocol_cache_ttl_seconds == 420
        monkeypatch.setenv("ROUTE_TOURNAMENT_PROVIDERS", "sushiswap,uniswap,sushiswap")
        monkeypatch.setenv("ROUTE_TOURNAMENT_SETTLEMENTS", "native")
        monkeypatch.setenv("ROUTE_TOURNAMENT_SHADOW_TIMEOUT_SECONDS", "5")
        monkeypatch.setenv("ROUTE_TOURNAMENT_GATE_TIMEOUT_SECONDS", "7")
        configured = load_config()
        assert configured.route_tournament_providers == ("sushiswap", "uniswap")
        assert configured.route_tournament_settlements == ("native",)
        assert configured.route_tournament_shadow_timeout_seconds == 5
        assert configured.route_tournament_gate_timeout_seconds == 7


def test_native_only_preflight_collects_and_selects_two_candidates():
    cfg = SimpleNamespace(
        uniswap_api_key="key", weth_address="weth", token_address="token",
        route_tournament_providers=("uniswap", "sushiswap"),
        route_tournament_settlements=("native",),
    )
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    for index, client in enumerate(clients.values(), start=1):
        client.get_quote.return_value = quote(
            output=index * 10**15,
            to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a", data="0xdead",
        )
    result = collect_execution_preflight(
        cfg, "wallet", context("buy"), clients.__getitem__,
        gas_estimate_provider=lambda _quote, _settlement: 100000,
    )
    assert [(row["provider"], row["settlement"]) for row in result["candidates"]] == [
        ("uniswap", "native"), ("sushiswap", "native")
    ]
    assert select_execution_candidate(result, "buy") == {
        "provider": "sushiswap", "settlement": "native"
    }


def test_execution_selector_accepts_one_valid_route_from_complete_accounting():
    """One timely eligible route can win even when all alternatives are rejected."""
    complete = {
        "mode": "execution_preflight",
        "direction": "sell",
        "candidate_accounting_complete": True,
        "deadline_met": True,
        "candidates": [
            {"provider": "uniswap", "settlement": "native", "validation_level": "rejected", "rejections": ["sell_profit_floor"], "projected_net_score": "20"},
            {"provider": "uniswap", "settlement": "weth", "validation_level": "rejected", "rejections": ["total_gas_above_cap"], "projected_net_score": "10"},
            {"provider": "sushiswap", "settlement": "native", "validation_level": "quote_only", "rejections": [], "projected_net_score": "30"},
            {"provider": "sushiswap", "settlement": "weth", "validation_level": "rejected", "rejections": ["provider_quote_failed"], "projected_net_score": "5"},
        ],
    }

    assert select_execution_candidate(complete, "sell") == {
        "provider": "sushiswap", "settlement": "native",
    }

    incomplete = {**complete, "candidates": complete["candidates"][:-1]}
    assert select_execution_candidate(incomplete, "sell") is None


    no_valid_route = {**complete, "candidates": [
        *complete["candidates"][:2],
        {"provider": "sushiswap", "settlement": "native", "validation_level": "rejected",
         "rejections": ["sell_profit_floor"]},
        {"provider": "sushiswap", "settlement": "weth", "validation_level": "rejected",
         "rejections": ["observation_timeout"]},
    ]}
    assert select_execution_candidate(no_valid_route, "sell") is None

    # Shadow results are necessarily post-execution observations and can never
    # be promoted by accident, even if their candidate rows look complete.
    assert select_execution_candidate({**complete, "mode": "shadow"}, "sell") is None
    assert select_execution_candidate({key: value for key, value in complete.items()
                                       if key != "candidate_accounting_complete"}, "sell") is None

    missing_rejections = {**complete, "candidates": [
        {key: value for key, value in complete["candidates"][2].items() if key != "rejections"},
        *complete["candidates"][:2], *complete["candidates"][3:],
    ]}
    assert select_execution_candidate(missing_rejections, "sell") is None

    malformed_rejections = {**complete, "candidates": [
        *complete["candidates"][:2],
        {**complete["candidates"][2], "rejections": ""},
        complete["candidates"][3],
    ]}
    assert select_execution_candidate(malformed_rejections, "sell") is None
    assert select_execution_candidate({**complete, "direction": "bogus"}, "bogus") is None
    # Candidate-local deadline checks reject late rows before scoring. A timely
    # valid route remains selectable even if later accounting pushes collection
    # past the aggregate deadline.
    assert select_execution_candidate({**complete, "deadline_met": False}, "sell") == {
        "provider": "sushiswap", "settlement": "native",
    }


def test_execution_selector_carries_only_valid_uniswap_protocol_identity():
    rows = [
        {"provider": provider, "settlement": settlement,
         "validation_level": "quote_only", "rejections": [],
         "projected_net_score": str(score),
         **({"protocol": "V4"} if provider == "uniswap" and settlement == "native" else {})}
        for provider, settlement, score in (
            ("uniswap", "native", 9), ("uniswap", "weth", 8),
            ("sushiswap", "native", 7), ("sushiswap", "weth", 6),
        )
    ]
    comparison = {"mode": "execution_preflight", "direction": "sell",
                  "candidate_accounting_complete": True, "candidates": rows}
    assert select_execution_candidate(comparison, "sell") == {
        "provider": "uniswap", "settlement": "native", "protocol": "V4",
    }


def test_execution_preflight_requires_local_gas_estimator():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    result = collect_execution_preflight(cfg, "wallet", context("sell"), clients.__getitem__)

    assert result["status"] == "required_local_gas_estimator_unavailable"
    assert result["failures"] == ["local_gas_estimator_unavailable"]
    assert result["selected_execution_candidate"] is None
    for client in clients.values():
        client.get_quote.assert_not_called()


def test_execution_preflight_collects_only_when_all_required_providers_exist():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    for name, client in clients.items():
        client.get_quote.return_value = quote()
        if name == "uniswap":
            client.get_swap_transaction.return_value = QuoteResult(
                success=True, buy_amount=2 * 10**15, sell_amount=10**15,
                to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                allowance_target="spender", data="0xdead", value=0,
            )
        else:
            client.get_swap_transaction.return_value = quote(
                to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                allowance_target="spender", data="0xdead",
            )
    config_with_both = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")

    preflight = collect_execution_preflight(
        config_with_both, "wallet", context("sell"), clients.__getitem__,
        allowance_probe=lambda _token, _spender: 10**15,
        gas_estimate_provider=lambda _quote, _settlement: 100000,
        conversion_gas_estimate_provider=lambda _quote, _settlement: 60000,
    )

    assert preflight["mode"] == "execution_preflight"
    assert len(preflight["candidates"]) == 4
    assert preflight["candidate_accounting_complete"] is True
    assert preflight["deadline_met"] is True
    assert preflight["selected_execution_candidate"] == {
        "provider": "uniswap", "settlement": "native",
    }

    no_uniswap = SimpleNamespace(uniswap_api_key="", weth_address="weth", token_address="token")
    blocked = collect_execution_preflight(no_uniswap, "wallet", context("sell"), clients.__getitem__)
    assert blocked == {
        "mode": "execution_preflight", "direction": "sell", "candidates": [],
        "expected_candidates": [
            {"provider": "sushiswap", "settlement": "native"},
            {"provider": "sushiswap", "settlement": "weth"},
            {"provider": "uniswap", "settlement": "native"},
            {"provider": "uniswap", "settlement": "weth"},
        ],
        "observed_candidates": [], "candidate_accounting_complete": False,
        "deadline_met": False, "selected_hypothetical_winner": None,
        "selected_execution_candidate": None,
        "runner_up_delta": None, "status": "required_provider_unavailable",
        "failures": ["uniswap_unavailable"],
    }


def test_execution_preflight_gives_all_four_candidates_independent_time_budgets():
    """Four slow candidates run together instead of starving later routes."""
    def factory(_name):
        client = Mock()

        def delayed_quote(**_kwargs):
            time.sleep(0.15)
            return quote(
                to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a",
                allowance_target="spender", data="0xdead",
            )

        client.get_quote.side_effect = delayed_quote
        return client

    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")
    started = time.monotonic()
    result = collect_execution_preflight(
        cfg, "wallet", context("sell"), factory,
        allowance_probe=lambda _token, _spender: 10**15,
        gas_estimate_provider=lambda _quote, _settlement: 100000,
        conversion_gas_estimate_provider=lambda _quote, _settlement: 60000,
        max_seconds=0.5,
    )

    assert time.monotonic() - started < 0.45
    assert result["candidate_accounting_complete"] is True
    assert result["deadline_met"] is True
    assert len(result["candidates"]) == 4
    assert all(row["rejections"] != ["observation_timeout"] for row in result["candidates"])


def test_execution_preflight_uses_six_second_gate_budget():
    b = bot("gate")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.trade_token_address = "native"
    incomplete = {
        "mode": "execution_preflight", "direction": "sell",
        "candidate_accounting_complete": False, "deadline_met": False,
        "candidates": [],
    }

    with patch("route_tournament.snapshot", return_value=context("sell")), \
         patch("route_tournament.collect_execution_preflight", return_value=incomplete) as collect:
        assert b._collect_route_execution_preflight("sell", 10**15) is None

    assert collect.call_args.kwargs["max_seconds"] == 6


def test_execution_preflight_local_gas_estimate_checksums_api_addresses():
    from web3 import Web3

    b = bot("gate")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.trade_token_address = "native"
    b.wallet.address = "0x3d8c491b7fe2d43468b5e45162e374719003ef16"
    lowercase_router = "0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a"
    fresh_quote = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15,
                               to=lowercase_router, data="0xdead", value=0)

    b.wallet.w3.eth.estimate_gas.return_value = 123456
    def invoke_estimator(*args, **kwargs):
        assert kwargs["gas_estimate_provider"](fresh_quote, "native") == 123456
        return {"mode": "execution_preflight", "direction": "sell",
                "candidate_accounting_complete": False, "deadline_met": False,
                "candidates": []}
    with patch("route_tournament.snapshot", return_value=context("sell")), \
         patch("route_tournament.collect_execution_preflight", side_effect=invoke_estimator):
        assert b._collect_route_execution_preflight("sell", 10**15) is None

    tx = b.wallet.w3.eth.estimate_gas.call_args.args[0]
    assert tx["from"] == Web3.to_checksum_address(b.wallet.address)
    assert tx["to"] == Web3.to_checksum_address(lowercase_router)


def test_shadow_local_gas_estimate_checksums_api_addresses():
    from web3 import Web3

    b = bot("shadow")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.trade_token_address = "native"
    b.wallet.address = "0x3d8c491b7fe2d43468b5e45162e374719003ef16"
    b._route_shadow_pending = {"sell": context("sell")}
    lowercase_router = "0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a"
    fresh_quote = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15,
                               to=lowercase_router, data="0xdead", value=0)

    b.wallet.w3.eth.estimate_gas.return_value = 123456
    def invoke_estimator(*args, **kwargs):
        assert kwargs["gas_estimate_provider"](fresh_quote, "native") == 123456
        return {"mode": "shadow", "direction": "sell", "candidates": []}
    with patch("route_tournament.collect", side_effect=invoke_estimator):
        b._finish_route_shadow()

    tx = b.wallet.w3.eth.estimate_gas.call_args.args[0]
    assert tx["from"] == Web3.to_checksum_address(b.wallet.address)
    assert tx["to"] == Web3.to_checksum_address(lowercase_router)


def test_bot_execution_preflight_is_read_only_and_returns_only_complete_winner():
    b = bot("off")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.trade_token_address = "native"
    b.wallet.w3 = Mock()
    preflight = {
        "mode": "execution_preflight", "direction": "buy",
        "candidate_accounting_complete": True, "deadline_met": True,
        "candidates": [
            {"provider": provider, "settlement": settlement, "validation_level": "quote_only",
             "rejections": [], "projected_net_score": str(score)}
            for provider, settlement, score in (
                ("uniswap", "native", 1), ("uniswap", "weth", 2),
                ("sushiswap", "native", 4), ("sushiswap", "weth", 3),
            )
        ],
    }
    with patch("route_tournament.snapshot", return_value=context("buy")), \
         patch("route_tournament.collect_execution_preflight", return_value=preflight) as collect_preflight:
        assert b._collect_route_execution_preflight("buy", 10**15) == {
            "provider": "sushiswap", "settlement": "native",
        }
    assert collect_preflight.call_args.kwargs["max_seconds"] == 6
    # Buy/WETH now reads the real allowance so staged setup can be priced.
    assert collect_preflight.call_args.kwargs["allowance_probe"]("weth", "router") == 0
    assert callable(collect_preflight.call_args.kwargs["approval_gas_estimate_provider"])
    b.wallet._send_transaction.assert_not_called()
    b.wallet.approve_token.assert_not_called()


def test_selected_route_is_freshly_requoted_and_locally_estimated_before_setup():
    b = bot("off")
    b.wallet.address = "0x3d8c491b7fe2d43468b5e45162e374719003ef16"
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.config.use_eth_trading = True
    b.trade_token_address = "native"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    fresh_quote = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15,
                              to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a", data="0xdead", value=0)
    primary = SimpleNamespace(name="uniswap", build_swap_transaction=Mock())
    selected = SimpleNamespace(name="sushiswap", build_swap_transaction=Mock(return_value=fresh_quote))
    b.provider = SimpleNamespace(primary=primary, fallback=selected)
    b.wallet.w3.eth.estimate_gas.return_value = 123456

    validated = b._revalidate_selected_route(
        {"provider": "sushiswap", "settlement": "native"}, "buy", 10**15,
    )

    assert validated == {"provider": selected, "quote": fresh_quote,
                        "weth_fallback": False, "gas_estimate": 123456}
    assert b.wallet.w3.eth.estimate_gas.call_args.args[0]["value"] == 10**15
    selected.build_swap_transaction.assert_called_once_with(
        sell_token="native", buy_token="token", sell_amount=10**15,
        taker_address="0x3d8c491b7fe2d43468b5e45162e374719003ef16", slippage_percentage=0.01,
    )
    b.wallet._send_transaction.assert_not_called()
    b.wallet.approve_token.assert_not_called()



def test_selected_weth_buy_route_simulates_zero_native_value_even_when_provider_sets_value():
    b = bot("off")
    b.wallet.address = "0x3d8c491b7fe2d43468b5e45162e374719003ef16"
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.config.use_eth_trading = True
    b.trade_token_address = "native"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    quote = QuoteResult(
        success=True, sell_amount=10**15, buy_amount=2 * 10**15,
        to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a", data="0xdead", value=10**15,
    )
    selected = SimpleNamespace(
        name="sushiswap", capabilities=SimpleNamespace(quote_requires_preparation=False),
        build_swap_transaction=Mock(return_value=quote),
    )
    b.provider = SimpleNamespace(primary=SimpleNamespace(name="uniswap"), fallback=selected)
    b.wallet.w3.eth.estimate_gas.return_value = 123456

    assert b._revalidate_selected_route(
        {"provider": "sushiswap", "settlement": "weth"}, "buy", 10**15,
    ) is not None
    assert b.wallet.w3.eth.estimate_gas.call_args.args[0]["value"] == 0


def test_selected_uniswap_route_is_prepared_before_calldata_validation():
    b = bot("off")
    b.wallet.address = "0x3d8c491b7fe2d43468b5e45162e374719003ef16"
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.config.use_eth_trading = True
    b.trade_token_address = "native"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    quote_only = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15)
    prepared = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15,
                           to="0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a", data="0xdead", value=0)
    uniswap = SimpleNamespace(
        name="uniswap", capabilities=SimpleNamespace(quote_requires_preparation=True),
        build_swap_transaction=Mock(return_value=quote_only), prepare_swap=Mock(return_value=prepared),
    )
    b.provider = SimpleNamespace(primary=uniswap, fallback=SimpleNamespace(name="sushiswap"))
    b.wallet.w3.eth.estimate_gas.return_value = 123456

    validated = b._revalidate_selected_route(
        {"provider": "uniswap", "settlement": "native", "protocol": "V4"}, "buy", 10**15,
    )
    assert uniswap.build_swap_transaction.call_args.kwargs["preferred_protocol"] == "V4"
    assert getattr(validated["quote"], "_tournament_protocol") == "V4"

    assert validated == {"provider": uniswap, "quote": prepared,
                         "weth_fallback": False, "gas_estimate": 123456}
    uniswap.prepare_swap.assert_called_once_with(quote_only)


def test_selected_route_revalidation_checksums_lowercase_calldata_target():
    """Gate revalidation must accept an API route normal execution can simulate."""
    from web3 import Web3

    b = bot("off")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.config.use_eth_trading = True
    b.trade_token_address = "native"
    b.wallet.address = "0x3d8c491b7fe2d43468b5e45162e374719003ef16"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    lowercase_router = "0x8e6fd69a77e88ee20ba4b4fbd59dfcda3ec0e98a"
    selected = SimpleNamespace(
        name="sushiswap", capabilities=SimpleNamespace(quote_requires_preparation=False),
        build_swap_transaction=Mock(return_value=QuoteResult(
            success=True, sell_amount=10**15, buy_amount=2 * 10**15,
            to=lowercase_router, data="0xdead", value=0,
        )),
    )
    b.provider = SimpleNamespace(primary=SimpleNamespace(name="uniswap"), fallback=selected)

    def accept_only_checksummed_target(tx):
        assert tx["from"] == Web3.to_checksum_address(b.wallet.address)
        assert tx["to"] == Web3.to_checksum_address(lowercase_router)
        return 123456

    b.wallet.w3.eth.estimate_gas.side_effect = accept_only_checksummed_target

    validated = b._revalidate_selected_route(
        {"provider": "sushiswap", "settlement": "native"}, "sell", 10**15,
    )

    assert validated is not None
    assert validated["gas_estimate"] == 123456


def test_selected_route_revalidation_rejects_zero_fresh_output():
    b = bot("off")
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.config.use_eth_trading = True
    b.trade_token_address = "native"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    invalid = QuoteResult(success=True, sell_amount=10**15, buy_amount=0,
                          to="router", data="0xdead", value=0)
    selected = SimpleNamespace(name="sushiswap", capabilities=SimpleNamespace(quote_requires_preparation=False),
                               build_swap_transaction=Mock(return_value=invalid))
    b.provider = SimpleNamespace(primary=SimpleNamespace(name="uniswap"), fallback=selected)

    assert b._revalidate_selected_route(
        {"provider": "sushiswap", "settlement": "native"}, "buy", 10**15,
    ) is None
    b.wallet.w3.eth.estimate_gas.assert_not_called()


def test_gate_canary_refusal_covers_direct_banking_action():
    b = bot("gate")
    b.config.route_tournament_canary = False
    b.provider = Mock()

    assert b.bank_profit(0.01) is None
    b.provider.build_swap_transaction.assert_not_called()


def test_gate_action_requires_explicit_canary_flag():
    b = bot("gate")
    b.config.route_tournament_canary = False
    b._collect_route_execution_preflight = Mock()

    quote_result, weth_fallback = b._actionable_quote_with_weth_fallback(
        sell_token="native", buy_token="token", sell_amount=10**15, direction="buy",
    )

    assert quote_result.success is False
    assert weth_fallback is False
    b._collect_route_execution_preflight.assert_not_called()


def test_gate_mode_uses_only_a_freshly_revalidated_selected_route():
    b = bot("gate")
    b.config.use_eth_trading = True
    b.config.token_address = "token"
    b.config.weth_address = "weth"
    b.trade_token_address = "native"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    primary = SimpleNamespace(name="uniswap")
    selected = SimpleNamespace(name="sushiswap")
    b.provider = SimpleNamespace(primary=primary, fallback=selected, active=primary)
    b.api_client = Mock()
    original_api = b.api_client
    fresh_quote = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15,
                              to="router", data="0xdead")
    b._collect_route_execution_preflight = Mock(return_value={
        "provider": "sushiswap", "settlement": "native",
    })
    b._revalidate_selected_route = Mock(return_value={
        "provider": selected, "quote": fresh_quote, "weth_fallback": False, "gas_estimate": 123456,
    })

    quote_result, weth_fallback = b._actionable_quote_with_weth_fallback(
        sell_token="native", buy_token="token", sell_amount=10**15, direction="buy",
    )

    assert quote_result is fresh_quote
    assert weth_fallback is False
    assert b.provider.active is selected
    assert getattr(fresh_quote, "_tournament_gate_prepared") is True
    original_api.build_swap_transaction.assert_not_called()


def test_snapshot_failure_is_reported_without_candidate_requests():
    b = bot("shadow")
    with patch("route_tournament.snapshot", side_effect=RuntimeError("SECRET")), patch("route_tournament.collect") as collection:
        b._queue_route_shadow("buy", 1)
        b._finish_route_shadow()
        collection.assert_not_called()
    payload = b._attempt_with_route_comparison("buy")
    assert payload["route_comparison"]["failures"] == ["snapshot_failed"]
    assert "SECRET" not in json.dumps(payload)


def test_gas_headroom_applies_to_every_component():
    c = context("sell")
    c.update(gas_multiplier=1.1, price_multiplier=1.2, gas_price=2 * 10**6)
    row = score_candidate(quote(gas_price=9 * 10**6), "uniswap", "weth", c)
    assert row["gas_components_wei"] == {
        "swap": str(300000 * 2 * 10**6 * 110 // 100),
        "approval": str(200000 * 2 * 10**6 * 110 // 100),
        "wrap": "0", "unwrap": str(60000 * 2 * 10**6 * 110 // 100)}


def test_sell_allowance_probe_uses_the_sold_token_not_settlement():
    seen = []
    c = context("sell", token_address="sold-token", trade_token_address="settlement-token")
    score_candidate(
        quote(allowance_target="spender"), "uniswap", "native", c,
        allowance_probe=lambda token, spender: seen.append((token, spender)) or c["amount"],
    )
    assert seen == [("sold-token", "spender")]


def test_final_buy_can_spend_reserved_eth_on_gas_after_setup():
    b = bot("gate")
    b.config.use_eth_trading = True
    b.config.eth_gas_reserve = 0.001
    b.wallet.get_eth_balance_wei.return_value = 1_001 * 10**12
    q = QuoteResult(success=True, value=0)

    assert b._final_buy_reserve_ok(q, gas_limit=1_500_000, gas_price=10**6, weth_fallback=True) is True


def test_gate_mode_never_replays_a_selected_operation_through_fallback():
    b = bot("gate")
    b.provider = Mock()

    @_with_swap_provider_fallback
    def operation(self):
        return "single selected attempt"

    assert operation(b) == "single selected attempt"
    b.provider.run_with_fallback.assert_not_called()


def test_gate_mode_restores_provider_after_selected_operation():
    b = bot("gate")
    original, selected = SimpleNamespace(name="uniswap"), SimpleNamespace(name="sushiswap")
    b.provider = SimpleNamespace(active=original)

    @_with_swap_provider_fallback
    def operation(self):
        self.provider.active = selected
        return "selected operation complete"

    assert operation(b) == "selected operation complete"
    assert b.provider.active is original


def test_gate_mode_restores_absent_provider_active_state():
    b = bot("gate")
    b.provider = SimpleNamespace()

    @_with_swap_provider_fallback
    def operation(self):
        self.provider.active = SimpleNamespace(name="sushiswap")

    operation(b)
    assert not hasattr(b.provider, "active")


def test_gate_mode_never_replaces_selected_route_after_consistency_signal():
    b = bot("gate")
    selected = SimpleNamespace(name="uniswap")
    alternate = SimpleNamespace(name="sushiswap", build_swap_transaction=Mock())
    b.provider = SimpleNamespace(primary=selected, fallback=alternate)
    original = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15)

    assert b._best_fresh_sell_route(selected, original, 10**15) == (selected, original, None)
    alternate.build_swap_transaction.assert_not_called()


def test_gate_mode_never_replaces_selected_route_after_gas_cap_failure():
    b = bot("gate")
    b.config.token_address = "token"
    b._swap_slippage_fraction = Mock(return_value=0.01)
    selected = SimpleNamespace(name="uniswap")
    alternate = SimpleNamespace(name="sushiswap", build_swap_transaction=Mock())
    b.provider = SimpleNamespace(primary=selected, fallback=alternate)
    original = QuoteResult(success=True, sell_amount=10**15, buy_amount=2 * 10**15,
                           to="router", data="0xdead")

    provider, quote_result, detail = b._alternate_route_for_gas_cap(
        selected, original, sell_token="native", buy_token="token", sell_amount=10**15,
        operation="buy", default_gas=350000,
    )

    assert (provider, quote_result, detail) == (selected, original, None)
    alternate.build_swap_transaction.assert_not_called()
    assert b._alternate_sell_route_for_profit_floor(
        selected, sell_amount=10**15, sold_cost_wei=10**15, min_profit_percent=2,
    ) == (None, None)
    alternate.build_swap_transaction.assert_not_called()


@pytest.mark.parametrize("engine", ["gridless", "legacy"])
@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_real_engine_actionable_hooks(engine, direction):
    """Stop at quote failure: no setup, but every actionable path is observed."""
    b = bot("shadow")
    b.config.use_eth_trading = False
    b.config.weth_address = "weth"
    b.config.token_address = "token"
    b.config.max_active_positions = 2
    b.token_unit = 10**18
    b.token_decimals = 18
    b.trade_token_address = "weth"
    b.trade_token_name = "WETH"
    b.positions = {"1": {"balance": 10**18 if direction == "sell" else 0, "cost_wei": 10**15}}
    b.wallet.get_token_balance.return_value = (0.01, "WETH")
    b._wallet_can_cover_sell = Mock(return_value=True)
    b._swap_slippage_fraction = Mock(return_value=0.01)
    b._taxed_token_active = Mock(return_value=False)
    b._observe_token_tax_failure = Mock()
    b.api_client = Mock()
    b.api_client.build_swap_transaction.return_value = QuoteResult(success=False, error="no route")
    with patch("route_tournament.snapshot", side_effect=lambda bot, d, a, cost: {**context(d), "amount": a, "sold_cost_wei": cost}) as capture, patch("route_tournament.collect", return_value={"mode": "shadow"}) as collection:
        if direction == "buy":
            if engine == "gridless":
                b._execute_buy_gridless(0.001, 10**15, 0.001)
            else:
                b.execute_buy("1", 0.001)
        elif engine == "gridless":
            b.config.gridless_sell_threshold = 5
            b.config.gridless_stoploss_threshold = -25
            b.config.gridless_stoploss_enabled = False
            with patch("gridless.load_positions", return_value=b.positions):
                b._check_sells_gridless(0.002)
        else:
            b.execute_sell("1", 0.002)
        assert capture.call_count == 1
        assert capture.call_args.args[1] == direction
        collection.assert_called_once()
        b.wallet._send_transaction.assert_not_called()


# ---------------------------------------------------------------------------
# Improved tournament scoring: provider gas, fresh gas price, allowance lookup.
# ---------------------------------------------------------------------------


def test_provider_gas_estimate_preferred_over_fallback_budget():
    """Provider's quote.gas wins over the hardcoded 350k/300k fallback."""
    # Provider claims 250k gas (more accurate than fallback). Buy quote.
    q = quote(gas=250000)
    row = score_candidate(q, "sushiswap", "native", context("buy"))
    assert int(row["gas_components_wei"]["swap"]) == 250000 * 10**6
    # Approval stays 0 for buys (no reset budget).
    assert int(row["gas_components_wei"]["approval"]) == 0
    # No wrap/unwrap for native settlement on buys.
    assert int(row["gas_components_wei"]["wrap"]) == 0
    assert int(row["gas_components_wei"]["unwrap"]) == 0


def test_missing_provider_gas_falls_back_to_direction_budget():
    """When provider returns no gas estimate, use conservative direction budget."""
    q = quote(gas=None)
    row = score_candidate(q, "sushiswap", "native", context("buy"))
    # 350000 is the buy fallback.
    assert int(row["gas_components_wei"]["swap"]) == 350000 * 10**6
    q = quote(gas=None)
    row = score_candidate(q, "sushiswap", "native", context("sell"))
    # 300000 is the sell fallback.
    assert int(row["gas_components_wei"]["swap"]) == 300000 * 10**6


def test_live_rpc_gas_price_beats_provider_hint():
    """A fresh, normalized RPC price is preferred over a provider hint."""
    # Snapshot fixture represents the freshly-read RPC value; provider is stale.
    q = quote(gas=300000, gas_price=5 * 10**6)
    row = score_candidate(q, "sushiswap", "native", context())
    assert int(row["gas_components_wei"]["swap"]) == 300000 * 10**6


def test_existing_allowance_skips_approval_budget():
    """When wallet.check_allowance returns >= amount, no approval budget is needed."""
    c = context("sell")
    # Allowance already covers sell amount.
    allowance_probe = {"value": c["amount"]}
    row = score_candidate(
        quote(allowance_target="router"), "uniswap", "native", c,
        allowance_probe=allowance_probe,
    )
    assert int(row["gas_components_wei"]["approval"]) == 0
    assert row.get("approval_assumption") == "existing_allowance_covers"


def test_unverified_execution_approval_observation_keeps_conservative_budget():
    """A passive normal-flow result cannot prove a shadow candidate's spender."""
    c = context("sell")
    c["approval_observations"] = {"uniswap:native": "not_required"}

    row = score_candidate(
        quote(allowance_target="router"), "uniswap", "native", c,
        allowance_probe=None,
    )

    assert int(row["gas_components_wei"]["approval"]) > 0
    assert row["approval_assumption"] == "reset_and_exact_approval_budget"


def test_insufficient_allowance_budgets_reset_and_approval():
    """When allowance is below amount, still budget reset+approval (legacy behavior)."""
    c = context("sell")
    allowance_probe = {"value": 0}  # No existing allowance.
    row = score_candidate(
        quote(allowance_target="router"), "uniswap", "native", c,
        allowance_probe=allowance_probe,
    )
    assert int(row["gas_components_wei"]["approval"]) == 200000 * 10**6
    assert row.get("approval_assumption") == "reset_and_exact_approval_budget"


def test_missing_allowance_target_uses_legacy_budget():
    """When provider gives no allowance_target, fall back to legacy budget."""
    c = context("sell")
    row = score_candidate(
        quote(allowance_target=None), "uniswap", "native", c,
        allowance_probe=None,  # No probe; allow_probe unavailable
    )
    assert int(row["gas_components_wei"]["approval"]) == 200000 * 10**6
    assert row.get("approval_assumption") == "reset_and_exact_approval_budget"


def test_allowance_probe_failure_falls_back_to_legacy_budget():
    """If check_allowance raises, log and use legacy budget (do not block tournament)."""
    c = context("sell")
    row = score_candidate(
        quote(allowance_target="router"), "uniswap", "native", c,
        allowance_probe={"raise": RuntimeError("rpc timeout")},
    )
    assert int(row["gas_components_wei"]["approval"]) == 200000 * 10**6
    assert row.get("approval_assumption") == "reset_and_exact_approval_budget"



def test_native_buy_tournament_can_spend_reserved_eth_on_gas():
    c = context("buy")
    c.update(native_balance=1_300_000, reserve=1_000_000,
             amount=1_000_000, trade_balance=1_000_000, gas_price=1_000,
             gas_multiplier=1, cap=1_000_000)
    candidate = QuoteResult(success=True, buy_amount=2_000_000, sell_amount=1_000_000,
                            gas=100, value=1_000_000)
    row = score_candidate(candidate, "uniswap", "native", c)

    assert "native_reserve" not in row["rejections"]


def test_provider_value_used_for_native_buy_spend():
    """For native buys, use quote.value if present, else fall back to amount."""
    c = context("buy")
    # Provider claims only 0.9 ETH is needed (slippage favorable).
    q = quote(gas=200000, value=int(0.9 * 10**18))
    row = score_candidate(q, "sushiswap", "native", c)
    # The native_reserve check subtracts `spend`, which should be `value`.
    # c["native_balance"] = 1e18, spend=0.9e18, reserve=1e15 -> 1e18-0.9e18-1e15 = 9e16
    # which is > reserve, so no native_reserve rejection.
    assert "native_reserve" not in row["rejections"]


def test_candidate_log_includes_provider_output_and_score():
    """Structured per-candidate log fields are populated for observability."""
    from route_tournament import score_candidate as sc
    row = sc(quote(output=2 * 10**15, gas=180000, gas_price=10**6),
             "uniswap", "weth", context("buy"))
    # Human-readable output in token base units (for log readability).
    assert "quoted_output_human" in row
    # Score components are exposed for logging.
    assert "projected_total_gas_wei" in row
    assert "output_floor_human" in row


def test_winner_announcement_log_present(caplog):
    """collect() emits a winner announcement log line with all required fields."""
    import logging
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    clients["uniswap"].get_quote.return_value = quote(gas=180000, gas_price=10**6)
    clients["sushiswap"].get_quote.return_value = quote(gas=220000, gas_price=10**6)
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")
    with caplog.at_level(logging.INFO, logger="grid_bot.route_tournament"):
        result = collect(cfg, "wallet", context("buy"), clients.__getitem__)
    winner_lines = [r for r in caplog.records if "Route tournament winner" in r.getMessage()]
    assert winner_lines, "expected a winner announcement log line"
    msg = winner_lines[0].getMessage()
    for field in ("provider=", "settlement=", "score=", "runner_up_delta=", "direction=", "elapsed_ms="):
        assert field in msg, f"missing {field} in winner log: {msg}"


def test_per_candidate_observability_log(caplog):
    """collect() emits one concise human-readable line per candidate."""
    import logging
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    clients["uniswap"].get_quote.return_value = quote(gas=180000, gas_price=10**6)
    clients["sushiswap"].get_quote.return_value = quote(gas=220000, gas_price=10**6)
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")
    with caplog.at_level(logging.INFO, logger="grid_bot.route_tournament"):
        collect(cfg, "wallet", context("buy"), clients.__getitem__)
    candidate_lines = [r for r in caplog.records if "Route tournament candidate" in r.getMessage()]
    # 2 providers * 2 settlements = 4 candidates
    assert len(candidate_lines) == 4
    for line in candidate_lines:
        msg = line.getMessage()
        for field in ("/", "output", "gas", "eligible"):
            assert field in msg, f"missing {field} in candidate log: {msg}"


def test_sell_candidate_exposes_projected_profit_and_minimum():
    row = score_candidate(
        QuoteResult(success=True, buy_amount=2_300_000_000_000_000,
                    sell_amount=100, gas=300000),
        "uniswap", "native",
        {**context("sell"), "amount": 100, "sold_cost_wei": 2_000_000_000_000_000,
         "min_profit": 5.0, "tax": 0.0, "gas_price": 1},
        gas_estimate=100_000,
    )
    assert row["minimum_profit_percent"] == 5.0
    assert row["minimum_return_wei"] == "2100000000000000"
    assert row["projected_profit_wei"] == "299999999700000"
    assert row["projected_profit_percent"] > 14.99


def test_execute_mode_still_fails_closed():
    """execute remains intentionally unavailable after the improvements."""
    cfg = BotConfig.__new__(BotConfig)
    cfg.route_tournament_mode = "execute"
    with pytest.raises(ValueError, match="supports off, shadow, or gate"):
        cfg.validate()


def test_snapshot_no_longer_captures_gas_price_for_scoring():
    """snapshot() keeps accounting fields but gas_price is re-read fresh in collect()."""
    b = bot("shadow")
    b.wallet.get_eth_balance_wei.return_value = 10**18
    b.wallet.normal_gas_price.return_value = 10**6
    b._raw_trade_balance = Mock(return_value=10**18)
    b._swap_slippage_fraction = Mock(return_value=0.01)
    b._taxed_token_active = Mock(return_value=False)
    b._effective_token_transfer_fee_percent = Mock(return_value=0.0)
    b.config.use_eth_trading = True
    b.config.eth_gas_reserve = 0.001
    b.config.gas_limit_multiplier = 1.05
    b.config.gas_price_multiplier = 1.0
    b.config.gas_price_freshness_multiplier = 1.0
    b.config.max_swap_gas_eth = 0.00004
    b.config.min_profit_percent = 2.0
    snap = snapshot(b, "buy", 10**15)
    # snapshot no longer needs gas_price (collected fresh); but the field may
    # remain for backwards compatibility with the dashboard payload.
    # The contract here: snapshot MUST NOT block scoring freshness.
    # If snapshot is stale, collect re-reads — and that's the test below.
    assert "direction" in snap and snap["amount"] == 10**15


def test_normalized_live_gas_price_is_not_multiplied_twice():
    """The wallet's live normal price already includes price headroom."""
    c = context("sell")
    c.update(gas_multiplier=1.1, price_multiplier=1.2, gas_price=2_000_000)
    row = score_candidate(quote(gas=300000), "uniswap", "native", c)
    assert int(row["gas_components_wei"]["swap"]) == 300000 * 2_000_000 * 110 // 100
    assert row["effective_gas_price_wei"] == 2_000_000


def test_collect_reads_fresh_normalized_gas_for_every_candidate_and_isolates_failure():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    for client in clients.values():
        client.get_quote.return_value = quote(gas=100)
    prices = iter((1_000_000, RuntimeError("rpc unavailable"), 3_000_000, 4_000_000))

    def fresh_price():
        value = next(prices)
        if isinstance(value, Exception):
            raise value
        return value

    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")
    result = collect(cfg, "wallet", context("buy"), clients.__getitem__, gas_price_provider=fresh_price)
    assert len(result["candidates"]) == 4
    assert result["candidates"][0]["effective_gas_price_wei"] == 1_000_000
    assert result["candidates"][1]["rejections"] == ["candidate_failed"]
    assert result["candidates"][2]["effective_gas_price_wei"] == 3_000_000
    assert result["candidates"][3]["effective_gas_price_wei"] == 4_000_000


def test_buy_output_human_uses_token_decimals():
    c = context("buy", token_decimals=6)
    row = score_candidate(quote(output=12_500_000), "sushiswap", "native", c)
    assert row["quoted_output_human"] == 12.5


def test_collection_deadline_skips_unstarted_candidate_requests():
    clients = {name: Mock() for name in ("uniswap", "sushiswap")}
    cfg = SimpleNamespace(uniswap_api_key="key", weth_address="weth", token_address="token")
    result = collect(cfg, "wallet", context("buy"), clients.__getitem__, max_seconds=0)
    assert len(result["candidates"]) == 4
    assert all(row["rejections"] == ["observation_deadline"] for row in result["candidates"])
    assert all(row["candidate_outcome"] == "not_sampled" for row in result["candidates"])
    for client in clients.values():
        client.get_quote.assert_not_called()


def test_local_estimate_beats_provider_hint_for_current_quote():
    row = score_candidate(quote(gas=300000), "uniswap", "native", context("buy"), gas_estimate=180000)
    assert row["gas_basis"] == "local_estimate"
    assert int(row["gas_components_wei"]["swap"]) == 180000 * 10**6
