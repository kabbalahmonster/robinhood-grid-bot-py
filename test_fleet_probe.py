import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROBE = Path(__file__).parent / "ops" / "fleet" / "probe-bot.py"


class TestFleetProbe(unittest.TestCase):
    def _checkout(self, directory, actual_chain=8453):
        checkout = Path(directory) / "bot"
        checkout.mkdir()
        (checkout / ".env").write_text("CHAIN_ID=8453\nPRIVATE_KEY=super-secret-value\n")
        os.chmod(checkout / ".env", 0o600)
        (checkout / "config.py").write_text(
            "from types import SimpleNamespace\n"
            "def load_config():\n"
            " return SimpleNamespace(bot_id='alpha', dashboard_name='Alpha', chain_id=8453, "
            "chain_name='Base', swap_fallback_provider='sushiswap', eth_gas_reserve=0.0005, "
            "token_symbol='TOKEN', token_address='0x'+'1'*40, usdg_address='0x'+'2'*40, "
            "weth_address='0x'+'3'*40, dashboard_url='', dashboard_api_key='')\n"
        )
        (checkout / "wallet.py").write_text(
            "from types import SimpleNamespace\n"
            f"class Eth:\n chain_id={actual_chain}\n def get_code(self,address): return b'x'\n"
            "class Wallet:\n"
            " def __init__(self,config): self.address='0x'+'a'*40; self.w3=SimpleNamespace(eth=Eth())\n"
            " def get_token_info(self,address): return SimpleNamespace(symbol='TOK',decimals=18)\n"
            " def get_token_balance(self,address): return ('1.25',1250000000000000000)\n"
            " def get_eth_balance_wei(self): return 2000000000000000\n"
        )
        (checkout / "swap_provider.py").write_text(
            "class FallbackSwapProvider: pass\n"
            "def resolve_provider_name(config): return 'fake'\n"
            "def create_swap_provider(config): raise AssertionError('quote disabled')\n"
        )
        return checkout

    def test_inventory_json_is_read_only_and_redacts_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = self._checkout(directory)
            result = subprocess.run(
                ["python3", str(PROBE), "--mode", "inventory", "--name", "alpha", "--json"],
                cwd=checkout, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["wallet"], "0x" + "a" * 40)
            self.assertEqual(report["chain_id"], 8453)
            self.assertEqual(len(report["assets"]), 3)
            self.assertNotIn("super-secret-value", result.stdout)
            self.assertEqual((checkout / ".env").read_text(), "CHAIN_ID=8453\nPRIVATE_KEY=super-secret-value\n")

    def test_rpc_chain_mismatch_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = self._checkout(directory, actual_chain=1)
            result = subprocess.run(
                ["python3", str(PROBE), "--mode", "doctor", "--no-quote", "--name", "alpha", "--json"],
                cwd=checkout, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "fail")
            check = next(item for item in report["checks"] if item["name"] == "rpc_chain")
            self.assertEqual(check["status"], "fail")


if __name__ == "__main__":
    unittest.main()
