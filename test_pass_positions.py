import importlib.util
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "pass-positions.py"
SPEC = importlib.util.spec_from_file_location("pass_positions", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PassPositionsTests(unittest.TestCase):
    def bot(self, parent, name, capacity, filled=0, reserve="0.0015"):
        root = Path(parent) / name
        (root / "data").mkdir(parents=True)
        env = root / ".env"
        env.write_text(
            f"MAX_ACTIVE_POSITIONS={capacity}\n"
            f"TREASURY_POSITION_RESERVE_ETH={reserve}\n"
            "ETH_GAS_RESERVE=0.0006\n"
            "CHAIN_ID=4663\n"
            f"PRIVATE_KEY={name}-key\n"
        )
        env.chmod(0o600)
        (root / "data" / "fleet_status.json").write_text(
            json.dumps({"filled_positions": filled})
        )
        return root

    def test_fair_source_and_destination_splits_use_whole_units(self):
        sources = module.fair_allocate(
            [("sarn", None), ("prism", None)], 3,
            {"sarn": 5, "prism": 5}, "source",
        )
        destinations = module.fair_allocate(
            [("urmom", None), ("delta", None)], 3, label="destination"
        )
        self.assertEqual(sources, {"sarn": 2, "prism": 1})
        self.assertEqual(destinations, {"urmom": 2, "delta": 1})

    def test_fair_source_split_spills_from_capacity_limited_donor(self):
        result = module.fair_allocate(
            [("small", None), ("large", None)], 4,
            {"small": 1, "large": 4}, "source",
        )
        self.assertEqual(result, {"small": 1, "large": 3})

    def test_manual_counts_are_exact_and_remainder_is_fair(self):
        result = module.fair_allocate(
            [("fixed", 2), ("a", None), ("b", None)], 5,
            {"fixed": 10, "a": 10, "b": 10}, "source",
        )
        self.assertEqual(result, {"fixed": 2, "a": 2, "b": 1})

    def test_impossible_manual_or_total_allocation_is_refused(self):
        with self.assertRaisesRegex(ValueError, "can give at most"):
            module.fair_allocate([("prism", 3)], 3, {"prism": 2}, "source")
        with self.assertRaisesRegex(ValueError, "Not enough available"):
            module.fair_allocate(
                [("a", None), ("b", None)], 5, {"a": 2, "b": 2}, "source"
            )

    def test_infer_total_accepts_one_fully_manual_side_and_checks_both(self):
        self.assertEqual(
            module.infer_total(None, [("a", 1), ("b", 2)], [("c", None)]), 3
        )
        with self.assertRaisesRegex(ValueError, "totals differ"):
            module.infer_total(None, [("a", 2)], [("b", 3)])

    def test_routes_aggregate_into_at_most_sources_plus_destinations_minus_one(self):
        routes = module.build_routes(
            {"a": 2, "b": 1}, {"x": 1, "y": 2}
        )
        self.assertEqual(routes, [
            {"source": "a", "destination": "x", "positions": 1},
            {"source": "a", "destination": "y", "positions": 1},
            {"source": "b", "destination": "y", "positions": 1},
        ])

    def test_send_route_records_broadcast_hash_before_receipt_timeout(self):
        w3 = SimpleNamespace(eth=Mock())
        w3.eth.gas_price = 1_000_000_000
        w3.eth.get_block.return_value = {"baseFeePerGas": 1_000_000_000}
        w3.eth.get_transaction_count.return_value = 3
        tx_hash = Mock()
        tx_hash.hex.return_value = "0xambiguous"
        w3.eth.send_raw_transaction.return_value = tx_hash
        w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("still pending")
        account = Mock()
        account.address = "0x0000000000000000000000000000000000000001"
        account.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"signed")
        broadcasts = []
        with self.assertRaises(TimeoutError):
            module.send_route(
                {"w3": w3, "account": account},
                {
                    "recipient": "0x0000000000000000000000000000000000000002",
                    "amount_wei": 1000, "gas": 21_000,
                },
                4663,
                on_broadcast=lambda transaction_hash, tx: broadcasts.append((transaction_hash, tx)),
            )
        self.assertEqual(broadcasts[0][0], "0xambiguous")
        self.assertEqual(broadcasts[0][1]["nonce"], 3)

    def test_capacity_text_preserves_comment_and_legacy_setting(self):
        updated = module.replace_capacity_text(
            "MAX_POSITIONS=10\nMAX_ACTIVE_POSITIONS=7 # live cap\n", 5
        )
        self.assertEqual(
            updated, "MAX_POSITIONS=10\nMAX_ACTIVE_POSITIONS=5 # live cap\n"
        )
        added = module.replace_capacity_text("MAX_POSITIONS=10\n", 8)
        self.assertEqual(added, "MAX_POSITIONS=10\nMAX_ACTIVE_POSITIONS=8\n")

    def test_metadata_counts_available_slots_and_reads_default_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            env = root / ".env"
            env.write_text(
                "MAX_ACTIVE_POSITIONS=5\nTREASURY_POSITION_RESERVE_ETH=0.0015\n"
                "ETH_GAS_RESERVE=0.0006\nPRIVATE_KEY=test\n"
            )
            env.chmod(0o600)
            (root / "data" / "gridless_positions.json").write_text(json.dumps({
                "one": {"balance": 1}, "empty": {"balance": 0}
            }))
            data = module.bot_metadata("prism", root, require_key=True)
            self.assertEqual(data["filled"], 1)
            self.assertEqual(data["available"], 4)
            self.assertEqual(data["reserve_wei"], 1_500_000_000_000_000)

    def test_metadata_refuses_exposed_private_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            env = root / ".env"
            env.write_text("MAX_ACTIVE_POSITIONS=1\nPRIVATE_KEY=test\n")
            env.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                module.bot_metadata("prism", root, require_key=True)

    def test_dry_run_builds_reproducible_many_to_many_plan_with_defaults(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                suffix = sum(key.encode())
                return SimpleNamespace(address=f"0x{suffix:040x}")

        def fake_prepare(plan, _metadata, execute=False):
            for route in plan["routes"]:
                route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                route["max_fee_wei"] = 21_000
                route["recipient"] = plan["wallet_addresses"][route["destination"]]
            plan["feasibility"] = {
                name: {
                    "balance_wei": 10**18,
                    "principal_wei": sum(
                        route["amount_wei"] for route in plan["routes"]
                        if route["source"] == name
                    ),
                    "maximum_fees_wei": 21_000,
                    "final_available_slots": 1,
                    "retained_position_reserve_wei": 1_500_000_000_000_000,
                    "effective_gas_reserve_wei": 600_000_000_000_000,
                    "post_fee_gas_reserve_floor_wei": 599_999_999_979_000,
                    "required_wei": 4_500_000_000_021_000,
                    "projected_remaining_wei": 995_499_999_979_000_000,
                }
                for name in plan["sources"]
            }
            return {}

        with tempfile.TemporaryDirectory() as directory:
            sarn = self.bot(directory, "sarn", 5)
            prism = self.bot(directory, "prism", 4)
            urmom = self.bot(directory, "urmom", 3)
            args = [
                "--from", "sarn,prism", "--to", "urmom", "--positions", "3",
                "--journal-dir", str(Path(directory) / "journals"),
                "--bot", f"sarn={sarn}", "--bot", f"prism={prism}",
                "--bot", f"urmom={urmom}",
            ]
            output = StringIO()
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(output):
                self.assertEqual(module.main(args), 0)
            body = output.getvalue()
            self.assertIn("sarn: give 2", body)
            self.assertIn("prism: give 1", body)
            self.assertIn("urmom: receive 3", body)
            self.assertIn("amount/position=0.0015 ETH", body)
            self.assertRegex(body, r"Plan ID: [0-9a-f]{16}")
            self.assertIn("POSITION PASS ALLOCATION PREVIEW", body)
            self.assertIn("POSITION PASS APPROVAL PLAN", body)
            self.assertIn("wallet balance: 1 ETH", body)
            self.assertIn("Total principal:", body)
            self.assertIn("Approval status: FEASIBLE", body)
            self.assertIn("DRY RUN COMPLETE", body)

    def test_main_refuses_same_bot_on_both_sides_before_chain_access(self):
        with tempfile.TemporaryDirectory() as directory:
            prism = self.bot(directory, "prism", 4)
            with self.assertRaisesRegex(ValueError, "both donors and recipients"):
                module.main([
                    "--from", "prism", "--to", "PRISM", "--positions", "1",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"prism={prism}",
                ])

    def test_main_refuses_amount_below_recipient_position_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            donor = self.bot(directory, "donor", 4, reserve="0.001")
            recipient = self.bot(directory, "recipient", 4, reserve="0.002")
            with self.assertRaisesRegex(ValueError, "below recipient's"):
                module.main([
                    "--from", "donor", "--to", "recipient", "--positions", "1",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"donor={donor}", "--bot", f"recipient={recipient}",
                ])

    def test_execute_journals_transfer_then_commits_both_capacities(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

        def fake_prepare(plan, _metadata, execute=False):
            for route in plan["routes"]:
                route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                route["max_fee_wei"] = 21_000
            return {name: {} for name in plan["sources"]}

        with tempfile.TemporaryDirectory() as directory:
            prism = self.bot(directory, "prism", 5, filled=2)
            urmom = self.bot(directory, "urmom", 4, filled=1)
            journal_dir = Path(directory) / "journals"
            base = [
                "--from", "prism", "--to", "urmom", "--positions", "2",
                "--journal-dir", str(journal_dir),
                "--bot", f"prism={prism}", "--bot", f"urmom={urmom}",
            ]
            output = StringIO()
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(output):
                module.main(base)
            plan_id = re.search(r"Plan ID: ([0-9a-f]{16})", output.getvalue()).group(1)
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    patch.object(module, "send_route", return_value=("0xconfirmed", {"gas": 21_000, "gasPrice": 1})), \
                    redirect_stdout(StringIO()):
                self.assertEqual(module.main(base + ["--execute", "--confirm-plan", plan_id]), 0)
            self.assertEqual(module.capacity_value(prism / ".env"), 3)
            self.assertEqual(module.capacity_value(urmom / ".env"), 6)
            journal = json.loads((journal_dir / f"{plan_id}.json").read_text())
            self.assertEqual(journal["status"], "complete")
            self.assertEqual(journal["plan"]["routes"][0]["tx_hash"], "0xconfirmed")
            self.assertTrue((prism / f".env.bak.position-pass.{plan_id}").exists())

            # Simulate a crash during the multi-file commit: one file reverted,
            # one already updated. Resume must finish locally without RPC access.
            prism_env = prism / ".env"
            prism_env.write_text(module.replace_capacity_text(prism_env.read_text(), 5))
            prism_env.chmod(0o600)
            journal["status"] = "committing"
            module.atomic_json(journal_dir / f"{plan_id}.json", journal)
            with patch.object(module, "prepare_chain", side_effect=AssertionError("RPC must not run")), \
                    redirect_stdout(StringIO()):
                module.main([
                    "--resume", plan_id, "--execute", "--confirm-plan", plan_id,
                    "--journal-dir", str(journal_dir),
                    "--bot", f"prism={prism}", "--bot", f"urmom={urmom}",
                ])
            self.assertEqual(module.capacity_value(prism_env), 3)

    def test_interrupted_execution_resumes_without_repeating_confirmed_route(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

        def fake_prepare(plan, _metadata, execute=False):
            for route in plan["routes"]:
                route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                route["max_fee_wei"] = 21_000
            return {name: {} for name in plan["sources"]}

        with tempfile.TemporaryDirectory() as directory:
            prism = self.bot(directory, "prism", 5)
            urmom = self.bot(directory, "urmom", 2)
            delta = self.bot(directory, "delta", 2)
            journal_dir = Path(directory) / "journals"
            mappings = [
                "--bot", f"prism={prism}", "--bot", f"urmom={urmom}",
                "--bot", f"delta={delta}", "--journal-dir", str(journal_dir),
            ]
            base = ["--from", "prism", "--to", "urmom,delta", "--positions", "2", *mappings]
            output = StringIO()
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(output):
                module.main(base)
            plan_id = re.search(r"Plan ID: ([0-9a-f]{16})", output.getvalue()).group(1)
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    patch.object(module, "send_route", side_effect=[
                        ("0xfirst", {"gas": 21_000, "gasPrice": 1}), RuntimeError("rpc down")
                    ]), redirect_stdout(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "resume safely"):
                    module.main(base + ["--execute", "--confirm-plan", plan_id])
            self.assertEqual(module.capacity_value(prism / ".env"), 5)
            interrupted = json.loads((journal_dir / f"{plan_id}.json").read_text())
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertEqual(interrupted["plan"]["routes"][0]["tx_hash"], "0xfirst")
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    patch.object(module, "send_route", return_value=("0xsecond", {"gas": 21_000, "gasPrice": 1})) as sender, \
                    redirect_stdout(StringIO()):
                module.main([
                    "--resume", plan_id, "--execute", "--confirm-plan", plan_id, *mappings
                ])
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(module.capacity_value(prism / ".env"), 3)
            self.assertEqual(module.capacity_value(urmom / ".env"), 3)
            self.assertEqual(module.capacity_value(delta / ".env"), 3)


if __name__ == "__main__":
    unittest.main()
