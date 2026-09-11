from types import SimpleNamespace
from unittest.mock import Mock, patch

from li_fi import LiFiClient


def config():
    return SimpleNamespace(chain_id=4663, li_fi_api_key="key", anti_mev_jitter=False)


def response(payload):
    item = Mock(status_code=200, text="")
    item.json.return_value = payload
    item.raise_for_status.return_value = None
    return item


def payload(**tx_overrides):
    tx = {"to": "0x1111111111111111111111111111111111111111",
          "data": "0x12345678", "value": "0", "gasLimit": "250000", "chainId": 4663}
    tx.update(tx_overrides)
    return {"action": {"fromAmount": "1000"},
            "estimate": {"toAmount": "1900", "approvalAddress": tx["to"]},
            "transactionRequest": tx}


@patch("li_fi.requests.get")
def test_tournament_timeout_and_executable_fields(get):
    get.return_value = response(payload())
    result = LiFiClient(config()).get_quote(
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
        sell_amount=1000, taker_address="0x4444444444444444444444444444444444444444",
        quote_timeout_seconds=2.5, apply_jitter_to_price=False)
    assert result.success and result.buy_amount == 1900 and result.gas == 250000
    assert get.call_args.kwargs["timeout"] == 2.5


@patch("li_fi.requests.get")
def test_wrong_chain_fails_closed(get):
    get.return_value = response(payload(chainId=1))
    result = LiFiClient(config()).get_quote(
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
        sell_amount=1000, taker_address="0x4444444444444444444444444444444444444444")
    assert not result.success and "wrong execution chain" in result.error


@patch("li_fi.requests.get")
def test_native_value_mismatch_fails_closed(get):
    get.return_value = response(payload(value="0"))
    result = LiFiClient(config()).get_quote(
        "0x0000000000000000000000000000000000000000",
        "0x3333333333333333333333333333333333333333",
        sell_amount=1000, taker_address="0x4444444444444444444444444444444444444444")
    assert not result.success and "unexpected native value" in result.error
