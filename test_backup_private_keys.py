import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "ops" / "fleet" / "backup-private-keys"


class TestBackupPrivateKeys(unittest.TestCase):
    def test_creates_owner_only_json_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bots"
            keys = ("1" * 64, "2" * 64)
            for name, symbol, key in (("alpha", "ALPHA", keys[0]), ("beta", "BETA", keys[1])):
                checkout = root / name / "robinhood-grid-bot-py"
                checkout.mkdir(parents=True)
                (checkout / "grid_bot.py").touch()
                (checkout / ".env").write_text(f"PRIVATE_KEY=0x{key}\nTOKEN_SYMBOL={symbol}\n")
            config = Path(directory) / "fleet.conf"
            config.write_text(f'FLEET_BOT_ROOT="{root}"\nFLEET_BOT_NAMES=(alpha beta)\n')
            output = Path(directory) / "keys.json"
            command = [SCRIPT, "--config", config, "--output", output]

            created = subprocess.run(command, text=True, capture_output=True,
                                     env={**os.environ, "HOME": directory})
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertNotIn(keys[0], created.stdout + created.stderr)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            document = json.loads(output.read_text())
            self.assertEqual(document["format"], "rh-grid-bot-private-key-backup-v1")
            self.assertEqual([(item["bot"], item["symbol"]) for item in document["entries"]],
                             [("alpha", "ALPHA"), ("beta", "BETA")])
            original = output.read_bytes()

            refused = subprocess.run(command, text=True, capture_output=True,
                                     env={**os.environ, "HOME": directory})
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
