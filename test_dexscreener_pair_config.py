import os
import unittest
from unittest.mock import patch

from config import load_config


VALID_ENV = {
    "PRIVATE_KEY": "0x" + "1" * 64,
    "RPC_URL": "https://rpc.example.invalid",
    "CHAIN_ID": "4663",
    "TOKEN_ADDRESS": "0x" + "2" * 40,
    "UNISWAP_API_KEY": "test-key",
}


class DexscreenerPairConfigTests(unittest.TestCase):
    def test_pair_override_defaults_blank_for_legacy_env(self):
        with patch.dict(os.environ, VALID_ENV, clear=True):
            self.assertEqual(load_config().dexscreener_pair_address, "")

    def test_pair_override_accepts_evm_pair_address(self):
        pair = "0x" + "9" * 40
        with patch.dict(
            os.environ,
            {**VALID_ENV, "DEXSCREENER_PAIR_ADDRESS": pair},
            clear=True,
        ):
            self.assertEqual(load_config().dexscreener_pair_address, pair)

    def test_pair_override_rejects_invalid_address(self):
        with patch.dict(
            os.environ,
            {**VALID_ENV, "DEXSCREENER_PAIR_ADDRESS": "token/weth"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "valid EVM pair address"):
                load_config()


if __name__ == "__main__":
    unittest.main()
