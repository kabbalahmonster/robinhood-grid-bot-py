import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "bundle-logs"


class BundleLogsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bots = {}
        for name in ("ALPHA", "BETA"):
            bot = self.root / name / "robinhood-grid-bot-py"
            (bot / "logs").mkdir(parents=True)
            (bot / "grid_bot.py").write_text("pass\n")
            self.bots[name] = bot
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            'FLEET_BOT_DIRS=("{}" "{}")\n'.format(self.bots["ALPHA"], self.bots["BETA"])
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_bundle(self, *args):
        return subprocess.run(
            [str(SCRIPT), *args, "--config", str(self.config)],
            text=True, capture_output=True,
        )

    def write_log(self, bot, filename, body, age=0):
        path = self.bots[bot] / "logs" / filename
        path.write_text(body)
        when = time.time() - age
        os.utime(path, (when, when))
        return path

    def test_newest_logs_merge_chronologically_with_continuations_and_redaction(self):
        self.write_log("ALPHA", "old.log", "2026-01-01 00:00:00 | INFO | ignore\n", age=100)
        self.write_log(
            "ALPHA", "alpha.log",
            "2026-09-12 18:00:02 | INFO | second PRIVATE_KEY=0x" + "a" * 64
            + " RPC=https://eth-mainnet.g.alchemy.com/v2/supersecret\ntrace line\n",
        )
        self.write_log(
            "BETA", "beta.log",
            "2026-09-12 18:00:01 | INFO | first tx=0x" + "b" * 64 + "\n",
        )
        output = self.root / "bundle.log"
        result = self.run_bundle("--all", "--chronological", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("ALPHA: source=alpha.log", body)
        self.assertNotIn("ignore", body)
        self.assertLess(body.index("first tx="), body.index("second PRIVATE_KEY="))
        self.assertIn("PRIVATE_KEY=[REDACTED]", body)
        self.assertNotIn("a" * 64, body)
        self.assertNotIn("supersecret", body)
        self.assertIn("https://eth-mainnet.g.alchemy.com/v2/[REDACTED]", body)
        self.assertIn("0x" + "b" * 64, body)
        self.assertIn("[CONT] [ALPHA] [alpha.log] trace line", body)

    def test_default_layout_groups_records_into_descriptive_bot_sections(self):
        self.write_log("ALPHA", "alpha.log", "2026-09-12 18:00:02 | INFO | alpha later\n")
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:01 | INFO | beta earlier\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("# layout: grouped_by_bot", body)
        self.assertIn("# included_bots: 2", body)
        self.assertIn("# total_records: 2", body)
        self.assertIn("# BOT SECTION: ALPHA", body)
        self.assertIn("# source_file: alpha.log", body)
        self.assertIn("# status: ok", body)
        self.assertIn("# included_records: 1", body)
        self.assertIn("# time_range_utc:", body)
        self.assertLess(body.index("alpha later"), body.index("# BOT SECTION: BETA"))
        self.assertLess(body.index("# BOT SECTION: BETA"), body.index("beta earlier"))

    def test_only_since_and_line_cap(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2020-01-01 00:00:00 | INFO | old\n"
            "2026-09-12 18:00:00 | INFO | one\n"
            "2026-09-12 18:00:01 | INFO | two\n",
        )
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | beta\n")
        output = self.root / "bundle.log"
        result = self.run_bundle(
            "--only", "ALPHA", "--since", "1w", "--max-lines-per-bot", "1",
            "--output", str(output),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("two", body)
        self.assertNotIn("| one\n", body)
        self.assertNotIn("BETA:", body)

    def test_missing_log_is_partial_bundle_and_nonzero(self):
        self.write_log("ALPHA", "alpha.log", "2026-09-12 18:00:00 | INFO | alpha\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--output", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(output.exists())
        self.assertIn("BETA: source=-", output.read_text())
        self.assertIn("1 failed", result.stdout)

    def test_tournament_only_includes_logs_with_tournament_events(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2026-09-12 18:00:00 | INFO | normal cycle\n"
            "2026-09-12 18:00:01 | INFO | Route tournament winner provider=uniswap direction=buy\n",
        )
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | normal cycle\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--tournament-only", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("ALPHA: source=alpha.log", body)
        self.assertIn("Route tournament winner", body)
        self.assertIn("BETA: source=beta.log bytes=", body)
        self.assertIn("status=skipped: no tournament in included records", body)
        self.assertNotIn("[BETA]", body)
        self.assertIn("1 skipped; 0 failed", result.stdout)

    def test_tournament_only_respects_since_filter(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2020-01-01 00:00:00 | INFO | Route tournament candidate provider=uniswap\n"
            "2026-09-12 18:00:00 | INFO | normal recent cycle\n",
        )
        self.write_log(
            "BETA", "beta.log",
            "2026-09-12 18:00:00 | INFO | Route tournament winner provider=sushiswap\n",
        )
        output = self.root / "bundle.log"
        result = self.run_bundle("--tournament-only", "--since", "1w", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("ALPHA: source=alpha.log", body)
        self.assertIn("ALPHA: source=alpha.log bytes=", body)
        self.assertIn("status=skipped: no tournament in included records", body)
        self.assertIn("[BETA]", body)
        self.assertNotIn("[ALPHA]", body)

    def test_tournament_rounds_only_keeps_each_round_and_intervening_diagnostics(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2026-09-12 18:00:00 | INFO | unrelated before\n"
            "2026-09-12 18:00:01 | INFO | Route tournament candidate provider=uniswap\n"
            "2026-09-12 18:00:02 | WARNING | provider timeout during round\n"
            "2026-09-12 18:00:03 | INFO | Route tournament candidate provider=sushiswap\n"
            "2026-09-12 18:00:04 | INFO | Route tournament winner provider=uniswap\n"
            "2026-09-12 18:00:05 | INFO | unrelated between\n"
            "2026-09-12 18:00:06 | INFO | Route tournament candidate provider=umbra\n"
            "2026-09-12 18:00:07 | INFO | Route tournament winner provider=umbra\n"
            "2026-09-12 18:00:08 | INFO | unrelated after\n",
        )
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | no rounds\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--tournament-rounds-only", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("provider timeout during round", body)
        self.assertIn("status=ok: rounds=2 incomplete=0", body)
        self.assertIn("BETA: source=beta.log", body)
        self.assertNotIn("unrelated before", body)
        self.assertNotIn("unrelated between", body)
        self.assertNotIn("unrelated after", body)
        self.assertNotIn("[BETA]", body)

    def test_tournament_rounds_only_keeps_and_labels_incomplete_final_round(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2026-09-12 18:00:00 | INFO | Route tournament candidate provider=uniswap\n"
            "2026-09-12 18:00:01 | ERROR | bot stopped before winner\n",
        )
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | no rounds\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--tournament-rounds-only", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("bot stopped before winner", body)
        self.assertIn("status=ok: rounds=0 incomplete=1", body)

    def test_tournament_rounds_use_ids_not_log_order(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2026-09-12 18:00:00 | INFO | Route tournament start tournament_id=round-a direction=sell\n"
            "2026-09-12 18:00:01 | INFO | Route tournament candidate provider=uniswap tournament_id=round-a\n"
            "2026-09-12 18:00:02 | INFO | Route tournament winner provider=none tournament_id=round-a\n"
            "2026-09-12 18:00:03 | INFO | unrelated activity that must not leak\n"
            "2026-09-12 18:00:04 | INFO | Route tournament candidate provider=lifi tournament_id=round-a\n"
            "2026-09-12 18:00:05 | INFO | Route tournament lifecycle tournament_id=round-a phase=completed\n",
        )
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | no rounds\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--tournament-rounds-only", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("provider=lifi tournament_id=round-a", body)
        self.assertIn("phase=completed", body)
        self.assertIn("rounds=1 incomplete=0 correlation=id", body)
        self.assertNotIn("unrelated activity", body)

    def test_tournament_modes_are_mutually_exclusive(self):
        result = self.run_bundle("--tournament-only", "--tournament-rounds-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stderr)

    def test_analysis_sample_only_keeps_provenance_performance_and_tournaments(self):
        self.write_log(
            "ALPHA", "alpha.log",
            "2026-09-12 18:00:00 | INFO | unrelated cycle detail\n"
            "2026-09-12 18:00:01 | INFO | Bot runtime provenance build_sha=abc1234\n"
            "2026-09-12 18:00:02 | INFO | Bot cycle performance total_ms=12.3\n"
            "2026-09-12 18:00:03 | INFO | Route tournament winner provider=none tournament_id=x\n",
        )
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | unrelated\n")
        output = self.root / "analysis.log"
        result = self.run_bundle("--analysis-sample-only", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = output.read_text()
        self.assertIn("# analysis_sample_only: enabled", body)
        self.assertIn("build_sha=abc1234", body)
        self.assertIn("Bot cycle performance", body)
        self.assertIn("Route tournament winner", body)
        self.assertNotIn("unrelated cycle detail", body)
        self.assertIn("skipped: no analysis telemetry", body)

    def test_existing_output_uses_next_numbered_name_unless_forced(self):
        self.write_log("ALPHA", "alpha.log", "2026-09-12 18:00:00 | INFO | alpha\n")
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | beta\n")
        output = self.root / "bundle.log"
        output.write_text("keep")
        (self.root / "bundle-1.log").write_text("also keep")
        numbered = self.run_bundle("--output", str(output))
        self.assertEqual(numbered.returncode, 0, numbered.stderr)
        self.assertEqual(output.read_text(), "keep")
        self.assertEqual((self.root / "bundle-1.log").read_text(), "also keep")
        self.assertIn("writing", numbered.stdout)
        self.assertIn("bundle-2.log", numbered.stdout)
        self.assertIn("RH GRID FLEET LOG BUNDLE", (self.root / "bundle-2.log").read_text())
        replaced = self.run_bundle("--output", str(output), "--force")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertIn("RH GRID FLEET LOG BUNDLE", output.read_text())

    def test_refuses_symlinked_logs_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "stolen.log").write_text("secret")
        logs = self.bots["ALPHA"] / "logs"
        logs.rmdir()
        logs.symlink_to(outside, target_is_directory=True)
        self.write_log("BETA", "beta.log", "2026-09-12 18:00:00 | INFO | beta\n")
        output = self.root / "bundle.log"
        result = self.run_bundle("--output", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("logs path is not a real directory", output.read_text())
        self.assertNotIn("secret", output.read_text())


if __name__ == "__main__":
    unittest.main()
