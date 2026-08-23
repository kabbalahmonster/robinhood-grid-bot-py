import json
import subprocess
import tempfile
import unittest
from pathlib import Path


AUDIT = Path(__file__).parent / "ops" / "fleet" / "audit-fleet.py"


class TestFleetAudit(unittest.TestCase):
    def test_reports_failed_and_incomplete_runs_and_emits_name(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "bot"
            data = checkout / "data"
            data.mkdir(parents=True)
            (checkout / ".env").write_text("CHAIN_ID=8453\n")
            (data / "treasury_transfers.json").write_text(json.dumps([
                {"timestamp": "2026-01-01", "success": False, "token": "ETH", "amount": "1", "error": "no gas"}
            ]))
            (data / "asset_liquidations.json").write_text(json.dumps([
                {"timestamp": "2026-01-02", "run_id": "run-one", "event": "asset_result",
                 "asset": {"label": "TOKEN", "success": True, "tx_hash": "0x" + "a" * 64}}
            ]))
            result = subprocess.run(
                ["python3", str(AUDIT), "--json", "--bot", f"alpha={checkout}"],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)[0]
            self.assertEqual(report["status"], "attention")
            self.assertIn("failed treasury transfer", report["attention"])
            self.assertIn("incomplete liquidation run run-one", report["attention"])
            asset_event = next(event for event in report["events"] if event["kind"] == "liquidation_asset")
            self.assertTrue(asset_event["explorer"].startswith("https://basescan.org/tx/"))

            emitted = subprocess.run(
                ["python3", str(AUDIT), "--emit-only", "--bot", f"alpha={checkout}"],
                text=True, capture_output=True,
            )
            self.assertEqual(emitted.returncode, 0)
            self.assertEqual(emitted.stdout.strip(), "alpha")

    def test_successful_complete_liquidation_is_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "bot"
            data = checkout / "data"
            data.mkdir(parents=True)
            (checkout / ".env").write_text("CHAIN_ID=4663\n")
            (data / "asset_liquidations.json").write_text(json.dumps([
                {"timestamp": "2026-01-01", "run_id": "done", "event": "complete",
                 "success": True, "positions_cleared": True}
            ]))
            result = subprocess.run(
                ["python3", str(AUDIT), "--json", "--bot", f"alpha={checkout}"],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)[0]["status"], "ok")

    def test_malformed_existing_log_requires_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "bot"
            data = checkout / "data"
            data.mkdir(parents=True)
            (checkout / ".env").write_text("CHAIN_ID=1\n")
            (data / "treasury_transfers.json").write_text("not-json")
            result = subprocess.run(
                ["python3", str(AUDIT), "--json", "--bot", f"alpha={checkout}"],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot read audit log", json.loads(result.stdout)[0]["attention"][0])


if __name__ == "__main__":
    unittest.main()
