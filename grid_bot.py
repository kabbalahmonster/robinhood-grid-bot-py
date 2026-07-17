#!/usr/bin/env python3
"""
Robinhood Chain Grid Trading Bot
Uses positions.json format compatible with original bot
"""

import json
import time
import logging
from decimal import Decimal
from web3 import Web3

from config import load_config
from wallet import Wallet
from zero_x import ZeroXClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('grid_bot')

class GridBot:
    def __init__(self):
        self.config = load_config()
        self.wallet = Wallet(self.config)
        self.zero_x = ZeroXClient(self.config)
        self.positions_file = "data/positions.json"
        self.positions = {}
        self.running = True
        
        logger.info(f"Grid Bot initialized")
        logger.info(f"Wallet: {self.wallet.address}")
        logger.info(f"Trading: {self.config.token_symbol}")
        logger.info(f"Max active positions: {self.config.max_active_positions}")
    
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
        """Get current token price in WETH."""
        # Get quote for small amount to determine price
        quote = self.zero_x.get_quote(
            sell_token=self.config.weth_address,
            buy_token=self.config.token_address,
            sell_amount=10**15,  # 0.001 WETH
            taker_address=self.wallet.address,
        )
        if quote.success and quote.buy_amount:
            # Price = WETH / tokens
            return 10**15 / quote.buy_amount
        return None
    
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
        for pos_id, pos in self.positions.items():
            if pos['balance'] > 0:  # Has tokens
                # Scale: 10^9 (nano-WETH)
                sell_min = pos['sellMin'] / 10**9
                
                if price >= sell_min:
                    logger.info(f"Sell trigger: Position {pos_id} at price {price:.10f} (sellMin: {sell_min:.10f})")
                    self.execute_sell(pos_id, price)
                    return  # One sell per cycle
    
    def execute_buy(self, pos_id, price):
        """Execute a buy order."""
        pos = self.positions[pos_id]
        
        # Calculate buy amount (divide available WETH by empty positions)
        weth_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        empty_positions = sum(1 for p in self.positions.values() if p['balance'] == 0)
        
        if empty_positions == 0:
            return
        
        # Use 90% of available WETH divided by empty positions
        buy_amount_eth = (weth_balance * 0.9) / empty_positions
        buy_amount_wei = int(buy_amount_eth * 10**18)
        
        logger.info(f"Buying position {pos_id}: {buy_amount_eth:.6f} WETH")
        
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
            
            logger.info(f"✅ Buy successful!")
            logger.info(f"   Position: #{pos_id}")
            logger.info(f"   Tokens: {tokens:.6f} {self.config.token_symbol}")
            logger.info(f"   Cost: {buy_amount_eth:.6f} WETH")
            logger.info(f"   Buy price: {buy_price:.10f} WETH per token")
            logger.info(f"   Tx: {result.tx_hash[:30]}...")
        else:
            logger.error(f"❌ Buy failed: {result.error}")
    
    def execute_sell(self, pos_id, price):
        """Execute a sell order."""
        pos = self.positions[pos_id]
        sell_amount = pos['balance']
        # Cost is WETH spent (in nano-WETH)
        cost_weth = pos['cost'] / 10**9
        tokens = sell_amount / 10**18
        buy_price = cost_weth / tokens if tokens > 0 else 0
        
        # Calculate profit
        if buy_price > 0:
            profit_percent = ((price - buy_price) / buy_price) * 100
        else:
            profit_percent = 0
        
        # Calculate expected WETH return
        expected_weth = tokens * price
        profit_weth = expected_weth - cost_weth
        
        logger.info(f"💰 Selling position {pos_id}:")
        logger.info(f"   Tokens: {tokens:.6f}")
        logger.info(f"   Buy price: {buy_price:.10f} WETH")
        logger.info(f"   Current: {price:.10f} WETH")
        logger.info(f"   Cost: {cost_weth:.6f} WETH")
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
            # Update position
            self.positions[pos_id]['balance'] = 0
            self.positions[pos_id]['cost'] = 0
            self.save_positions()
            logger.info(f"✅ Sell successful (tx: {result.tx_hash[:20]}...)")
        else:
            logger.error(f"❌ Sell failed: {result.error}")
    
    def run_cycle(self):
        """Run one trading cycle."""
        # Get balances
        weth_bal, _ = self.wallet.get_token_balance(self.config.weth_address)
        token_bal, _ = self.wallet.get_token_balance(self.config.token_address)
        
        # Count positions
        active = sum(1 for p in self.positions.values() if p['balance'] > 0)
        empty = sum(1 for p in self.positions.values() if p['balance'] == 0)
        
        # Get price
        price = self.get_token_price()
        if price is None:
            logger.warning("Could not get price")
            return
        
        # Verbose round summary
        logger.info("=" * 60)
        logger.info(f"ROUND SUMMARY | {self.config.token_symbol}")
        logger.info("=" * 60)
        logger.info(f"💰 WETH Balance: {weth_bal:.6f}")
        logger.info(f"🪙 Token Balance: {token_bal / 10**18:.6f}")
        logger.info(f"📊 Price: 1 {self.config.token_symbol} = {price:.10f} WETH")
        logger.info(f"📈 Positions: {active} active / {empty} empty")
        
        # Show active positions with P&L
        if active > 0:
            logger.info("🎯 Active Positions:")
            for pos_id, pos in self.positions.items():
                if pos['balance'] > 0:
                    tokens = pos['balance'] / 10**18
                    cost_weth = pos['cost'] / 10**9
                    buy_price = cost_weth / tokens if tokens > 0 else 0
                    pnl = ((price - buy_price) / buy_price * 100) if buy_price > 0 else 0
                    logger.info(f"   #{pos_id}: {tokens:.4f} tokens @ {buy_price:.10f} | P&L: {pnl:+.2f}%")
        
        logger.info("-" * 60)
        
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
