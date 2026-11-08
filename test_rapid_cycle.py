import json

import pytest

from rapid_cycle import RapidCycleEngine, RapidCycleIdentity, RapidCycleJournal


def identity(mode="shadow", **overrides):
    values = dict(
        mode=mode, chain_id=4663, token_address="0xToken",
        wallet_address="0xWallet", principal_wei=10**15,
        entry_drop_percent=2.0, min_profit_percent=1.0,
    )
    values.update(overrides)
    return RapidCycleIdentity(**values)


def buy(engine, tokens, block=1, gas=10**12):
    economic_cost = engine.journal.identity.principal_wei + gas
    exit_gas = 10**12
    return engine.observe_buy(
        block_number=block, provider="uniswap", settlement="native",
        token_floor_raw=tokens, projected_gas_wei=gas,
        exit_provider="sushiswap", exit_settlement="native",
        exit_output_floor_wei=int(economic_cost * 1.02) + exit_gas,
        exit_projected_gas_wei=exit_gas, exit_block_number=block,
        observed_at=f"2026-01-01T00:00:0{block}+00:00",
    )


def sell(engine, output, block=4, gas=10**12):
    return engine.observe_sell(
        block_number=block, provider="sushiswap", settlement="native",
        output_floor_wei=output, projected_gas_wei=gas,
        observed_at=f"2026-01-01T00:00:0{block}+00:00",
    )


def test_shadow_requires_warmup_drop_and_exit_confidence_then_opens_shot(tmp_path):
    engine = RapidCycleEngine(RapidCycleJournal(tmp_path / "state.json", identity()))
    assert buy(engine, 1_000_000, 1) is None
    assert buy(engine, 1_010_000, 2) is None
    action = buy(engine, 1_030_000, 3)
    assert action["type"] == "buy"
    assert action["paper_filled"] is True
    assert action["position_id"] == "paper-1"
    assert engine.state["status"] == "open"
    assert engine.state["position"] is None
    assert engine.state["positions"][0]["token_amount_raw"] == "1030000"
    assert json.loads((tmp_path / "state.json").read_text())["status"] == "open"


def test_entry_drop_never_opens_without_a_complete_profitable_exit(tmp_path):
    engine = RapidCycleEngine(RapidCycleJournal(tmp_path / "state.json", identity()))
    for block, tokens in enumerate((1_000_000, 1_010_000, 1_030_000), 1):
        action = engine.observe_buy(
            block_number=block, provider="uniswap", settlement="native",
            token_floor_raw=tokens, projected_gas_wei=10**12,
            observed_at=f"2026-01-01T00:00:0{block}+00:00",
        )
    assert action is None
    assert engine.state["status"] == "flat"
    assert engine.state["completed_cycles"] == []
    assert engine.state["round_trip_confidence_streak"] == 0
    assert engine.state["last_observation"]["round_trip_qualified"] is False


def test_shadow_can_take_repeated_independent_exit_qualified_shots(tmp_path):
    engine = RapidCycleEngine(RapidCycleJournal(
        tmp_path / "state.json", identity(max_open_positions=10)
    ))
    for offset in (0, 3):
        assert buy(engine, 1_000_000, 1 + offset) is None
        assert buy(engine, 1_010_000, 2 + offset) is None
        action = buy(engine, 1_030_000, 3 + offset)
        assert action["position_id"] == f"paper-{1 + offset // 3}"
        assert engine.state["status"] == "open"
    assert len(engine.state["positions"]) == 2
    for position in list(engine.state["positions"]):
        cost = int(position["cost_wei"])
        action = engine.observe_sell(
            block_number=9, provider="sushiswap", settlement="native",
            output_floor_wei=int(cost * 1.02) + 10**12,
            projected_gas_wei=10**12, position_id=position["position_id"],
        )
        assert action["paper_filled"] is True
    assert len(engine.state["completed_cycles"]) == 2
    assert engine.state["positions"] == []
    assert engine.state["status"] == "flat"


def test_shadow_hard_caps_ten_open_positions(tmp_path):
    rapid_identity = identity(
        max_open_positions=10, entry_drop_percent=0.01,
        min_confidence_samples=1,
    )
    engine = RapidCycleEngine(
        RapidCycleJournal(tmp_path / "ten.json", rapid_identity),
        min_buy_samples=2,
    )
    assert buy(engine, 1_000_000, 1) is None
    for index in range(10):
        action = buy(engine, 1_010_000 + index * 10_000, 2 + index)
        assert action["position_id"] == f"paper-{index + 1}"
    samples_at_cap = engine.state["buy_sample_count"]
    assert buy(engine, 2_000_000, 20) is None
    assert len(engine.state["positions"]) == 10
    assert engine.state["buy_sample_count"] == samples_at_cap
    assert engine.shadow_exit_candidate(0)["position_id"] == "paper-1"
    assert engine.shadow_exit_candidate(9)["position_id"] == "paper-10"
    assert engine.shadow_exit_candidate(10)["position_id"] == "paper-1"


def test_shadow_sell_uses_floor_minus_sell_gas_and_records_cycle(tmp_path):
    engine = RapidCycleEngine(RapidCycleJournal(tmp_path / "state.json", identity()))
    buy(engine, 1_000_000, 1)
    buy(engine, 1_010_000, 2)
    buy(engine, 1_030_000, 3)
    position = engine.state["positions"][0]
    cost = int(position["cost_wei"])
    assert engine.observe_sell(
        block_number=4, provider="sushiswap", settlement="native",
        output_floor_wei=cost + 10**12, projected_gas_wei=10**12,
        position_id=position["position_id"],
    ) is None
    action = engine.observe_sell(
        block_number=5, provider="sushiswap", settlement="native",
        output_floor_wei=int(cost * 1.02) + 10**12,
        projected_gas_wei=10**12, position_id=position["position_id"],
    )
    assert action["paper_filled"] is True
    assert engine.state["status"] == "flat"
    record = engine.state["completed_cycles"][0]
    assert record["profit_percent"] == pytest.approx(2.0)


def test_live_requests_actions_but_never_invents_settlement(tmp_path):
    engine = RapidCycleEngine(RapidCycleJournal(
        tmp_path / "live.json", identity("live")
    ))
    buy(engine, 1_000_000, 1)
    buy(engine, 1_010_000, 2)
    action = buy(engine, 1_030_000, 3)
    assert action["type"] == "buy"
    assert engine.state["status"] == "action_pending"
    assert engine.state["position"] is None
    engine.confirm_live_buy(
        position_id="7", token_amount_raw=1_020_000,
        cost_wei=1_002_000_000_000_000, tx_hash="0xbuy",
    )
    assert engine.state["status"] == "open"
    cost = int(engine.state["position"]["cost_wei"])
    action = sell(engine, int(cost * 1.02) + 10**12, gas=10**12)
    assert action["type"] == "sell"
    assert engine.state["status"] == "action_pending"
    engine.confirm_live_sell(
        received_wei=int(cost * 1.02), profit_wei=int(cost * 0.02),
        tx_hash="0xsell",
    )
    assert engine.state["status"] == "flat"
    assert engine.state["completed_cycles"][0]["sell_tx_hash"] == "0xsell"


def test_cancel_pending_buy_returns_flat_and_sell_returns_open(tmp_path):
    engine = RapidCycleEngine(RapidCycleJournal(
        tmp_path / "live.json", identity("live")
    ))
    buy(engine, 1_000_000, 1)
    buy(engine, 1_010_000, 2)
    buy(engine, 1_030_000, 3)
    engine.cancel_pending("guard rejected")
    assert engine.state["status"] == "flat"
    buy(engine, 1_000_000, 4)
    buy(engine, 1_010_000, 5)
    buy(engine, 1_030_000, 6)
    engine.confirm_live_buy(position_id="1", token_amount_raw=1_000_000, cost_wei=10**15)
    sell(engine, int(1.02 * 10**15) + 10**12, gas=10**12)
    engine.cancel_pending("fresh quote failed")
    assert engine.state["status"] == "open"


def test_shadow_journal_is_never_accepted_as_live(tmp_path):
    path = tmp_path / "state.json"
    shadow = RapidCycleEngine(RapidCycleJournal(path, identity("shadow")))
    buy(shadow, 1_000_000, 1)
    with pytest.raises(RuntimeError, match="identity/config changed"):
        RapidCycleJournal(path, identity("live"))


def test_changed_threshold_requires_explicit_new_journal(tmp_path):
    path = tmp_path / "state.json"
    engine = RapidCycleEngine(RapidCycleJournal(path, identity()))
    buy(engine, 1_000_000, 1)
    with pytest.raises(RuntimeError, match="identity/config changed"):
        RapidCycleJournal(path, identity(min_profit_percent=2.0))


def test_corrupt_and_unknown_schema_fail_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not-json")
    with pytest.raises(RuntimeError, match="unreadable"):
        RapidCycleJournal(path, identity())
    path.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(RuntimeError, match="schema is unsupported"):
        RapidCycleJournal(path, identity())


@pytest.mark.parametrize("version", [1, 2])
def test_older_experimental_journal_requires_explicit_archive(tmp_path, version):
    path = tmp_path / f"state-v{version}.json"
    path.write_text(json.dumps({"schema_version": version}))
    with pytest.raises(RuntimeError, match="archive it"):
        RapidCycleJournal(path, identity())


def test_buy_baseline_is_a_bounded_recent_window(tmp_path):
    rapid_identity = identity(entry_drop_percent=98, lookback_samples=3)
    engine = RapidCycleEngine(
        RapidCycleJournal(tmp_path / "window.json", rapid_identity),
        min_buy_samples=2,
    )
    buy(engine, 100_000, 1)
    buy(engine, 101_000, 2)
    buy(engine, 102_000, 3)
    buy(engine, 103_000, 4)
    window = [int(value) for value in engine.state["buy_unit_cost_window"]]
    assert len(window) == 3
    first_unit_cost = (10**15 + 10**12) * engine.UNIT_SCALE // 100_000
    assert first_unit_cost not in window
    assert int(engine.state["peak_unit_cost_scaled"]) == max(window)


def test_direct_engine_rejects_invalid_lookback_contract(tmp_path):
    with pytest.raises(ValueError, match="lookback_samples must be at least 2"):
        RapidCycleJournal(tmp_path / "bad.json", identity(lookback_samples=1))
    journal = RapidCycleJournal(tmp_path / "short.json", identity(lookback_samples=2))
    with pytest.raises(ValueError, match="cannot exceed"):
        RapidCycleEngine(journal, min_buy_samples=3)
