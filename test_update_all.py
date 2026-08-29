import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "update-all"


class UpdateAllTests(unittest.TestCase):
    def test_runs_checkout_update_before_fleet_update_and_forwards_args(self):
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root)
            scripts = checkout / "ops" / "fleet"
            scripts.mkdir(parents=True)
            trace = checkout / "trace"
            self.make_script(
                scripts / "update-this-checkout",
                f'printf "self\\n" >> "{trace}"',
            )
            self.make_script(
                scripts / "update-fleet",
                f'printf "fleet %s\\n" "$*" >> "{trace}"',
            )

            result = subprocess.run(
                [str(SCRIPT), "--detach"],
                env={**os.environ, "FLEET_COMMAND_CHECKOUT": str(checkout)},
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(trace.read_text(), "self\nfleet --restart --detach\n")

    @staticmethod
    def make_script(path, command):
        path.write_text(f"#!/usr/bin/env bash\nset -Eeuo pipefail\n{command}\n")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
