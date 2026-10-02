import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPORTER = Path(__file__).parent / "ops" / "fleet" / "balance-report.py"
SPEC = importlib.util.spec_from_file_location("balance_report", REPORTER)
balance_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(balance_report)


def probe(name, eth_wei, usdg_raw, token_raw, *, status="pass", legacy=False):
    assets = [
        {
            "label": "TOK", "symbol": "TOK", "address": "0x" + "1" * 40,
            "decimals": 18, "balance": str(token_raw / 10**18), "balance_raw": str(token_raw),
        },
        {
            "label": "USDG", "symbol": "USDG", "address": "0x" + "2" * 40,
            "decimals": 6, "balance": str(usdg_raw / 10**6), "balance_raw": str(usdg_raw),
        },
    ]
    if not legacy:
        assets[0]["role"] = "managed"
        assets[1]["role"] = "usdg"
    return {
        "name": name,
        "status": status,
        "chain_id": 8453,
        "chain_name": "Base",
        "wallet": "0x" + "a" * 40,
        "native_eth": str(eth_wei / 10**18),
        "native_eth_wei": str(eth_wei),
        "assets": assets,
        "checks": [],
    }


class TestBalanceReport(unittest.TestCase):
    def test_aggregates_native_usdg_and_managed_tokens(self):
        report = balance_report.build_report([
            probe("one", 10**18, 1_500_000, 2 * 10**18),
            probe("two", 2 * 10**18, 2_500_000, 3 * 10**18),
        ])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["totals"]["native_eth"][0]["balance"], "3")
        self.assertEqual(report["totals"]["usdg"][0]["balance"], "4")
        self.assertEqual(report["totals"]["managed_tokens"][0]["balance"], "5")
        rendered = balance_report.render_human(report)
        self.assertIn("one [PASS] | ETH=1 | USDG=1.5 | TOK=2", rendered)
        self.assertIn("Fleet totals:", rendered)

    def test_accepts_legacy_probe_assets_without_roles(self):
        report = balance_report.build_report([
            probe("old", 10**18, 1_000_000, 10**18, legacy=True),
        ])
        self.assertEqual(report["bots"][0]["usdg"]["symbol"], "USDG")
        self.assertEqual(report["bots"][0]["managed_token"]["symbol"], "TOK")

    def test_one_asset_can_fill_managed_and_usdg_roles(self):
        item = probe("same", 10**18, 1_000_000, 10**18)
        shared = item["assets"][0]
        shared["roles"] = ["managed", "usdg"]
        item["assets"] = [shared]
        report = balance_report.build_report([item])
        self.assertEqual(report["bots"][0]["managed_token"]["address"], shared["address"])
        self.assertEqual(report["bots"][0]["usdg"]["address"], shared["address"])

    def test_json_cli_returns_failure_but_keeps_partial_report(self):
        good = probe("good", 10**18, 1_000_000, 10**18)
        bad = {
            "name": "bad", "status": "fail", "checks": [
                {"name": "runtime", "status": "fail", "detail": "RPC unavailable"}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, value in enumerate((good, bad)):
                path = Path(directory) / f"{index}.json"
                path.write_text(json.dumps(value))
                paths.append(str(path))
            result = subprocess.run(
                ["python3", str(REPORTER), "--json", *paths],
                text=True, capture_output=True,
            )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(len(payload["bots"]), 2)
        self.assertEqual(payload["bots"][1]["errors"][0]["detail"], "RPC unavailable")


if __name__ == "__main__":
    unittest.main()
