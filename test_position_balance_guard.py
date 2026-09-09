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

    def test_halts_when_pre_sell_balance_cannot_be_snapshotted(self):
        bot = self.bot(0)
        bot.running = True
        bot.wallet.unresolved_broadcast = None
        bot.wallet.get_token_balance.side_effect = RuntimeError("RPC unavailable")

        with patch("grid_bot.logger.critical"):
            balance = bot._snapshot_sell_token_balance_or_halt("7", {"nonce": 42})

        self.assertIsNone(balance)
        self.assertTrue(bot.running)
        self.assertTrue(bot._safety_halted)
        bot.wallet._record_unresolved_broadcast.assert_called_once()

    def test_halts_and_journals_when_failed_sell_reduces_token_balance(self):
        bot = self.bot(6_726)
        bot.running = True
        bot.wallet.unresolved_broadcast = None
        tx = {"nonce": 42, "to": "router", "value": 0}

        with patch("grid_bot.logger.critical"):
            detected = bot._halt_on_unexpected_sell_balance_delta(
                balance_before=10_656, sell_amount=3_930, position_id="7", tx=tx,
                tx_hash="0xknown",
            )

        self.assertTrue(detected)
        self.assertTrue(bot.running)
        self.assertTrue(bot._safety_halted)
        bot.wallet._record_unresolved_broadcast.assert_called_once()
        record = bot.wallet._record_unresolved_broadcast.call_args
        self.assertEqual(record.args[0], "0xknown")
        self.assertEqual(record.args[1], tx)
        self.assertIn("position 7", record.args[2])

    def test_does_not_halt_when_failed_sell_leaves_balance_unchanged(self):
        bot = self.bot(10_656)
        bot.running = True
        bot.wallet.unresolved_broadcast = None

        detected = bot._halt_on_unexpected_sell_balance_delta(
            balance_before=10_656, sell_amount=3_930, position_id="7", tx={"nonce": 42}
        )

        self.assertFalse(detected)
        self.assertTrue(bot.running)
        bot.wallet._record_unresolved_broadcast.assert_not_called()

    def test_preserves_existing_broadcast_journal(self):
        bot = self.bot(6_726)
        bot.running = True
        bot.wallet.unresolved_broadcast = {"tx_hash": "0xactual"}

        with patch("grid_bot.logger.critical"):
            bot._halt_on_unexpected_sell_balance_delta(
                balance_before=10_656, sell_amount=3_930, position_id="7",
                tx={"nonce": 42}, tx_hash="0xknown",
            )

        bot.wallet._record_unresolved_broadcast.assert_not_called()
        self.assertEqual(bot._sell_attempt["tx_hash"], "0xactual")


if __name__ == "__main__":
    unittest.main()
