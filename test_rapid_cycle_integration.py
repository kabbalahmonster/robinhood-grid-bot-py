import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from grid_bot import GridBot
from rapid_cycle import RapidCycleEngine, RapidCycleIdentity, RapidCycleJournal


def engine(tmp_path, mode):
    identity = RapidCycleIdentity(
        mode=mode, chain_id=4663, token_address="0xtoken",
        wallet_address="0xwallet", principal_wei=10**15,
        entry_drop_percent=2.0, min_profit_percent=1.0,
    )
    return RapidCycleEngine(
        RapidCycleJournal(tmp_path / f"{mode}.json", identity), min_buy_samples=2
    )


def bare_bot(tmp_path, mode):
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(
        rapid_cycle_mode=mode,
        rapid_cycle_poll_every_cycles=1,
        rapid_cycle_min_profit_percent=1.0,
    )
    bot._rapid_cycle_engine = engine(tmp_path, mode)
    bot._rapid_cycle_lock = threading.Lock()
    bot._rapid_cycle_observer_active = False
    bot._safety_halted = False
    bot.round_count = 1
    bot.wallet = SimpleNamespace(
        has_unresolved_broadcast=Mock(return_value=False),
        _send_transaction=Mock(),
    )
    bot.session_buys = 0
    bot.session_sells = 0
    bot.dashboard_trades = []
    return bot


def wait_for_observer(bot):
    deadline = time.monotonic() + 2
    while bot._rapid_cycle_observer_active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bot._rapid_cycle_observer_active is False


def test_shadow_observer_can_paper_fill_but_never_broadcasts(tmp_path):
    bot = bare_bot(tmp_path, "shadow")
    observations = [
        {"block_number": 1, "provider": "uniswap", "settlement": "native",
         "output_floor_raw": 1_000_000, "projected_gas_wei": 10**12},
        {"block_number": 2, "provider": "sushiswap", "settlement": "native",
         "output_floor_raw": 1_030_000, "projected_gas_wei": 10**12},
    ]
    bot._collect_rapid_cycle_observation = Mock(side_effect=observations)
    bot._dispatch_rapid_cycle_observer()
    wait_for_observer(bot)
    bot.round_count += 1
    bot._dispatch_rapid_cycle_observer()
    wait_for_observer(bot)
    assert bot._rapid_cycle_engine.state["status"] == "open"
    assert bot._rapid_cycle_engine.state["position"]["kind"] == "paper"
    bot.wallet._send_transaction.assert_not_called()


def test_dashboard_snapshot_exposes_bounded_public_shadow_state(tmp_path):
    bot = bare_bot(tmp_path, "shadow")
    bot.token_unit = 10**6
    rapid = bot._rapid_cycle_engine
    rapid.observe_buy(
        block_number=1, provider="uniswap", settlement="native",
        token_floor_raw=1_000_000, projected_gas_wei=10**12,
    )
    rapid.observe_buy(
        block_number=2, provider="sushiswap", settlement="weth",
        token_floor_raw=1_030_000, projected_gas_wei=10**12,
    )

    snapshot = bot._rapid_cycle_dashboard_snapshot()

    assert snapshot["mode"] == "shadow"
    assert snapshot["status"] == "open"
    assert snapshot["principal_eth"] == 0.001
    assert snapshot["position"]["kind"] == "paper"
    assert snapshot["position"]["token_amount"] == 1.03
    assert snapshot["last_observation"]["direction"] == "buy"
    assert snapshot["last_observation"]["provider"] == "sushiswap"
    assert "token_amount_raw" not in snapshot["position"]
    assert "identity" not in snapshot


def test_observation_uses_read_only_execution_preflight_and_loss_visibility():
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(
        token_address="0xtoken", weth_address="0xweth",
        use_eth_trading=True, route_tournament_gate_timeout_seconds=12,
    )
    bot.token_decimals = 18
    bot.trade_token_address = "0xnative"
    bot.wallet = SimpleNamespace(
        address="0xwallet",
        normal_gas_price=Mock(return_value=10),
        check_allowance=Mock(return_value=0),
        w3=SimpleNamespace(eth=SimpleNamespace(block_number=99)),
    )
    context = {"direction": "sell", "amount": 123, "min_profit": 5}

    def fake_collect(_config, _address, passed, **kwargs):
        assert passed["execution_preflight"] is True
        assert passed["min_profit"] == -99.0
        assert callable(kwargs["gas_estimate_provider"])
        assert callable(kwargs["approval_gas_estimate_provider"])
        return {
            "tournament_id": "rapid",
            "selected_hypothetical_winner": {
                "provider": "uniswap", "settlement": "native",
            },
            "candidates": [{
                "provider": "uniswap", "settlement": "native",
                "validation_level": "quote_only", "output_floor_raw": "1100",
                "projected_total_gas_wei": "100", "projected_net_score": "1000",
            }],
        }

    with patch("route_tournament.snapshot", return_value=context), patch(
        "route_tournament.collect_execution_preflight", side_effect=fake_collect
    ):
        result = bot._collect_rapid_cycle_observation("sell", 123, 1000)
    assert result == {
        "block_number": 99, "provider": "uniswap", "settlement": "native",
        "output_floor_raw": 1100, "projected_gas_wei": 100,
        "projected_net_score": 1000, "tournament_id": "rapid",
    }


def test_live_buy_signal_is_requoted_through_existing_guarded_execution(tmp_path):
    bot = bare_bot(tmp_path, "live")
    rapid = bot._rapid_cycle_engine
    rapid.observe_buy(
        block_number=1, provider="uniswap", settlement="native",
        token_floor_raw=1_000_000, projected_gas_wei=10**12,
    )
    rapid.observe_buy(
        block_number=2, provider="uniswap", settlement="native",
        token_floor_raw=1_030_000, projected_gas_wei=10**12,
    )
    assert rapid.state["status"] == "action_pending"

    position = {"balance": 1_020_000, "cost_wei": 1_002_000_000_000_000}

    def guarded_buy(*_args, **_kwargs):
        bot.session_buys += 1
        bot.dashboard_trades.append({"tx_hash": "0xbuy"})

    bot._execute_buy_gridless = Mock(side_effect=guarded_buy)
    with patch("gridless.load_positions", side_effect=[{}, {"0": position}]):
        bot._consume_rapid_cycle_live_action(0.001)

    bot._execute_buy_gridless.assert_called_once()
    assert rapid.state["status"] == "open"
    assert rapid.state["position"]["position_id"] == "0"
    assert rapid.state["position"]["buy_tx_hash"] == "0xbuy"


def test_live_rejected_guard_cancels_signal_without_inventing_fill(tmp_path):
    bot = bare_bot(tmp_path, "live")
    rapid = bot._rapid_cycle_engine
    rapid.observe_buy(
        block_number=1, provider="uniswap", settlement="native",
        token_floor_raw=1_000_000, projected_gas_wei=10**12,
    )
    rapid.observe_buy(
        block_number=2, provider="uniswap", settlement="native",
        token_floor_raw=1_030_000, projected_gas_wei=10**12,
    )
    bot._execute_buy_gridless = Mock()
    with patch("gridless.load_positions", side_effect=[{}, {}]):
        bot._consume_rapid_cycle_live_action(0.001)
    assert rapid.state["status"] == "flat"
    assert rapid.state["completed_cycles"] == []


def test_live_sell_signal_uses_existing_guarded_sell_and_records_receipt(tmp_path):
    bot = bare_bot(tmp_path, "live")
    rapid = bot._rapid_cycle_engine
    rapid.state.update({
        "status": "action_pending",
        "pending_action": {"type": "buy", "trigger": {}},
    })
    rapid.journal.save()
    rapid.confirm_live_buy(
        position_id="0", token_amount_raw=1_000_000,
        cost_wei=10**15, tx_hash="0xbuy",
    )
    rapid.observe_sell(
        block_number=3, provider="sushiswap", settlement="native",
        output_floor_wei=1_021_000_000_000_000,
        projected_gas_wei=10**12,
    )
    assert rapid.state["status"] == "action_pending"
    position = {"balance": 1_000_000, "cost_wei": 10**15}

    def guarded_sell(*_args, **_kwargs):
        bot.session_sells += 1
        bot.dashboard_trades.append({
            "side": "sell", "eth_amount": 0.00102,
            "profit_eth": 0.00002, "tx_hash": "0xsell",
        })

    bot._attempt_gridless_sell_candidate = Mock(side_effect=guarded_sell)
    with patch("gridless.load_positions", side_effect=[{"0": position}, {}]):
        bot._consume_rapid_cycle_live_action(0.001)
    bot._attempt_gridless_sell_candidate.assert_called_once()
    assert rapid.state["status"] == "flat"
    assert rapid.state["completed_cycles"][0]["sell_tx_hash"] == "0xsell"


def test_restart_discards_stale_pending_buy_signal_without_execution(tmp_path):
    bot = bare_bot(tmp_path, "live")
    rapid = bot._rapid_cycle_engine
    rapid.observe_buy(
        block_number=1, provider="uniswap", settlement="native",
        token_floor_raw=1_000_000, projected_gas_wei=10**12,
    )
    rapid.observe_buy(
        block_number=2, provider="uniswap", settlement="native",
        token_floor_raw=1_030_000, projected_gas_wei=10**12,
    )
    with patch("gridless.load_positions", return_value={}):
        bot._validate_rapid_cycle_live_state()
    assert rapid.state["status"] == "flat"
    bot.wallet._send_transaction.assert_not_called()


def test_restart_fails_closed_if_pending_buy_has_durable_position(tmp_path):
    bot = bare_bot(tmp_path, "live")
    rapid = bot._rapid_cycle_engine
    rapid.observe_buy(
        block_number=1, provider="uniswap", settlement="native",
        token_floor_raw=1_000_000, projected_gas_wei=10**12,
    )
    rapid.observe_buy(
        block_number=2, provider="uniswap", settlement="native",
        token_floor_raw=1_030_000, projected_gas_wei=10**12,
    )
    with patch("gridless.load_positions", return_value={"0": {"balance": 1}}):
        with pytest.raises(RuntimeError, match="reconciliation required"):
            bot._validate_rapid_cycle_live_state()
