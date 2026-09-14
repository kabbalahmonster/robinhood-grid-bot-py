import json
import logging
import os
import tempfile
import unittest

from grid_bot import (
    DashboardEventHandler, GridBot, _runtime_build_provenance, _safe_event_message,
)


class TestDashboardEvents(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bot = GridBot.__new__(GridBot)
        self.bot.dashboard_events_file = os.path.join(self.temp_dir.name, "events.json")
        self.bot._dashboard_event_lock = __import__('threading').Lock()
        self.bot.dashboard_events = []
        self.bot.route_incident_file = os.path.join(self.temp_dir.name, "route_incident.json")
        self.bot.route_incident = {}
        self.bot.config = type("Config", (), {"token_symbol": "TEST"})()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_records_and_persists_event(self):
        self.bot._record_dashboard_event("error", "quote_failed", "Quote failed", source="grid_bot.zero_x")
        with open(self.bot.dashboard_events_file) as handle:
            persisted = json.load(handle)
        self.assertEqual(persisted[0]["level"], "error")
        self.assertEqual(persisted[0]["code"], "quote_failed")
        self.assertEqual(persisted[0]["source"], "grid_bot.zero_x")

    def test_environment_build_sha_avoids_git_probe(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("git must not run for an explicit build SHA")

        self.assertEqual(
            _runtime_build_provenance({"BOT_BUILD_SHA": "A" * 40}, forbidden),
            ("a" * 40, "unknown", "environment"),
        )

    def test_cycle_performance_reports_sanitized_rpc_deltas(self):
        self.bot.config.performance_telemetry_every_cycles = 1
        self.bot.config.poll_interval_seconds = 6
        self.bot.build_sha = "a" * 40
        self.bot.build_dirty = "false"
        self.bot.process_started_utc = "2026-09-14T00:00:00+00:00"
        self.bot.round_count = 7
        self.bot._performance_cycle_count = 0
        snapshots = iter((
            {"logical_calls": 10, "attempts": 11, "failures": 1, "total_ms": 50,
             "methods": {"eth.call": {"calls": 10}}},
            {"logical_calls": 13, "attempts": 15, "failures": 2, "total_ms": 90,
             "methods": {"eth.call": {"calls": 12}, "eth.get_balance": {"calls": 1}}},
        ))
        self.bot.wallet = type(
            "Wallet", (), {"rpc_telemetry_snapshot": lambda _self: next(snapshots)}
        )()
        self.bot._run_cycle_body = lambda: None

        with self.assertLogs("grid_bot", level="INFO") as observed:
            self.bot.run_cycle()

        message = "\n".join(observed.output)
        self.assertIn("rpc_logical_calls=3", message)
        self.assertIn("rpc_attempts=4", message)
        self.assertIn("rpc_failures=1", message)
        self.assertIn("rpc_method_stats=eth.call:2/0/0.0,eth.get_balance:1/0/0.0", message)

    def test_consecutive_duplicates_are_counted(self):
        self.bot._record_dashboard_event("warning", "rpc_warning", "RPC slow")
        self.bot._record_dashboard_event("warning", "rpc_warning", "RPC slow")
        self.assertEqual(len(self.bot.dashboard_events), 1)
        self.assertEqual(self.bot.dashboard_events[0]["count"], 2)

    def test_success_event_preserves_level_and_transaction(self):
        tx_hash = "0x" + "a" * 64
        self.bot._record_dashboard_event(
            "success", "usdg_banked", "Banked 0.001 ETH into 4.00 USDG", tx_hash=tx_hash
        )
        event = self.bot.dashboard_events[0]
        self.assertEqual(event["level"], "success")
        self.assertEqual(event["tx_hash"], tx_hash)

    def test_tx_hash_exemption_requires_exact_key_and_valid_hash(self):
        hash_shaped_secret = "0x" + "b" * 64
        self.bot._record_dashboard_event(
            "warning", "test", "Safe message", transaction=hash_shaped_secret, tx_hash="0xabc"
        )
        event = self.bot.dashboard_events[0]
        self.assertEqual(event["transaction"], "[REDACTED]")
        self.assertEqual(event["tx_hash"], "0xabc")

    def test_distinct_transactions_are_not_deduplicated(self):
        message = "Banked 0.001 ETH into 4.00 USDG"
        self.bot._record_dashboard_event("success", "usdg_banked", message, tx_hash="0xabc")
        self.bot._record_dashboard_event("success", "usdg_banked", message, tx_hash="0xdef")
        self.assertEqual(len(self.bot.dashboard_events), 2)

    def test_event_history_is_bounded(self):
        for index in range(55):
            self.bot._record_dashboard_event("warning", f"warning_{index}", f"Warning {index}")
        self.assertEqual(len(self.bot.dashboard_events), 50)
        self.assertEqual(self.bot.dashboard_events[0]["code"], "warning_5")

    def test_redacts_secret_material(self):
        key = "a" * 64
        message = _safe_event_message(f"api_key={key} private={key}")
        self.assertNotIn(key, message)
        self.assertIn("[REDACTED]", message)

    def test_redacts_provider_request_id(self):
        request_id = "a45b05e6a365eb11232b52fe72bfbd44"
        message = _safe_event_message(
            f'Response: {{"detail":"No quotes available","requestId":"{request_id}"}}'
        )
        self.assertNotIn(request_id, message)
        self.assertIn('"requestId":[REDACTED]', message)

    def test_logging_handler_maps_errors(self):
        self.bot.dashboard_events = []
        handler = DashboardEventHandler(self.bot._record_dashboard_event)
        record = logging.LogRecord("grid_bot.zero_x", logging.ERROR, __file__, 1, "API unavailable", (), None)
        handler.emit(record)
        self.assertEqual(self.bot.dashboard_events[0]["level"], "error")
        self.assertEqual(self.bot.dashboard_events[0]["code"], "log_error")

    def test_route_incident_alerts_on_third_failure_and_persists(self):
        for _ in range(3):
            self.bot._record_route_failure("Sushi route status: NoWay")
        self.assertTrue(self.bot.route_incident["active"])
        self.assertEqual(self.bot.route_incident["attempts"], 3)
        self.assertEqual(self.bot.dashboard_events[-1]["code"], "route_degraded")
        with open(self.bot.route_incident_file) as handle:
            self.assertEqual(json.load(handle)["attempts"], 3)

    def test_primary_failure_recovered_by_fallback_is_not_an_incident(self):
        self.bot.provider = type("Provider", (), {
            "fallback": object(), "fallback_active": False,
        })()
        self.bot._record_route_failure("Sell candidate #4 but quote failed")
        self.assertEqual(self.bot.route_incident, {})

        self.bot.provider.fallback_active = True
        self.bot._record_route_failure("Sell candidate #4 but quote failed")
        self.assertEqual(self.bot.route_incident["attempts"], 1)

    def test_confirmed_trade_closes_active_route_incident(self):
        self.bot.route_incident = {
            "active": True,
            "started_at": "2026-09-05T00:00:00+00:00",
            "attempts": 4,
        }
        self.bot.dashboard_trades = []
        self.bot.dashboard_trades_file = os.path.join(self.temp_dir.name, "trades.json")
        self.bot.wallet = type("Wallet", (), {"address": "0xwallet"})()
        self.bot.config.max_active_positions = 5
        self.bot._record_dashboard_trade("sell", 1, 2, 3, "0xtx")
        self.assertFalse(self.bot.route_incident["active"])
        self.assertEqual(self.bot.dashboard_events[-1]["code"], "route_recovered")

    def test_confirmed_buy_marks_tournament_complete_with_timestamp(self):
        self.bot.dashboard_trades = []
        self.bot.dashboard_trades_file = os.path.join(self.temp_dir.name, "trades.json")
        self.bot.wallet = type("Wallet", (), {"address": "0xwallet"})()
        self.bot.config.route_tournament_mode = "gate"
        self.bot.config.max_active_positions = 5
        self.bot._route_comparisons = {
            "buy": {"mode": "execution_preflight", "direction": "buy",
                    "status": "preflight_candidate_selected"}
        }

        self.bot._record_dashboard_trade(
            "buy", 0.003, 12345, 0.000000243, "0x" + "a" * 64,
            gas_fee_eth=0.00004,
        )

        comparison = self.bot._route_comparisons["buy"]
        self.assertEqual(comparison["status"], "completed")
        self.assertEqual(comparison["updated_at"], self.bot.dashboard_trades[-1]["timestamp"])
        self.assertEqual(comparison["final"]["side"], "buy")
        self.assertEqual(comparison["final"]["token_amount"], 12345.0)

    def test_tournament_submission_is_published_before_confirmation(self):
        updates = []
        self.bot.config.route_tournament_mode = "gate"
        self.bot._reporter = type(
            "Reporter", (), {"report_update": lambda _self, **value: updates.append(value)}
        )()
        self.bot._route_comparisons = {
            "sell": {"mode": "execution_preflight", "direction": "sell",
                     "status": "preflight_candidate_selected", "tournament_id": "round-1",
                     "started_at": "2026-09-12T17:00:00+00:00", "revision": 2,
                     "candidates": []}
        }

        callback = self.bot._tournament_submission_callback("sell")
        callback("0x" + "c" * 64)

        comparison = self.bot._route_comparisons["sell"]
        self.assertEqual(comparison["status"], "transaction_submitted")
        self.assertEqual(comparison["revision"], 3)
        self.assertEqual(comparison["pending_transaction"]["side"], "sell")
        self.assertEqual(updates[-1]["sell_attempt"]["route_comparison"], comparison)

    def test_selected_tournament_early_exit_gets_one_terminal_abort(self):
        self.bot.config.route_tournament_mode = "gate"
        self.bot._route_comparisons = {
            "sell": {"mode": "execution_preflight", "direction": "sell",
                     "status": "preflight_candidate_selected", "tournament_id": "round-2",
                     "revision": 2, "candidates": []}
        }

        first = self.bot._close_incomplete_tournament(
            "sell", reason="post_selection_exit_without_broadcast"
        )
        first_revision = first["revision"]
        second = self.bot._close_incomplete_tournament(
            "sell", reason="must_not_duplicate"
        )

        self.assertEqual(first["status"], "execution_aborted")
        self.assertEqual(first["terminal_reason"], "post_selection_exit_without_broadcast")
        self.assertEqual(second["revision"], first_revision)

    def test_terminal_tournament_state_rejects_late_transition(self):
        self.bot.config.route_tournament_mode = "gate"
        self.bot._route_comparisons = {
            "sell": {"mode": "execution_preflight", "direction": "sell",
                     "status": "settlement_unresolved", "tournament_id": "round-final",
                     "revision": 4, "terminal_reason": "confirmed_proceeds_unreconciled",
                     "candidates": []}
        }

        observed = self.bot._publish_tournament_transition(
            "sell", "completed", receipt_status=1,
            final={"measured_proceeds_wei": "123"},
        )

        self.assertEqual(observed["status"], "settlement_unresolved")
        self.assertEqual(observed["revision"], 4)
        self.assertNotIn("final", observed)

    def test_submitted_tournament_early_exit_is_execution_failed(self):
        self.bot.config.route_tournament_mode = "gate"
        self.bot._route_comparisons = {
            "buy": {"mode": "execution_preflight", "direction": "buy",
                    "status": "transaction_submitted", "tournament_id": "round-3",
                    "revision": 3, "candidates": []}
        }

        observed = self.bot._close_incomplete_tournament(
            "buy", reason="broadcast_or_receipt_failed"
        )

        self.assertEqual(observed["status"], "execution_failed")

    def test_completion_recovers_submission_order_and_records_exact_economics(self):
        self.bot.dashboard_trades = []
        self.bot.dashboard_trades_file = os.path.join(self.temp_dir.name, "trades.json")
        self.bot.wallet = type("Wallet", (), {"address": "0xwallet"})()
        self.bot.config.route_tournament_mode = "gate"
        self.bot.config.max_active_positions = 5
        self.bot._route_comparisons = {
            "sell": {"mode": "execution_preflight", "direction": "sell",
                     "status": "preflight_candidate_selected", "tournament_id": "round-4",
                     "revision": 2, "candidates": []}
        }
        receipt_result = type("Result", (), {
            "gas_used": 21000, "effective_gas_price": 123,
            "receipt": {"status": 1},
        })()

        self.bot._record_dashboard_trade(
            "sell", 1, 2, 0.5, "0x" + "d" * 64,
            profit_eth=0.1, gas_fee_eth=0.01,
            receipt_result=receipt_result, measured_amount_raw=999,
            realized_profit_wei=77,
        )

        comparison = self.bot._route_comparisons["sell"]
        self.assertEqual(comparison["status"], "completed")
        self.assertEqual(comparison["revision"], 4)
        self.assertEqual(comparison["final"]["receipt_status"], 1)
        self.assertEqual(comparison["final"]["gas_used"], "21000")
        self.assertEqual(comparison["final"]["effective_gas_price_wei"], "123")
        self.assertEqual(comparison["final"]["measured_proceeds_wei"], "999")
        self.assertEqual(comparison["final"]["realized_profit_wei"], "77")


if __name__ == "__main__":
    unittest.main()
