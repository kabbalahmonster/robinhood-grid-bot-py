from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from grid_bot import GridBot


def make_bot(tmp_path):
    bot = GridBot.__new__(GridBot)
    bot.config = SimpleNamespace(
        use_eth_trading=True,
        weth_address="0x" + "11" * 20,
        token_address="0x" + "22" * 20,
        eth_gas_reserve=0.001,
    )
    bot.wallet = Mock()
    bot.wallet.address = "0x" + "33" * 20
    bot.wallet.unresolved_broadcast_path = str(tmp_path / "guard.json")
    bot.wallet.unresolved_broadcast = None
    bot.api_client = Mock()
    bot._swap_slippage_fraction = Mock(return_value=0.05)
    return bot


def quote(success, error=None):
    return SimpleNamespace(success=success, error=error, buy_amount=900, sell_amount=1000)


def test_buy_falls_back_from_native_to_weth(tmp_path):
    bot = make_bot(tmp_path)
    bot.api_client.build_swap_transaction.side_effect = [quote(False, "NoRoute"), quote(True)]
    result, uses_weth = bot._actionable_quote_with_weth_fallback(
        sell_token="0x" + "00" * 20,
        buy_token=bot.config.token_address,
        sell_amount=1000,
        direction="buy",
    )
    assert result.success and uses_weth
    assert bot.api_client.build_swap_transaction.call_args_list[1].kwargs["sell_token"] == bot.config.weth_address


def test_sell_falls_back_from_native_output_to_weth(tmp_path):
    bot = make_bot(tmp_path)
    bot.api_client.build_swap_transaction.side_effect = [quote(False, "NoRoute"), quote(True)]
    result, uses_weth = bot._actionable_quote_with_weth_fallback(
        sell_token=bot.config.token_address,
        buy_token="0x" + "00" * 20,
        sell_amount=1000,
        direction="sell",
    )
    assert result.success and uses_weth
    assert bot.api_client.build_swap_transaction.call_args_list[1].kwargs["buy_token"] == bot.config.weth_address


def test_successful_weth_route_cancels_pending_provider_replay(tmp_path):
    bot = make_bot(tmp_path)
    bot.provider = Mock()
    bot.api_client.build_swap_transaction.side_effect = [quote(False, "NoRoute"), quote(True)]

    result, uses_weth = bot._actionable_quote_with_weth_fallback(
        sell_token=bot.config.token_address,
        buy_token="0x" + "00" * 20,
        sell_amount=1000,
        direction="sell",
    )

    assert result.success and uses_weth
    bot.provider.recover_current_operation.assert_called_once_with()


def test_no_weth_fallback_in_weth_mode(tmp_path):
    bot = make_bot(tmp_path)
    bot.config.use_eth_trading = False
    bot.api_client.build_swap_transaction.return_value = quote(False, "NoRoute")
    result, uses_weth = bot._actionable_quote_with_weth_fallback(
        sell_token=bot.config.weth_address,
        buy_token=bot.config.token_address,
        sell_amount=1000,
        direction="buy",
    )
    assert not result.success and not uses_weth
    assert bot.api_client.build_swap_transaction.call_count == 1


def test_confirmed_wrap_guard_is_durable_and_clearable(tmp_path):
    bot = make_bot(tmp_path)
    bot.wallet._record_unresolved_broadcast = lambda tx_hash, tx, error: (
        Path(bot.wallet.unresolved_broadcast_path).write_text(error),
        setattr(bot.wallet, "unresolved_broadcast", {"tx_hash": tx_hash}),
    )
    bot._guard_confirmed_wrap(SimpleNamespace(tx_hash="0xabc"), 123)
    assert Path(bot.wallet.unresolved_broadcast_path).exists()
    assert bot.wallet.unresolved_broadcast
    bot._clear_settlement_guard()
    assert not Path(bot.wallet.unresolved_broadcast_path).exists()
    assert bot.wallet.unresolved_broadcast is None


def test_unwrap_requires_reserve_and_returns_confirmed_gas(tmp_path):
    bot = make_bot(tmp_path)
    tx = {"gas": 50_000, "gasPrice": 2, "to": bot.config.weth_address}
    bot._project_weth_operation_gas = Mock(return_value=(tx, 100_000))
    bot.wallet.get_eth_balance_wei.return_value = 10**18
    result = SimpleNamespace(success=True, tx_hash="0xdef", gas_used=40_000, effective_gas_price=2)
    bot.wallet.unwrap_weth.return_value = result
    bot._receipt_gas_cost_wei = Mock(return_value=80_000)
    actual, gas = bot._execute_weth_unwrap(999)
    assert actual is result and gas == 80_000
