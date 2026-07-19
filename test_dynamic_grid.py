"""
Test script for Dynamic Grid Mode (Gridless) implementation.

Tests the following scenarios:
1. Empty grid triggers initial buy
2. Price drop triggers second buy
3. Price recovery triggers sells
4. Stop loss triggers when enabled
5. Rate limiting prevents rapid buys
6. Max position limit enforced
"""

import json
import time
import tempfile
import os
from decimal import Decimal

from dynamic_grid import (
    DynamicGridCalculator, DynamicGridConfig, DynamicGridState, DynamicPosition
)


class TestDynamicGrid:
    """Test suite for dynamic grid functionality."""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tests = []
    
    def test(self, name, condition, details=""):
        """Record a test result."""
        if condition:
            self.passed += 1
            status = "✅ PASS"
        else:
            self.failed += 1
            status = "❌ FAIL"
        
        self.tests.append((name, status, details))
        return condition
    
    def print_results(self):
        """Print all test results."""
        print("\n" + "="*70)
        print("DYNAMIC GRID MODE TEST RESULTS")
        print("="*70)
        
        for name, status, details in self.tests:
            print(f"{status}: {name}")
            if details:
                print(f"       {details}")
        
        print("-"*70)
        print(f"TOTAL: {self.passed + self.failed} tests | {self.passed} passed | {self.failed} failed")
        print("="*70)
        
        return self.failed == 0


def test_pnl_calculation():
    """Test P&L calculation logic."""
    print("\n--- Testing P&L Calculation ---")
    tester = TestDynamicGrid()
    
    # Test 1: Profit scenario
    pnl = DynamicGridCalculator.calculate_pnl_percent(100, 110)
    tester.test("P&L: 10% profit", abs(pnl - 10.0) < 0.001, f"Expected 10%, got {pnl}%")
    
    # Test 2: Loss scenario
    pnl = DynamicGridCalculator.calculate_pnl_percent(100, 90)
    tester.test("P&L: 10% loss", abs(pnl - (-10.0)) < 0.001, f"Expected -10%, got {pnl}%")
    
    # Test 3: Break-even
    pnl = DynamicGridCalculator.calculate_pnl_percent(100, 100)
    tester.test("P&L: Break-even", abs(pnl - 0.0) < 0.001, f"Expected 0%, got {pnl}%")
    
    # Test 4: Edge case - zero buy price
    pnl = DynamicGridCalculator.calculate_pnl_percent(0, 100)
    tester.test("P&L: Zero buy price protection", pnl == 0.0, "Should return 0 for zero buy price")
    
    # Test 5: Negative buy price
    pnl = DynamicGridCalculator.calculate_pnl_percent(-100, 100)
    tester.test("P&L: Negative buy price", pnl == 0.0, "Should return 0 for negative buy price")
    
    # Test 6: Large profit
    pnl = DynamicGridCalculator.calculate_pnl_percent(1, 1000)
    tester.test("P&L: Large profit (99900%)", abs(pnl - 99900.0) < 0.1, f"Expected 99900%, got {pnl}%")
    
    return tester.print_results()


def test_top_position_identification():
    """Test top position (lowest buy price) identification."""
    print("\n--- Testing Top Position Identification ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(enabled=True)
    state = DynamicGridState(config)
    
    # Create positions with different buy prices
    pos1 = DynamicPosition(id=0, buy_price=100.0, sell_target=110.0, stop_loss=90.0, status="HOLDING")
    pos2 = DynamicPosition(id=1, buy_price=90.0, sell_target=99.0, stop_loss=81.0, status="HOLDING")
    pos3 = DynamicPosition(id=2, buy_price=110.0, sell_target=121.0, stop_loss=99.0, status="HOLDING")
    pos4 = DynamicPosition(id=3, buy_price=80.0, sell_target=88.0, stop_loss=72.0, status="SOLD")  # Not holding
    
    state.add_position(pos1)
    state.add_position(pos2)
    state.add_position(pos3)
    state.add_position(pos4)
    
    # Find top position - should be pos2 (lowest buy price among HOLDING)
    top = DynamicGridCalculator.find_top_position(state.positions)
    
    tester.test("Top position: Lowest buy price", 
                top is not None and top.id == 1, 
                f"Expected ID 1 (buy_price=90), got ID {top.id if top else None}")
    
    # Test with no positions
    empty_state = DynamicGridState(config)
    top = DynamicGridCalculator.find_top_position(empty_state.positions)
    tester.test("Top position: Empty positions", 
                top is None, 
                "Should return None for empty positions")
    
    # Test with no holding positions
    sold_pos = DynamicPosition(id=0, buy_price=100.0, sell_target=110.0, status="SOLD")
    state2 = DynamicGridState(config)
    state2.add_position(sold_pos)
    top = DynamicGridCalculator.find_top_position(state2.positions)
    tester.test("Top position: No holding positions", 
                top is None, 
                "Should return None when no positions are holding")
    
    return tester.print_results()


def test_buy_triggers():
    """Test buy trigger logic."""
    print("\n--- Testing Buy Triggers ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(
        enabled=True,
        buy_threshold_percent=-10.0,  # Buy when down 10%
        max_active_positions=4,
        min_buy_interval_seconds=30
    )
    
    # Test 1: Empty grid triggers buy
    state = DynamicGridState(config)
    should_buy, reason, _ = DynamicGridCalculator.should_buy(
        state.positions, 100.0, config, 0
    )
    tester.test("Buy trigger: Empty grid", 
                should_buy, 
                f"Reason: {reason}")
    
    # Test 2: Price drop below threshold triggers buy
    pos = DynamicPosition(id=0, buy_price=100.0, sell_target=108.0, status="HOLDING")
    state.add_position(pos)
    
    # Price drops 15% from buy price (below -10% threshold)
    should_buy, reason, top = DynamicGridCalculator.should_buy(
        state.positions, 85.0, config, 0
    )
    tester.test("Buy trigger: Price drop below threshold", 
                should_buy, 
                f"Top P&L at 85: {DynamicGridCalculator.calculate_pnl_percent(100, 85):.1f}%")
    
    # Test 3: Price above threshold doesn't trigger
    should_buy, reason, _ = DynamicGridCalculator.should_buy(
        state.positions, 95.0, config, 0
    )
    tester.test("Buy trigger: Price above threshold blocked", 
                not should_buy, 
                f"P&L at 95: {DynamicGridCalculator.calculate_pnl_percent(100, 95):.1f}%")
    
    # Test 4: Rate limiting prevents rapid buys
    recent_time = time.time()
    should_buy, reason, _ = DynamicGridCalculator.should_buy(
        state.positions, 85.0, config, recent_time
    )
    tester.test("Buy trigger: Rate limiting prevents rapid buys", 
                not should_buy, 
                f"Reason: {reason}")
    
    # Test 5: Max positions limit
    for i in range(4):
        p = DynamicPosition(id=i, buy_price=100.0 - i*5, sell_target=110.0, status="HOLDING")
        state.add_position(p)
    
    # Allow enough time to pass
    old_time = time.time() - 60
    should_buy, reason, _ = DynamicGridCalculator.should_buy(
        state.positions, 70.0, config, old_time
    )
    tester.test("Buy trigger: Max positions enforced", 
                not should_buy, 
                f"Reason: {reason}")
    
    # Test 6: Dynamic mode disabled
    config_disabled = DynamicGridConfig(enabled=False)
    should_buy, reason, _ = DynamicGridCalculator.should_buy(
        state.positions, 70.0, config_disabled, 0
    )
    tester.test("Buy trigger: Disabled mode blocks buys", 
                not should_buy, 
                f"Reason: {reason}")
    
    return tester.print_results()


def test_sell_triggers():
    """Test sell trigger logic."""
    print("\n--- Testing Sell Triggers ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(
        enabled=True,
        sell_threshold_percent=8.0,  # Sell at 8% profit
        stop_loss_percent=5.0,  # Stop loss at 5% loss
    )
    
    pos = DynamicPosition(id=0, buy_price=100.0, sell_target=108.0, stop_loss=95.0, status="HOLDING")
    
    # Test 1: Profit target reached
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 108.0, config)
    tester.test("Sell trigger: Profit target reached", 
                should_sell, 
                f"Reason: {reason}")
    
    # Test 2: Profit target exceeded
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 115.0, config)
    tester.test("Sell trigger: Profit target exceeded", 
                should_sell, 
                f"Reason: {reason}")
    
    # Test 3: Below profit target - no sell
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 105.0, config)
    tester.test("Sell trigger: Below target - no sell", 
                not should_sell, 
                f"P&L: 5%, target: 8%")
    
    # Test 4: Stop loss triggered
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 94.0, config)
    tester.test("Sell trigger: Stop loss triggered", 
                should_sell, 
                f"Reason: {reason}")
    
    # Test 5: Exactly at stop loss
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 95.0, config)
    tester.test("Sell trigger: Exactly at stop loss (-5%)", 
                should_sell, 
                f"P&L: -5%, stop: 5%")
    
    # Test 6: Stop loss disabled (0%)
    config_no_sl = DynamicGridConfig(enabled=True, stop_loss_percent=0.0)
    pos_sl = DynamicPosition(id=1, buy_price=100.0, sell_target=108.0, stop_loss=0.0, status="HOLDING")
    should_sell, reason = DynamicGridCalculator.should_sell(pos_sl, 50.0, config_no_sl)
    tester.test("Sell trigger: Stop loss disabled", 
                not should_sell, 
                "Should not sell even with large loss when SL disabled")
    
    # Test 7: Non-holding position
    pos_empty = DynamicPosition(id=2, buy_price=100.0, sell_target=108.0, status="EMPTY")
    should_sell, reason = DynamicGridCalculator.should_sell(pos_empty, 150.0, config)
    tester.test("Sell trigger: Non-holding position blocked", 
                not should_sell, 
                f"Reason: {reason}")
    
    return tester.print_results()


def test_trailing_stop():
    """Test trailing stop functionality."""
    print("\n--- Testing Trailing Stop ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(
        enabled=True,
        sell_threshold_percent=10.0,
        use_trailing_stop=True,
        trailing_stop_percent=5.0,
        trailing_activation_percent=3.0
    )
    
    # Position peaks at 15% profit, then drops
    pos = DynamicPosition(
        id=0, 
        buy_price=100.0, 
        sell_target=110.0, 
        status="HOLDING",
        peak_pnl_percent=15.0  # Peak was 15%
    )
    
    # Test 1: Price at 10% (dropped 5% from peak, triggers trailing stop)
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 110.0, config)
    tester.test("Trailing stop: Triggered at 10% (dropped 5% from 15% peak)", 
                should_sell, 
                f"Peak: 15%, Current: 10%, Trail: 5%, Stop level: 10%")
    
    # Test 2: Price at 11% (only dropped 4% from peak, no trigger)
    should_sell, reason = DynamicGridCalculator.should_sell(pos, 111.0, config)
    tester.test("Trailing stop: Not triggered at 11% (only 4% drop)", 
                not should_sell, 
                "Only dropped 4% from peak, should not trigger")
    
    # Test 3: Peak not activated yet (below 3% activation)
    pos2 = DynamicPosition(
        id=1,
        buy_price=100.0,
        sell_target=110.0,
        status="HOLDING",
        peak_pnl_percent=2.0  # Below activation threshold
    )
    should_sell, reason = DynamicGridCalculator.should_sell(pos2, 100.0, config)
    tester.test("Trailing stop: Not activated below threshold", 
                not should_sell, 
                "Peak 2% < activation 3%, trailing stop not active")
    
    return tester.print_results()


def test_position_id_management():
    """Test position ID uniqueness and management."""
    print("\n--- Testing Position ID Management ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(enabled=True)
    state = DynamicGridState(config)
    
    # Create positions
    pos1 = state.create_new_position(100.0)
    pos2 = state.create_new_position(95.0)
    pos3 = state.create_new_position(90.0)
    
    # Test unique IDs
    ids = [pos1.id, pos2.id, pos3.id]
    tester.test("Position IDs: Unique IDs assigned", 
                len(ids) == len(set(ids)), 
                f"IDs: {ids}")
    
    # Test sequential IDs
    tester.test("Position IDs: Sequential (0, 1, 2)", 
                ids == [0, 1, 2], 
                f"Expected [0, 1, 2], got {ids}")
    
    # Test no collision after loading from dict
    state_dict = state.to_dict()
    state2 = DynamicGridState.from_dict(state_dict)
    
    pos4 = state2.create_new_position(85.0)
    tester.test("Position IDs: No collision after deserialization", 
                pos4.id == 3, 
                f"Expected ID 3, got {pos4.id}")
    
    return tester.print_results()


def test_state_persistence():
    """Test state serialization and deserialization."""
    print("\n--- Testing State Persistence ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(
        enabled=True,
        buy_threshold_percent=-12.0,
        sell_threshold_percent=9.0,
        stop_loss_percent=4.0
    )
    state = DynamicGridState(config)
    
    # Create and populate positions
    pos1 = state.create_new_position(100.0)
    pos1.status = "HOLDING"
    pos1.balance = 1000000000000000000  # 1 token
    pos1.cost = 1000000000  # 1 WETH in nano
    pos1.buy_tx = "0xabc123"
    
    pos2 = state.create_new_position(90.0)
    pos2.status = "HOLDING"
    pos2.balance = 2000000000000000000
    pos2.cost = 1800000000
    pos2.peak_pnl_percent = 15.0
    pos2.lowest_pnl_percent = -5.0
    
    # Close one position
    state.close_position(pos1.id, 110.0, 0.1, "0xsell123")
    
    # Serialize
    state_dict = state.to_dict()
    
    # Test serialization produces valid dict
    tester.test("State persistence: Serialization produces dict", 
                isinstance(state_dict, dict), 
                f"Type: {type(state_dict)}")
    
    # Test config preserved
    saved_config = state_dict.get("config", {})
    tester.test("State persistence: Config preserved", 
                saved_config.get("buy_threshold_percent") == -12.0, 
                f"Buy threshold: {saved_config.get('buy_threshold_percent')}")
    
    # Deserialize
    state2 = DynamicGridState.from_dict(state_dict)
    
    # Test positions restored
    tester.test("State persistence: Positions restored", 
                len(state2.positions) == 2, 
                f"Expected 2 positions, got {len(state2.positions)}")
    
    # Test position data preserved
    restored_pos2 = state2.positions.get(1)
    tester.test("State persistence: Position data preserved", 
                restored_pos2 is not None and 
                restored_pos2.balance == 2000000000000000000 and
                restored_pos2.peak_pnl_percent == 15.0, 
                f"Balance: {restored_pos2.balance if restored_pos2 else None}")
    
    # Test history preserved
    tester.test("State persistence: History preserved", 
                len(state2.position_history) == 1, 
                f"History count: {len(state2.position_history)}")
    
    # Test history data
    hist = state2.position_history[0]
    tester.test("State persistence: History data correct", 
                hist.status == "SOLD" and hist.sell_tx == "0xsell123", 
                f"Status: {hist.status}, Sell TX: {hist.sell_tx}")
    
    return tester.print_results()


def test_config_validation():
    """Test configuration validation."""
    print("\n--- Testing Config Validation ---")
    tester = TestDynamicGrid()
    
    # Test 1: Valid config
    config = DynamicGridConfig(
        enabled=True,
        buy_threshold_percent=-10.0,
        sell_threshold_percent=8.0,
        max_active_positions=4
    )
    is_valid, errors = DynamicGridCalculator.validate_config(config)
    tester.test("Config validation: Valid config passes", 
                is_valid and len(errors) == 0, 
                f"Errors: {errors}")
    
    # Test 2: Positive buy threshold (should be negative)
    config2 = DynamicGridConfig(
        enabled=True,
        buy_threshold_percent=5.0,  # Wrong: should be negative
        sell_threshold_percent=8.0
    )
    is_valid, errors = DynamicGridCalculator.validate_config(config2)
    tester.test("Config validation: Positive buy threshold rejected", 
                not is_valid and any("buy_threshold" in str(e) for e in errors), 
                f"Errors: {errors}")
    
    # Test 3: Negative sell threshold (should be positive)
    config3 = DynamicGridConfig(
        enabled=True,
        buy_threshold_percent=-10.0,
        sell_threshold_percent=-5.0  # Wrong: should be positive
    )
    is_valid, errors = DynamicGridCalculator.validate_config(config3)
    tester.test("Config validation: Negative sell threshold rejected", 
                not is_valid and any("sell_threshold" in str(e) for e in errors), 
                f"Errors: {errors}")
    
    # Test 4: Sell <= absolute buy (should be >)
    config4 = DynamicGridConfig(
        enabled=True,
        buy_threshold_percent=-10.0,
        sell_threshold_percent=5.0  # Wrong: should be > 10
    )
    is_valid, errors = DynamicGridCalculator.validate_config(config4)
    tester.test("Config validation: Sell <= abs(buy) rejected", 
                not is_valid, 
                f"Errors: {errors}")
    
    # Test 5: Zero max positions
    config5 = DynamicGridConfig(
        enabled=True,
        max_active_positions=0
    )
    is_valid, errors = DynamicGridCalculator.validate_config(config5)
    tester.test("Config validation: Zero max positions rejected", 
                not is_valid, 
                f"Errors: {errors}")
    
    # Test 6: Negative stop loss
    config6 = DynamicGridConfig(
        enabled=True,
        stop_loss_percent=-5.0
    )
    is_valid, errors = DynamicGridCalculator.validate_config(config6)
    tester.test("Config validation: Negative stop loss rejected", 
                not is_valid, 
                f"Errors: {errors}")
    
    # Test 7: Disabled mode skips validation
    config_disabled = DynamicGridConfig(enabled=False, buy_threshold_percent=5.0)
    is_valid, errors = DynamicGridCalculator.validate_config(config_disabled)
    tester.test("Config validation: Disabled mode skips validation", 
                is_valid, 
                "Should pass even with invalid settings when disabled")
    
    return tester.print_results()


def test_balance_conversions():
    """Test wei/nano-wei conversions."""
    print("\n--- Testing Balance Conversions ---")
    tester = TestDynamicGrid()
    
    # Test nano-WETH to WETH conversion
    cost_nano = 1000000000  # 1 WETH in nano
    cost_weth = cost_nano / 1e9
    tester.test("Balance: 1 WETH in nano-WETH", 
                abs(cost_weth - 1.0) < 0.0001, 
                f"1e9 nano = {cost_weth} WETH")
    
    # Test wei to ETH conversion
    balance_wei = 1000000000000000000  # 1 ETH
    tokens = balance_wei / 1e18
    tester.test("Balance: 1 token in wei", 
                abs(tokens - 1.0) < 0.0001, 
                f"1e18 wei = {tokens} tokens")
    
    # Test zero values
    pnl_eth = DynamicGridCalculator.calculate_pnl_eth(0, 1000000, 100, 110)
    tester.test("Balance: Zero cost returns zero P&L ETH", 
                pnl_eth == 0.0, 
                f"Got {pnl_eth}")
    
    # Test zero balance
    pnl_eth = DynamicGridCalculator.calculate_pnl_eth(1000000000, 0, 100, 110)
    tester.test("Balance: Zero balance returns zero P&L ETH", 
                pnl_eth == 0.0, 
                f"Got {pnl_eth}")
    
    return tester.print_results()


def test_gas_reserve():
    """Test gas reserve logic."""
    print("\n--- Testing Gas Reserve ---")
    tester = TestDynamicGrid()
    
    # Simulating the logic from _execute_buy_dynamic
    weth_balance = 0.01  # 0.01 WETH
    gas_reserve = 0.001  # Reserve 0.001 WETH for gas
    
    available_weth = max(0, weth_balance - gas_reserve)
    tester.test("Gas reserve: 0.001 WETH reserved", 
                abs(available_weth - 0.009) < 0.0001, 
                f"Available: {available_weth}")
    
    # Test with balance exactly at reserve
    weth_balance = 0.001
    available_weth = max(0, weth_balance - gas_reserve)
    tester.test("Gas reserve: Zero available at exact reserve", 
                available_weth == 0.0, 
                f"Available: {available_weth}")
    
    # Test with balance below reserve
    weth_balance = 0.0005
    available_weth = max(0, weth_balance - gas_reserve)
    tester.test("Gas reserve: Zero available below reserve", 
                available_weth == 0.0, 
                f"Available: {available_weth}")
    
    return tester.print_results()


def test_edge_cases():
    """Test edge cases and boundary conditions."""
    print("\n--- Testing Edge Cases ---")
    tester = TestDynamicGrid()
    
    config = DynamicGridConfig(enabled=True)
    
    # Test 1: Extreme price change (99% drop)
    pnl = DynamicGridCalculator.calculate_pnl_percent(100, 1)
    tester.test("Edge case: 99% price drop", 
                abs(pnl - (-99.0)) < 0.1, 
                f"P&L: {pnl}%")
    
    # Test 2: Extreme price increase (1000%)
    pnl = DynamicGridCalculator.calculate_pnl_percent(10, 110)
    tester.test("Edge case: 1000% price increase", 
                abs(pnl - 1000.0) < 0.1, 
                f"P&L: {pnl}%")
    
    # Test 3: Very small prices
    pnl = DynamicGridCalculator.calculate_pnl_percent(0.0000001, 0.00000011)
    tester.test("Edge case: Very small prices", 
                abs(pnl - 10.0) < 0.1, 
                f"P&L: {pnl}%")
    
    # Test 4: Position with very large ID
    pos = DynamicPosition(
        id=999999999,
        buy_price=100.0,
        sell_target=108.0,
        status="HOLDING"
    )
    tester.test("Edge case: Very large position ID", 
                pos.id == 999999999, 
                f"ID: {pos.id}")
    
    # Test 5: Very old position (ancient timestamp)
    ancient_time = 1000000  # ~1970
    pos2 = DynamicPosition(
        id=1,
        buy_price=100.0,
        sell_target=108.0,
        created_at=ancient_time,
        status="HOLDING"
    )
    tester.test("Edge case: Very old position timestamp", 
                pos2.created_at == ancient_time, 
                f"Created: {pos2.created_at}")
    
    # Test 6: Empty string for transaction hash
    pos3 = DynamicPosition(
        id=2,
        buy_price=100.0,
        sell_target=108.0,
        buy_tx="",
        status="HOLDING"
    )
    tester.test("Edge case: Empty transaction hash", 
                pos3.buy_tx == "", 
                f"TX: '{pos3.buy_tx}'")
    
    return tester.print_results()


def test_moonbag_handling():
    """Test moonbag token handling."""
    print("\n--- Testing Moonbag Handling ---")
    tester = TestDynamicGrid()
    
    # This tests the logic used in grid_bot.py _execute_sell_dynamic
    total_balance = 1000000000000000000  # 1 token in wei
    
    # Test 1: 10% moonbag
    moonbag_pct = 10
    moonbag_tokens = int(total_balance * moonbag_pct / 100)
    sell_tokens = total_balance - moonbag_tokens
    
    tester.test("Moonbag: 10% moonbag calculated correctly", 
                moonbag_tokens == 100000000000000000 and sell_tokens == 900000000000000000, 
                f"Moonbag: {moonbag_tokens}, Sell: {sell_tokens}")
    
    # Test 2: 0% moonbag (no moonbag)
    moonbag_pct = 0
    moonbag_tokens = int(total_balance * moonbag_pct / 100)
    sell_tokens = total_balance - moonbag_tokens
    
    tester.test("Moonbag: 0% moonbag sells all", 
                moonbag_tokens == 0 and sell_tokens == total_balance, 
                f"Moonbag: {moonbag_tokens}, Sell: {sell_tokens}")
    
    # Test 3: 100% moonbag (keep all, sell none)
    moonbag_pct = 100
    moonbag_tokens = int(total_balance * moonbag_pct / 100)
    sell_tokens = total_balance - moonbag_tokens
    
    tester.test("Moonbag: 100% moonbag keeps all", 
                moonbag_tokens == total_balance and sell_tokens == 0, 
                f"Moonbag: {moonbag_tokens}, Sell: {sell_tokens}")
    
    return tester.print_results()


def run_all_tests():
    """Run all test suites."""
    print("="*70)
    print("DYNAMIC GRID MODE (GRIDLESS) COMPREHENSIVE TEST SUITE")
    print("="*70)
    
    results = []
    results.append(test_pnl_calculation())
    results.append(test_top_position_identification())
    results.append(test_buy_triggers())
    results.append(test_sell_triggers())
    results.append(test_trailing_stop())
    results.append(test_position_id_management())
    results.append(test_state_persistence())
    results.append(test_config_validation())
    results.append(test_balance_conversions())
    results.append(test_gas_reserve())
    results.append(test_edge_cases())
    results.append(test_moonbag_handling())
    
    print("\n" + "="*70)
    print("OVERALL TEST SUMMARY")
    print("="*70)
    
    if all(results):
        print("✅ ALL TEST SUITES PASSED")
        return True
    else:
        print(f"❌ {sum(1 for r in results if not r)} TEST SUITE(S) FAILED")
        return False


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)
