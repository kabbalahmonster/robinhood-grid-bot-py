import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


INITIALIZE_BOTS = Path(__file__).parent / "ops" / "fleet" / "initialize-bots"
ADDRESS = "0x" + "ab" * 20
PUBLIC_WALLET = "0x" + "12" * 20
PRIVATE_KEY = "0x" + "34" * 32


class InitializeBotsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.seed = self.root / "seed"
        self.remote = self.root / "remote.git"
        self.bot_root = self.root / "bots"
        self.seed.mkdir()
        (self.seed / "grid_bot.py").write_text("# bot\n", encoding="utf-8")
        (self.seed / "requirements.txt").write_text("", encoding="utf-8")
        (self.seed / "generate_wallet.py").write_text(
            """
import os

def generate_wallet():
    return {
        "address": "0x" + "12" * 20,
        "private_key": "0x" + "34" * 32,
        "created_at": "test",
    }

def save_wallet(wallet, filepath, chmod=True):
    with open(filepath, "x", encoding="utf-8") as handle:
        handle.write(f"Address: {wallet['address']}\\nPrivateKey: {wallet['private_key']}\\n")
    if chmod:
        os.chmod(filepath, 0o600)
""".lstrip(),
            encoding="utf-8",
        )
        self.git("init", "-b", "main", str(self.seed), cwd=self.root)
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "seed", cwd=self.seed)
        self.git("init", "--bare", str(self.remote), cwd=self.root)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "-u", "origin", "main", cwd=self.seed)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)

        self.template = self.root / "bot.env"
        self.template.write_text(
            "PRIVATE_KEY=replace\nTOKEN_SYMBOL=replace\nTOKEN_ADDRESS=replace\nPOLL_INTERVAL_SECONDS=8\n",
            encoding="utf-8",
        )
        self.config = self.root / "fleet.conf"
        self.config.write_text(
            f'FLEET_BOT_ROOT="{self.bot_root}"\nFLEET_CHECKOUT_DIRNAME="checkout"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, *args, cwd):
        return subprocess.run(
            ["git", "-c", "user.name=Fleet Test", "-c", "user.email=fleet@example.invalid", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        )

    def run_initializer(self, *tokens, apply=False, template=None):
        command = [
            str(INITIALIZE_BOTS),
            "--config",
            str(self.config),
            "--template",
            str(template or self.template),
            "--repo",
            str(self.remote),
            "--python",
            sys.executable,
        ]
        if apply:
            command.append("--apply")
        command.extend(tokens)
        return subprocess.run(
            command,
            cwd=self.root,
            env={**os.environ, "HOME": str(self.root)},
            text=True,
            capture_output=True,
        )

    def test_preview_creates_nothing(self):
        result = self.run_initializer(f"NeT={ADDRESS}", "INDEX")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preview only", result.stdout)
        self.assertFalse(self.bot_root.exists())
        self.assertFalse((self.bot_root / "net").exists())
        self.assertFalse((self.bot_root / "index").exists())

    def test_apply_creates_multiple_bots_with_mixed_addresses(self):
        result = self.run_initializer(f"NeT={ADDRESS}", "INDEX", apply=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(PRIVATE_KEY, result.stdout + result.stderr)
        for folder, symbol, address in (("net", "NET", ADDRESS), ("index", "INDEX", "")):
            checkout = self.bot_root / folder / "checkout"
            env_text = (checkout / ".env").read_text(encoding="utf-8")
            self.assertIn(f"TOKEN_SYMBOL={symbol}\n", env_text)
            self.assertIn(f"TOKEN_ADDRESS={address}\n", env_text)
            self.assertIn(f"PRIVATE_KEY={PRIVATE_KEY}\n", env_text)
            self.assertEqual(stat.S_IMODE((checkout / ".env").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((checkout / "wallet.txt").stat().st_mode), 0o600)
            self.assertTrue((checkout / ".git").is_dir())
        self.assertEqual(result.stdout.count(PUBLIC_WALLET), 2)

    def test_validation_failure_publishes_no_partial_directories(self):
        bad_template = self.root / "bad.env"
        bad_template.write_text(
            "PRIVATE_KEY=one\nPRIVATE_KEY=two\nTOKEN_SYMBOL=X\nTOKEN_ADDRESS=\n",
            encoding="utf-8",
        )
        result = self.run_initializer("ONE", "TWO", apply=True, template=bad_template)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bot_root / "one").exists())
        self.assertFalse((self.bot_root / "two").exists())

    def test_existing_destination_blocks_batch_before_clone(self):
        (self.bot_root / "two").mkdir(parents=True)
        result = self.run_initializer("ONE", "TWO", apply=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bot_root / "one").exists())


if __name__ == "__main__":
    unittest.main()
