import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
HELPER = ROOT / "ops" / "fleet" / "template-from-bot.py"
SCRIPT = ROOT / "ops" / "fleet" / "template-from-bot"


class TemplateFromBotTests(unittest.TestCase):
    def test_preview_is_sanitized_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bot.env"
            destination = root / "template.env"
            source.write_text("PRIVATE_KEY=super-secret\nTOKEN_SYMBOL=V4\nTOKEN_ADDRESS=0xabc\nKEEP=yes\n")
            destination.write_text("old=true\n")
            result = subprocess.run([HELPER, "--source", source, "--destination", destination], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("super-secret", result.stdout)
            self.assertEqual(destination.read_text(), "old=true\n")

    def test_apply_writes_sanitized_template_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bot.env"
            destination = root / "template.env"
            source.write_text("PRIVATE_KEY=super-secret\nTOKEN_SYMBOL=V4\nTOKEN_ADDRESS=0xabc\nKEEP=yes\n")
            destination.write_text("old=true\n")
            result = subprocess.run([HELPER, "--source", source, "--destination", destination, "--apply"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(destination.read_text(), "KEEP=yes\n")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(root.glob("template.env.bak.*"))), 1)

    def test_wrapper_uses_fleet_template_and_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot = root / "bots" / "v4" / "robinhood-grid-bot-py"
            bot.mkdir(parents=True)
            (bot / "grid_bot.py").write_text("pass\n")
            (bot / ".env").write_text("PRIVATE_KEY=super-secret\nTOKEN_SYMBOL=V4\nTOKEN_ADDRESS=0xabc\nKEEP=yes\n")
            template = root / "template.env"
            config = root / "fleet.conf"
            config.write_text(f'FLEET_BOT_DIRS=("{bot}")\nFLEET_ENV_TEMPLATE="{template}"\n')
            denied = subprocess.run([SCRIPT, "v4", "--config", config, "--apply"], text=True, capture_output=True, env={**os.environ, "HOME": directory})
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("--apply requires --confirm-template-update", denied.stderr)
            result = subprocess.run([SCRIPT, "v4", "--config", config, "--apply", "--confirm-template-update"], text=True, capture_output=True, env={**os.environ, "HOME": directory})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(template.read_text(), "KEEP=yes\n")


if __name__ == "__main__":
    unittest.main()
