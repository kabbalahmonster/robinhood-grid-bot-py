import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from grid_bot import GridBot


class BankingGasReserveTests(unittest.TestCase):
    def make_bot(self, balance_wei):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            use_eth_trading=True,
            usdg_address="0x1111111111111111111111111111111111111111",
            bank_min_amount=0.5,
            gas_limit_multiplier=1.0,
            gas_price_multiplier=1.0,
            eth_gas_reserve=0.0002,
            chain_id=4663,
        )
        bot.trade_token_address = "0x0000000000000000000000000000000000000000"
        bot.trade_token_name = "ETH"
        bot.wallet = Mock()
        bot.wallet.address = "0x2222222222222222222222222222222222222222"
        bot.wallet.get_eth_balance_wei.return_value = balance_wei
        bot.wallet.w3.eth.get_transaction_count.return_value = 1
        bot.provider = Mock()
        bot.provider.capabilities.quote_requires_preparation = False
        bot.provider.build_swap_transaction.return_value = SimpleNamespace(
            success=True,
            buy_amount=1_000_000,
            gas=100_000,
            gas_price=1_000_000_000,
            value=500_000_000_000_000,
            to="0x3333333333333333333333333333333333333333",
            data="0x",
        )
        return bot

    def test_native_banking_skips_when_it_would_breach_reserve(self):
        bot = self.make_bot(balance_wei=700_000_000_000_000)

        GridBot.bank_profit.__wrapped__(bot, 0.0005)

        bot.wallet._send_transaction.assert_not_called()

    def test_native_banking_executes_when_reserve_and_gas_are_covered(self):
        bot = self.make_bot(balance_wei=1_000_000_000_000_000)
        bot.wallet._send_transaction.return_value = SimpleNamespace(success=False, error="test", tx_hash=None)

        GridBot.bank_profit.__wrapped__(bot, 0.0005)

        bot.wallet._send_transaction.assert_called_once()

    def test_banking_skips_when_amount_plus_gas_exceeds_net_profit_budget(self):
        bot = self.make_bot(balance_wei=2_000_000_000_000_000)

        GridBot.bank_profit.__wrapped__(bot, 0.0004, profit_budget_eth=0.00045)

        # 0.0004 principal + 0.0001 projected gas would consume principal.
        bot.wallet._send_transaction.assert_not_called()

    def test_banking_executes_when_amount_plus_gas_fits_net_profit_budget(self):
        bot = self.make_bot(balance_wei=2_000_000_000_000_000)
        bot.wallet._send_transaction.return_value = SimpleNamespace(success=False, error="test", tx_hash=None)

        GridBot.bank_profit.__wrapped__(bot, 0.0004, profit_budget_eth=0.0006)

        bot.wallet._send_transaction.assert_called_once()


if __name__ == "__main__":
    unittest.main()
