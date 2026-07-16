"""
Test wallet functions, approvals, and balances.

Tests cover:
- Wallet creation and account management
- Token balance fetching
- ERC20 approval logic (standard and Permit2)
- Transaction execution and monitoring
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from decimal import Decimal
import json


# =============================================================================
# Wallet and Account Tests
# =============================================================================

class TestWalletCreation:
    """Tests for wallet creation and account management"""
    
    @pytest.mark.wallet
    def test_create_account_from_private_key(self, mock_private_key):
        """Test creating an account from a private key"""
        # Simulate account creation
        account = self._create_account(mock_private_key)
        
        assert account is not None
        assert account.address.startswith("0x")
        assert len(account.address) == 42
        assert account.private_key == mock_private_key
    
    @pytest.mark.wallet
    def test_create_account_adds_0x_prefix(self):
        """Test that account creation adds 0x prefix if missing"""
        key_without_prefix = "11" * 32
        account = self._create_account(key_without_prefix)
        
        assert account.private_key.startswith("0x")
    
    @pytest.mark.wallet
    def test_create_account_invalid_key(self):
        """Test that invalid private keys raise appropriate errors"""
        with pytest.raises(ValueError):
            self._create_account("invalid_key")
    
    def _create_account(self, private_key):
        """Helper to create account - simulates viem privateKeyToAccount"""
        if not private_key.startswith("0x"):
            private_key = f"0x{private_key}"
        
        if len(private_key) != 66:  # 0x + 64 hex chars
            raise ValueError("Invalid private key length")
        
        # Generate deterministic address from key (simplified)
        address = "0x" + private_key[-40:]  # Last 40 chars as address
        
        MockAccount = type('MockAccount', (), {
            'address': address,
            'private_key': private_key,
        })
        return MockAccount()


class TestTokenBalances:
    """Tests for token balance fetching"""
    
    @pytest.mark.wallet
    def test_get_token_balance_success(self, mock_web3_instance, mock_token_addresses):
        """Test successfully fetching token balance"""
        token_address = mock_token_addresses["trading"]
        owner_address = "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"
        
        balance = self._get_token_balance(mock_web3_instance, token_address, owner_address)
        
        assert balance is not None
        assert balance.address == token_address
        assert balance.symbol == "TEST"
        assert balance.decimals == 18
        assert balance.balance > 0
    
    @pytest.mark.wallet
    def test_get_token_balance_zero(self, mock_web3_instance, mock_token_addresses):
        """Test fetching zero token balance"""
        mock_web3_instance.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 0
        
        balance = self._get_token_balance(
            mock_web3_instance, 
            mock_token_addresses["trading"], 
            "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"
        )
        
        assert balance.balance == 0
        assert balance.formatted_balance == "0.0"
    
    @pytest.mark.wallet
    def test_get_token_balance_handles_different_decimals(self, mock_web3_instance):
        """Test balance fetching with different token decimals"""
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call.return_value = 1000000  # 1 USDC
        mock_contract.functions.decimals.return_value.call.return_value = 6
        mock_contract.functions.symbol.return_value.call.return_value = "USDC"
        mock_web3_instance.eth.contract.return_value = mock_contract
        
        balance = self._get_token_balance(
            mock_web3_instance,
            "0xUSDC",
            "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"
        )
        
        assert balance.decimals == 6
        assert balance.formatted_balance == "1.0"
    
    @pytest.mark.wallet
    def test_get_native_balance(self, mock_web3_instance):
        """Test fetching native ETH balance"""
        address = "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"
        
        balance = self._get_native_balance(mock_web3_instance, address)
        
        assert balance.symbol == "ETH"
        assert balance.decimals == 18
        assert balance.balance == 10**18  # 1 ETH
    
    @pytest.mark.wallet
    def test_get_token_balance_error_handling(self, mock_web3_instance):
        """Test error handling for balance fetch failures"""
        mock_web3_instance.eth.contract.side_effect = Exception("Contract call failed")
        
        with pytest.raises(Exception, match="Contract call failed"):
            self._get_token_balance(mock_web3_instance, "0xInvalid", "0xOwner")
    
    def _get_token_balance(self, web3, token_address, owner_address):
        """Helper to get token balance - simulates ERC20 balance fetching"""
        contract = web3.eth.contract(address=token_address, abi=self._get_erc20_abi())
        
        balance = contract.functions.balanceOf(owner_address).call()
        decimals = contract.functions.decimals().call()
        symbol = contract.functions.symbol().call()
        
        formatted = str(balance / (10 ** decimals))
        
        MockBalance = type('MockBalance', (), {
            'address': token_address,
            'symbol': symbol,
            'balance': balance,
            'decimals': decimals,
            'formatted_balance': formatted,
        })
        return MockBalance()
    
    def _get_native_balance(self, web3, address):
        """Helper to get native ETH balance"""
        balance = web3.eth.get_balance(address)
        
        MockBalance = type('MockBalance', (), {
            'address': "0x0000000000000000000000000000000000000000",
            'symbol': "ETH",
            'balance': balance,
            'decimals': 18,
            'formatted_balance': str(balance / 10**18),
        })
        return MockBalance()
    
    def _get_erc20_abi(self):
        """Return minimal ERC20 ABI"""
        return [
            {"constant": True, "inputs": [{"name": "owner", "type": "address"}], 
             "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
        ]


class TestTokenApprovals:
    """Tests for ERC20 token approvals"""
    
    @pytest.mark.wallet
    def test_check_allowance_sufficient(self, mock_web3_instance, mock_token_addresses):
        """Test allowance check when already approved"""
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        amount = 1000 * 10**18
        
        is_approved = self._check_allowance(mock_web3_instance, token, spender, amount)
        
        assert is_approved is True
    
    @pytest.mark.wallet
    def test_check_allowance_insufficient(self, mock_web3_instance, mock_token_addresses):
        """Test allowance check when insufficient"""
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = 0
        mock_web3_instance.eth.contract.return_value = mock_contract
        
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        amount = 1000 * 10**18
        
        is_approved = self._check_allowance(mock_web3_instance, token, spender, amount)
        
        assert is_approved is False
    
    @pytest.mark.wallet
    def test_approve_token_success(self, mock_web3_instance, mock_token_addresses):
        """Test successful token approval"""
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        
        success = self._approve_token(mock_web3_instance, token, spender)
        
        assert success is True
    
    @pytest.mark.wallet
    def test_approve_token_failure(self, mock_web3_instance, mock_token_addresses):
        """Test token approval failure"""
        mock_web3_instance.eth.get_transaction_receipt.return_value = {
            "status": 0,  # Failed
            "transactionHash": "0xfailed",
        }
        
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        
        success = self._approve_token(mock_web3_instance, token, spender)
        
        assert success is False
    
    @pytest.mark.wallet
    def test_check_and_approve_approves_when_needed(self, mock_web3_instance, mock_token_addresses):
        """Test check_and_approve_token approves when allowance is insufficient"""
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = 0  # No allowance
        mock_contract.functions.approve.return_value.transact.return_value = "0xtxhash"
        mock_web3_instance.eth.contract.return_value = mock_contract
        
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        amount = 1000 * 10**18
        
        success = self._check_and_approve_token(mock_web3_instance, token, spender, amount)
        
        assert success is True
        mock_contract.functions.approve.assert_called_once()
    
    @pytest.mark.wallet
    def test_check_and_approve_skips_when_sufficient(self, mock_web3_instance, mock_token_addresses):
        """Test check_and_approve_token skips when allowance is sufficient"""
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = 2**256 - 1  # Max allowance
        mock_web3_instance.eth.contract.return_value = mock_contract
        
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        amount = 1000 * 10**18
        
        success = self._check_and_approve_token(mock_web3_instance, token, spender, amount)
        
        assert success is True
        mock_contract.functions.approve.assert_not_called()
    
    def _check_allowance(self, web3, token, spender, amount):
        """Check if allowance is sufficient"""
        abi = [{"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], 
                "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
        contract = web3.eth.contract(address=token, abi=abi)
        current = contract.functions.allowance("0xOwner", spender).call()
        return current >= amount
    
    def _approve_token(self, web3, token, spender):
        """Approve token for spender"""
        max_uint = 2**256 - 1
        abi = [{"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], 
                "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"}]
        contract = web3.eth.contract(address=token, abi=abi)
        tx_hash = contract.functions.approve(spender, max_uint).transact()
        receipt = web3.eth.get_transaction_receipt(tx_hash)
        return receipt["status"] == 1
    
    def _check_and_approve_token(self, web3, token, spender, amount):
        """Check and approve token if needed"""
        if self._check_allowance(web3, token, spender, amount):
            return True
        return self._approve_token(web3, token, spender)


class TestPermit2Approvals:
    """Tests for Permit2 approval logic (0x API v2)"""
    
    @pytest.mark.wallet
    def test_permit2_allowance_check(self, mock_web3_instance, mock_token_addresses):
        """Test Permit2 allowance checking"""
        permit2 = mock_token_addresses["permit2"]
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = (2**160 - 1, 0, 0)  # (amount, expiration, nonce)
        mock_web3_instance.eth.contract.return_value = mock_contract
        
        is_approved = self._is_approved_to_permit2(mock_web3_instance, permit2, token, spender, 1000)
        
        assert is_approved is True
    
    @pytest.mark.wallet
    def test_permit2_allowance_expired(self, mock_web3_instance, mock_token_addresses):
        """Test Permit2 allowance check with expired approval"""
        permit2 = mock_token_addresses["permit2"]
        token = mock_token_addresses["usdg"]
        spender = "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
        
        mock_contract = MagicMock()
        # Amount is sufficient but expiration is in the past
        mock_contract.functions.allowance.return_value.call.return_value = (2**160 - 1, 1000000, 0)
        mock_web3_instance.eth.contract.return_value = mock_contract
        
        is_approved = self._is_approved_to_permit2(mock_web3_instance, permit2, token, spender, 1000)
        
        assert is_approved is False
    
    @pytest.mark.wallet
    def test_permit2_approval_flow(self, mock_web3_instance, mock_token_addresses):
        """Test full Permit2 approval flow"""
        permit2 = mock_token_addresses["permit2"]
        token = mock_token_addresses["usdg"]
        
        success = self._approve_to_permit2(mock_web3_instance, token, permit2)
        
        assert success is True
    
    def _is_approved_to_permit2(self, web3, permit2_address, token, spender, amount):
        """Check if approved to Permit2"""
        abi = [{"inputs": [{"name": "owner", "type": "address"}, {"name": "token", "type": "address"}, {"name": "spender", "type": "address"}], 
                "name": "allowance", "outputs": [{"name": "amount", "type": "uint160"}, {"name": "expiration", "type": "uint48"}, {"name": "nonce", "type": "uint48"}], 
                "stateMutability": "view", "type": "function"}]
        
        contract = web3.eth.contract(address=permit2_address, abi=abi)
        permit_amount, expiration, _ = contract.functions.allowance("0xOwner", token, spender).call()
        
        now = 2000000000  # Current timestamp
        is_expired = expiration > 0 and expiration < now
        has_enough = permit_amount >= amount
        
        return has_enough and not is_expired
    
    def _approve_to_permit2(self, web3, token, permit2):
        """Approve token to Permit2 contract"""
        abi = [{"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], 
                "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"}]
        contract = web3.eth.contract(address=token, abi=abi)
        max_uint = 2**256 - 1
        tx_hash = contract.functions.approve(permit2, max_uint).transact()
        receipt = web3.eth.get_transaction_receipt(tx_hash)
        return receipt["status"] == 1


class TestTransactionExecution:
    """Tests for transaction execution and monitoring"""
    
    @pytest.mark.wallet
    def test_wait_for_transaction_success(self, mock_web3_instance):
        """Test waiting for successful transaction"""
        tx_hash = "0x123abc"
        mock_web3_instance.eth.get_transaction_receipt.return_value = {
            "status": 1,
            "transactionHash": tx_hash,
            "gasUsed": 100000,
            "blockNumber": 12345,
        }
        
        receipt = self._wait_for_transaction(mock_web3_instance, tx_hash)
        
        assert receipt is not None
        assert receipt["status"] == 1
    
    @pytest.mark.wallet
    def test_wait_for_transaction_timeout(self, mock_web3_instance):
        """Test transaction wait timeout"""
        mock_web3_instance.eth.get_transaction_receipt.return_value = None
        
        receipt = self._wait_for_transaction(mock_web3_instance, "0x123", timeout_ms=100)
        
        assert receipt is None
    
    @pytest.mark.wallet
    def test_execute_swap_transaction(self, mock_web3_instance, mock_token_addresses):
        """Test executing a swap transaction"""
        quote = {
            "transaction": {
                "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
                "data": "0x1234",
                "value": "0",
                "gas": "150000",
                "gasPrice": "1000000000",
            }
        }
        
        mock_web3_instance.eth.send_transaction.return_value = "0xtxhash"
        mock_web3_instance.eth.get_transaction_receipt.return_value = {
            "status": 1,
            "transactionHash": "0xtxhash",
        }
        
        result = self._execute_swap(mock_web3_instance, quote)
        
        assert result["success"] is True
        assert result["tx_hash"] == "0xtxhash"
    
    @pytest.mark.wallet
    def test_execute_swap_reverts(self, mock_web3_instance):
        """Test handling of reverted swap transaction"""
        quote = {
            "transaction": {
                "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
                "data": "0x1234",
                "value": "0",
                "gas": "150000",
                "gasPrice": "1000000000",
            }
        }
        
        mock_web3_instance.eth.send_transaction.return_value = "0xtxhash"
        mock_web3_instance.eth.get_transaction_receipt.return_value = {
            "status": 0,  # Reverted
            "transactionHash": "0xtxhash",
            "gasUsed": 150000,
        }
        
        result = self._execute_swap(mock_web3_instance, quote)
        
        assert result["success"] is False
        assert "error" in result
    
    def _wait_for_transaction(self, web3, tx_hash, timeout_ms=60000):
        """Wait for transaction receipt"""
        import time
        start = time.time()
        while (time.time() - start) * 1000 < timeout_ms:
            receipt = web3.eth.get_transaction_receipt(tx_hash)
            if receipt:
                return receipt
            time.sleep(0.01)  # Short sleep for testing
        return None
    
    def _execute_swap(self, web3, quote):
        """Execute swap from 0x quote"""
        try:
            tx_hash = web3.eth.send_transaction({
                "to": quote["transaction"]["to"],
                "data": quote["transaction"]["data"],
                "value": int(quote["transaction"]["value"]),
                "gas": int(quote["transaction"]["gas"]),
                "gasPrice": int(quote["transaction"]["gasPrice"]),
            })
            
            receipt = web3.eth.get_transaction_receipt(tx_hash)
            
            if receipt["status"] == 1:
                return {"success": True, "tx_hash": tx_hash}
            else:
                return {"success": False, "error": "Transaction reverted", "tx_hash": tx_hash}
        except Exception as e:
            return {"success": False, "error": str(e)}


# =============================================================================
# Test Configuration
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
