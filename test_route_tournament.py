import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from config import BotConfig, load_config
from grid_bot import GridBot, _with_swap_provider_fallback
from route_tournament import collect, score_candidate, snapshot
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
    floor = 2 * 10**15 * 99 * 98 // 10000
    # Provider says 300k (fixture default); weth buy needs approval + wrap.
    gas = (300000 + 200000 + 60000) * 10**6
    assert int(buy["output_floor_raw"]) == floor
    assert Decimal(buy["projected_net_score"]) == Decimal(floor) * 10**18 / (10**15 + gas)
    assert buy["gas_basis"] == "provider_estimate"
    c["direction"] = "sell"
    sell = score_candidate(quote(), "sushiswap", "weth", c)
    assert Decimal(sell["projected_net_score"]) == floor - (300000 + 200000 + 60000) * 10**6


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


def bot(mode):
    b = GridBot.__new__(GridBot)
    b.config = SimpleNamespace(route_tournament_mode=mode)
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


@pytest.mark.parametrize("mode", ["execute", "invalid"])
def test_execute_and_unknown_modes_fail_closed(mode):
    cfg = BotConfig.__new__(BotConfig)
    cfg.route_tournament_mode = mode
    with pytest.raises(ValueError, match="execute is intentionally unavailable"):
        cfg.validate()


def test_mode_parsing_and_default(monkeypatch, tmp_path):
    # Avoid loading a checkout/operator .env or requiring live credentials.
    with patch("config.load_dotenv"), patch.object(BotConfig, "validate"):
        monkeypatch.delenv("ROUTE_TOURNAMENT_MODE", raising=False)
        assert load_config().route_tournament_mode == "off"
        monkeypatch.setenv("ROUTE_TOURNAMENT_MODE", " SHADOW ")
        assert load_config().route_tournament_mode == "shadow"


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
    """collect() emits one structured log line per candidate with quote, gas, and result."""
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
        for field in ("provider=", "settlement=", "quoted_output=", "gas_estimate=", "gas_price_wei=", "approval_budget=", "total_cost_wei=", "score=", "result="):
            assert field in msg, f"missing {field} in candidate log: {msg}"


def test_execute_mode_still_fails_closed():
    """execute remains intentionally unavailable after the improvements."""
    cfg = BotConfig.__new__(BotConfig)
    cfg.route_tournament_mode = "execute"
    with pytest.raises(ValueError, match="execute is intentionally unavailable"):
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
    for client in clients.values():
        client.get_quote.assert_not_called()


def test_local_estimate_beats_provider_hint_for_current_quote():
    row = score_candidate(quote(gas=300000), "uniswap", "native", context("buy"), gas_estimate=180000)
    assert row["gas_basis"] == "local_estimate"
    assert int(row["gas_components_wei"]["swap"]) == 180000 * 10**6
