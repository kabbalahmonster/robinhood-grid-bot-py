#!/usr/bin/env python3
"""
Simple Uniswap V3 swap using direct router calls.
Works with standard ERC20 approvals (no Permit2).
"""

from web3 import Web3
from typing import Optional, Tuple
import logging

# Uniswap V3 Router on Robinhood Chain (if exists) or generic
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"  # Standard address

# Minimal Router ABI for exactInputSingle
ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType": "address", "name": "tokenIn", "type": "address"},
                {"internalType": "address", "name": "tokenOut", "type": "address"},
                {"internalType": "uint24", "name": "fee", "type": "uint24"},
                {"internalType": "address", "name": "recipient", "type": "address"},
                {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
            ],
            "internalType": "struct ISwapRouter.ExactInputSingleParams",
            "name": "params",
            "type": "tuple"
        }],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

logger = logging.getLogger('uniswap_v3')


class UniswapV3Swap:
    """Simple Uniswap V3 swap handler."""
    
    def __init__(self, config):
        self.config = config
        self.w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(UNISWAP_V3_ROUTER),
            abi=ROUTER_ABI
        )
    
    def swap_exact_input_single(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        min_amount_out: int,
        recipient: str,
        fee: int = 3000,  # 0.3%
        deadline: Optional[int] = None
    ) -> Tuple[bool, Optional[str], Optional[int]]:
        """
        Execute exact input swap on Uniswap V3.
        
        Returns: (success, tx_hash, amount_out)
        """
        if deadline is None:
            import time
            deadline = int(time.time()) + 300  # 5 min
        
        try:
            params = (
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                fee,
                Web3.to_checksum_address(recipient),
                deadline,
                amount_in,
                min_amount_out,
                0  # sqrtPriceLimitX96 (no limit)
            )
            
            logger.info(f"Swapping {amount_in} {token_in} -> {token_out}")
            
            # Build transaction (will be signed by wallet)
            tx = self.router.functions.exactInputSingle(params).build_transaction({
                'from': recipient,
                'gas': 250000,
                'gasPrice': int(self.w3.eth.gas_price * 1.2),
                'nonce': self.w3.eth.get_transaction_count(recipient),
                'chainId': self.config.chain_id,
                'value': 0
            })
            
            logger.info(f"Built tx: {tx}")
            return True, None, tx  # Return tx for wallet to sign
            
        except Exception as e:
            logger.error(f"Failed to build swap: {e}")
            return False, str(e), None
