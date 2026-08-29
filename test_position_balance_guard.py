import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grid_bot import GridBot


class PositionBalanceGuardTests(unittest.TestCase):
    def bot(self, wallet_balance):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(token_address="token")
        bot.wallet = MagicMock()
        bot.wallet.get_token_balance.return_value = (wallet_balance / 1e18, wallet_balance)
        bot._sell_attempt = None
        return bot

    def test_allows_sell_covered_by_wallet_balance(self):
        bot = self.bot(1_000)

        self.assertTrue(bot._wallet_can_cover_sell(1_000, "1"))
        self.assertIsNone(bot._sell_attempt)

    def test_blocks_tracked_position_larger_than_wallet_balance(self):
        bot = self.bot(400)

        with patch("grid_bot.logger.error") as error:
            self.assertFalse(bot._wallet_can_cover_sell(1_000, "1"))

        self.assertEqual(bot._sell_attempt, {
            "status": "position_balance_mismatch",
            "position_id": "1",
            "tracked_sell_amount_raw": 1_000,
            "wallet_balance_raw": 400,
            "deficit_raw": 600,
        })
        self.assertIn("POSITION BALANCE MISMATCH", error.call_args.args[0])

    def test_blocks_sell_when_wallet_balance_read_fails(self):
        bot = self.bot(0)
        bot.wallet.get_token_balance.side_effect = RuntimeError("RPC unavailable")

        with patch("grid_bot.logger.error"):
            self.assertFalse(bot._wallet_can_cover_sell(1_000, "1"))


if __name__ == "__main__":
    unittest.main()
