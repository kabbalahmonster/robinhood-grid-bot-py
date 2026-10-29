from types import SimpleNamespace
from unittest.mock import Mock, patch

from abyss_api import AbyssAPIClient


TOKEN = "0x15f3385625D7e364C5a6216FBbceadf10fa90e7d"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
TAKER = "0x0000000000000000000000000000000000000001"
NATIVE = "0x0000000000000000000000000000000000000000"


def config(chain_id=4663):
    return SimpleNamespace(
        chain_id=chain_id, weth_address=WETH,
        abyss_api_url="https://api.abyss.trading",
        rpc_urls=["https://rpc.example"], rpc_url="",
        anti_mev_jitter=False,
    )


def route():
    key = (
        WETH, TOKEN, 3, 10_000, True,
        bytes.fromhex("c0e9bed88d70a13fd3ab31451fefdd073b7266e838aee0ad1c236c8c9eff855d"),
    )
    return [(key, TOKEN, 1461446703485210103287273052203988822378723970341)]


def candidate_body(amount):
    return {
        "chainId": 4663, "amountIn": str(amount), "candidates": [[{
            "pool": "0xdec74a2afabd34aa6f268c5161d2e9e210ca7185",
            "key": {
                "token0": WETH.lower(), "token1": TOKEN.lower(),
                "profile": 3, "fee": 10_000, "quoteIsToken0": True,
                "oracleConfigId": "0xc0e9bed88d70a13fd3ab31451fefdd073b7266e838aee0ad1c236c8c9eff855d",
            },
            "tokenIn": TOKEN.lower(), "tokenOut": WETH.lower(),
            "sqrtPriceLimitX96": "1461446703485210103287273052203988822378723970341",
        }]],
    }


def test_rejects_non_robinhood_chain():
    try:
        AbyssAPIClient(config(8453))
    except ValueError as exc:
        assert "4663 only" in str(exc)
    else:
        raise AssertionError("Abyss must remain Robinhood-only")


def test_candidate_response_is_validated_and_encoded():
    amount = 10**18
    response = Mock(status_code=200, content=b"{}")
    response.json.return_value = candidate_body(amount)
    client = AbyssAPIClient(config())

    with patch("abyss_api.requests.get", return_value=response):
        paths = client._candidate_paths(TOKEN, WETH, amount, 1)

    assert len(paths) == 1
    assert paths[0][0][0][3] == 10_000
    assert paths[0][0][1] == TOKEN


def test_native_exit_builds_pinned_router_transaction():
    amount = 10**18
    client = AbyssAPIClient(config())
    client._candidate_paths = Mock(return_value=[route()])
    client._quote_paths = Mock(return_value=(2 * 10**15, route()))

    quote = client.build_swap_transaction(
        TOKEN, NATIVE, amount, TAKER, slippage_percentage=0.01,
        quote_timeout_seconds=2,
    )

    assert quote.success is True
    assert quote.to == client.ROUTER
    assert quote.allowance_target == client.ROUTER
    assert quote.value == 0
    assert quote.buy_amount == 2 * 10**15
    assert quote.raw_response["amountOutMinimum"] == 1_980_000_000_000_000
    assert quote.data.startswith("0x") and len(quote.data) > 10


def test_native_entry_sets_exact_input_value():
    amount = 10**15
    reverse = [(route()[0][0], WETH, 4295128740)]
    client = AbyssAPIClient(config())
    client._candidate_paths = Mock(return_value=[reverse])
    client._quote_paths = Mock(return_value=(10**18, reverse))

    quote = client.build_swap_transaction(NATIVE, TOKEN, amount, TAKER, 0.02, 2)

    assert quote.success is True
    assert quote.value == amount
    assert quote.raw_response["amountOutMinimum"] == 980_000_000_000_000_000
