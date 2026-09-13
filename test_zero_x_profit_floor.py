from types import SimpleNamespace
from unittest.mock import Mock, patch

from zero_x import ZeroXClient


def config():
    return SimpleNamespace(
        chain_id=4663,
        zero_x_api_key="key",
        zero_x_api_url="https://api.0x.org",
        zero_x_proxy="0x1111111111111111111111111111111111111111",
        anti_mev_jitter=False,
    )


@patch("zero_x.requests.get")
def test_slippage_fraction_is_sent_as_basis_points_and_minimum_is_exposed(get):
    response = Mock(status_code=200, text="")
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "buyAmount": "1900",
        "minBuyAmount": "1850",
        "sellAmount": "1000",
        "allowanceTarget": "0x2222222222222222222222222222222222222222",
        "transaction": {
            "to": "0x3333333333333333333333333333333333333333",
            "data": "0x12345678",
            "value": "0",
            "gas": "250000",
            "gasPrice": "100",
        },
    }
    get.return_value = response

    result = ZeroXClient(config()).get_quote(
        "0x4444444444444444444444444444444444444444",
        "0x5555555555555555555555555555555555555555",
        sell_amount=1000,
        taker_address="0x6666666666666666666666666666666666666666",
        slippage_percentage=0.01,
        apply_jitter_to_price=False,
    )

    assert result.success
    assert result.minimum_buy_amount == 1850
    assert get.call_args.kwargs["params"]["slippageBps"] == 100
