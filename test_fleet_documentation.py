import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
FLEET = ROOT / "ops" / "fleet"
INTERNAL_SHELL = {"fleet-common.sh"}
DIRECT_PYTHON_TOOL = {"probe-uniswap-gateway.py"}


class FleetDocumentationTests(unittest.TestCase):
    def test_every_operator_entrypoint_is_named_in_fleet_guide(self):
        guide = (FLEET / "README.md").read_text()
        commands = {
            path.name
            for path in FLEET.iterdir()
            if path.is_file()
            and path.name not in INTERNAL_SHELL
            and (path.suffix != ".py" or path.name in DIRECT_PYTHON_TOOL)
            and bool(path.stat().st_mode & 0o100)
        }
        missing = {
            command for command in commands
            if not re.search(rf"(?<![A-Za-z0-9_-]){re.escape(command)}(?![A-Za-z0-9_-])", guide)
        }
        self.assertEqual(missing, set())

    def test_recent_financial_safety_features_are_in_operator_docs(self):
        docs = "\n".join([
            (FLEET / "README.md").read_text(),
            (FLEET / "OPERATOR_RUNBOOK.md").read_text(),
        ])
        for required in (
            "TREASURY_POSITION_RESERVE_ETH",
            "--position-reserve-eth",
            "--send-to-treasury",
            "quote-provider",
            "projected gas",
        ):
            with self.subTest(term=required):
                self.assertIn(required, docs)

    def test_lifecycle_commands_have_home_bin_install_links(self):
        guide = (FLEET / "README.md").read_text()
        for command in ("start-fleet", "stop-fleet", "restart-fleet", "stop-bot", "restart-bot"):
            with self.subTest(command=command):
                self.assertIn(
                    f'ln -sf "$PWD/ops/fleet/{command}" "$HOME/bin/{command}"',
                    guide,
                )


if __name__ == "__main__":
    unittest.main()
