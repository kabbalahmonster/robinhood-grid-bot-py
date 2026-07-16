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
        
        # Get quote
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
        
        # Execute swap
        result = self.wallet._send_transaction({
            "from": self.wallet.address,
            "to": quote.to,
            "data": quote.data,
            "value": quote.value or 0,
            "gas": int(quote.gas * 1.2),
            "gasPrice": int(self.wallet.w3.eth.gas_price * 1.2),
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        })
        
        if result.success:
            # Update position - store cost in nano-WETH
            tokens_received = quote.buy_amount
            self.positions[pos_id]['balance'] = tokens_received
            self.positions[pos_id]['cost'] = int(price * 10**9)  # nano-WETH
            self.save_positions()
            logger.info(f"✅ Buy successful: {tokens_received / 10**18:.6f} tokens (tx: {result.tx_hash[:20]}...)")
        else:
            logger.error(f"❌ Buy failed: {result.error}")
    
    def execute_sell(self, pos_id, price):
        """Execute a sell order."""
        pos = self.positions[pos_id]
        sell_amount = pos['balance']
        # Cost is stored in nano-WETH
        cost_basis = pos['cost'] / 10**9
        
        # Calculate profit
        if cost_basis > 0:
            profit_percent = ((price - cost_basis) / cost_basis) * 100
        else:
            profit_percent = 0
        logger.info(f"Selling position {pos_id}: Cost basis {cost_basis:.10f}, Current {price:.10f}, Profit {profit_percent:.2f}%")
        
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
        
        # Execute swap
        result = self.wallet._send_transaction({
            "from": self.wallet.address,
            "to": quote.to,
            "data": quote.data,
            "value": quote.value or 0,
            "gas": int(quote.gas * 1.2),
            "gasPrice": int(self.wallet.w3.eth.gas_price * 1.2),
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
        price = self.get_token_price()
        if price is None:
            logger.warning("Could not get price")
            return
        
        logger.info(f"Price: 1 {self.config.token_symbol} = {price:.10f} WETH")
        
        # Check sells first (take profits)
        self.check_sells(price)
        
        # Then check buys
        self.check_buys(price)
    
    def run(self):
        """Main bot loop."""
        self.load_positions()
        
        logger.info("Starting main loop...")
        while self.running:
            try:
                self.run_cycle()
                time.sleep(30)  # 30 second intervals
            except KeyboardInterrupt:
                logger.info("Stopping bot...")
                self.running = False
            except Exception as e:
                logger.error(f"Error in cycle: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = GridBot()
    bot.run()
