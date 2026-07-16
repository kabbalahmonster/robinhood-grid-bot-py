"""
Pytest configuration and shared fixtures for grid bot tests.
"""
import pytest
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from decimal import Decimal
from unittest.mock import MagicMock


@dataclass
class MockPosition:
    """Mock position for testing"""
    id: str
    balance: float
    cost_weth: float
    cost: float
    buy_min: float
    buy_max: float
    sell_min: float
    stoploss: float
    token_address: str = "0x1234567890123456789012345678901234567890"
    symbol: str = "TEST"
    created_at: Optional[int] = None
    last_buy_at: Optional[int] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = 1700000000000


@dataclass
class MockTokenBalance:
    """Mock token balance for testing"""
    address: str
    symbol: str
    balance: int
    decimals: int
    formatted_balance: str


@dataclass
class MockZeroXQuote:
    """Mock 0x quote for testing"""
    chain_id: int
    buy_token: str
    sell_token: str
    buy_amount: str
    sell_amount: str
    allowance_target: str
    transaction: Dict[str, str]
    estimated_price_impact: str
    gross_price: str
    net_price: str


@dataclass  
class MockZeroXPrice:
    """Mock 0x price for testing"""
    buy_token: str
    sell_token: str
    buy_amount: str
    sell_amount: str
    estimated_price_impact: str
    gross_price: str
    net_price: str


@pytest.fixture
def mock_wallet_address():
    """Sample wallet address for testing"""
    return "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"


@pytest.fixture
def mock_private_key():
    """Sample private key for testing (not a real key)"""
    return "0x" + "11" * 32


@pytest.fixture
def mock_token_addresses():
    """Sample token addresses for testing"""
    return {
        "usdg": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "trading": "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
    }


@pytest.fixture
def sample_positions():
    """Sample positions for testing"""
    return {
        "1": MockPosition(
            id="1",
            balance=100.0,
            cost_weth=0.001,
            cost=2.0,
            buy_min=0.0009,
            buy_max=0.0011,
            sell_min=0.00105,
            stoploss=0.0008,
        ),
        "2": MockPosition(
            id="2",
            balance=0,
            cost_weth=0,
            cost=0,
            buy_min=0.0008,
            buy_max=0.0009,
            sell_min=0.000945,
            stoploss=0.0007,
        ),
        "3": MockPosition(
            id="3",
            balance=50.0,
            cost_weth=0.00085,
            cost=1.7,
            buy_min=0.00075,
            buy_max=0.00085,
            sell_min=0.0008925,
            stoploss=0.00065,
        ),
    }


@pytest.fixture
def empty_positions():
    """Empty positions for testing grid generation"""
    return {}


@pytest.fixture
def mock_env_vars():
    """Mock environment variables for configuration testing"""
    return {
        "PRIVATE_KEY": "0x" + "22" * 32,
        "ZEROX_API_KEY": "test-api-key-12345",
        "RPC_URL": "https://rpc.robinhoodchain.com",
        "CHAIN_ID": "4663",
        "USDG_ADDRESS": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "WETH_ADDRESS": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "TRADING_TOKEN_ADDRESS": "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12",
        "TRADING_TOKEN_SYMBOL": "TEST",
        "GRID_SIZE_USD": "10",
        "MAX_POSITIONS": "20",
        "GRID_SPACING_PERCENT": "5",
        "PROFIT_THRESHOLD_PERCENT": "5",
        "STOPLOSS_PERCENTAGE": "-10",
        "MOONBAG_PERCENTAGE": "10",
        "CHECK_INTERVAL_MS": "10000",
        "BUY_COOLDOWN_MS": "30000",
        "GAS_RESERVE_ETH": "0.001",
        "BANK_MIN_AMOUNT": "1.0",
        "MIN_PROFIT": "1.05",
        "GRID_MODE": "dynamic",
        "BUY_AMOUNT_MODE": "dynamic",
        "BANK_PROFIT": "true",
        "SELLS_ACTIVE": "true",
        "BUYS_ACTIVE": "true",
        "BANK_MOONBAG": "true",
        "STOPLOSS_ACTIVE": "true",
    }


@pytest.fixture
def mock_zerox_quote(mock_token_addresses):
    """Sample 0x quote for testing"""
    return MockZeroXQuote(
        chain_id=4663,
        buy_token=mock_token_addresses["trading"],
        sell_token=mock_token_addresses["weth"],
        buy_amount="100000000000000000000",  # 100 tokens
        sell_amount="100000000000000000",     # 0.1 WETH
        allowance_target="0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
        transaction={
            "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
            "data": "0x1234abcd",
            "value": "0",
            "gas": "150000",
            "gasPrice": "1000000000",
        },
        estimated_price_impact="0.5",
        gross_price="0.001",
        net_price="0.000995",
    )


@pytest.fixture
def mock_zerox_price(mock_token_addresses):
    """Sample 0x price for testing"""
    return MockZeroXPrice(
        buy_token=mock_token_addresses["trading"],
        sell_token=mock_token_addresses["weth"],
        buy_amount="100000000000000000000",
        sell_amount="100000000000000000",
        estimated_price_impact="0.5",
        gross_price="0.001",
        net_price="0.000995",
    )


@pytest.fixture
def mock_rpc_response():
    """Mock RPC response for testing"""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": "0x0000000000000000000000000000000000000000000000000000000000000000"
    }


@pytest.fixture
def mock_token_balances(mock_token_addresses):
    """Sample token balances for testing"""
    return {
        "weth": MockTokenBalance(
            address=mock_token_addresses["weth"],
            symbol="WETH",
            balance=10**18,  # 1 WETH
            decimals=18,
            formatted_balance="1.0",
        ),
        "usdg": MockTokenBalance(
            address=mock_token_addresses["usdg"],
            symbol="USDG",
            balance=1000 * 10**18,  # 1000 USDG
            decimals=18,
            formatted_balance="1000.0",
        ),
        "trading": MockTokenBalance(
            address=mock_token_addresses["trading"],
            symbol="TEST",
            balance=500 * 10**18,  # 500 tokens
            decimals=18,
            formatted_balance="500.0",
        ),
    }


@pytest.fixture
def grid_config():
    """Grid configuration for testing"""
    return {
        "grid_size_usd": 10,
        "max_positions": 20,
        "grid_spacing_percent": 5,
        "profit_threshold_percent": 5,
        "stoploss_percentage": -10,
        "moonbag_percentage": 10,
        "min_profit": 1.05,
        "gas_reserve_eth": 0.001,
    }


@pytest.fixture
def mock_web3_instance(mock_wallet_address, mock_token_balances, mock_token_addresses):
    """Mock Web3 instance for testing"""
    web3 = MagicMock()
    web3.eth.get_balance.return_value = 10**18  # 1 ETH
    web3.is_connected.return_value = True
    web3.eth.chain_id = 4663
    
    # Mock contract
    mock_contract = MagicMock()
    mock_contract.functions.balanceOf.return_value.call.return_value = 1000 * 10**18
    mock_contract.functions.decimals.return_value.call.return_value = 18
    mock_contract.functions.symbol.return_value.call.return_value = "TEST"
    mock_contract.functions.allowance.return_value.call.return_value = 2**256 - 1  # Max approval
    mock_contract.functions.approve.return_value.transact.return_value = "0xtxhash"
    
    web3.eth.contract.return_value = mock_contract
    web3.eth.get_transaction_receipt.return_value = {
        "status": 1,
        "transactionHash": "0xreceipt",
        "gasUsed": 100000,
    }
    
    return web3


@pytest.fixture
def mock_axios_response():
    """Mock axios/requests response for API testing"""
    class MockResponse:
        def __init__(self, data, status_code=200):
            self.data = data
            self.status_code = status_code
            
        def json(self):
            return self.data
    
    return MockResponse


# Pytest markers for test categorization
def pytest_configure(config):
    """Configure pytest markers"""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow running tests")
    config.addinivalue_line("markers", "wallet: Wallet-related tests")
    config.addinivalue_line("markers", "zerox: 0x API tests")
    config.addinivalue_line("markers", "bot: Bot logic tests")
    config.addinivalue_line("markers", "config: Configuration tests")
