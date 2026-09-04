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

    def test_leave_stopped_stops_before_update_and_does_not_restart(self):
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root)
            scripts = checkout / "ops" / "fleet"
            scripts.mkdir(parents=True)
            trace = checkout / "trace"
            self.make_script(scripts / "update-this-checkout", f'printf "self\\n" >> "{trace}"')
            self.make_script(scripts / "stop-fleet", f'printf "stop %s\\n" "$*" >> "{trace}"')
            self.make_script(scripts / "update-fleet", f'printf "fleet %s\\n" "$*" >> "{trace}"')

            result = subprocess.run(
                [str(SCRIPT), "--leave-stopped", "--config", "/tmp/fleet.conf"],
                env={**os.environ, "FLEET_COMMAND_CHECKOUT": str(checkout)},
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                trace.read_text(),
                "stop --if-running --config /tmp/fleet.conf\nself\nfleet --config /tmp/fleet.conf\n",
            )

    def test_leave_stopped_rejects_restart_or_detach_in_either_order(self):
        for args in (
            ("--leave-stopped", "--detach"),
            ("--detach", "--leave-stopped"),
            ("--leave-stopped", "--restart"),
            ("--restart", "--leave-stopped"),
        ):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as root:
                checkout = Path(root)
                scripts = checkout / "ops" / "fleet"
                scripts.mkdir(parents=True)
                self.make_script(scripts / "update-this-checkout", ":")
                self.make_script(scripts / "update-fleet", ":")
                result = subprocess.run(
                    [str(SCRIPT), *args],
                    env={**os.environ, "FLEET_COMMAND_CHECKOUT": str(checkout)},
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot be combined", result.stderr)

    def test_leave_stopped_remains_stopped_when_checkout_update_fails(self):
        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root)
            scripts = checkout / "ops" / "fleet"
            scripts.mkdir(parents=True)
            trace = checkout / "trace"
            self.make_script(scripts / "stop-fleet", f'printf "stopped\\n" >> "{trace}"')
            self.make_script(scripts / "update-this-checkout", "exit 17")
            self.make_script(scripts / "update-fleet", f'printf "unexpected\\n" >> "{trace}"')
            result = subprocess.run(
                [str(SCRIPT), "--leave-stopped"],
                env={**os.environ, "FLEET_COMMAND_CHECKOUT": str(checkout)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 17)
            self.assertEqual(trace.read_text(), "stopped\n")

    @staticmethod
    def make_script(path, command):
        path.write_text(f"#!/usr/bin/env bash\nset -Eeuo pipefail\n{command}\n")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
