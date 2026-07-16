"""
Wallet management module for the Robinhood Chain Grid Trading Bot.

Handles account management, transaction signing, and token interactions
including approvals via Permit2.
"""

import time
import logging
from typing import Optional, Any
from dataclasses import dataclass
from eth_account import Account
from eth_account.datastructures import SignedTransaction
from web3 import Web3
from web3.types import TxParams, Wei, ChecksumAddress
from eth_abi import encode
from hexbytes import HexBytes

from config import BotConfig
from utils import wei_to_eth, eth_to_wei

# Standard ERC20 ABI (minimal for balance and approve)
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

# WETH ABI (for deposit/withdraw)
WETH_ABI = [
    {
        "constant": False,
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "payable": True,
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "payable": False,
        "type": "function",
    },
    *ERC20_ABI,
]

# Permit2 ABI
PERMIT2_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
        ],
        "name": "approve",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [
            {"internalType": "uint160", "name": "amount", "type": "uint160"},
            {"internalType": "uint48", "name": "expiration", "type": "uint48"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class TokenInfo:
    """Token information dataclass."""
    address: str
    symbol: str
    decimals: int


@dataclass
class TransactionResult:
    """Transaction result dataclass."""
    success: bool
    tx_hash: Optional[str] = None
    receipt: Optional[Any] = None
    error: Optional[str] = None
    gas_used: Optional[int] = None
    effective_gas_price: Optional[int] = None


class Wallet:
    """
    Wallet manager for EVM blockchain interactions.
    
    Handles account operations, token interactions, and transaction
    signing for the grid trading bot.
    """
    
    def __init__(self, config: BotConfig):
        """
        Initialize wallet with configuration.
        
        Args:
            config: Bot configuration object.
        """
        self.config = config
        self.logger = logging.getLogger("grid_bot.wallet")
        
        # Initialize Web3 connection
        self.w3 = Web3(Web3.HTTPProvider(config.rpc_url))
        
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC: {config.rpc_url}")
        
        # Load account from private key
        self.account = Account.from_key(config.private_key)
        self.address = self.account.address
        
        self.logger.info(f"Wallet initialized: {self.address}")
        
        # Initialize token contracts
        self._token_info_cache: dict[str, TokenInfo] = {}
        
    @property
    def checksum_address(self) -> ChecksumAddress:
        """Get checksum address."""
        return Web3.to_checksum_address(self.address)
    
    def get_eth_balance(self) -> float:
        """
        Get ETH balance.
        
        Returns:
            float: Balance in ETH.
        """
        balance_wei = self.w3.eth.get_balance(self.address)
        return wei_to_eth(balance_wei)
    
    def get_token_balance(self, token_address: str) -> tuple[float, int]:
        """
        Get token balance.
        
        Args:
            token_address: Token contract address.
            
        Returns:
            tuple[float, int]: (Balance in token units, raw balance).
        """
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        
        decimals = self._get_token_decimals(token_address)
        raw_balance = token.functions.balanceOf(self.address).call()
        
        return raw_balance / (10 ** decimals), raw_balance
    
    def _get_token_decimals(self, token_address: str) -> int:
        """Get token decimals from cache or contract."""
        if token_address not in self._token_info_cache:
            self._load_token_info(token_address)
        return self._token_info_cache[token_address].decimals
    
    def _load_token_info(self, token_address: str) -> TokenInfo:
        """Load and cache token information."""
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        
        try:
            symbol = token.functions.symbol().call()
        except Exception:
            symbol = "UNKNOWN"
        
        try:
            decimals = token.functions.decimals().call()
        except Exception:
            decimals = 18
        
        info = TokenInfo(
            address=token_address,
            symbol=symbol,
            decimals=decimals,
        )
        
        self._token_info_cache[token_address] = info
        return info
    
    def get_token_info(self, token_address: str) -> TokenInfo:
        """
        Get token information.
        
        Args:
            token_address: Token contract address.
            
        Returns:
            TokenInfo: Token information.
        """
        if token_address not in self._token_info_cache:
            return self._load_token_info(token_address)
        return self._token_info_cache[token_address]
    
    def approve_token(
        self,
        token_address: str,
        spender_address: str,
        amount: int,
        wait_for_receipt: bool = True,
    ) -> TransactionResult:
        """
        Approve token spending via standard ERC20 approve.
        
        Args:
            token_address: Token to approve.
            spender_address: Spender address.
            amount: Approval amount.
            wait_for_receipt: Whether to wait for transaction receipt.
            
        Returns:
            TransactionResult: Transaction result.
        """
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        
        # Use legacy gas pricing for Robinhood Chain to avoid base fee volatility
        gas_price = self.w3.eth.gas_price
        # Add 20% buffer
        gas_price = int(gas_price * 1.2)
        gas_price_params = {
            "gasPrice": gas_price,
        }
        
        # Build transaction
        tx = token.functions.approve(
            Web3.to_checksum_address(spender_address),
            amount,
        ).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 100000,  # Approve is typically ~45k gas
            **gas_price_params,
        })
        
        return self._send_transaction(tx, wait_for_receipt)
    
    def approve_token_permit2(
        self,
        token_address: str,
        spender_address: str,
        amount: int,
        expiration_seconds: int = 3600,
        wait_for_receipt: bool = True,
    ) -> TransactionResult:
        """
        Approve token spending via Permit2.
        
        Args:
            token_address: Token to approve.
            spender_address: Spender address (e.g., 0x Exchange Proxy).
            amount: Approval amount.
            expiration_seconds: Approval expiration time.
            wait_for_receipt: Whether to wait for transaction receipt.
            
        Returns:
            TransactionResult: Transaction result.
        """
        permit2 = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config.permit2_address),
            abi=PERMIT2_ABI,
        )

        expiration = int(time.time()) + expiration_seconds

        # Use legacy gas pricing for Robinhood Chain to avoid base fee volatility
        gas_price = self.w3.eth.gas_price
        # Add 20% buffer
        gas_price = int(gas_price * 1.2)
        gas_price_params = {
            "gasPrice": gas_price,
        }
        
        tx = permit2.functions.approve(
            Web3.to_checksum_address(token_address),
            Web3.to_checksum_address(spender_address),
            amount,
            expiration,
        ).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 100000,
            **gas_price_params,
        })
        
        return self._send_transaction(tx, wait_for_receipt)
    
    def check_allowance(
        self,
        token_address: str,
        spender_address: str,
        use_permit2: bool = False,
    ) -> int:
        """
        Check token allowance.
        
        Args:
            token_address: Token address.
            spender_address: Spender address.
            use_permit2: Whether to check Permit2 allowance.
            
        Returns:
            int: Current allowance.
        """
        if use_permit2:
            permit2 = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.config.permit2_address),
                abi=PERMIT2_ABI,
            )
            amount, _ = permit2.functions.allowance(
                self.address,
                Web3.to_checksum_address(token_address),
                Web3.to_checksum_address(spender_address),
            ).call()
            return amount
        else:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI,
            )
            return token.functions.allowance(
                self.address,
                Web3.to_checksum_address(spender_address),
            ).call()
    
    def ensure_approval(
        self,
        token_address: str,
        spender_address: str,
        required_amount: int,
        use_permit2: bool = True,
    ) -> bool:
        """
        Ensure token is approved for spending, approving if necessary.
        
        Args:
            token_address: Token address.
            spender_address: Spender address.
            required_amount: Amount that needs to be approved.
            use_permit2: Whether to use Permit2.
            
        Returns:
            bool: True if approval is sufficient.
        """
        current_allowance = self.check_allowance(token_address, spender_address, use_permit2)
        
        if current_allowance >= required_amount:
            return True
        
        self.logger.info(f"Approving {token_address} for {spender_address}")
        
        # Approve max uint256 for unlimited approval
        max_approval = 2**256 - 1
        
        if use_permit2:
            result = self.approve_token_permit2(token_address, spender_address, max_approval)
        else:
            result = self.approve_token(token_address, spender_address, max_approval)
        
        if result.success:
            self.logger.info(f"Approval successful: {result.tx_hash}")
        else:
            self.logger.error(f"Approval failed: {result.error}")
        
        return result.success
    
    def _send_transaction(
        self,
        tx: TxParams,
        wait_for_receipt: bool = True,
    ) -> TransactionResult:
        """
        Sign and send a transaction.
        
        Args:
            tx: Transaction parameters.
            wait_for_receipt: Whether to wait for receipt.
            
        Returns:
            TransactionResult: Transaction result.
        """
        try:
            # Sign transaction
            signed_tx = self.account.sign_transaction(tx)
            
            # Send transaction - handle both old and new eth-account versions
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            if raw_tx is None:
                raise AttributeError("SignedTransaction has no raw_transaction attribute")
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash.hex()
            
            self.logger.debug(f"Transaction sent: {tx_hash_hex}")
            
            if not wait_for_receipt:
                return TransactionResult(success=True, tx_hash=tx_hash_hex)
            
            # Wait for receipt
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] == 1:
                return TransactionResult(
                    success=True,
                    tx_hash=tx_hash_hex,
                    receipt=receipt,
                    gas_used=receipt.get("gasUsed"),
                    effective_gas_price=receipt.get("effectiveGasPrice"),
                )
            else:
                # Try to get revert reason
                error_msg = "Transaction failed (status=0)"
                try:
                    # Replay transaction to get revert reason
                    tx_data = self.w3.eth.get_transaction(tx_hash)
                    self.w3.eth.call({
                        "from": tx_data["from"],
                        "to": tx_data["to"],
                        "data": tx_data["input"],
                        "value": tx_data.get("value", 0),
                    }, receipt["blockNumber"])
                except Exception as call_e:
                    error_msg = f"Transaction reverted: {str(call_e)}"

                return TransactionResult(
                    success=False,
                    tx_hash=tx_hash_hex,
                    receipt=receipt,
                    error=error_msg,
                )

        except Exception as e:
            self.logger.error(f"Transaction failed: {e}")
            return TransactionResult(success=False, error=str(e))
    
    def send_raw_transaction(
        self,
        signed_tx: SignedTransaction,
        wait_for_receipt: bool = True,
    ) -> TransactionResult:
        """
        Send a pre-signed transaction.
        
        Args:
            signed_tx: Signed transaction object.
            wait_for_receipt: Whether to wait for receipt.
            
        Returns:
            TransactionResult: Transaction result.
        """
        try:
            # Handle both old and new eth-account versions
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            if raw_tx is None:
                raise AttributeError("SignedTransaction has no raw_transaction attribute")
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash.hex()
            
            if not wait_for_receipt:
                return TransactionResult(success=True, tx_hash=tx_hash_hex)
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt["status"] == 1:
                return TransactionResult(
                    success=True,
                    tx_hash=tx_hash_hex,
                    receipt=receipt,
                    gas_used=receipt.get("gasUsed"),
                    effective_gas_price=receipt.get("effectiveGasPrice"),
                )
            else:
                return TransactionResult(
                    success=False,
                    tx_hash=tx_hash_hex,
                    receipt=receipt,
                    error="Transaction failed (status=0)",
                )
        
        except Exception as e:
            self.logger.error(f"Raw transaction failed: {e}")
            return TransactionResult(success=False, error=str(e))
    
    def get_gas_price(self) -> dict:
        """
        Get current gas prices.
        
        Returns:
            dict: Gas price information.
        """
        return {
            "base_fee": self.w3.eth.get_block("latest")["baseFeePerGas"],
            "max_fee": self.w3.eth.max_fee_per_gas,
            "priority_fee": self.w3.eth.max_priority_fee_per_gas,
        }
    
    def estimate_gas(self, tx: TxParams) -> int:
        """
        Estimate gas for a transaction.
        
        Args:
            tx: Transaction parameters.
            
        Returns:
            int: Estimated gas.
        """
        return self.w3.eth.estimate_gas(tx)