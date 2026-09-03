import os
import subprocess
import tempfile
import unittest
from pathlib import Path


COMMON = Path(__file__).parent / "ops" / "fleet" / "fleet-common.sh"


class TestFleetConfig(unittest.TestCase):
    def _load(self, config_text, checkout_paths=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bots"
            root.mkdir()
            for relative in checkout_paths:
                checkout = root / relative
                checkout.mkdir(parents=True)
                (checkout / "grid_bot.py").touch()
            config = Path(directory) / "fleet.conf"
            config.write_text(config_text.replace("__BOT_ROOT__", str(root)))
            command = (
                f'source "{COMMON}"; fleet_load_config "{config}"; '
                "printf '%s\\n' \"$FLEET_MEMBERSHIP_SOURCE\"; "
                "printf '%s\\n' \"${FLEET_BOT_DIRS[@]}\""
            )
            return subprocess.run(
                ["bash", "-c", command],
                text=True,
                capture_output=True,
                env={**os.environ, "HOME": directory},
            )

    def test_root_discovers_sorted_checkouts(self):
        result = self._load(
            'FLEET_BOT_ROOT="__BOT_ROOT__"\n',
            ("zeta/robinhood-grid-bot-py", "alpha/robinhood-grid-bot-py"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith("discovered under "))
        self.assertTrue(lines[1].endswith("alpha/robinhood-grid-bot-py"))
        self.assertTrue(lines[2].endswith("zeta/robinhood-grid-bot-py"))

    def test_nonempty_explicit_list_overrides_root(self):
        result = self._load(
            'FLEET_BOT_ROOT="__BOT_ROOT__"\nFLEET_BOT_DIRS=("__BOT_ROOT__/chosen/repo")\n',
            ("chosen/repo", "ignored/repo"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "explicit FLEET_BOT_DIRS")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("chosen/repo"))

    def test_empty_explicit_list_falls_back_to_root(self):
        result = self._load(
            'FLEET_BOT_ROOT="__BOT_ROOT__"\nFLEET_BOT_DIRS=()\n',
            ("only/repo",),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.splitlines()[0].startswith("discovered under "))

    def test_bot_names_resolve_standard_checkout_paths(self):
        result = self._load(
            'FLEET_BOT_ROOT="__BOT_ROOT__"\nFLEET_BOT_NAMES=(alpha zeta)\n',
            ("alpha/robinhood-grid-bot-py", "zeta/robinhood-grid-bot-py"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "explicit FLEET_BOT_NAMES")
        self.assertTrue(lines[1].endswith("alpha/robinhood-grid-bot-py"))
        self.assertTrue(lines[2].endswith("zeta/robinhood-grid-bot-py"))

    def test_console_targets_sort_alphabetically_by_bot_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bots"
            paths = []
            for name in ("zeta", "Alpha", "beta"):
                checkout = root / name / "robinhood-grid-bot-py"
                checkout.mkdir(parents=True)
                (checkout / "grid_bot.py").touch()
                paths.append(str(checkout))
            quoted = " ".join(f'"{path}"' for path in paths)
            command = (
                f'source "{COMMON}"; FLEET_BOT_DIRS=({quoted}); '
                "fleet_sort_targets_by_name; "
                'for path in "${FLEET_BOT_DIRS[@]}"; do fleet_bot_name "$path"; done'
            )
            result = subprocess.run(["bash", "-c", command], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["Alpha", "beta", "zeta"])

    def test_bot_names_and_dirs_are_mutually_exclusive(self):
        result = self._load(
            'FLEET_BOT_ROOT="__BOT_ROOT__"\nFLEET_BOT_NAMES=(alpha)\n'
            'FLEET_BOT_DIRS=("__BOT_ROOT__/alpha/robinhood-grid-bot-py")\n',
            ("alpha/robinhood-grid-bot-py",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not both", result.stderr)

    def test_only_and_exclude_select_by_checkout_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bots"
            for name in ("alpha", "beta", "gamma"):
                checkout = root / name / "robinhood-grid-bot-py"
                checkout.mkdir(parents=True)
                (checkout / "grid_bot.py").touch()
            config = Path(directory) / "fleet.conf"
            config.write_text(f'FLEET_BOT_ROOT="{root}"\n')
            command = (
                f'source "{COMMON}"; fleet_load_config "{config}"; '
                'fleet_apply_selection "alpha,gamma" "gamma"; '
                "printf '%s\\n' \"${FLEET_SELECTED_NAMES[@]}\""
            )
            result = subprocess.run(["bash", "-c", command], text=True, capture_output=True,
                                    env={**os.environ, "HOME": directory})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["alpha"])

    def test_selectors_are_case_insensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bots"
            checkout = root / "hookr" / "robinhood-grid-bot-py"
            checkout.mkdir(parents=True)
            (checkout / "grid_bot.py").touch()
            config = Path(directory) / "fleet.conf"
            config.write_text(f'FLEET_BOT_ROOT="{root}"\nFLEET_BOT_NAMES=(hookr)\n')
            command = (
                f'source "{COMMON}"; fleet_load_config "{config}"; '
                'fleet_apply_selection "HOOKR" ""; printf \'%s\\n\' "${FLEET_SELECTED_NAMES[@]}"'
            )
            result = subprocess.run(["bash", "-c", command], text=True, capture_output=True,
                                    env={**os.environ, "HOME": directory})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["hookr"])

    def test_selector_rejects_unknown_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bots"
            checkout = root / "alpha" / "robinhood-grid-bot-py"
            checkout.mkdir(parents=True)
            (checkout / "grid_bot.py").touch()
            config = Path(directory) / "fleet.conf"
            config.write_text(f'FLEET_BOT_ROOT="{root}"\n')
            command = f'source "{COMMON}"; fleet_load_config "{config}"; fleet_apply_selection missing ""'
            failed = subprocess.run(["bash", "-c", command], text=True, capture_output=True,
                                    env={**os.environ, "HOME": directory})
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("Unknown bot in --only", failed.stderr)


if __name__ == "__main__":
    unittest.main()
