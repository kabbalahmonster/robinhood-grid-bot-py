from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grid_bot import GridBot


def make_bot(balance_wei):
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(use_eth_trading=True, eth_gas_reserve=0.001)
    bot.wallet = MagicMock()
    bot.wallet.get_eth_balance_wei.return_value = balance_wei
    return bot


def test_native_buy_can_spend_reserved_eth_on_gas_when_gas_is_under_cap():
    bot = make_bot(balance_wei=1_000_000)
    quote = SimpleNamespace(value=900_000)

    with patch("grid_bot.logger.warning"):
        allowed = bot._final_buy_reserve_ok(
            quote, gas_limit=100, gas_price=1_000, weth_fallback=False
        )

    assert allowed is True


def test_native_buy_without_quote_value_uses_requested_principal_for_funding():
    bot = make_bot(balance_wei=100_000)
    quote = SimpleNamespace(value=0)

    with patch("grid_bot.logger.warning"):
        allowed = bot._final_buy_reserve_ok(
            quote, gas_limit=100, gas_price=1_000, weth_fallback=False,
            requested_principal_wei=900_000,
        )

    assert allowed is False


def test_weth_fallback_requires_funds_for_approval_before_wrapping():
    bot = make_bot(balance_wei=1_000_000)
    bot._projected_gas_cost_wei = MagicMock(return_value=100_000)
    quote = SimpleNamespace()

    with patch("grid_bot.logger.warning"):
        allowed = bot._weth_buy_fallback_funds_ok(
            quote, buy_amount_wei=700_001, wrap_gas_wei=100_000, approval_gas_wei=100_000
        )

    assert allowed is False


def test_weth_fallback_can_use_reserved_eth_for_gas_when_all_costs_fit():
    bot = make_bot(balance_wei=1_000_000)
    bot._projected_gas_cost_wei = MagicMock(return_value=100_000)
    quote = SimpleNamespace()

    with patch("grid_bot.logger.warning"):
        allowed = bot._weth_buy_fallback_funds_ok(
            quote, buy_amount_wei=800_000, wrap_gas_wei=100_000, approval_gas_wei=0
        )

    assert allowed is True


def test_native_buy_is_refused_when_principal_and_projected_gas_exceed_wallet_balance():
    bot = make_bot(balance_wei=999_999)
    quote = SimpleNamespace(value=900_000)

    with patch("grid_bot.logger.warning"):
        allowed = bot._final_buy_reserve_ok(
            quote, gas_limit=100, gas_price=1_000, weth_fallback=False
        )

    assert allowed is False
