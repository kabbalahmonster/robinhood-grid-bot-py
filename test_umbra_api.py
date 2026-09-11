from types import SimpleNamespace
from unittest.mock import Mock, patch

from umbra_api import UmbraAPIClient

def config(chain_id=4663):
    return SimpleNamespace(chain_id=chain_id, anti_mev_jitter=False)

def response(status, payload):
    item = Mock(status_code=status)
    item.json.return_value = payload
    return item

QUOTE = {"amountIn": "1000", "expectedOut": "2000", "netOut": "1900",
         "verification": {"feeOnTransfer": False, "taxBps": 0}}

@patch("umbra_api.requests.post")
def test_quote_uses_fee_adjusted_net_output(post):
    post.return_value = response(200, QUOTE)
    result = UmbraAPIClient(config()).get_quote("0xin", "0xout", sell_amount=1000)
    assert result.success and result.buy_amount == 1900
    assert result.allowance_target == UmbraAPIClient.ROUTER

@patch("umbra_api.requests.post")
def test_quote_without_net_output_is_rejected(post):
    post.return_value = response(200, {**QUOTE, "netOut": None})
    assert not UmbraAPIClient(config()).get_quote("0xin", "0xout", sell_amount=1000).success

@patch("umbra_api.requests.post")
def test_build_pins_router_and_ignores_flat_provider_gas(post):
    build = {"router": UmbraAPIClient.ROUTER, "calldata": "0x12345678", "value": "1000",
             "minOut": "1800", "expectedOut": "1900", "gas": "3000000"}
    post.side_effect = [response(200, QUOTE), response(200, build)]
    result = UmbraAPIClient(config()).build_swap_transaction(
        UmbraAPIClient.NATIVE, "0xout", 1000, "0xrecipient")
    assert result.success and result.to == UmbraAPIClient.ROUTER and result.gas is None

@patch("umbra_api.requests.post")
def test_build_rejects_router_substitution(post):
    post.return_value = response(200, {"router": "0xevil", "calldata": "0x12345678",
                                      "value": "0", "minOut": "1", "expectedOut": "1"})
    result = UmbraAPIClient(config()).get_swap_transaction(
        SimpleNamespace(success=True), sell_token="0xin", buy_token="0xout",
        sell_amount=1000, taker_address="0xrecipient")
    assert not result.success and "untrusted router" in result.error

def test_wrong_chain_is_rejected():
    try:
        UmbraAPIClient(config(1))
    except ValueError as exc:
        assert "4663" in str(exc)
    else:
        raise AssertionError("wrong-chain Umbra client was accepted")
