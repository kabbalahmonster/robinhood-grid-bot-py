import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import CHAIN_CONFIG, load_config
from grid_bot import GridBot
from gridless import calculate_pnl, get_buy_price
from rpc_rotator import CHAIN_DEFAULT_RPCS


ARC_ENV = {
    "PRIVATE_KEY": "0x" + "1" * 64,
    "RPC_URL": "https://rpc.mainnet.arc.io",
    "CHAIN_ID": "5042",
    "TOKEN_ADDRESS": "0x" + "2" * 40,
    "SWAP_PROVIDER": "lifi",
    "SWAP_FALLBACK_PROVIDER": "",
    "LI_FI_API_KEY": "test-key",
    "USE_UNISWAP_API": "false",
    "USE_ETH_TRADING": "false",
    "ROUTE_TOURNAMENT_MODE": "off",
}


class ArcSupportTests(unittest.TestCase):
    def bot(self):
        bot = GridBot.__new__(GridBot)
        bot.config = SimpleNamespace(
            settlement_decimals=6,
            native_decimals=18,
            settlement_shares_native_balance=True,
            use_eth_trading=False,
            weth_address=CHAIN_CONFIG[5042]["weth"],
            eth_gas_reserve=1,
        )
        bot.trade_token_unit = 10**6
        bot.native_token_unit = 10**18
        return bot

    def test_arc_chain_metadata_uses_usdc_interface_and_public_rpcs(self):
        arc = CHAIN_CONFIG[5042]
        self.assertEqual(arc["settlement_symbol"], "USDC")
        self.assertEqual(arc["settlement_decimals"], 6)
        self.assertFalse(arc["supports_wrapped_native"])
        self.assertIn("https://rpc.mainnet.arc.io", CHAIN_DEFAULT_RPCS[5042])

    def test_arc_config_is_fail_closed_to_lifi_without_fallback(self):
        with patch("config.load_dotenv"), patch.dict(os.environ, ARC_ENV, clear=True):
            config = load_config()
        self.assertEqual(config.chain_name, "Arc")
        self.assertEqual(config.settlement_symbol, "USDC")
        self.assertEqual(config.settlement_decimals, 6)
        self.assertFalse(config.supports_wrapped_native)

    def test_arc_rejects_unverified_provider(self):
        env = {**ARC_ENV, "SWAP_PROVIDER": "sushiswap"}
        with patch("config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "requires SWAP_PROVIDER=lifi"):
                load_config()

    def test_native_gas_converts_to_six_decimal_settlement_units(self):
        bot = self.bot()
        self.assertEqual(bot._gas_wei_to_trade_units(17_000_000_000_000_000), 17_000)
        quote = SimpleNamespace()
        bot._projected_gas_cost_wei = Mock(return_value=17_000_000_000_000_000)
        self.assertEqual(
            bot._minimum_gas_aware_return_wei(1_000_000, quote, 5),
            1_067_000,
        )

    def test_shared_usdc_balance_preserves_native_gas_reserve(self):
        bot = self.bot()
        bot.wallet = SimpleNamespace(
            get_token_balance=Mock(return_value=(10.0, 10_000_000)),
            get_eth_balance_wei=Mock(return_value=10 * 10**18),
        )
        self.assertEqual(bot._available_trade_balance(), 9.0)
        self.assertFalse(
            bot._final_buy_reserve_ok(
                SimpleNamespace(value=0),
                gas_limit=1,
                gas_price=1_500_000_000_000_000_000,
                weth_fallback=False,
                requested_principal_wei=8_000_000,
            )
        )

    def test_gridless_pnl_supports_six_decimal_cost_basis(self):
        position = {"cost_wei": 2_000_000, "balance": 100 * 10**18}
        self.assertAlmostEqual(get_buy_price(position, 18, 6), 0.02)
        self.assertAlmostEqual(calculate_pnl(position, 0.022, 18, 6), 10.0)


if __name__ == "__main__":
    unittest.main()
