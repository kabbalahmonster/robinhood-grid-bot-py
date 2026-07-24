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
from li_fi import LiFiClient
from uniswap_api import UniswapAPIClient

# Native ETH address for 0x API (used when trading with native ETH instead of WETH)
# Native ETH address for 0x API
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
# Native ETH address for Uniswap API (zero address)
UNISWAP_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

# Global logger - will be configured by GridBot
logger = logging.getLogger('grid_bot')

class GridBot:
    def __init__(self):
        self.config = load_config()
        
        # Setup logging FIRST so we capture all initialization logs
        self._setup_logging()
        
        self.wallet = Wallet(self.config)
        
        # Select API provider based on config priority:
        # 1. Uniswap API, 2. LI.FI, 3. 0x (default)
        if getattr(self.config, 'use_uniswap_api', False):
            self.api_client = UniswapAPIClient(self.config)
            logger.info("Using Uniswap API for swaps")
        elif getattr(self.config, 'use_li_fi', False):
            self.api_client = LiFiClient(self.config)
            print(f"DEBUG: use_li_fi={self.config.use_li_fi}, api_key={self.config.li_fi_api_key[:20]}...")
            logger.info("Using LI.FI API for swaps")
        else:
            self.api_client = ZeroXClient(self.config)
            logger.info("Using 0x API for swaps")
        
        # Keep zero_x reference for backward compatibility
        self.zero_x = self.api_client
        
        self.positions_file = "data/positions.json"
        self.positions = {}
        self.running = True
        self.round_count = 0
        self.start_time = time.time()
        self.session_buys = 0
        self.session_sells = 0
        self.session_profit_weth = 0.0
        
        # Cooldown tracking for gridless buys
        self.last_buy_time = 0
        self.gridless_buy_cooldown = getattr(self.config, 'gridless_buy_cooldown_seconds', 300)  # Default 5 min
        
        # Trading token setup (WETH or native ETH)
        if getattr(self.config, 'use_eth_trading', False):
            # Use native ETH address (zero address for Uniswap API)
            self.trade_token_address = UNISWAP_ETH_ADDRESS
            self.trade_token_name = "ETH"
            logger.info("Trading mode: Native ETH")
        else:
            self.trade_token_address = self.config.weth_address
            self.trade_token_name = "WETH"
            logger.info("Trading mode: WETH")
        
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
        
        # Set logger level from config
        log_level = getattr(self.config, 'log_level', 'INFO')
        logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        
        # Clear any existing handlers
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # File handler always gets full timestamps
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(file_handler)
        
        # Console handler - minimal or full format
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
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
        # Check if gridless mode is enabled
        if getattr(self.config, 'use_gridless', False):
            # In gridless mode, initialize empty positions (loaded dynamically)
            self.positions = {}
            logger.info("Gridless mode: positions loaded dynamically")
            return
        
        # Classic grid mode - load from file
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
        """Get current token price in ETH/WETH using the lighter /price endpoint."""
        # Use /price endpoint for price discovery (doesn't count against quote-to-trade metrics)
        # For LI.FI and Uniswap API, we need to pass the wallet address
        if getattr(self.config, 'use_li_fi', False) or getattr(self.config, 'use_uniswap_api', False):
            # LI.FI and Uniswap API require taker address
            result = self.api_client.get_quote(
                sell_token=self.trade_token_address,
                buy_token=self.config.token_address,
                sell_amount=10**15,  # 0.001 ETH/WETH
                taker_address=self.wallet.address,
                apply_jitter_to_price=False,
            )
            if result.success and result.price:
                logger.debug(f"API price: {result.price}")
                return result.price
            else:
                logger.debug(f"API price failed: success={result.success}, price={result.price}, error={result.error}")
            return None
        else:
            # 0x price endpoint doesn't require taker
            price = self.zero_x.get_price(
                sell_token=self.trade_token_address,
                buy_token=self.config.token_address,
                sell_amount=10**15,  # 0.001 ETH/WETH
            )
            return price
    
    def check_buys(self, price):
        """Check for buy opportunities."""
        # Gridless mode
        if getattr(self.config, 'use_gridless', False):
            return self._check_buys_gridless(price)
        
        # Get available ETH/WETH
        if getattr(self.config, 'use_eth_trading', False):
            trade_balance = self.wallet.get_eth_balance()
            gas_reserve = getattr(self.config, 'eth_gas_reserve', 0.001)
            trade_balance = max(0, trade_balance - gas_reserve)
        else:
            trade_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        
        if trade_balance < 0.001:
            logger.warning(f"Low {self.trade_token_name} balance: {trade_balance:.6f}")
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
        # Gridless mode
        if getattr(self.config, 'use_gridless', False):
            return self._check_sells_gridless(price)
        
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
                    return  # Blocked - do not sell
    
    def _check_buys_gridless(self, price):
        """Gridless buy logic - buy when no positions or top position P&L <= threshold."""
        from gridless import should_buy, load_positions, add_position
        
        # Check cooldown
        time_since_last_buy = time.time() - self.last_buy_time
        if time_since_last_buy < self.gridless_buy_cooldown:
            logger.debug(f"Gridless: Buy cooldown active ({time_since_last_buy:.0f}s < {self.gridless_buy_cooldown}s)")
            return
        
        # Load gridless positions
        gridless_positions = load_positions()
        
        # Check if we should buy
        should_buy_flag, reason = should_buy(gridless_positions, price, self.config)
        if not should_buy_flag:
            logger.debug(f"Gridless: No buy - {reason}")
            return
        
        # Get available ETH/WETH
        if getattr(self.config, 'use_eth_trading', False):
            eth_balance = self.wallet.get_eth_balance()
            gas_reserve = getattr(self.config, 'eth_gas_reserve', 0.001)
            trade_balance = max(0, eth_balance - gas_reserve)
        else:
            trade_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        
        if trade_balance < 0.001:
            logger.warning(f"Gridless: Low {self.trade_token_name} balance: {trade_balance:.6f}")
            return
        
        # Calculate buy amount
        active_count = len(gridless_positions)
        available_slots = self.config.max_active_positions - active_count
        if available_slots <= 0:
            logger.debug(f"Gridless: Max positions reached ({active_count}/{self.config.max_active_positions})")
            return
        
        tradeable_pct = getattr(self.config, 'tradeable_balance_percent', 90.0) / 100.0
        buy_amount_eth = (trade_balance * tradeable_pct) / available_slots
        buy_amount_wei = int(buy_amount_eth * 10**18)
        
        logger.info(f"🎯 Gridless buy triggered: {reason}")
        logger.info(f"   Amount: {buy_amount_eth:.6f} {self.trade_token_name} ({trade_balance:.6f} × {tradeable_pct*100:.0f}% / {available_slots} slots)")
        
        # Execute the buy via execute_buy_gridless
        is_leading_edge_buy = "Leading edge" in reason
        self._execute_buy_gridless(buy_amount_eth, buy_amount_wei, price, is_leading_edge_buy)
    
    def _execute_buy_gridless(self, buy_amount_eth, buy_amount_wei, price, is_leading_edge_buy=False):
        """Execute a gridless buy order."""
        from gridless import add_position
        
        # Get quote
        quote = self.api_client.build_swap_transaction(
            sell_token=self.trade_token_address,
            buy_token=self.config.token_address,
            sell_amount=buy_amount_wei,
            taker_address=self.wallet.address,
            slippage_percentage=0.02,
        )
        
        if not quote.success:
            logger.error(f"Gridless buy quote failed: {quote.error}")
            return
        
        # Load positions for execution margin check
        from gridless import load_positions as reload_positions
        gridless_positions = reload_positions()
        
        # Validate execution price is still within buy threshold margin
        # Skip for leading edge buys (buying into strength with single position)
        execution_margin_pct = getattr(self.config, 'gridless_buy_execution_margin', 50.0)  # Default 50%
        
        # Skip execution margin check for leading edge buys
        if not is_leading_edge_buy and quote.buy_amount and quote.buy_amount > 0:
            from gridless import get_buy_price, calculate_pnl
            top = None
            if gridless_positions:
                top_id, top_pos, top_price = None, None, float('inf')
                for pos_id, pos in gridless_positions.items():
                    buy_price = get_buy_price(pos)
                    if buy_price > 0 and buy_price < top_price:
                        top_price, top_id, top_pos = buy_price, pos_id, pos
                top = (top_id, top_pos) if top_id else None
            
            if top:
                # Calculate what the P&L would be at the quoted price
                tokens_at_quote = quote.buy_amount / 1e18
                quote_buy_price = buy_amount_eth / tokens_at_quote if tokens_at_quote > 0 else 0
                pnl_at_quote = calculate_pnl(top[1], quote_buy_price)
                buy_threshold = getattr(self.config, 'gridless_buy_threshold', -10.0)
                
                # Calculate block threshold as percentage of threshold distance from 0
                # e.g., -10% threshold with 50% margin = -10 + (10 * 0.5) = -5%
                distance_from_zero = abs(buy_threshold)
                max_recovery = distance_from_zero * (execution_margin_pct / 100.0)
                block_threshold = buy_threshold + max_recovery
                
                # Block if price recovered too much (quote P&L above block threshold)
                if pnl_at_quote > block_threshold:
                    logger.info(f"⏸️ Buy aborted: Quote P&L ({pnl_at_quote:.1f}%) recovered past {execution_margin_pct}% margin (block above {block_threshold:.1f}%)")
                    logger.info(f"   Price moved from trigger. Buy price: {quote_buy_price:.10f}, Top position buy: {get_buy_price(top[1]):.10f}")
                    return
        
        # Determine approval spender
        spender = quote.allowance_target or self.config.zero_x_proxy
        
        # Check/approve WETH
        allowance = self.wallet.check_allowance(self.config.weth_address, spender, use_permit2=False)
        # Check/approve WETH (skip for native ETH - it doesn't need approval)
        if not getattr(self.config, 'use_eth_trading', False):
            allowance = self.wallet.check_allowance(self.config.weth_address, spender, use_permit2=False)
            if allowance < buy_amount_wei:
                logger.info(f"Approving WETH to {spender[:20]}...")
                result = self.wallet.approve_token(self.config.weth_address, spender, 2**256 - 1)
                if not result.success:
                    logger.error(f"Approval failed: {result.error}")
                    return
                # Refresh quote after approval for LI.FI
                if getattr(self.config, 'use_li_fi', False):
                    quote = self.api_client.refresh_quote(
                        sell_token=self.config.weth_address,
                        buy_token=self.config.token_address,
                        sell_amount=buy_amount_wei,
                        taker_address=self.wallet.address,
                        slippage_percentage=0.02,
                    )
                    if not quote.success:
                        logger.error(f"Refreshed quote failed: {quote.error}")
                        return
        
        # For Uniswap API, get swap transaction from quote
        if getattr(self.config, 'use_uniswap_api', False):
            from uniswap_api import UniswapAPIClient
            if isinstance(self.api_client, UniswapAPIClient):
                swap_result = self.api_client.get_swap_transaction(quote.raw_response)
                if not swap_result.success:
                    logger.error(f"Uniswap swap transaction failed: {swap_result.error}")
                    return
                quote = swap_result
        
        # Execute swap with configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit_mult = getattr(self.config, 'gas_limit_multiplier', 1.05)
        gas_price_mult = getattr(self.config, 'gas_price_multiplier', 1.05)
        gas_limit = int(quote.gas * gas_limit_mult) if quote.gas else 350000
        if quote.gas_price and quote.gas_price > 0:
            gas_price = int(quote.gas_price * gas_price_mult)
        else:
            gas_price = int(self.wallet.w3.eth.gas_price * gas_price_mult)
        
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
        
        result = self.wallet._send_transaction(tx_params)
        
        if result.success:
            # Record position in gridless format
            tokens_received = quote.buy_amount if quote.buy_amount else 0
            # Use the actual sell amount from the quote in wei for precision
            cost_wei = quote.sell_amount if quote.sell_amount else buy_amount_wei
            
            logger.debug(f"Recording position: cost_wei={cost_wei}, tokens_received={tokens_received}")
            logger.debug(f"Quote buy_amount: {quote.buy_amount}, sell_amount: {quote.sell_amount}")
            
            pos_id = add_position(cost_wei, tokens_received)
            
            tokens = tokens_received / 10**18
            buy_price = buy_amount_eth / tokens if tokens > 0 else 0
            self.session_buys += 1
            self.last_buy_time = time.time()  # Update cooldown timer
            
            logger.info(f"✅ Gridless buy successful! Position #{pos_id}")
            logger.info(f"   Tokens: {tokens:.6f} {self.config.token_symbol}")
            logger.info(f"   Cost: {buy_amount_eth:.6f} {self.trade_token_name}")
            logger.info(f"   Buy price: {buy_price:.10f} {self.trade_token_name}/token")
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Gridless buy failed: {result.error}")
    
    def _check_sells_gridless(self, price):
        """Gridless sell logic - sell when P&L >= threshold or stoploss triggered."""
        from gridless import load_positions, find_sell_candidate, calculate_pnl, remove_position, get_buy_price
        
        # Load gridless positions
        gridless_positions = load_positions()
        if not gridless_positions:
            return
        
        # Find best sell candidate based on P&L only (quote checked at execution)
        # This allows profitable positions to sell even when aggregate portfolio is down
        sell_threshold = getattr(self.config, 'gridless_sell_threshold', 5.0)
        stoploss_enabled = getattr(self.config, 'gridless_stoploss_enabled', False)
        stoploss_threshold = getattr(self.config, 'gridless_stoploss_threshold', -25.0)
        
        best_candidate = None
        best_priority = 999
        best_pnl = float('-inf')
        
        for pos_id, pos in gridless_positions.items():
            pnl = calculate_pnl(pos, price)
            
            # Check stoploss first (highest priority)
            if stoploss_enabled and pnl <= stoploss_threshold:
                if best_priority > 0 or pnl > best_pnl:
                    best_candidate = (pos_id, pos, f"STOPLOSS: {pnl:.1f}%")
                    best_priority = 0
                    best_pnl = pnl
            # Check profit target
            elif pnl >= sell_threshold:
                if best_priority > 1 or pnl > best_pnl:
                    best_candidate = (pos_id, pos, f"PROFIT: {pnl:.1f}%")
                    best_priority = 1
                    best_pnl = pnl
        
        if best_candidate is None:
            return
        
        pos_id, pos, reason = best_candidate
        
        # Verify with individual position quote before executing
        balance = pos.get('balance', 0)
        if balance <= 0:
            return
            
        quote = self.api_client.build_swap_transaction(
            sell_token=self.config.token_address,
            buy_token=self.trade_token_address,
            sell_amount=balance,
            taker_address=self.wallet.address,
            slippage_percentage=0.02,
        )
        
        if not quote.success:
            logger.debug(f"Sell candidate #{pos_id} but quote failed: {quote.error}")
            return
        
        # Check min profit requirement against individual position quote
        # Support both cost_wei (new) and cost (legacy nano-ETH)
        cost_wei = pos.get('cost_wei', 0)
        if cost_wei <= 0 and 'cost' in pos:
            old_cost = pos.get('cost', 0)
            if old_cost > 0:
                cost_wei = old_cost * 10**9
        cost_eth = cost_wei / 1e18
        min_profit = getattr(self.config, 'min_profit_percent', 1.5)
        min_profit_eth = cost_eth * (min_profit / 100)
        quote_return_eth = quote.buy_amount / 10**18 if quote.buy_amount else 0
        quote_profit_eth = quote_return_eth - cost_eth
        
        if quote_profit_eth < min_profit_eth:
            buy_price = get_buy_price(pos)
            pnl_at_check = calculate_pnl(pos, price)
            logger.info(f"⏸️  Position #{pos_id} at {pnl_at_check:.1f}% P&L but quote profit ({quote_profit_eth:.6f}) < min ({min_profit_eth:.6f}) - skipping")
            return
        
        logger.info(f"🎯 Gridless sell trigger: Position #{pos_id} - {reason}")
        self._execute_sell_gridless(pos_id, pos, price, quote)
    
    def _execute_sell_gridless(self, pos_id, pos, price, pre_fetched_quote=None):
        """Execute a gridless sell order."""
        from gridless import remove_position, calculate_pnl
        
        balance = pos.get('balance', 0)
        # Support both cost_wei (new) and cost (legacy nano-ETH)
        cost_wei = pos.get('cost_wei', 0)
        if cost_wei <= 0 and 'cost' in pos:
            old_cost = pos.get('cost', 0)
            if old_cost > 0:
                cost_wei = old_cost * 10**9
        
        if balance <= 0 or cost_wei <= 0:
            logger.warning(f"Invalid position #{pos_id}: balance={balance}, cost_wei={cost_wei}")
            return
        
        tokens = balance / 10**18
        cost_eth = cost_wei / 1e18
        buy_price = cost_eth / tokens if tokens > 0 else 0
        pnl = calculate_pnl(pos, price)
        
        # Moonbag logic
        moonbag_pct = getattr(self.config, 'moonbag_percentage', 0)
        if moonbag_pct > 0:
            moonbag_tokens = int(balance * moonbag_pct / 100)
            sell_amount = balance - moonbag_tokens
            sell_tokens = sell_amount / 10**18
            logger.info(f"🌙 Moonbag: Keeping {moonbag_tokens/1e18:.4f} ({moonbag_pct}%), selling {sell_tokens:.4f}")
        else:
            sell_amount = balance
            sell_tokens = tokens
            moonbag_tokens = 0
        
        sold_cost_eth = cost_eth * (sell_tokens / tokens) if tokens > 0 else 0
        expected_eth = sell_tokens * price
        profit_eth = expected_eth - sold_cost_eth
        
        logger.info(f"💰 Gridless sell position #{pos_id}:")
        logger.info(f"   Position data: cost_wei={cost_wei}, balance={balance} wei")
        logger.info(f"   Calculated: cost={cost_eth:.6f} {self.trade_token_name}, tokens={tokens:.6f}, buy_price={buy_price:.10f}")
        logger.info(f"   Selling: {sell_tokens:.6f} tokens")
        logger.info(f"   Buy price: {buy_price:.10f}, Current: {price:.10f}")
        logger.info(f"   Expected: {expected_eth:.6f} {self.trade_token_name}, Profit: {profit_eth:.6f} ({pnl:+.2f}%)")
        
        # Use pre-fetched quote if available (for moonbag, need to re-quote with different amount)
        if pre_fetched_quote and moonbag_pct == 0 and sell_amount == balance:
            quote = pre_fetched_quote
        else:
            # Get fresh quote (for moonbag or if no pre-fetched quote)
            quote = self.api_client.build_swap_transaction(
                sell_token=self.config.token_address,
                buy_token=self.trade_token_address,
                sell_amount=sell_amount,
                taker_address=self.wallet.address,
                slippage_percentage=0.02,
            )
        
        if not quote.success:
            logger.error(f"Gridless sell quote failed: {quote.error}")
            return
        
        # Validate minimum profit
        min_profit = getattr(self.config, 'min_profit_percent', 1.5)
        min_profit_eth = sold_cost_eth * (min_profit / 100)
        min_return_eth = sold_cost_eth + min_profit_eth
        quote_return_eth = quote.buy_amount / 10**18 if quote.buy_amount else 0
        
        # Skip min_profit check for stoploss
        stoploss_enabled = getattr(self.config, 'gridless_stoploss_enabled', False)
        stoploss_threshold = getattr(self.config, 'gridless_stoploss_threshold', -25.0)
        is_stoploss = stoploss_enabled and pnl <= stoploss_threshold
        
        if not is_stoploss and quote_return_eth < min_return_eth:
            logger.warning(f"❌ Sell aborted: Quote ({quote_return_eth:.6f}) < min ({min_return_eth:.6f})")
            return
        
        # For Uniswap API, use check_approval flow to get correct approval transaction
        if getattr(self.config, 'use_uniswap_api', False):
            from uniswap_api import UniswapAPIClient
            if isinstance(self.api_client, UniswapAPIClient):
                # Step 1: Check approval via Uniswap API
                approval_result = self.api_client.check_approval(
                    token=self.config.token_address,
                    amount=sell_amount,
                    wallet=self.wallet.address,
                )
                
                if "error" in approval_result:
                    logger.error(f"Approval check failed: {approval_result.get('error')}")
                    return
                
                # Step 2: Execute approval transactions if needed
                # Uniswap check_approval returns {"approval": tx} when approval is needed, null otherwise
                cancel_tx = approval_result.get("cancel")
                approval_tx = approval_result.get("approval")
                
                # Helper function to build EIP-1559 transaction with fresh fees
                def build_eip1559_tx(api_tx):
                    from web3 import Web3
                    # Get fresh block data
                    latest_block = self.wallet.w3.eth.get_block("latest")
                    base_fee = int(latest_block.get("baseFeePerGas", 0))
                    
                    # Get priority fee (with fallback)
                    try:
                        priority_fee = int(self.wallet.w3.eth.max_priority_fee)
                    except Exception:
                        priority_fee = 1_000_000  # 0.001 gwei fallback
                    priority_fee = max(priority_fee, 1_000_000)
                    
                    # Calculate max fee with 2x headroom
                    max_fee = base_fee * 2 + priority_fee
                    
                    # Build transaction
                    tx = {
                        "from": Web3.to_checksum_address(api_tx.get("from", self.wallet.address)),
                        "to": Web3.to_checksum_address(api_tx.get("to")),
                        "data": api_tx.get("data"),
                        "value": int(api_tx.get("value", "0x0"), 16) if isinstance(api_tx.get("value"), str) else int(api_tx.get("value", 0)),
                        "chainId": int(api_tx.get("chainId", self.config.chain_id)),
                        "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address, "pending"),
                        "maxPriorityFeePerGas": priority_fee,
                        "maxFeePerGas": max_fee,
                        "type": 2,  # EIP-1559
                    }
                    
                    # Estimate gas with headroom
                    try:
                        estimated_gas = self.wallet.w3.eth.estimate_gas(tx)
                        tx["gas"] = int(estimated_gas * 1.2)  # 20% headroom
                    except Exception as e:
                        logger.warning(f"Gas estimation failed: {e}, using default")
                        tx["gas"] = int(api_tx.get("gas", 100000))
                    
                    logger.info(f"Approval fees: base={base_fee} priority={priority_fee} max={max_fee} nonce={tx['nonce']} gas={tx['gas']}")
                    return tx
                
                # Handle cancel transaction first (if present)
                if cancel_tx is not None:
                    logger.info("Approval cancel/reset transaction required")
                    tx = build_eip1559_tx(cancel_tx)
                    result = self.wallet._send_transaction(tx)
                    if not result.success:
                        logger.error(f"Cancel transaction failed: {result.error}")
                        return
                    logger.info(f"Cancel transaction confirmed: {result.tx_hash}")
                    # Wait for confirmation
                    import time
                    time.sleep(3)
                
                # Handle approval transaction
                if approval_tx is not None:
                    logger.info("ERC20 approval transaction required")
                    tx = build_eip1559_tx(approval_tx)
                    result = self.wallet._send_transaction(tx)
                    if not result.success:
                        logger.error(f"Approval transaction failed: {result.error}")
                        return
                    logger.info(f"Approval transaction confirmed: {result.tx_hash}")
                    # Wait for confirmation
                    import time
                    time.sleep(3)
                else:
                    logger.info("No approval transaction required")
                
                # Step 3b: Verify allowance if approval was sent
                if approval_tx is not None:
                    # Decode spender from approval calldata (0x095ea7b3 = approve(address,uint256))
                    import binascii
                    data = approval_tx.get("data", "")
                    if data.startswith("0x095ea7b3") or data.startswith("095ea7b3"):
                        # spender is the first 32-byte word after selector
                        clean_data = data[10:] if data.startswith("0x") else data[8:]
                        spender_word = clean_data[:64]
                        spender_addr = "0x" + spender_word[-40:]
                        logger.info(f"Decoded spender from approval: {spender_addr}")
                        
                        # Check allowance on-chain
                        from web3 import Web3
                        token_contract = self.wallet.w3.eth.contract(
                            address=Web3.to_checksum_address(self.config.token_address),
                            abi=[{
                                "name": "allowance",
                                "type": "function",
                                "stateMutability": "view",
                                "inputs": [
                                    {"name": "owner", "type": "address"},
                                    {"name": "spender", "type": "address"},
                                ],
                                "outputs": [{"name": "", "type": "uint256"}],
                            }]
                        )
                        confirmed_allowance = token_contract.functions.allowance(
                            self.wallet.address,
                            Web3.to_checksum_address(spender_addr),
                        ).call()
                        logger.info(f"Confirmed allowance: {confirmed_allowance} >= required {sell_amount}")
                        if confirmed_allowance < sell_amount:
                            logger.error("Approval succeeded but allowance is insufficient!")
                            return
                
                # Step 4: Get fresh quote after approval
                quote = self.api_client.get_quote(
                    sell_token=self.config.token_address,
                    buy_token=self.trade_token_address,
                    sell_amount=sell_amount,
                    taker_address=self.wallet.address,
                    slippage_percentage=0.02,
                )
                if not quote.success:
                    logger.error(f"Fresh quote after approval failed: {quote.error}")
                    return
                
                # Step 5: Get swap transaction
                swap_result = self.api_client.get_swap_transaction(quote.raw_response)
                if not swap_result.success:
                    logger.error(f"Uniswap swap transaction failed: {swap_result.error}")
                    return
                quote = swap_result
        else:
            # Non-Uniswap: use standard approval flow
            spender = quote.allowance_target or self.config.zero_x_proxy
            token_allowance = self.wallet.check_allowance(self.config.token_address, spender, use_permit2=False)
            if token_allowance < sell_amount:
                logger.info(f"Approving {self.config.token_symbol} to {spender[:20]}...")
                result = self.wallet.approve_token(self.config.token_address, spender, 2**256 - 1)
                if not result.success:
                    logger.error(f"Approval failed: {result.error}")
                    return
            
                # For LI.FI, refresh quote after approval
                if getattr(self.config, 'use_li_fi', False):
                    quote = self.api_client.refresh_quote(
                        sell_token=self.config.token_address,
                        buy_token=self.trade_token_address,
                        sell_amount=sell_amount,
                        taker_address=self.wallet.address,
                        slippage_percentage=0.02,
                    )
                    if not quote.success:
                        logger.error(f"Refreshed quote failed: {quote.error}")
                        return
        
        # Execute swap with configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit_mult = getattr(self.config, 'gas_limit_multiplier', 1.05)
        gas_price_mult = getattr(self.config, 'gas_price_multiplier', 1.05)
        gas_limit = int(quote.gas * gas_limit_mult) if quote.gas else 300000
        if quote.gas_price and quote.gas_price > 0:
            gas_price = int(quote.gas_price * gas_price_mult)
        else:
            gas_price = int(self.wallet.w3.eth.gas_price * gas_price_mult)
        
        from web3 import Web3
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
            eth_received = quote.buy_amount / 10**18 if quote.buy_amount else 0
            actual_profit = eth_received - sold_cost_eth
            
            self.session_sells += 1
            self.session_profit_weth += actual_profit
            
            # Remove position
            remove_position(pos_id)
            
            if moonbag_tokens > 0:
                logger.info(f"   Moonbag: {moonbag_tokens/1e18:.4f} tokens to wallet")
            
            profit_pct = (actual_profit / sold_cost_eth * 100) if sold_cost_eth > 0 else 0
            logger.info(f"✅ Gridless sell successful! Profit: {actual_profit:.6f} {self.trade_token_name} ({profit_pct:+.2f}%)")
            
            # Reset buy cooldown so we can buy again immediately after selling
            self.last_buy_time = 0
            logger.debug(f"🔄 Buy cooldown reset after sell")
            
            # Banking
            bank_pct = getattr(self.config, 'bank_percentage', 0)
            if bank_pct > 0 and actual_profit > 0:
                bank_amount = actual_profit * bank_pct / 100
                logger.info(f"🏦 Banking {bank_pct}% of profit = {bank_amount:.6f} {self.trade_token_name} → USDG")
                self.bank_profit(bank_amount)
            
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Gridless sell failed: {result.error}")
    
    def execute_buy(self, pos_id, price):
        """Execute a buy order."""
        pos = self.positions[pos_id]
        
        # Calculate buy amount (divide available ETH/WETH by available slots up to max_active_positions)
        if getattr(self.config, 'use_eth_trading', False):
            eth_balance = self.wallet.get_eth_balance()
            gas_reserve = getattr(self.config, 'eth_gas_reserve', 0.001)
            trade_balance = max(0, eth_balance - gas_reserve)
        else:
            trade_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        
        active_positions = sum(1 for p in self.positions.values() if p['balance'] > 0)
        available_slots = self.config.max_active_positions - active_positions
        
        if available_slots <= 0:
            logger.debug(f"Max active positions reached ({active_positions}/{self.config.max_active_positions})")
            return
        
        # Use configured % of available balance divided by available slots
        tradeable_pct = getattr(self.config, 'tradeable_balance_percent', 90.0) / 100.0
        buy_amount_eth = (trade_balance * tradeable_pct) / available_slots
        buy_amount_wei = int(buy_amount_eth * 10**18)
        
        logger.info(f"Buying position {pos_id}: {buy_amount_eth:.6f} {self.trade_token_name} ({trade_balance:.6f} {self.trade_token_name} × {tradeable_pct*100:.0f}% / {available_slots} slots)")
        
        # Get quote FIRST to know the approval spender
        logger.info("Getting quote...")
        quote = self.api_client.build_swap_transaction(
            sell_token=self.trade_token_address,
            buy_token=self.config.token_address,
            sell_amount=buy_amount_wei,
            taker_address=self.wallet.address,
            slippage_percentage=0.02,
        )
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            return
        
        # Determine approval spender - use quote's allowance_target if available (LI.FI)
        spender = quote.allowance_target or self.config.zero_x_proxy
        
        # Check ERC20 approval (skip for native ETH - it doesn't need approval)
        if not getattr(self.config, 'use_eth_trading', False):
            allowance = self.wallet.check_allowance(
                self.config.weth_address,
                spender,
                use_permit2=False
            )
            logger.info(f"WETH allowance to {spender[:20]}...: {allowance}")
            if allowance < buy_amount_wei:
                logger.info(f"Approving WETH to {spender[:20]}...")
                result = self.wallet.approve_token(
                    self.config.weth_address,
                    spender,
                    2**256 - 1
                )
                if not result.success:
                    logger.error(f"Approval failed: {result.error}")
                    return
                
                # IMPORTANT: For LI.FI, refresh quote after approval
                if getattr(self.config, 'use_li_fi', False):
                    logger.info("Refreshing LI.FI quote after approval...")
                    quote = self.api_client.refresh_quote(
                        sell_token=self.config.weth_address,
                        buy_token=self.config.token_address,
                        sell_amount=buy_amount_wei,
                        taker_address=self.wallet.address,
                        slippage_percentage=0.02,
                    )
                    if not quote.success:
                        logger.error(f"Refreshed quote failed: {quote.error}")
                        return
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            return
        
        # For Uniswap API, get swap transaction from quote
        if getattr(self.config, 'use_uniswap_api', False):
            from uniswap_api import UniswapAPIClient
            if isinstance(self.api_client, UniswapAPIClient):
                swap_result = self.api_client.get_swap_transaction(quote.raw_response)
                if not swap_result.success:
                    logger.error(f"Uniswap swap transaction failed: {swap_result.error}")
                    return
                quote = swap_result
        
        # Execute swap with checksummed addresses and configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit_mult = getattr(self.config, 'gas_limit_multiplier', 1.05)
        gas_price_mult = getattr(self.config, 'gas_price_multiplier', 1.05)
        gas_limit = int(quote.gas * gas_limit_mult) if quote.gas else 350000
        if quote.gas_price and quote.gas_price > 0:
            gas_price = int(quote.gas_price * gas_price_mult)
        else:
            gas_price = int(self.wallet.w3.eth.gas_price * gas_price_mult)
        
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
            # Cost = actual WETH spent for profit calculation (in wei for precision)
            cost_wei = quote.sell_amount if quote.sell_amount else buy_amount_wei
            self.positions[pos_id]['cost_wei'] = cost_wei
            # Keep legacy 'cost' field for backward compatibility
            self.positions[pos_id]['cost'] = cost_wei // 10**9
            self.save_positions()
            
            # Calculate buy price for logging
            buy_price = buy_amount_eth / tokens if tokens > 0 else 0
            
            # Track session stats
            self.session_buys += 1
            
            logger.info(f"✅ Buy successful!")
            logger.info(f"   Position: #{pos_id}")
            logger.info(f"   Tokens: {tokens:.6f} {self.config.token_symbol}")
            logger.info(f"   Cost: {buy_amount_eth:.6f} {self.trade_token_name}")
            logger.info(f"   Buy price: {buy_price:.10f} {self.trade_token_name} per token")
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Buy failed: {result.error}")
    
    def execute_sell(self, pos_id, price):
        """Execute a sell order with moonbag and banking."""
        pos = self.positions[pos_id]
        total_balance = pos['balance']
        total_tokens = total_balance / 10**18
        
        # Validate position has tokens and cost basis
        cost_wei = pos.get('cost_wei', pos.get('cost', 0) * 10**9)
        if total_balance <= 0 or cost_wei <= 0:
            logger.warning(f"Skipping sell for position {pos_id}: balance={total_balance}, cost_wei={cost_wei}")
            return
        
        # Cost is ETH/WETH spent (in wei)
        cost_eth = cost_wei / 10**18
        buy_price = cost_eth / total_tokens if total_tokens > 0 else 0
        
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
        
        # Calculate expected ETH/WETH return (proportional to sold amount)
        expected_eth = sell_tokens * price
        # Cost basis for sold portion only
        sold_cost_eth = cost_eth * (sell_tokens / total_tokens) if total_tokens > 0 else 0
        profit_eth = expected_eth - sold_cost_eth
        
        logger.info(f"💰 Selling position {pos_id}:")
        logger.info(f"   Total tokens: {total_tokens:.6f}")
        logger.info(f"   Selling: {sell_tokens:.6f}")
        logger.info(f"   Buy price: {buy_price:.10f} {self.trade_token_name}")
        logger.info(f"   Current: {price:.10f} {self.trade_token_name}")
        logger.info(f"   Cost basis (sold): {sold_cost_eth:.6f} {self.trade_token_name}")
        logger.info(f"   Expected return: {expected_eth:.6f} {self.trade_token_name}")
        logger.info(f"   Profit: {profit_eth:.6f} {self.trade_token_name} ({profit_percent:+.2f}%)")
        
        # Get quote
        quote = self.zero_x.build_swap_transaction(
            sell_token=self.config.token_address,
            buy_token=self.trade_token_address,
            sell_amount=sell_amount,
            taker_address=self.wallet.address,
            slippage_percentage=0.02,
        )
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            return
        
        # Determine approval spender - use quote's allowance_target if available (LI.FI)
        # otherwise fall back to zero_x_proxy (0x Protocol)
        spender = quote.allowance_target or self.config.zero_x_proxy
        
        # Check/approve token for selling
        token_allowance = self.wallet.check_allowance(
            self.config.token_address,
            spender,
            use_permit2=False
        )
        if token_allowance < sell_amount:
            logger.info(f"Approving {self.config.token_symbol} to {spender[:20]}...")
            result = self.wallet.approve_token(
                self.config.token_address,
                spender,
                2**256 - 1
            )
            if not result.success:
                logger.error(f"Token approval failed: {result.error}")
                return
            
            # IMPORTANT: For LI.FI, refresh quote after approval
            # Gas prices, calldata, and routes may have changed
            if getattr(self.config, 'use_li_fi', False):
                logger.info("Refreshing LI.FI quote after approval...")
                quote = self.api_client.refresh_quote(
                    sell_token=self.config.token_address,
                    buy_token=self.trade_token_address,
                    sell_amount=sell_amount,
                    taker_address=self.wallet.address,
                    slippage_percentage=0.02,
                )
                if not quote.success:
                    logger.error(f"Refreshed quote failed: {quote.error}")
                    return
        
        # Validate quote meets minimum profit requirement (NEVER sell at loss)
        # Minimum return = cost + min_profit% (gas excluded for now)
        min_profit_percent = getattr(self.config, 'min_profit_percent', 2.0)
        min_profit_eth = sold_cost_eth * (min_profit_percent / 100)
        min_return_eth = sold_cost_eth + min_profit_eth
        
        # quote.buy_amount is in wei
        quote_return_eth = quote.buy_amount / 10**18 if quote.buy_amount else 0
        
        if quote_return_eth < min_return_eth:
            logger.warning(f"❌ Sell ABORTED: Quote return ({quote_return_eth:.6f} {self.trade_token_name}) < minimum ({min_return_eth:.6f} {self.trade_token_name})")
            logger.warning(f"   Cost: {sold_cost_eth:.6f}, Min profit: {min_profit_eth:.6f}")
            return  # Abort - never sell at loss
        
        logger.info(f"✅ Quote validated: {quote_return_eth:.6f} {self.trade_token_name} >= {min_return_eth:.6f} {self.trade_token_name} minimum")
        
        # For Uniswap API, get swap transaction from quote
        if getattr(self.config, 'use_uniswap_api', False):
            from uniswap_api import UniswapAPIClient
            if isinstance(self.api_client, UniswapAPIClient):
                swap_result = self.api_client.get_swap_transaction(quote.raw_response)
                if not swap_result.success:
                    logger.error(f"Uniswap swap transaction failed: {swap_result.error}")
                    return
                quote = swap_result
        
        # Execute swap with checksummed addresses and configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit_mult = getattr(self.config, 'gas_limit_multiplier', 1.05)
        gas_price_mult = getattr(self.config, 'gas_price_multiplier', 1.05)
        gas_limit = int(quote.gas * gas_limit_mult) if quote.gas else 300000
        if quote.gas_price and quote.gas_price > 0:
            gas_price = int(quote.gas_price * gas_price_mult)
        else:
            gas_price = int(self.wallet.w3.eth.gas_price * gas_price_mult)
        
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
            # Get actual ETH/WETH received from transaction
            eth_received = quote.buy_amount / 10**18 if quote.buy_amount else 0
            actual_profit_eth = eth_received - sold_cost_eth
            
            # Track session stats
            self.session_sells += 1
            self.session_profit_weth += actual_profit_eth
            
            # Position is always cleared to 0 after sell
            # Moonbag tokens go to wallet balance (not tracked in position)
            self.positions[pos_id]['balance'] = 0
            self.positions[pos_id]['cost'] = 0
            self.save_positions()
            
            if moonbag_tokens > 0:
                logger.info(f"   Moonbag: {moonbag_tokens / 10**18:.4f} tokens added to wallet balance")
            
            logger.info(f"✅ Sell successful!")
            logger.info(f"   Actual return: {eth_received:.6f} {self.trade_token_name}")
            logger.info(f"   Profit: {actual_profit_eth:.6f} {self.trade_token_name} ({(actual_profit_eth/sold_cost_eth*100) if sold_cost_eth > 0 else 0:+.2f}%)")
            
            # Banking: Swap % of profit to USDG
            bank_pct = getattr(self.config, 'bank_percentage', 0)
            if bank_pct > 0 and actual_profit_eth > 0:
                bank_amount = actual_profit_eth * bank_pct / 100
                logger.info(f"🏦 Banking: Swapping {bank_pct}% of profit = {bank_amount:.6f} {self.trade_token_name} → USDG")
                self.bank_profit(bank_amount)
            
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Sell failed: {result.error}")
    
    def bank_profit(self, eth_amount):
        """Swap ETH/WETH profit to USDG for banking."""
        if eth_amount <= 0:
            return
        
        # Convert to wei
        eth_wei = int(eth_amount * 10**18)
        
        logger.info(f"🏦 Getting quote for banking {eth_amount:.6f} {self.trade_token_name} → USDG...")
        
        # Get quote for ETH/WETH -> USDG
        quote = self.zero_x.build_swap_transaction(
            sell_token=self.trade_token_address,
            buy_token=self.config.usdg_address,
            sell_amount=eth_wei,
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
        
        logger.info(f"🏦 Banking {eth_amount:.6f} {self.trade_token_name} → ~{expected_usdg:.2f} USDG...")
        
        # Check/approve WETH for swapping (skip for native ETH)
        if not getattr(self.config, 'use_eth_trading', False):
            weth_allowance = self.wallet.check_allowance(
                self.config.weth_address,
                self.config.zero_x_proxy,
                use_permit2=False
            )
            if weth_allowance < eth_wei:
                logger.info(f"Approving WETH to AllowanceHolder for banking...")
                result = self.wallet.approve_token(
                    self.config.weth_address,
                    self.config.zero_x_proxy,
                    2**256 - 1
                )
                if not result.success:
                    logger.error(f"WETH approval for banking failed: {result.error}")
                    return
        
        # For Uniswap API, get swap transaction from quote
        if getattr(self.config, 'use_uniswap_api', False):
            from uniswap_api import UniswapAPIClient
            if isinstance(self.api_client, UniswapAPIClient):
                swap_result = self.api_client.get_swap_transaction(quote.raw_response)
                if not swap_result.success:
                    logger.error(f"Uniswap swap transaction failed: {swap_result.error}")
                    return
                quote = swap_result
        
        # Execute banking swap with configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit_mult = getattr(self.config, 'gas_limit_multiplier', 1.05)
        gas_price_mult = getattr(self.config, 'gas_price_multiplier', 1.05)
        gas_limit = int(quote.gas * gas_limit_mult) if quote.gas else 300000
        if quote.gas_price and quote.gas_price > 0:
            gas_price = int(quote.gas_price * gas_price_mult)
        else:
            gas_price = int(self.wallet.w3.eth.gas_price * gas_price_mult)
        
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
        if getattr(self.config, 'use_eth_trading', False):
            eth_bal = self.wallet.get_eth_balance()
            weth_bal = eth_bal  # Use ETH balance for display
        else:
            weth_bal, weth_raw = self.wallet.get_token_balance(self.config.weth_address)
        token_bal, token_raw = self.wallet.get_token_balance(self.config.token_address)
        
        # Check if gridless mode is enabled
        use_gridless = getattr(self.config, 'use_gridless', False)
        
        # Count positions - balance > 0 means active (even if cost is 0, could be moonbag)
        if use_gridless:
            # Load gridless positions for display
            from gridless import load_positions
            gridless_positions = load_positions()
            active = len(gridless_positions)
            empty = 0  # Gridless doesn't have empty slots
            position_balance_total = sum(p.get('balance', 0) for p in gridless_positions.values()) / 10**18
        else:
            active = sum(1 for p in self.positions.values() if p['balance'] > 0)
            empty = sum(1 for p in self.positions.values() if p['balance'] == 0)
            position_balance_total = sum(p['balance'] for p in self.positions.values()) / 10**18
        
        # Calculate moonbag (tokens in wallet not in positions)
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
            
            # Line 2 & 3: W/T/Pos and B/S/P stats
            balance_letter = "E" if getattr(self.config, 'use_eth_trading', False) else "W"
            logger.info(f"{balance_letter}:{weth_bal:.3f} T:{token_bal:.0f} {active}/{self.config.max_active_positions}/{active+empty}")
            logger.info(f"B:{self.session_buys} S:{self.session_sells} P:{self.session_profit_weth:.6f}")
            
            # Separator matches 26 char width
            logger.info("-" * 26)
            
            # Each position on its own line (max 3), no price shown
            if use_gridless:
                # Sort by buy price ascending for consistent display
                from gridless import get_buy_price
                active_positions = sorted(
                    [(pid, p) for pid, p in gridless_positions.items()],
                    key=lambda x: get_buy_price(x[1])
                )
                for pos_id, pos in active_positions[:3]:
                    tokens = pos.get('balance', 0) / 10**18
                    # Support both cost_wei (new) and cost (legacy nano-ETH)
                    cost_wei = pos.get('cost_wei', 0)
                    if cost_wei <= 0 and 'cost' in pos:
                        old_cost = pos.get('cost', 0)
                        if old_cost > 0:
                            cost_wei = old_cost * 10**9
                    cost_eth = cost_wei / 10**18
                    if tokens > 0 and cost_eth > 0:
                        buy_price = cost_eth / tokens
                        pnl = ((price - buy_price) / buy_price * 100)
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | P&L: {pnl:>+5.1f}%")
                    else:
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | N/A")
            else:
                active_positions = [(pid, p) for pid, p in self.positions.items() if p['balance'] > 0]
                for pos_id, pos in active_positions[:3]:
                    tokens = pos['balance'] / 10**18
                    cost_weth = pos['cost'] / 10**9
                    if tokens > 0 and cost_weth > 0:
                        buy_price = cost_weth / tokens
                        pnl = ((price - buy_price) / buy_price * 100)
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | P&L: {pnl:>+5.1f}%")
                    else:
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | moonbag")
            if len(active_positions) > 3:
                logger.info(f"... and {len(active_positions) - 3} more")
            
            # Final separator
            logger.info("-" * 26)
        else:
            # Verbose round summary (original format)
            balance_label = "ETH" if getattr(self.config, 'use_eth_trading', False) else "WETH"
            logger.info("=" * 70)
            logger.info(f"ROUND #{self.round_count} | {self.config.token_symbol} | Elapsed: {elapsed:.0f}s")
            logger.info("=" * 70)
            logger.info(f"💰 {balance_label} Balance: {weth_bal:.6f}")
            logger.info(f"🪙 Token Balance: {token_bal:.6f} (in positions: {position_balance_total:.4f}, moonbag: {moonbag_balance:.4f})")
            logger.info(f"📊 Price: 1 {self.config.token_symbol} = {price:.10f} {balance_label}")
            logger.info(f"📈 Positions: {active} active / {empty} empty (max active: {self.config.max_active_positions})")
            logger.info(f"📊 Session: {self.session_buys} buys, {self.session_sells} sells, {self.session_profit_weth:.6f} {balance_label} profit")
            
            # Show active positions with P&L and sell targets
            if active > 0:
                logger.info("🎯 Active Positions:")
                if use_gridless:
                    # Display gridless positions sorted by buy price ascending
                    from gridless import get_buy_price
                    sell_threshold = getattr(self.config, 'gridless_sell_threshold', 5.0)
                    sorted_positions = sorted(
                        gridless_positions.items(),
                        key=lambda x: get_buy_price(x[1])
                    )
                    for pos_id, pos in sorted_positions:
                        balance_raw = pos.get('balance', 0)
                        # Support both cost_wei (new) and cost (legacy nano-ETH)
                        cost_wei = pos.get('cost_wei', 0)
                        if cost_wei <= 0 and 'cost' in pos:
                            old_cost = pos.get('cost', 0)
                            if old_cost > 0:
                                cost_wei = old_cost * 10**9
                        tokens = balance_raw / 10**18
                        cost_eth = cost_wei / 10**18
                        # Calculate buy_price from cost/balance
                        if tokens > 0 and cost_eth > 0:
                            buy_price = cost_eth / tokens
                            sell_target = buy_price * (1 + sell_threshold / 100)
                            pnl = ((price - buy_price) / buy_price * 100)
                            price_diff = sell_target - price
                            price_pct = (price_diff / price * 100) if price > 0 else 0
                            logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: {buy_price:>12.10f} | Sell@: {sell_target:>12.10f} | P&L: {pnl:>+6.2f}% (need +{price_pct:>5.1f}% more to sell)")
                        else:
                            logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: N/A | P&L: N/A")
                else:
                    # Display classic grid positions
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
                                logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: {buy_price:>12.10f} | Sell@: {sell_min:>12.10f} | P&L: {pnl:>+6.2f}% (need +{price_pct:>5.1f}% more to sell)")
                            else:
                                # Moonbag or dust position with unknown cost
                                price_diff = sell_min - price
                                price_pct = (price_diff / price * 100) if price > 0 else 0
                                logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: moonbag | Sell@: {sell_min:>12.10f} | P&L: N/A (need +{price_pct:>5.1f}% more to sell)")
            
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
