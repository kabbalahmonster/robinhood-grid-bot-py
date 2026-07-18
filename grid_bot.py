#!/usr/bin/env python3
"""
Robinhood Chain Grid Trading Bot
Uses positions.json format compatible with original bot
"""

import json
import time
import logging
import os
from datetime import datetime
from decimal import Decimal
from web3 import Web3

from config import load_config
from wallet import Wallet
from zero_x import ZeroXClient

# Global logger - will be configured by GridBot
logger = logging.getLogger('grid_bot')

class GridBot:
    def __init__(self):
        self.config = load_config()
        self.wallet = Wallet(self.config)
        self.zero_x = ZeroXClient(self.config)
        self.positions_file = "data/positions.json"
        self.positions = {}
        self.running = True
        self.round_count = 0
        self.start_time = time.time()
        self.session_buys = 0
        self.session_sells = 0
        self.session_profit_weth = 0.0
        
        # Reconfigure logging based on config (must happen after config load)
        self._setup_logging()
        
        logger.info(f"Grid Bot initialized")
        logger.info(f"Wallet: {self.wallet.address}")
        logger.info(f"Trading: {self.config.token_symbol}")
        logger.info(f"Max active positions: {self.config.max_active_positions}")
    
    def _setup_logging(self):
        """Configure logging based on config settings."""
        minimal = getattr(self.config, 'minimal_logs', False)
        
        # Setup log file path
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        self.log_filename = os.path.join(log_dir, f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        
        # Set logger level
        logger.setLevel(logging.INFO)
        
        # Clear any existing handlers
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # File handler always gets full timestamps
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(file_handler)
        
        # Console handler - minimal or full format
        console_handler = logging.StreamHandler()
        if minimal:
            console_handler.setFormatter(logging.Formatter('%(message)s'))
        else:
            console_handler.setFormatter(logging.Formatter(
                '%(asctime)s | %(levelname)s | %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            ))
        logger.addHandler(console_handler)
        
        logger.info(f"Logging to: {self.log_filename}")
    
    def load_positions(self):
        """Load positions from JSON file."""
        try:
            with open(self.positions_file, 'r') as f:
                self.positions = json.load(f)
            logger.info(f"Loaded {len(self.positions)} positions")
        except FileNotFoundError:
            logger.error(f"No positions file found. Run generate_grid.py first!")
            raise
    
    def save_positions(self):
        """Save positions to JSON file."""
        with open(self.positions_file, 'w') as f:
            json.dump(self.positions, f, indent=2)
    
    def get_token_price(self):
        """Get current token price in WETH using the lighter /price endpoint."""
        # Use /price endpoint for price discovery (doesn't count against quote-to-trade metrics)
        price = self.zero_x.get_price(
            sell_token=self.config.weth_address,
            buy_token=self.config.token_address,
            sell_amount=10**15,  # 0.001 WETH
        )
        # get_price now returns WETH per token directly
        return price
    
    def check_buys(self, price):
        """Check for buy opportunities."""
        # Get available WETH
        weth_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        if weth_balance < 0.001:
            logger.warning(f"Low WETH balance: {weth_balance:.6f}")
            return
        
        # Check max active positions limit
        active_positions = sum(1 for p in self.positions.values() if p['balance'] > 0)
        if active_positions >= self.config.max_active_positions:
            logger.debug(f"Max active positions reached ({active_positions}/{self.config.max_active_positions})")
            return
        
        # Find empty positions where price is in buy range
        for pos_id, pos in self.positions.items():
            if pos['balance'] == 0:  # Empty position
                # Scale: 10^9 (nano-WETH)
                buy_min = pos['buyMin'] / 10**9
                buy_max = pos['buyMax'] / 10**9
                
                # Handle first position with buyMin = 0
                if buy_min == 0:
                    buy_min = 0
                
                if buy_min <= price <= buy_max:
                    logger.info(f"Buy trigger: Position {pos_id} at price {price:.10f} (range: {buy_min:.10f} - {buy_max:.10f})")
                    self.execute_buy(pos_id, price)
                    return  # One buy per cycle
    
    def check_sells(self, price):
        """Check for sell opportunities."""
        min_profit_percent = getattr(self.config, 'min_profit_percent', 2.0)  # Default 2% minimum profit
        slippage_buffer = 1.5  # Require extra 1.5% to cover slippage
        effective_min_profit = min_profit_percent + slippage_buffer
        fast_profit = getattr(self.config, 'fast_profit', False)
        
        for pos_id, pos in self.positions.items():
            if pos['balance'] > 0:  # Has tokens
                # Scale: 10^9 (nano-WETH)
                sell_min = pos['sellMin'] / 10**9
                
                # Calculate actual profit %
                tokens = pos['balance'] / 10**18
                cost_weth = pos['cost'] / 10**9
                buy_price = cost_weth / tokens if tokens > 0 else 0
                current_profit = ((price - buy_price) / buy_price * 100) if buy_price > 0 else 0
                
                # FAST PROFIT MODE: Sell if profit exceeds minimum, regardless of sellMin
                if fast_profit and current_profit >= effective_min_profit:
                    logger.info(f"🚀 Fast profit trigger: Position {pos_id} at {price:.10f} (profit: {current_profit:.2f}%, sellMin: {sell_min:.10f})")
                    self.execute_sell(pos_id, price)
                    return  # One sell per cycle
                
                # STANDARD MODE: Use higher of sellMin or min profit + buffer
                required_price = max(sell_min, buy_price * (1 + effective_min_profit / 100))
                
                if price >= required_price:
                    logger.info(f"Sell trigger: Position {pos_id} at price {price:.10f} (required: {required_price:.10f}, profit: {current_profit:.2f}%)")
                    self.execute_sell(pos_id, price)
                    return  # One sell per cycle
                elif price >= sell_min:
                    logger.info(f"Sell blocked: Position {pos_id} at {price:.10f} - profit {current_profit:.2f}% < required {effective_min_profit}% (buffer for slippage)")
                    self.execute_sell(pos_id, price)
                    return  # One sell per cycle
    
    def execute_buy(self, pos_id, price):
        """Execute a buy order."""
        pos = self.positions[pos_id]
        
        # Calculate buy amount (divide available WETH by available slots up to max_active_positions)
        weth_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        active_positions = sum(1 for p in self.positions.values() if p['balance'] > 0)
        available_slots = self.config.max_active_positions - active_positions
        
        if available_slots <= 0:
            logger.debug(f"Max active positions reached ({active_positions}/{self.config.max_active_positions})")
            return
        
        # Use configured % of available WETH divided by available slots
        tradeable_pct = getattr(self.config, 'tradeable_balance_percent', 90.0) / 100.0
        buy_amount_eth = (weth_balance * tradeable_pct) / available_slots
        buy_amount_wei = int(buy_amount_eth * 10**18)
        
        logger.info(f"Buying position {pos_id}: {buy_amount_eth:.6f} WETH ({weth_balance:.6f} WETH × {tradeable_pct*100:.0f}% / {available_slots} slots)")
        
        # Check ERC20 approval to AllowanceHolder
        allowance = self.wallet.check_allowance(
            self.config.weth_address,
            self.config.zero_x_proxy,  # This is now the AllowanceHolder address
            use_permit2=False  # Standard ERC20 approval
        )
        logger.info(f"WETH AllowanceHolder allowance: {allowance}")
        if allowance < buy_amount_wei:
            logger.info(f"Approving WETH to AllowanceHolder...")
            result = self.wallet.approve_token(
                self.config.weth_address,
                self.config.zero_x_proxy,
                2**256 - 1  # Max approval
            )
            if not result.success:
                logger.error(f"Approval failed: {result.error}")
                return

        # Get quote FIRST, then execute immediately (quote expires fast)
        logger.info("Getting 0x quote...")
        quote = self.zero_x.build_swap_transaction(
            sell_token=self.config.weth_address,
            buy_token=self.config.token_address,
            sell_amount=buy_amount_wei,
            taker_address=self.wallet.address,
            slippage_percentage=0.02,
        )
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            return
        
        # Execute swap with checksummed addresses
        # Use 0x provided gas limit or default
        gas_limit = int(quote.gas * 1.2) if quote.gas else 350000
        gas_price = int(self.wallet.w3.eth.gas_price * 1.2)
        
        from web3 import Web3
        tx_params = {
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        }
        
        logger.info(f"Sending tx to {quote.to} with gas {gas_limit}")
        result = self.wallet._send_transaction(tx_params)
        
        if result.success:
            # Update position - store actual WETH cost (not price) in nano-WETH
            tokens_received = quote.buy_amount
            tokens = tokens_received / 10**18
            self.positions[pos_id]['balance'] = tokens_received
            # Cost = actual WETH spent for profit calculation
            cost_nano = int(buy_amount_eth * 10**9)
            self.positions[pos_id]['cost'] = cost_nano
            self.save_positions()
            
            # Calculate buy price for logging
            buy_price = buy_amount_eth / tokens if tokens > 0 else 0
            
            # Track session stats
            self.session_buys += 1
            
            logger.info(f"✅ Buy successful!")
            logger.info(f"   Position: #{pos_id}")
            logger.info(f"   Tokens: {tokens:.6f} {self.config.token_symbol}")
            logger.info(f"   Cost: {buy_amount_eth:.6f} WETH")
            logger.info(f"   Buy price: {buy_price:.10f} WETH per token")
            logger.info(f"   Tx: {result.tx_hash[:30]}...")
        else:
            logger.error(f"❌ Buy failed: {result.error}")
    
    def execute_sell(self, pos_id, price):
        """Execute a sell order with moonbag and banking."""
        pos = self.positions[pos_id]
        total_balance = pos['balance']
        total_tokens = total_balance / 10**18
        
        # Validate position has tokens and cost basis
        if total_balance <= 0 or pos['cost'] <= 0:
            logger.warning(f"Skipping sell for position {pos_id}: balance={total_balance}, cost={pos['cost']}")
            return
        
        # Cost is WETH spent (in nano-WETH)
        cost_weth = pos['cost'] / 10**9
        buy_price = cost_weth / total_tokens if total_tokens > 0 else 0
        
        # Calculate profit
        if buy_price > 0:
            profit_percent = ((price - buy_price) / buy_price) * 100
        else:
            profit_percent = 0
        
        # Moonbag: Keep X% of tokens, sell the rest
        moonbag_pct = getattr(self.config, 'moonbag_percentage', 0)
        if moonbag_pct > 0:
            moonbag_tokens = int(total_balance * moonbag_pct / 100)
            sell_amount = total_balance - moonbag_tokens
            sell_tokens = sell_amount / 10**18
            logger.info(f"🌙 Moonbag: Keeping {moonbag_tokens / 10**18:.4f} tokens ({moonbag_pct}%), selling {sell_tokens:.4f}")
        else:
            sell_amount = total_balance
            sell_tokens = total_tokens
            moonbag_tokens = 0
        
        # Calculate expected WETH return (proportional to sold amount)
        expected_weth = sell_tokens * price
        # Cost basis for sold portion only
        sold_cost_weth = cost_weth * (sell_tokens / total_tokens) if total_tokens > 0 else 0
        profit_weth = expected_weth - sold_cost_weth
        
        logger.info(f"💰 Selling position {pos_id}:")
        logger.info(f"   Total tokens: {total_tokens:.6f}")
        logger.info(f"   Selling: {sell_tokens:.6f}")
        logger.info(f"   Buy price: {buy_price:.10f} WETH")
        logger.info(f"   Current: {price:.10f} WETH")
        logger.info(f"   Cost basis (sold): {sold_cost_weth:.6f} WETH")
        logger.info(f"   Expected return: {expected_weth:.6f} WETH")
        logger.info(f"   Profit: {profit_weth:.6f} WETH ({profit_percent:+.2f}%)")
        
        # Get quote
        quote = self.zero_x.build_swap_transaction(
            sell_token=self.config.token_address,
            buy_token=self.config.weth_address,
            sell_amount=sell_amount,
            taker_address=self.wallet.address,
            slippage_percentage=0.02,
        )
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            return
        
        # Check/approve token for selling
        token_allowance = self.wallet.check_allowance(
            self.config.token_address,
            self.config.zero_x_proxy,
            use_permit2=False
        )
        if token_allowance < sell_amount:
            logger.info(f"Approving {self.config.token_symbol} to AllowanceHolder...")
            result = self.wallet.approve_token(
                self.config.token_address,
                self.config.zero_x_proxy,
                2**256 - 1
            )
            if not result.success:
                logger.error(f"Token approval failed: {result.error}")
                return
        
        # Execute swap with checksummed addresses
        gas_limit = int(quote.gas * 1.5) if quote.gas else 300000
        gas_price = int(self.wallet.w3.eth.gas_price * 1.3)
        
        result = self.wallet._send_transaction({
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        })
        
        if result.success:
            # Get actual WETH received from transaction
            weth_received = quote.buy_amount / 10**18 if quote.buy_amount else 0
            actual_profit_weth = weth_received - sold_cost_weth
            
            # Track session stats
            self.session_sells += 1
            self.session_profit_weth += actual_profit_weth
            
            # Position is always cleared to 0 after sell
            # Moonbag tokens go to wallet balance (not tracked in position)
            self.positions[pos_id]['balance'] = 0
            self.positions[pos_id]['cost'] = 0
            self.save_positions()
            
            if moonbag_tokens > 0:
                logger.info(f"   Moonbag: {moonbag_tokens / 10**18:.4f} tokens added to wallet balance")
            
            logger.info(f"✅ Sell successful!")
            logger.info(f"   Actual return: {weth_received:.6f} WETH")
            logger.info(f"   Profit: {actual_profit_weth:.6f} WETH ({(actual_profit_weth/sold_cost_weth*100) if sold_cost_weth > 0 else 0:+.2f}%)")
            
            # Banking: Swap % of profit to USDG
            bank_pct = getattr(self.config, 'bank_percentage', 0)
            if bank_pct > 0 and actual_profit_weth > 0:
                bank_amount = actual_profit_weth * bank_pct / 100
                logger.info(f"🏦 Banking: Swapping {bank_pct}% of profit = {bank_amount:.6f} WETH → USDG")
                self.bank_profit(bank_amount)
            
            logger.info(f"   Tx: {result.tx_hash[:30]}...")
        else:
            logger.error(f"❌ Sell failed: {result.error}")
    
    def bank_profit(self, weth_amount):
        """Swap WETH profit to USDG for banking."""
        if weth_amount <= 0:
            return
        
        # Convert to wei
        weth_wei = int(weth_amount * 10**18)
        
        logger.info(f"🏦 Getting quote for banking {weth_amount:.6f} WETH → USDG...")
        
        # Get quote for WETH -> USDG
        quote = self.zero_x.build_swap_transaction(
            sell_token=self.config.weth_address,
            buy_token=self.config.usdg_address,
            sell_amount=weth_wei,
            taker_address=self.wallet.address,
            slippage_percentage=0.01,  # 1% slippage for stable swaps
        )
        
        if not quote.success:
            logger.error(f"Banking quote failed: {quote.error}")
            return
        
        # Check minimum bank amount (in USDG - 6 decimals)
        bank_min_usdg = getattr(self.config, 'bank_min_amount', 0.5)  # Default 0.5 USDG
        expected_usdg = quote.buy_amount / 10**6 if quote.buy_amount else 0
        
        if expected_usdg < bank_min_usdg:
            logger.info(f"🏦 Banking skipped: {expected_usdg:.2f} USDG below minimum {bank_min_usdg} USDG")
            return
        
        logger.info(f"🏦 Banking {weth_amount:.6f} WETH → ~{expected_usdg:.2f} USDG...")
        
        # Check/approve WETH for swapping
        weth_allowance = self.wallet.check_allowance(
            self.config.weth_address,
            self.config.zero_x_proxy,
            use_permit2=False
        )
        if weth_allowance < weth_wei:
            logger.info(f"Approving WETH to AllowanceHolder for banking...")
            result = self.wallet.approve_token(
                self.config.weth_address,
                self.config.zero_x_proxy,
                2**256 - 1
            )
            if not result.success:
                logger.error(f"WETH approval for banking failed: {result.error}")
                return
        
        # Execute banking swap
        gas_limit = int(quote.gas * 1.5) if quote.gas else 300000
        gas_price = int(self.wallet.w3.eth.gas_price * 1.3)
        
        result = self.wallet._send_transaction({
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        })
        
        if result.success:
            usdg_received = quote.buy_amount / 10**6 if quote.buy_amount else 0  # USDG is 6 decimals
            logger.info(f"✅ Banked! Received {usdg_received:.2f} USDG")
        else:
            logger.error(f"❌ Banking failed: {result.error}")
    
    def run_cycle(self):
        """Run one trading cycle."""
        self.round_count += 1
        elapsed = time.time() - self.start_time
        
        # Get balances
        weth_bal, weth_raw = self.wallet.get_token_balance(self.config.weth_address)
        token_bal, token_raw = self.wallet.get_token_balance(self.config.token_address)
        
        # Count positions - balance > 0 means active (even if cost is 0, could be moonbag)
        active = sum(1 for p in self.positions.values() if p['balance'] > 0)
        empty = sum(1 for p in self.positions.values() if p['balance'] == 0)
        
        # Calculate moonbag (tokens in wallet not in positions)
        position_balance_total = sum(p['balance'] for p in self.positions.values()) / 10**18
        moonbag_balance = token_bal - position_balance_total
        
        # Get price
        price = self.get_token_price()
        if price is None:
            logger.warning("Could not get price")
            return
        
        # Check for compact mode (tmux-friendly output)
        compact_mode = getattr(self.config, 'compact_mode', False)
        
        if compact_mode:
            # Compact output for tmux multi-pane view
            from datetime import datetime
            time_str = datetime.now().strftime('%H:%M')
            
            # Line 1: Time, round, token
            logger.info(f"{time_str} R#{self.round_count} | {self.config.token_symbol}")
            
            # Line 2: WETH, Token, Positions
            pos_line = f"W:{weth_bal:.3f} | T:{token_bal:.0f} | {active}/{active+empty}"
            logger.info(pos_line)
            
            # Line 3: Buys, Sells, Profit
            bsp_line = f"B:{self.session_buys} | S:{self.session_sells} | P:{self.session_profit_weth:.5f}"
            logger.info(bsp_line)
            
            # Separator - hardcoded to match B/S/P line length (21 chars for B:0 | S:0 | P:0.00000)
            logger.info("---------------------")
            
            # Each position on its own line (max 3), no price shown
            active_positions = [(pid, p) for pid, p in self.positions.items() if p['balance'] > 0]
            for pos_id, pos in active_positions[:3]:
                tokens = pos['balance'] / 10**18
                cost_weth = pos['cost'] / 10**9
                if tokens > 0 and cost_weth > 0:
                    buy_price = cost_weth / tokens
                    pnl = ((price - buy_price) / buy_price * 100)
                    logger.info(f"#{pos_id}: {tokens:.1f} | P&L: {pnl:+.1f}%")
                else:
                    logger.info(f"#{pos_id}: {tokens:.1f} | moonbag")
            if len(active_positions) > 3:
                logger.info(f"... and {len(active_positions) - 3} more")
        else:
            # Verbose round summary (original format)
            logger.info("=" * 70)
            logger.info(f"ROUND #{self.round_count} | {self.config.token_symbol} | Elapsed: {elapsed:.0f}s")
            logger.info("=" * 70)
            logger.info(f"💰 WETH Balance: {weth_bal:.6f}")
            logger.info(f"🪙 Token Balance: {token_bal:.6f} (in positions: {position_balance_total:.4f}, moonbag: {moonbag_balance:.4f})")
            logger.info(f"📊 Price: 1 {self.config.token_symbol} = {price:.10f} WETH")
            logger.info(f"📈 Positions: {active} active / {empty} empty (max active: {self.config.max_active_positions})")
            logger.info(f"📊 Session: {self.session_buys} buys, {self.session_sells} sells, {self.session_profit_weth:.6f} WETH profit")
            
            # Show active positions with P&L and sell targets
            if active > 0:
                logger.info("🎯 Active Positions:")
                for pos_id, pos in self.positions.items():
                    if pos['balance'] > 0:
                        balance_raw = pos['balance']
                        cost_raw = pos['cost']
                        tokens = balance_raw / 10**18
                        cost_weth = cost_raw / 10**9
                        sell_min = pos['sellMin'] / 10**9
                        # Buy price = WETH spent / tokens received
                        if tokens > 0 and cost_weth > 0:
                            buy_price = cost_weth / tokens
                            pnl = ((price - buy_price) / buy_price * 100)
                            # Show how much more price needs to rise to hit sell target
                            price_diff = sell_min - price
                            price_pct = (price_diff / price * 100) if price > 0 else 0
                            logger.info(f"   #{pos_id}: {tokens:.4f} tokens | Buy: {buy_price:.10f} | Sell@: {sell_min:.10f} | P&L: {pnl:+.2f}% (need +{price_pct:.1f}% more to sell)")
                        else:
                            # Moonbag or dust position with unknown cost
                            price_diff = sell_min - price
                            price_pct = (price_diff / price * 100) if price > 0 else 0
                            logger.info(f"   #{pos_id}: {tokens:.4f} tokens | Buy: moonbag | Sell@: {sell_min:.10f} | P&L: N/A (need +{price_pct:.1f}% more to sell)")
            
            # Show next buy trigger (lowest empty position buy range)
            if empty > 0:
                next_buy = None
                for pos_id, pos in self.positions.items():
                    if pos['balance'] == 0:  # Empty position
                        buy_max = pos['buyMax'] / 10**9
                        buy_min = pos['buyMin'] / 10**9
                        # Find the highest buyMax below current price (closest buy trigger)
                        if buy_max <= price:
                            if next_buy is None or buy_max > next_buy['buy_max']:
                                next_buy = {
                                    'pos_id': pos_id,
                                    'buy_min': buy_min,
                                    'buy_max': buy_max
                                }
                
                if next_buy:
                    drop_pct = (price - next_buy['buy_max']) / price * 100
                    logger.info(f"🛒 Next Buy: Position #{next_buy['pos_id']} at {next_buy['buy_min']:.10f}-{next_buy['buy_max']:.10f} (need -{drop_pct:.1f}% drop)")
                else:
                    # All empty positions are above current price, find lowest
                    lowest_buy = None
                    for pos_id, pos in self.positions.items():
                        if pos['balance'] == 0:
                            buy_max = pos['buyMax'] / 10**9
                            if lowest_buy is None or buy_max < lowest_buy['buy_max']:
                                lowest_buy = {'pos_id': pos_id, 'buy_max': buy_max}
                    if lowest_buy:
                        rise_pct = (lowest_buy['buy_max'] - price) / price * 100
                        logger.info(f"🛒 Next Buy: Position #{lowest_buy['pos_id']} at {lowest_buy['buy_max']:.10f} (need +{rise_pct:.1f}% rise to enter range)")
        
        logger.info("-" * 70)
        
        # Check sells first (take profits)
        self.check_sells(price)
        
        # Then check buys
        self.check_buys(price)
    
    def run(self):
        """Main bot loop."""
        self.load_positions()

        poll_interval = getattr(self.config, 'poll_interval_seconds', 30)
        logger.info(f"Starting main loop (polling every {poll_interval}s)...")
        while self.running:
            try:
                self.run_cycle()
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Stopping bot...")
                self.running = False
            except Exception as e:
                logger.error(f"Error in cycle: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = GridBot()
    bot.run()
