import importlib.util
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "pass-positions.py"
SPEC = importlib.util.spec_from_file_location("pass_positions", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PassPositionsTests(unittest.TestCase):
    def bot(self, parent, name, capacity, filled=0, reserve="0.0015", gas_cap=None):
        root = Path(parent) / name
        (root / "data").mkdir(parents=True)
        env = root / ".env"
        gas_cap_line = (
            f"MAX_FEE_TRANSFER_GAS_ETH={gas_cap}\n" if gas_cap is not None else ""
        )
        env.write_text(
            f"MAX_ACTIVE_POSITIONS={capacity}\n"
            f"TREASURY_POSITION_RESERVE_ETH={reserve}\n"
            "ETH_GAS_RESERVE=0.0006\n"
            f"{gas_cap_line}"
            "RPC_URL=http://example.invalid\n"
            "CHAIN_ID=4663\n"
            f"PRIVATE_KEY={name}-key\n"
        )
        env.chmod(0o600)
        (root / "data" / "fleet_status.json").write_text(
            json.dumps({"filled_positions": filled})
        )
        return root

    def treasury(self, parent):
        path = Path(parent) / "treasury.env"
        path.write_text(
            "PRIVATE_KEY=treasury-key\n"
            "RPC_URL=http://example.invalid\n"
            "CHAIN_ID=4663\n"
            "ETH_GAS_RESERVE=0.0005\n"
            "TREASURY_POSITION_RESERVE_ETH=0.0015\n"
            "MAX_FEE_TRANSFER_GAS_ETH=0.0001\n"
        )
        path.chmod(0o600)
        return path

    def test_treasury_metadata_warns_but_accepts_broad_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.treasury(directory)
            path.chmod(0o664)
            warnings = StringIO()
            with redirect_stderr(warnings):
                data = module.treasury_metadata(path)
            self.assertEqual(data["private_key"], "treasury-key")
            self.assertIn("Treasury: .env permissions are broader", warnings.getvalue())

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

    def test_donors_are_drained_to_even_remaining_availability(self):
        specs = [("a", None), ("b", None)]
        capacities = {"a": 2, "b": 5}
        self.assertEqual(
            module.fair_allocate(specs, 3, capacities, "source"),
            {"a": 0, "b": 3},
        )
        self.assertEqual(
            module.fair_allocate(specs, 4, capacities, "source"),
            {"a": 1, "b": 3},
        )
        self.assertEqual(
            module.fair_allocate(specs, 5, capacities, "source"),
            {"a": 1, "b": 4},
        )

    def test_recipients_are_filled_to_even_open_availability(self):
        result = module.fair_allocate(
            [("c", None), ("d", None)], 3,
            initial_availability={"c": 1, "d": 0},
            label="destination",
        )
        self.assertEqual(result, {"c": 1, "d": 2})

    def test_explicit_counts_are_preserved_before_balancing_remainder(self):
        donors = module.fair_allocate(
            [("fixed", 2), ("a", None), ("b", None)], 5,
            {"fixed": 2, "a": 2, "b": 5}, "source",
        )
        recipients = module.fair_allocate(
            [("fixed", 2), ("c", None), ("d", None)], 5,
            initial_availability={"fixed": 9, "c": 1, "d": 0},
            label="destination",
        )
        self.assertEqual(donors, {"fixed": 2, "a": 0, "b": 3})
        self.assertEqual(recipients, {"fixed": 2, "c": 1, "d": 2})

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

    def test_positions_accepts_all_and_available_aliases(self):
        self.assertEqual(module.parse_positions("all"), "available")
        self.assertEqual(module.parse_positions("AVAILABLE"), "available")
        self.assertEqual(module.parse_positions("3"), 3)
        with self.assertRaisesRegex(ValueError, "positive integer, all, or available"):
            module.parse_positions("everything")

    def test_maximum_source_total_preserves_exact_overrides(self):
        self.assertEqual(
            module.maximum_source_total(
                [("fixed", 2), ("flexible", None)],
                {"fixed": 5, "flexible": 4},
            ),
            6,
        )

    def test_from_all_expands_fleet_in_order_and_excludes_recipients(self):
        bots = {"alpha": "a", "beta": "b", "gamma": "c"}
        self.assertEqual(
            module.resolve_source_specs(
                ["all"], bots, [("beta", None)]
            ),
            [("alpha", None), ("gamma", None)],
        )
        with self.assertRaisesRegex(ValueError, "must be used alone"):
            module.resolve_source_specs(
                ["all,alpha"], bots, [("beta", None)]
            )

    def test_routes_aggregate_into_at_most_sources_plus_destinations_minus_one(self):
        routes = module.build_routes(
            {"a": 2, "b": 1}, {"x": 1, "y": 2}
        )
        self.assertEqual(routes, [
            {"source": "a", "destination": "x", "positions": 1},
            {"source": "a", "destination": "y", "positions": 1},
            {"source": "b", "destination": "y", "positions": 1},
        ])

    def test_treasury_affordability_preserves_reserve_and_route_gas(self):
        self.assertEqual(
            module.treasury_affordable_positions(
                6,
                1_500_000_000_000_000,
                7_000_000_000_000_000,
                500_000_000_000_000,
                100_000_000_000_000,
                {"x": 3, "y": 3},
            ),
            4,
        )

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
                    "gas_cap_wei": 10**18,
                },
                4663,
                on_broadcast=lambda transaction_hash, tx: broadcasts.append((transaction_hash, tx)),
            )
        self.assertEqual(broadcasts[0][0], "0xambiguous")
        self.assertEqual(broadcasts[0][1]["nonce"], 3)

    def test_send_route_rechecks_gas_cap_at_broadcast(self):
        w3 = SimpleNamespace(eth=Mock())
        w3.eth.gas_price = 2_000_000_000
        w3.eth.get_block.return_value = {"baseFeePerGas": 2_000_000_000}
        w3.eth.get_transaction_count.return_value = 3
        account = Mock(address="0x0000000000000000000000000000000000000001")
        with self.assertRaisesRegex(ValueError, "exceeds gas cap"):
            module.send_route(
                {"w3": w3, "account": account},
                {
                    "recipient": "0x0000000000000000000000000000000000000002",
                    "amount_wei": 1000, "gas": 21_000,
                    "gas_cap_wei": 40_000_000_000_000,
                },
                4663,
            )
        w3.eth.send_raw_transaction.assert_not_called()

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
            self.assertEqual(data["transfer_gas_cap_wei"], 100_000_000_000_000)

    def test_metadata_uses_env_fee_transfer_gas_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.bot(directory, "prism", 5, gas_cap="0.00007")
            data = module.bot_metadata("prism", root)
            self.assertEqual(data["transfer_gas_cap_eth"], "0.00007")
            self.assertEqual(data["transfer_gas_cap_wei"], 70_000_000_000_000)

    def test_live_preflight_requires_only_donor_principal_and_gas_reserve(self):
        source_address = "0x0000000000000000000000000000000000000001"
        recipient_address = "0x0000000000000000000000000000000000000002"

        class FakeAccount:
            @staticmethod
            def from_key(key):
                address = source_address if key == "source-key" else recipient_address
                return SimpleNamespace(address=address)

        class FakeEth:
            chain_id = 4663
            gas_price = 1_000_000_000

            def get_block(self, _which):
                return {"baseFeePerGas": 1_000_000_000}

            def get_code(self, _address):
                return b""

            def estimate_gas(self, _tx):
                return 21_000

            def get_balance(self, _address):
                # Principal plus gas reserve, with nothing for retained slots.
                return 1_600_000_000_000_000

        class FakeWeb3:
            @staticmethod
            def HTTPProvider(url, request_kwargs=None):
                return url

            @staticmethod
            def to_checksum_address(address):
                return address

            def __init__(self, _provider):
                self.eth = FakeEth()

            def is_connected(self):
                return True

        plan = {
            "sources": {"source": 1},
            "destinations": {"recipient": 1},
            "routes": [{"source": "source", "destination": "recipient", "positions": 1}],
            "amounts_wei": {"source": 1_500_000_000_000_000},
            "gas_caps_wei": {"source": 100_000_000_000_000},
            "wallet_addresses": {
                "source": source_address, "recipient": recipient_address,
            },
        }
        metadata = {
            "source": {
                "private_key": "source-key",
                "values": {"RPC_URL": "rpc", "CHAIN_ID": "4663", "GAS_LIMIT_MULTIPLIER": "1", "PRIVATE_KEY": "source-key"},
                "available": 10,
                "reserve_wei": 1_500_000_000_000_000,
                "gas_reserve_wei": 100_000_000_000_000,
            },
            "recipient": {"private_key": "recipient-key", "values": {}},
        }
        with patch.object(module, "chain_imports", return_value=(FakeAccount, FakeWeb3)), \
                redirect_stdout(StringIO()):
            module.prepare_chain(plan, metadata)
        self.assertEqual(plan["feasibility"]["source"]["required_wei"], 1_600_000_000_000_000)
        self.assertNotIn("retained_position_reserve_wei", plan["feasibility"]["source"])

    def test_live_preflight_enforces_fee_transfer_gas_cap(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                suffix = 1 if key == "source" else 2
                return SimpleNamespace(address=f"0x{suffix:040x}")

        class FakeEth:
            chain_id = 4663
            gas_price = 1_000_000_000

            def get_block(self, _which): return {"baseFeePerGas": 1_000_000_000}
            def get_code(self, _address): return b""
            def estimate_gas(self, _tx): return 21_000

        class FakeWeb3:
            HTTPProvider = staticmethod(lambda url, request_kwargs=None: url)
            to_checksum_address = staticmethod(lambda address: address)
            def __init__(self, _provider): self.eth = FakeEth()
            def is_connected(self): return True

        plan = {
            "sources": {"source": 1}, "destinations": {"recipient": 1},
            "routes": [{"source": "source", "destination": "recipient", "positions": 1}],
            "amounts_wei": {"source": 1}, "gas_caps_wei": {"source": 20_000_000_000_000},
            "wallet_addresses": {"source": f"0x{1:040x}", "recipient": f"0x{2:040x}"},
        }
        metadata = {
            "source": {"private_key": "source", "values": {"RPC_URL": "rpc", "CHAIN_ID": "4663", "GAS_LIMIT_MULTIPLIER": "1", "PRIVATE_KEY": "source"}},
            "recipient": {"private_key": "recipient", "values": {}},
        }
        with patch.object(module, "chain_imports", return_value=(FakeAccount, FakeWeb3)), \
                redirect_stdout(StringIO()), \
                self.assertRaisesRegex(ValueError, "exceeds gas cap"):
            module.prepare_chain(plan, metadata)

    def test_metadata_allows_unrelated_duplicate_env_values_with_last_value_winning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.bot(directory, "earn", 5)
            env = root / ".env"
            with env.open("a", encoding="utf-8") as handle:
                handle.write(
                    "GRIDLESS_LEADING_EDGE=false\n"
                    "GRIDLESS_LEADING_EDGE=true\n"
                )
            data = module.bot_metadata("earn", root, require_key=True)
            self.assertEqual(data["capacity"], 5)
            self.assertEqual(data["values"]["GRIDLESS_LEADING_EDGE"], "true")

    def test_metadata_refuses_duplicate_capacity_target_before_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.bot(directory, "earn", 5)
            env = root / ".env"
            with env.open("a", encoding="utf-8") as handle:
                handle.write("MAX_ACTIVE_POSITIONS=6\n")
            with self.assertRaisesRegex(ValueError, "must be unique.*MAX_ACTIVE_POSITIONS"):
                module.bot_metadata("earn", root, require_key=True)

    def test_metadata_warns_but_continues_for_broad_env_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            env = root / ".env"
            env.write_text("MAX_ACTIVE_POSITIONS=1\nPRIVATE_KEY=test\n")
            env.chmod(0o664)
            warnings = StringIO()
            with redirect_stderr(warnings):
                data = module.bot_metadata("prism", root, require_key=True)
                module.bot_metadata("prism", root, require_key=True)
            self.assertEqual(data["capacity"], 1)
            self.assertIn("permissions are broader than recommended (664)", warnings.getvalue())
            self.assertIn(f"chmod 600 {env}", warnings.getvalue())
            self.assertEqual(warnings.getvalue().count("POSITION PASS WARNING"), 1)

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
            self.assertIn("principal=0.0015 ETH/position", body)
            self.assertIn("gas cap=0.0001 ETH/transfer", body)
            self.assertRegex(body, r"Plan ID: [0-9a-f]{16}")
            self.assertIn("POSITION PASS ALLOCATION PREVIEW", body)
            self.assertIn("POSITION PASS APPROVAL PLAN", body)
            self.assertIn("wallet balance: 1 ETH", body)
            self.assertIn("Total principal:", body)
            self.assertIn("Approval status: FEASIBLE", body)
            self.assertIn("DRY RUN COMPLETE", body)

    def test_treasury_funds_maximum_first_then_bots_level_remainder(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

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
                    "final_available_slots": None if name == module.TREASURY_SOURCE else 4,
                    "effective_gas_reserve_wei": 500_000_000_000_000,
                    "post_fee_gas_reserve_floor_wei": 499_999_999_979_000,
                    "required_wei": 1,
                    "projected_remaining_wei": 10**18,
                }
                for name in plan["sources"]
            }
            return {}

        with tempfile.TemporaryDirectory() as directory:
            a = self.bot(directory, "a", 5)
            b = self.bot(directory, "b", 5)
            x = self.bot(directory, "x", 3)
            y = self.bot(directory, "y", 3)
            treasury = self.treasury(directory)
            captured = {}

            def capture(plan, metadata, execute=False):
                captured.update(plan)
                return fake_prepare(plan, metadata, execute)

            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "treasury_live_balance", return_value=("0xtreasury", 7_000_000_000_000_000)), \
                    patch.object(module, "prepare_chain", side_effect=capture), \
                    redirect_stdout(StringIO()):
                self.assertEqual(module.main([
                    "--from-treasury", "--treasury-env", str(treasury),
                    "--from", "a,b", "--to", "x,y", "--positions", "6",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"a={a}", "--bot", f"b={b}",
                    "--bot", f"x={x}", "--bot", f"y={y}",
                ]), 0)
            self.assertEqual(captured["sources"], {"Treasury": 4, "a": 1, "b": 1})
            self.assertEqual(captured["destinations"], {"x": 3, "y": 3})
            self.assertNotIn("Treasury", captured["capacity_snapshot"])
            self.assertEqual(
                [(route["source"], route["destination"], route["positions"])
                 for route in captured["routes"]],
                [("Treasury", "x", 3), ("Treasury", "y", 1),
                 ("a", "y", 1), ("b", "y", 1)],
            )

    def test_positions_all_drains_every_available_slot_from_whole_fleet(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

        captured = {}

        def fake_prepare(plan, _metadata, execute=False):
            captured.update(plan)
            for route in plan["routes"]:
                route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                route["max_fee_wei"] = 1
                route["recipient"] = plan["wallet_addresses"][route["destination"]]
            return {}

        with tempfile.TemporaryDirectory() as directory:
            alpha = self.bot(directory, "alpha", 4, filled=2)
            beta = self.bot(directory, "beta", 7, filled=2)
            recipient = self.bot(directory, "recipient", 3, filled=3)
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(StringIO()):
                self.assertEqual(module.main([
                    "--from", "all", "--to", "recipient", "--positions", "all",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"alpha={alpha}", "--bot", f"beta={beta}",
                    "--bot", f"recipient={recipient}",
                ]), 0)
            self.assertEqual(captured["positions"], 7)
            self.assertEqual(captured["sources"], {"alpha": 2, "beta": 5})
            self.assertEqual(captured["destinations"], {"recipient": 7})

    def test_positions_available_adds_treasury_max_to_all_bot_capacity(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

        captured = {}

        def fake_prepare(plan, _metadata, execute=False):
            captured.update(plan)
            for route in plan["routes"]:
                route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                route["max_fee_wei"] = 1
                route["recipient"] = plan["wallet_addresses"][route["destination"]]
            return {}

        with tempfile.TemporaryDirectory() as directory:
            donor = self.bot(directory, "donor", 4, filled=2)
            recipient = self.bot(directory, "recipient", 3, filled=3)
            treasury = self.treasury(directory)
            # Treasury safely covers four slots: 4*0.0015 + one 0.0001 route
            # plus its preserved 0.0005 reserve = 0.0066 ETH.
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "treasury_live_balance", return_value=("0xtreasury", 6_700_000_000_000_000)), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(StringIO()):
                self.assertEqual(module.main([
                    "--from-treasury", "--treasury-env", str(treasury),
                    "--from", "donor", "--to", "recipient",
                    "--positions", "available",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"donor={donor}", "--bot", f"recipient={recipient}",
                ]), 0)
            self.assertEqual(captured["positions"], 6)
            self.assertEqual(captured["sources"], {"Treasury": 4, "donor": 2})

    def test_exact_bot_source_count_is_reserved_before_treasury(self):
        self.assertEqual(
            module.fair_allocate([("fixed", 2), ("plain", None)], 2,
                                 {"fixed": 2, "plain": 5}, "source"),
            {"fixed": 2, "plain": 0},
        )

    def test_treasury_only_execution_increases_recipient_without_donor_capacity(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

        def fake_prepare(plan, _metadata, execute=False):
            route = plan["routes"][0]
            route["amount_wei"] = plan["amounts_wei"][module.TREASURY_SOURCE]
            route["max_fee_wei"] = 21_000
            return {module.TREASURY_SOURCE: {}}

        with tempfile.TemporaryDirectory() as directory:
            recipient = self.bot(directory, "recipient", 3, filled=1)
            treasury = self.treasury(directory)
            journal_dir = Path(directory) / "journals"
            base = [
                "--from-treasury", "--treasury-env", str(treasury),
                "--to", "recipient", "--positions", "1",
                "--journal-dir", str(journal_dir),
                "--bot", f"recipient={recipient}",
            ]
            output = StringIO()
            patches = (
                patch.object(module, "chain_imports", return_value=(FakeAccount, object())),
                patch.object(module, "treasury_live_balance", return_value=("0xtreasury", 10**18)),
                patch.object(module, "prepare_chain", side_effect=fake_prepare),
            )
            with patches[0], patches[1], patches[2], redirect_stdout(output):
                self.assertEqual(module.main(base), 0)
            plan_id = re.search(r"Plan ID: ([0-9a-f]{16})", output.getvalue()).group(1)
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "treasury_live_balance", return_value=("0xtreasury", 10**18)), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    patch.object(module, "send_route", return_value=("0xtreasury", {"gas": 21_000, "gasPrice": 1})), \
                    redirect_stdout(StringIO()):
                self.assertEqual(
                    module.main(base + ["--execute", "--confirm-plan", plan_id]), 0
                )
            self.assertEqual(module.capacity_value(recipient / ".env"), 4)
            self.assertFalse(Path(str(treasury) + f".bak.position-pass.{plan_id}").exists())
            journal = json.loads((journal_dir / f"{plan_id}.json").read_text())
            self.assertEqual(set(journal["capacity_changes"]), {"recipient"})

    def test_main_refuses_same_bot_on_both_sides_before_chain_access(self):
        with tempfile.TemporaryDirectory() as directory:
            prism = self.bot(directory, "prism", 4)
            with self.assertRaisesRegex(ValueError, "both donors and recipients"):
                module.main([
                    "--from", "prism", "--to", "PRISM", "--positions", "1",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"prism={prism}",
                ])

    def test_local_preflight_validates_confirmation_without_rpc_or_mutation(self):
        class FakeAccount:
            @staticmethod
            def from_key(key):
                return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

        def fake_prepare(plan, _metadata, execute=False):
            for route in plan["routes"]:
                route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                route["max_fee_wei"] = 1
            return {}

        with tempfile.TemporaryDirectory() as directory:
            donor = self.bot(directory, "donor", 4, filled=1)
            recipient = self.bot(directory, "recipient", 3, filled=2)
            journal_dir = Path(directory) / "journals"
            base = [
                "--from", "donor", "--to", "recipient", "--positions", "1",
                "--journal-dir", str(journal_dir),
                "--bot", f"donor={donor}", "--bot", f"recipient={recipient}",
            ]
            preview = StringIO()
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(preview):
                module.main(base)
            plan_id = re.search(r"Plan ID: ([0-9a-f]{16})", preview.getvalue()).group(1)

            for confirmation in (None, "stale-plan-id"):
                args = base + ["--execute", "--local-preflight"]
                if confirmation is not None:
                    args += ["--confirm-plan", confirmation]
                with self.subTest(confirmation=confirmation), \
                        patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                        patch.object(module, "prepare_chain", side_effect=AssertionError("RPC must not run")), \
                        self.assertRaisesRegex(ValueError, f"--confirm-plan {plan_id}"):
                    module.main(args)

            output = StringIO()
            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=AssertionError("RPC must not run")), \
                    redirect_stdout(output):
                self.assertEqual(module.main(
                    base + ["--execute", "--local-preflight", "--confirm-plan", plan_id]
                ), 0)
            self.assertEqual(output.getvalue().splitlines(), ["donor", "recipient"])
            self.assertFalse(journal_dir.exists())
            self.assertEqual(module.capacity_value(donor / ".env"), 4)
            self.assertEqual(module.capacity_value(recipient / ".env"), 3)

    def test_full_donor_is_not_an_active_transfer_participant(self):
        with tempfile.TemporaryDirectory() as directory:
            full = self.bot(directory, "full", 12, filled=12, reserve="0")
            open_bot = self.bot(directory, "open", 12, filled=7)
            recipient = self.bot(directory, "recipient", 10, filled=10)
            captured = {}

            class FakeAccount:
                @staticmethod
                def from_key(key):
                    return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

            def fake_prepare(plan, _metadata, execute=False):
                captured.update(plan)
                for route in plan["routes"]:
                    route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                    route["max_fee_wei"] = 1
                return {}

            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(StringIO()):
                module.main([
                    "--from", "full,open", "--to", "recipient", "--positions", "3",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"full={full}", "--bot", f"open={open_bot}",
                    "--bot", f"recipient={recipient}",
                ])
            self.assertEqual(captured["sources"], {"open": 3})
            self.assertNotIn("full", captured["wallet_addresses"])

    def test_donor_principal_is_not_replaced_by_recipient_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            donor = self.bot(directory, "donor", 4, reserve="0.001")
            recipient = self.bot(directory, "recipient", 4, reserve="0.002")
            captured = {}

            class FakeAccount:
                @staticmethod
                def from_key(key):
                    return SimpleNamespace(address=f"0x{sum(key.encode()):040x}")

            def fake_prepare(plan, _metadata, execute=False):
                captured.update(plan)
                for route in plan["routes"]:
                    route["amount_wei"] = plan["amounts_wei"][route["source"]] * route["positions"]
                    route["max_fee_wei"] = 1
                return {}

            with patch.object(module, "chain_imports", return_value=(FakeAccount, object())), \
                    patch.object(module, "prepare_chain", side_effect=fake_prepare), \
                    redirect_stdout(StringIO()):
                module.main([
                    "--from", "donor", "--to", "recipient", "--positions", "1",
                    "--max-gas", "0.00008", "--max-gas-from", "donor=0.00006",
                    "--journal-dir", str(Path(directory) / "journals"),
                    "--bot", f"donor={donor}", "--bot", f"recipient={recipient}",
                ])
            self.assertEqual(captured["amounts_eth"], {"donor": "0.001"})
            self.assertEqual(captured["routes"][0]["amount_wei"], 10**15)
            self.assertEqual(captured["gas_caps_eth"], {"donor": "0.00006"})

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
            # Compatibility: journals created before transfer gas caps were
            # persisted must remain resumable after partial execution.
            interrupted["plan"].pop("gas_caps_eth")
            interrupted["plan"].pop("gas_caps_wei")
            for safety in interrupted["plan"]["donor_safety"].values():
                safety.pop("configured_transfer_gas_cap_wei")
            module.atomic_json(journal_dir / f"{plan_id}.json", interrupted)
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
