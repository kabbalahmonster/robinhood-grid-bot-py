import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from config import BotConfig, load_config
from grid_bot import GridBot, _with_swap_provider_fallback
from route_tournament import collect, score_candidate
from swap_provider import FallbackSwapProvider
from zero_x import QuoteResult


def context(direction="buy", **overrides):
    return dict(direction=direction, amount=10**15, sold_cost_wei=10**15,
                native_trading=True, native_balance=10**18, trade_balance=10**18,
                gas_price=10**6, gas_multiplier=1, price_multiplier=1,
                reserve=10**15, cap=10**14, slippage=0.01, tax=0.02,
                min_profit=2, **overrides)


def quote(output=2 * 10**15, **kwargs):
    return QuoteResult(success=True, buy_amount=output, sell_amount=10**15,
                       gas=300000, **kwargs)


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
    c = context()
    buy = score_candidate(quote(), "sushiswap", "weth", c)
    floor = 2 * 10**15 * 99 * 98 // 10000
    gas = (350000 + 200000 + 60000) * 10**6
    assert int(buy["output_floor_raw"]) == floor
    assert Decimal(buy["projected_net_score"]) == Decimal(floor) * 10**18 / (10**15 + gas)
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

    def observe(*args):
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
    c.update(gas_multiplier=1.1, price_multiplier=1.2)
    row = score_candidate(quote(gas_price=2 * 10**6), "uniswap", "weth", c)
    assert row["gas_components_wei"] == {
        "swap": str(300000 * 2 * 10**6 * 132 // 100),
        "approval": str(200000 * 2 * 10**6 * 132 // 100),
        "wrap": "0", "unwrap": str(60000 * 2 * 10**6 * 132 // 100)}


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
