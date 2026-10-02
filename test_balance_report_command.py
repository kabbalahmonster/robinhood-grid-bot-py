import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "balance-report"


class TestBalanceReportCommand(unittest.TestCase):
    def make_bot(self, root, name):
        checkout = root / name / "robinhood-grid-bot-py"
        checkout.mkdir(parents=True)
        (checkout / "grid_bot.py").write_text("pass\n")
        (checkout / ".env").write_text("PRIVATE_KEY=never-print-this\n")
        os.chmod(checkout / ".env", 0o600)
        (checkout / "config.py").write_text(
            "from types import SimpleNamespace\n"
            "def load_config():\n"
            f" return SimpleNamespace(bot_id='{name}', dashboard_name='{name}', chain_id=8453, "
            "chain_name='Base', swap_fallback_provider='', eth_gas_reserve=0.0005, "
            "token_symbol='TOK', token_address='0x'+'1'*40, usdg_address='0x'+'2'*40, "
            "weth_address='0x'+'3'*40, dashboard_url='', dashboard_api_key='')\n"
        )
        (checkout / "wallet.py").write_text(
            "from types import SimpleNamespace\n"
            "class Eth:\n chain_id=8453\n def get_code(self,address): return b'x'\n"
            "class Wallet:\n"
            " def __init__(self,config): self.address='0x'+'a'*40; self.w3=SimpleNamespace(eth=Eth())\n"
            " def get_token_info(self,address):\n"
            "  return SimpleNamespace(symbol='TOK' if address == '0x'+'1'*40 else ('USDG' if address == '0x'+'2'*40 else 'WETH'), decimals=6 if address == '0x'+'2'*40 else 18)\n"
            " def get_token_balance(self,address):\n"
            "  return ('1.5',1500000) if address == '0x'+'2'*40 else ('2.5',2500000000000000000)\n"
            " def get_eth_balance_wei(self): return 2000000000000000\n"
        )
        (checkout / "swap_provider.py").write_text(
            "class FallbackSwapProvider: pass\n"
            "def resolve_provider_name(config): return 'fake'\n"
            "def create_swap_provider(config): raise AssertionError('unused')\n"
        )
        return checkout

    def test_reports_whole_fleet_and_supports_json_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha = self.make_bot(root, "ALPHA")
            beta = self.make_bot(root, "BETA")
            config = root / "fleet.conf"
            config.write_text(f'FLEET_BOT_DIRS=("{alpha}" "{beta}")\n')
            env = {**os.environ, "HOME": str(root)}

            human = subprocess.run(
                ["bash", str(SCRIPT), "--config", str(config)],
                cwd=root, env=env, text=True, capture_output=True,
            )
            selected = subprocess.run(
                ["bash", str(SCRIPT), "--config", str(config), "--only", "ALPHA", "--json"],
                cwd=root, env=env, text=True, capture_output=True,
            )

        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("ALPHA [WARN] | ETH=0.002 | USDG=1.5 | TOK=2.5", human.stdout)
        self.assertIn("BETA [WARN] | ETH=0.002 | USDG=1.5 | TOK=2.5", human.stdout)
        self.assertIn("Base (8453) ETH=0.004", human.stdout)
        self.assertIn("USDG USDG=3", human.stdout)
        self.assertIn("managed TOK=5", human.stdout)
        self.assertNotIn("never-print-this", human.stdout + human.stderr)

        self.assertEqual(selected.returncode, 0, selected.stderr)
        payload = json.loads(selected.stdout)
        self.assertEqual([bot["name"] for bot in payload["bots"]], ["ALPHA"])
        self.assertEqual(payload["totals"]["native_eth"][0]["balance"], "0.002")


if __name__ == "__main__":
    unittest.main()
