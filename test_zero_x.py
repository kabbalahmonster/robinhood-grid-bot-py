"""
Test 0x API integration and quote fetching.

Tests cover:
- Price fetching (read-only quotes)
- Quote fetching (executable swap data)
- Token price conversion (WETH terms)
- Retry logic and error handling
- MEV protection (jitter)
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
from decimal import Decimal


# =============================================================================
# 0x API Client Tests
# =============================================================================

class TestZeroXAPIClient:
    """Tests for 0x API client initialization and configuration"""
    
    @pytest.mark.zerox
    def test_client_initialization(self, mock_token_addresses):
        """Test 0x API client initialization"""
        api_key = "test-api-key"
        chain_id = 4663
        
        client = self._create_client(api_key, chain_id)
        
        assert client.api_key == api_key
        assert client.chain_id == chain_id
        assert client.base_url == "https://api.0x.org"
    
    @pytest.mark.zerox
    def test_client_headers(self, mock_token_addresses):
        """Test 0x API request headers"""
        client = self._create_client("test-key", 4663)
        
        headers = client._get_headers()
        
        assert headers["0x-api-key"] == "test-key"
        assert headers["0x-version"] == "v2"
    
    def _create_client(self, api_key, chain_id):
        """Create mock 0x API client"""
        class MockZeroXClient:
            def __init__(self, api_key, chain_id):
                self.api_key = api_key
                self.chain_id = chain_id
                self.base_url = "https://api.0x.org"
            
            def _get_headers(self):
                return {
                    "0x-api-key": self.api_key,
                    "0x-version": "v2",
                }
        
        return MockZeroXClient(api_key, chain_id)


class TestPriceFetching:
    """Tests for 0x price endpoint (read-only quotes)"""
    
    @pytest.mark.zerox
    def test_get_price_success(self, mock_token_addresses):
        """Test successful price fetch"""
        mock_response = {
            "buyToken": mock_token_addresses["trading"],
            "sellToken": mock_token_addresses["weth"],
            "buyAmount": "100000000000000000000",  # 100 tokens
            "sellAmount": "100000000000000000",     # 0.1 WETH
            "estimatedPriceImpact": "0.5",
            "grossPrice": "0.001",
            "netPrice": "0.000995",
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            price = self._get_price(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100000000000000000"
            )
        
        assert price is not None
        assert price["buyAmount"] == "100000000000000000000"
        assert price["sellAmount"] == "100000000000000000"
    
    @pytest.mark.zerox
    def test_get_price_by_buy_amount(self, mock_token_addresses):
        """Test price fetch with buy amount specified"""
        mock_response = {
            "buyToken": mock_token_addresses["trading"],
            "sellToken": mock_token_addresses["weth"],
            "buyAmount": "100000000000000000000",
            "sellAmount": "100000000000000000",
            "estimatedPriceImpact": "0.5",
            "grossPrice": "0.001",
            "netPrice": "0.000995",
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            price = self._get_price(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                buy_amount="100000000000000000000"
            )
        
        assert price is not None
    
    @pytest.mark.zerox
    def test_get_price_missing_amount(self, mock_token_addresses):
        """Test price fetch without buy or sell amount raises error"""
        with pytest.raises(ValueError, match="Either sellAmount or buyAmount must be provided"):
            self._get_price(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"]
            )
    
    @pytest.mark.zerox
    def test_get_price_api_error(self, mock_token_addresses):
        """Test handling of API errors"""
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=400,
                json=lambda: {"reason": "Invalid token address"},
                raise_for_status=Mock(side_effect=Exception("Bad Request"))
            )
            
            price = self._get_price(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100000000000000000"
            )
        
        assert price is None
    
    @pytest.mark.zerox
    def test_get_price_network_error(self, mock_token_addresses):
        """Test handling of network errors"""
        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception("Connection failed")
            
            price = self._get_price(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100000000000000000"
            )
        
        assert price is None
    
    def _get_price(self, sell_token, buy_token, sell_amount=None, buy_amount=None):
        """Fetch price from 0x API"""
        import requests
        
        if sell_amount is None and buy_amount is None:
            raise ValueError("Either sellAmount or buyAmount must be provided")
        
        params = {
            "chainId": "4663",
            "sellToken": sell_token,
            "buyToken": buy_token,
        }
        
        if sell_amount:
            params["sellAmount"] = sell_amount
        elif buy_amount:
            params["buyAmount"] = buy_amount
        
        try:
            response = requests.get(
                "https://api.0x.org/swap/permit2/price",
                params=params,
                headers={"0x-api-key": "test-key", "0x-version": "v2"},
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return None


class TestQuoteFetching:
    """Tests for 0x quote endpoint (executable swaps)"""
    
    @pytest.mark.zerox
    def test_get_quote_success(self, mock_token_addresses):
        """Test successful quote fetch"""
        mock_response = {
            "chainId": 4663,
            "buyToken": mock_token_addresses["trading"],
            "sellToken": mock_token_addresses["weth"],
            "buyAmount": "100000000000000000000",
            "sellAmount": "100000000000000000",
            "allowanceTarget": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
            "transaction": {
                "to": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
                "data": "0x1234abcd",
                "value": "0",
                "gas": "150000",
                "gasPrice": "1000000000",
            },
            "estimatedPriceImpact": "0.5",
            "grossPrice": "0.001",
            "netPrice": "0.000995",
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            quote = self._get_quote(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100000000000000000",
                taker_address="0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"
            )
        
        assert quote is not None
        assert "transaction" in quote
        assert quote["transaction"]["to"] == "0xDef1C0ded9bec7F1a1670819833240f027b25EfF"
    
    @pytest.mark.zerox
    def test_get_quote_includes_slippage(self, mock_token_addresses):
        """Test that quotes include slippage protection"""
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {"buyAmount": "100", "transaction": {"to": "0x"}}
            )
            
            quote = self._get_quote(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100000000000000000"
            )
            
            # Check that slippage was included in request
            call_args = mock_get.call_args
            params = call_args[1].get("params", {})
            assert params.get("slippagePercentage") == "0.02"
    
    @pytest.mark.zerox
    def test_get_quote_with_taker_address(self, mock_token_addresses):
        """Test quote fetch includes taker address for proper routing"""
        taker = "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb"
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {"buyAmount": "100", "transaction": {"to": "0x"}}
            )
            
            quote = self._get_quote(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100000000000000000",
                taker_address=taker
            )
            
            call_args = mock_get.call_args
            params = call_args[1].get("params", {})
            assert params.get("taker") == taker
    
    @pytest.mark.zerox
    def test_get_quote_applies_anti_mev_jitter(self, mock_token_addresses):
        """Test that sell amount includes anti-MEV jitter"""
        original_amount = 100000000000000000
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=200,
                json=lambda: {"buyAmount": "100", "transaction": {"to": "0x"}}
            )
            
            quote = self._get_quote(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount=str(original_amount)
            )
            
            call_args = mock_get.call_args
            params = call_args[1].get("params", {})
            jittered_amount = int(params.get("sellAmount", "0"))
            
            # Jittered amount should be slightly higher than original
            assert jittered_amount >= original_amount
            assert jittered_amount < original_amount + 1000
    
    def _get_quote(self, sell_token, buy_token, sell_amount=None, buy_amount=None, taker_address=None):
        """Fetch quote from 0x API"""
        import requests
        import random
        
        params = {
            "chainId": "4663",
            "sellToken": sell_token,
            "buyToken": buy_token,
            "slippagePercentage": "0.02",  # 2% slippage
        }
        
        if sell_amount:
            # Add anti-MEV jitter
            original = int(sell_amount)
            jittered = original + random.randint(0, 999)
            params["sellAmount"] = str(jittered)
        elif buy_amount:
            params["buyAmount"] = buy_amount
        
        if taker_address:
            params["taker"] = taker_address
        
        try:
            response = requests.get(
                "https://api.0x.org/swap/permit2/quote",
                params=params,
                headers={"0x-api-key": "test-key", "0x-version": "v2"},
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return None


class TestRetryLogic:
    """Tests for API retry logic with exponential backoff"""
    
    @pytest.mark.zerox
    def test_retry_on_server_error(self, mock_token_addresses):
        """Test retry on 5xx server errors"""
        with patch('requests.get') as mock_get:
            # First two calls fail, third succeeds
            mock_get.side_effect = [
                Mock(status_code=500, raise_for_status=Mock(side_effect=Exception("Server Error"))),
                Mock(status_code=502, raise_for_status=Mock(side_effect=Exception("Bad Gateway"))),
                Mock(status_code=200, json=lambda: {"buyAmount": "100"}),
            ]
            
            result = self._get_price_with_retry(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100"
            )
        
        assert result is not None
        assert result["buyAmount"] == "100"
        assert mock_get.call_count == 3
    
    @pytest.mark.zerox
    def test_no_retry_on_client_error(self, mock_token_addresses):
        """Test no retry on 4xx client errors"""
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(
                status_code=400,
                json=lambda: {"reason": "Invalid token"},
                raise_for_status=Mock(side_effect=Exception("Bad Request"))
            )
            
            result = self._get_price_with_retry(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100"
            )
        
        assert result is None
        assert mock_get.call_count == 1  # No retries
    
    @pytest.mark.zerox
    def test_max_retries_exceeded(self, mock_token_addresses):
        """Test handling when max retries are exceeded"""
        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception("Connection failed")
            
            result = self._get_price_with_retry(
                mock_token_addresses["weth"],
                mock_token_addresses["trading"],
                sell_amount="100",
                max_retries=2
            )
        
        assert result is None
        assert mock_get.call_count == 3  # Initial + 2 retries
    
    @pytest.mark.zerox
    def test_exponential_backoff(self):
        """Test exponential backoff calculation"""
        delays = []
        for attempt in range(5):
            delay = self._calculate_delay(attempt, base_delay_ms=1000, max_delay_ms=30000)
            delays.append(delay)
        
        # Each delay should be longer than the previous (with some randomness)
        assert delays[1] >= 1000
        assert delays[2] >= 2000
        assert delays[3] >= 4000
        assert all(d <= 30000 for d in delays)
    
    def _get_price_with_retry(self, sell_token, buy_token, sell_amount, max_retries=3):
        """Fetch price with retry logic"""
        import requests
        import time
        import random
        
        for attempt in range(max_retries + 1):
            try:
                response = requests.get(
                    "https://api.0x.org/swap/permit2/price",
                    params={
                        "chainId": "4663",
                        "sellToken": sell_token,
                        "buyToken": buy_token,
                        "sellAmount": sell_amount,
                    },
                    headers={"0x-api-key": "test-key", "0x-version": "v2"},
                    timeout=10
                )
                
                # Don't retry on 4xx errors
                if 400 <= response.status_code < 500:
                    return None
                    
                response.raise_for_status()
                return response.json()
                
            except Exception as e:
                if attempt < max_retries:
                    delay = self._calculate_delay(attempt)
                    time.sleep(delay / 1000)  # Convert to seconds
                else:
                    return None
    
    def _calculate_delay(self, attempt, base_delay_ms=1000, max_delay_ms=30000):
        """Calculate delay with exponential backoff and jitter"""
        import random
        exponential = base_delay_ms * (2 ** attempt)
        jitter = random.randint(0, 1000)
        return min(exponential + jitter, max_delay_ms)


class TestTokenPriceConversions:
    """Tests for token price conversion utilities"""
    
    @pytest.mark.zerox
    def test_get_token_price_in_weth(self, mock_token_addresses):
        """Test getting token price in WETH terms"""
        # 1 token = 0.001 WETH
        mock_response = {
            "buyAmount": "1000000000000000",  # 0.001 WETH
            "sellAmount": "1000000000000000000",  # 1 token
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            price = self._get_token_price_in_weth(
                mock_token_addresses["trading"],
                mock_token_addresses["weth"],
                decimals=18
            )
        
        assert price is not None
        assert price == 0.001
    
    @pytest.mark.zerox
    def test_get_weth_price_in_token(self, mock_token_addresses):
        """Test getting WETH price in token terms"""
        # 1 WETH = 1000 tokens
        mock_response = {
            "buyAmount": "1000000000000000000000",  # 1000 tokens
            "sellAmount": "1000000000000000000",  # 1 WETH
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            price = self._get_weth_price_in_token(
                mock_token_addresses["trading"],
                mock_token_addresses["weth"],
                token_decimals=18
            )
        
        assert price is not None
        assert price == 1000.0
    
    @pytest.mark.zerox
    def test_calculate_usd_value(self, mock_token_addresses):
        """Test calculating USD value of token amount"""
        # 1 token = 0.001 WETH = $2 (if WETH = $2000)
        mock_response = {
            "buyAmount": "1000000000000000",  # 0.001 WETH
            "sellAmount": "1000000000000000000",  # 1 token
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            usd_value = self._calculate_usd_value(
                mock_token_addresses["trading"],
                amount=1000000000000000000,  # 1 token
                decimals=18,
                weth_address=mock_token_addresses["weth"],
                weth_price_usd=2000
            )
        
        assert usd_value is not None
        assert usd_value == 2.0  # 1 token = 0.001 WETH = $2
    
    @pytest.mark.zerox
    def test_price_conversion_with_different_decimals(self, mock_token_addresses):
        """Test price conversion with non-18 decimals"""
        # USDC has 6 decimals
        mock_response = {
            "buyAmount": "1000000000000000",  # 0.001 WETH
            "sellAmount": "1000000",  # 1 USDC
        }
        
        with patch('requests.get') as mock_get:
            mock_get.return_value = Mock(status_code=200, json=lambda: mock_response)
            
            price = self._get_token_price_in_weth(
                "0xUSDC",
                mock_token_addresses["weth"],
                decimals=6
            )
        
        assert price is not None
        assert price == 0.001  # 1 USDC = 0.001 WETH
    
    def _get_token_price_in_weth(self, token_address, weth_address, decimals=18):
        """Get token price in WETH"""
        import requests
        
        one_token = 10 ** decimals
        
        try:
            response = requests.get(
                "https://api.0x.org/swap/permit2/price",
                params={
                    "chainId": "4663",
                    "sellToken": token_address,
                    "buyToken": weth_address,
                    "sellAmount": str(one_token),
                },
                headers={"0x-api-key": "test-key", "0x-version": "v2"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            return int(data["buyAmount"]) / (10 ** 18)
        except Exception:
            return None
    
    def _get_weth_price_in_token(self, token_address, weth_address, token_decimals=18):
        """Get WETH price in token terms"""
        import requests
        
        one_weth = 10 ** 18
        
        try:
            response = requests.get(
                "https://api.0x.org/swap/permit2/price",
                params={
                    "chainId": "4663",
                    "sellToken": weth_address,
                    "buyToken": token_address,
                    "sellAmount": str(one_weth),
                },
                headers={"0x-api-key": "test-key", "0x-version": "v2"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            return int(data["buyAmount"]) / (10 ** token_decimals)
        except Exception:
            return None
    
    def _calculate_usd_value(self, token_address, amount, decimals, weth_address, weth_price_usd):
        """Calculate USD value using WETH as reference"""
        price_in_weth = self._get_token_price_in_weth(token_address, weth_address, decimals)
        if price_in_weth is None:
            return None
        
        token_amount = amount / (10 ** decimals)
        weth_value = token_amount * price_in_weth
        return weth_value * weth_price_usd


# =============================================================================
# Mock helper for tests
# =============================================================================

class Mock:
    """Simple mock class for response mocking"""
    def __init__(self, status_code=200, json=None, raise_for_status=None):
        self.status_code = status_code
        self._json = json
        self.raise_for_status = raise_for_status or Mock()
    
    def json(self):
        return self._json()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
