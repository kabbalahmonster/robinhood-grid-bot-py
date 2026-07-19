"""
Simple tests for gridless trading mode.
"""

import os
import json
import tempfile
import unittest
from unittest.mock import MagicMock

# Import gridless module
import gridless


class TestPNLCalculation(unittest.TestCase):
    """Test P&L calculations."""
    
    def test_pnl_zero_position(self):
        """P&L should be 0 for empty position."""
        pos = {'cost': 0, 'balance': 0}
        self.assertEqual(gridless.calculate_pnl(pos, 1.0), 0.0)
    
    def test_pnl_basic_profit(self):
        """Test basic profit calculation."""
        # Bought 10 tokens for 1 WETH = 0.1 WETH per token
        # cost = 1 WETH = 1e9 nano-WETH
        # balance = 10 tokens = 10e18 wei
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        # Current price = 0.11 WETH (10% profit)
        current_price = 0.11
        pnl = gridless.calculate_pnl(pos, current_price)
        self.assertAlmostEqual(pnl, 10.0, places=1)
    
    def test_pnl_basic_loss(self):
        """Test basic loss calculation."""
        # Bought 10 tokens for 1 WETH = 0.1 WETH per token
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        # Current price = 0.09 WETH (10% loss)
        current_price = 0.09
        pnl = gridless.calculate_pnl(pos, current_price)
        self.assertAlmostEqual(pnl, -10.0, places=1)
    
    def test_pnl_no_change(self):
        """P&L should be 0 when price hasn't changed."""
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.1  # Same as buy price
        pnl = gridless.calculate_pnl(pos, current_price)
        self.assertAlmostEqual(pnl, 0.0, places=5)


class TestShouldBuy(unittest.TestCase):
    """Test buy trigger logic."""
    
    def test_buy_when_no_positions(self):
        """Should buy when no positions exist."""
        config = MagicMock()
        config.max_active_positions = 5
        config.gridless_buy_threshold = -10.0
        config.gridless_sell_threshold = 5.0
        config.gridless_leading_edge = False
        
        positions = {}
        should_buy, reason = gridless.should_buy(positions, 1.0, config)
        self.assertTrue(should_buy)
        self.assertIn("Initial", reason)
    
    def test_buy_when_under_max_and_threshold_met(self):
        """Should buy when under max positions and top position at threshold."""
        config = MagicMock()
        config.max_active_positions = 5
        config.gridless_buy_threshold = -10.0
        config.gridless_sell_threshold = 5.0
        config.gridless_leading_edge = False
        
        # Position at -10% P&L
        positions = {
            '0': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        }
        current_price = 0.09  # -10% from 0.1 buy price
        should_buy, reason = gridless.should_buy(positions, current_price, config)
        self.assertTrue(should_buy)
        self.assertIn("threshold", reason)
    
    def test_no_buy_when_max_reached(self):
        """Should not buy when max positions reached."""
        config = MagicMock()
        config.max_active_positions = 2
        config.gridless_buy_threshold = -10.0
        config.gridless_sell_threshold = 5.0
        config.gridless_leading_edge = False
        
        positions = {
            '0': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000},
            '1': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000},
        }
        should_buy, reason = gridless.should_buy(positions, 0.09, config)
        self.assertFalse(should_buy)
        self.assertIn("Max", reason)
    
    def test_no_buy_when_above_threshold(self):
        """Should not buy when top position P&L above threshold."""
        config = MagicMock()
        config.max_active_positions = 5
        config.gridless_buy_threshold = -10.0
        config.gridless_sell_threshold = 5.0
        config.gridless_leading_edge = False
        
        # Position at -5% P&L (better than -10% threshold)
        positions = {
            '0': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        }
        current_price = 0.095  # -5% from 0.1 buy price
        should_buy, reason = gridless.should_buy(positions, current_price, config)
        self.assertFalse(should_buy)
    
    def test_leading_edge_buy(self):
        """Should buy on leading edge when single position is up 50% of sell threshold."""
        config = MagicMock()
        config.max_active_positions = 5
        config.gridless_buy_threshold = -10.0
        config.gridless_sell_threshold = 5.0  # Sell at +5%
        config.gridless_leading_edge = True
        
        # Single position, buy price = 0.1
        positions = {
            '0': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        }
        # Price at 0.1025 = +2.5% P&L (50% of 5% sell threshold)
        current_price = 0.1025
        should_buy, reason = gridless.should_buy(positions, current_price, config)
        self.assertTrue(should_buy)
        self.assertIn("Leading edge", reason)
    
    def test_no_leading_edge_when_disabled(self):
        """Should not buy on leading edge when feature disabled."""
        config = MagicMock()
        config.max_active_positions = 5
        config.gridless_buy_threshold = -10.0
        config.gridless_sell_threshold = 5.0
        config.gridless_leading_edge = False  # Disabled
        
        positions = {
            '0': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        }
        # Price at 0.1025 = +2.5% P&L
        current_price = 0.1025
        should_buy, reason = gridless.should_buy(positions, current_price, config)
        self.assertFalse(should_buy)


class TestShouldSell(unittest.TestCase):
    """Test sell trigger logic."""
    
    def test_sell_when_profit_target_met(self):
        """Should sell when P&L >= sell threshold."""
        config = MagicMock()
        config.gridless_sell_threshold = 5.0
        config.gridless_stoploss_enabled = False
        config.min_profit_percent = 1.5
        
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.11  # +10% from 0.1 buy price (clearly above 5% threshold)
        
        # quote_profit_eth needs to be high enough to pass min_profit check
        # cost = 1 WETH, min_profit = 1.5%, so need at least 0.015 WETH profit
        should_sell, reason = gridless.should_sell(pos, current_price, config, quote_profit_eth=0.02)
        self.assertTrue(should_sell)
        self.assertIn("PROFIT", reason)
    
    def test_no_sell_when_below_target(self):
        """Should not sell when P&L below sell threshold."""
        config = MagicMock()
        config.gridless_sell_threshold = 5.0
        config.gridless_stoploss_enabled = False
        config.min_profit_percent = 1.5
        
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.102  # +2% from 0.1 buy price
        
        should_sell, reason = gridless.should_sell(pos, current_price, config, quote_profit_eth=0.1)
        self.assertFalse(should_sell)
    
    def test_no_sell_when_quote_profit_too_low(self):
        """Should not sell when quote profit is too low."""
        config = MagicMock()
        config.gridless_sell_threshold = 5.0
        config.gridless_stoploss_enabled = False
        config.min_profit_percent = 1.5
        
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.11  # +10% from 0.1 buy price
        
        # quote_profit_eth too low (cost = 1 WETH, min_profit = 1.5%, need 0.015 WETH)
        should_sell, reason = gridless.should_sell(pos, current_price, config, quote_profit_eth=0.0001)
        self.assertFalse(should_sell)
        self.assertIn("quote", reason.lower())


class TestStoploss(unittest.TestCase):
    """Test stoploss trigger logic."""
    
    def test_stoploss_triggered(self):
        """Should sell when stoploss threshold reached."""
        config = MagicMock()
        config.gridless_sell_threshold = 5.0
        config.gridless_stoploss_enabled = True
        config.gridless_stoploss_threshold = -25.0
        config.min_profit_percent = 1.5
        
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.075  # -25% from 0.1 buy price
        
        should_sell, reason = gridless.should_sell(pos, current_price, config, quote_profit_eth=0.01)
        self.assertTrue(should_sell)
        self.assertIn("STOPLOSS", reason)
    
    def test_stoploss_not_triggered_when_disabled(self):
        """Should not sell on stoploss when disabled."""
        config = MagicMock()
        config.gridless_sell_threshold = 5.0
        config.gridless_stoploss_enabled = False
        config.gridless_stoploss_threshold = -25.0
        config.min_profit_percent = 1.5
        
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.075  # -25% from 0.1 buy price
        
        should_sell, reason = gridless.should_sell(pos, current_price, config, quote_profit_eth=0.01)
        self.assertFalse(should_sell)
    
    def test_stoploss_blocked_on_loss(self):
        """Should not sell stoploss if quote shows loss."""
        config = MagicMock()
        config.gridless_sell_threshold = 5.0
        config.gridless_stoploss_enabled = True
        config.gridless_stoploss_threshold = -25.0
        config.min_profit_percent = 1.5
        
        pos = {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000}
        current_price = 0.075  # -25% from 0.1 buy price
        
        should_sell, reason = gridless.should_sell(pos, current_price, config, quote_profit_eth=-0.01)
        self.assertFalse(should_sell)
        self.assertIn("loss", reason.lower())


class TestPositionManagement(unittest.TestCase):
    """Test position add/remove operations."""
    
    def setUp(self):
        """Create temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.original_file = gridless.POSITIONS_FILE
        gridless.POSITIONS_FILE = os.path.join(self.temp_dir, "gridless_positions.json")
    
    def tearDown(self):
        """Clean up temporary files."""
        gridless.POSITIONS_FILE = self.original_file
        import shutil
        shutil.rmtree(self.temp_dir)
    
    def test_add_position(self):
        """Test adding a position."""
        pos_id = gridless.add_position(1_000_000_000, 10_000_000_000_000_000_000)
        self.assertEqual(pos_id, '0')
        
        positions = gridless.load_positions()
        self.assertIn('0', positions)
        self.assertEqual(positions['0']['cost'], 1_000_000_000)
        self.assertEqual(positions['0']['balance'], 10_000_000_000_000_000_000)
    
    def test_add_multiple_positions(self):
        """Test adding multiple positions with auto-increment IDs."""
        id1 = gridless.add_position(1_000_000_000, 10_000_000_000_000_000_000)
        id2 = gridless.add_position(2_000_000_000, 20_000_000_000_000_000_000)
        
        self.assertEqual(id1, '0')
        self.assertEqual(id2, '1')
        
        positions = gridless.load_positions()
        self.assertEqual(len(positions), 2)
    
    def test_remove_position(self):
        """Test removing a position."""
        gridless.add_position(1_000_000_000, 10_000_000_000_000_000_000)
        
        result = gridless.remove_position('0')
        self.assertTrue(result)
        
        positions = gridless.load_positions()
        self.assertEqual(len(positions), 0)
    
    def test_remove_nonexistent_position(self):
        """Test removing a position that doesn't exist."""
        result = gridless.remove_position('999')
        self.assertFalse(result)
    
    def test_load_invalid_position(self):
        """Test that invalid positions are filtered out."""
        # Create file with invalid position
        with open(gridless.POSITIONS_FILE, 'w') as f:
            json.dump({
                '0': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000},
                '1': {'cost': -100, 'balance': 10_000_000_000_000_000_000},  # Invalid: negative cost
                '2': {'cost': 1_000_000_000, 'balance': -10},  # Invalid: negative balance
            }, f)
        
        positions = gridless.load_positions()
        self.assertEqual(len(positions), 1)
        self.assertIn('0', positions)


class TestMigration(unittest.TestCase):
    """Test grid to gridless migration."""
    
    def test_migrate_from_grid(self):
        """Test migrating grid positions to gridless."""
        grid_positions = {
            '0': {'balance': 10_000_000_000_000_000_000, 'cost': 1_000_000_000, 'buyMin': 0, 'buyMax': 100, 'sellMin': 110},
            '1': {'balance': 0, 'cost': 0, 'buyMin': 100, 'buyMax': 110, 'sellMin': 120},  # Empty, skip
            '2': {'balance': 20_000_000_000_000_000_000, 'cost': 2_000_000_000, 'buyMin': 110, 'buyMax': 120, 'sellMin': 130},
        }
        
        gridless_positions = gridless.migrate_from_grid(grid_positions)
        
        # Should only have 2 positions (skipped empty one)
        self.assertEqual(len(gridless_positions), 2)
        self.assertIn('0', gridless_positions)
        self.assertIn('1', gridless_positions)
        
        # Check values copied correctly
        self.assertEqual(gridless_positions['0']['balance'], 10_000_000_000_000_000_000)
        self.assertEqual(gridless_positions['0']['cost'], 1_000_000_000)
        self.assertEqual(gridless_positions['1']['balance'], 20_000_000_000_000_000_000)
        self.assertEqual(gridless_positions['1']['cost'], 2_000_000_000)


class TestTopPosition(unittest.TestCase):
    """Test getting top position (lowest buy price)."""
    
    def test_get_top_position(self):
        """Should return position with lowest buy price."""
        positions = {
            '0': {'cost': 2_000_000_000, 'balance': 10_000_000_000_000_000_000},  # Buy price = 0.2
            '1': {'cost': 1_000_000_000, 'balance': 10_000_000_000_000_000_000},  # Buy price = 0.1 (lowest)
            '2': {'cost': 3_000_000_000, 'balance': 10_000_000_000_000_000_000},  # Buy price = 0.3
        }
        
        top_id, top_pos = gridless.get_top_position(positions)
        self.assertEqual(top_id, '1')
        self.assertEqual(top_pos['cost'], 1_000_000_000)
    
    def test_get_top_position_empty(self):
        """Should return None for empty positions."""
        result = gridless.get_top_position({})
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
