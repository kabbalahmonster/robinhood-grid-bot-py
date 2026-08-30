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

    def run_initializer(
        self, *tokens, apply=False, template=None, show_private_keys=False,
        add_to_fleet=False, use_config_template=False, overrides=()
    ):
        command = [
            str(INITIALIZE_BOTS),
            "--config",
            str(self.config),
            "--repo",
            str(self.remote),
            "--python",
            sys.executable,
        ]
        if not use_config_template:
            command.extend(("--template", str(template or self.template)))
        if apply:
            command.append("--apply")
        if show_private_keys:
            command.append("--show-private-keys")
        if add_to_fleet:
            command.append("--add-to-fleet")
        for override in overrides:
            command.extend(("--overwrite-default", override))
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

    def test_configured_template_is_default_and_cli_can_override_it(self):
        alternate = self.root / "alternate.env"
        alternate.write_text(
            "PRIVATE_KEY=x\nTOKEN_SYMBOL=x\nTOKEN_ADDRESS=x\nPOLL_INTERVAL_SECONDS=17\n",
            encoding="utf-8",
        )
        self.config.write_text(
            self.config.read_text(encoding="utf-8") + f'FLEET_ENV_TEMPLATE="{self.template}"\n',
            encoding="utf-8",
        )

        configured = self.run_initializer("ONE", apply=True, use_config_template=True)
        overridden = self.run_initializer("TWO", apply=True, template=alternate)

        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        one_env = (self.bot_root / "one" / "checkout" / ".env").read_text(encoding="utf-8")
        two_env = (self.bot_root / "two" / "checkout" / ".env").read_text(encoding="utf-8")
        self.assertIn("POLL_INTERVAL_SECONDS=8\n", one_env)
        self.assertIn("POLL_INTERVAL_SECONDS=17\n", two_env)

    def test_missing_template_explains_both_configuration_options(self):
        result = self.run_initializer("ONE", use_config_template=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pass --template PATH or set FLEET_ENV_TEMPLATE", result.stderr)

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

    def test_explicit_reveal_flag_prints_import_list_after_success(self):
        result = self.run_initializer("NET", apply=True, show_private_keys=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SENSITIVE — MetaMask private-key import list", result.stdout)
        self.assertIn(f"NET\t{PUBLIC_WALLET}\t{PRIVATE_KEY}", result.stdout)
        self.assertTrue((self.bot_root / "net" / "checkout" / "wallet.txt").exists())

    def test_repeatable_defaults_update_existing_and_append_missing_variables(self):
        result = self.run_initializer(
            "NET",
            apply=True,
            overrides=("POLL_INTERVAL_SECONDS=12", "MAX_POSITIONS=6"),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        env_text = (self.bot_root / "net" / "checkout" / ".env").read_text(encoding="utf-8")
        self.assertIn("POLL_INTERVAL_SECONDS=12\n", env_text)
        self.assertIn("MAX_POSITIONS=6\n", env_text)

    def test_generated_identity_fields_cannot_be_overridden(self):
        result = self.run_initializer(
            "NET", apply=True, overrides=("PRIVATE_KEY=not-the-generated-key",)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed by initialize-bots", result.stderr)
        self.assertFalse(self.bot_root.exists())

    def test_existing_destination_blocks_batch_before_clone(self):
        (self.bot_root / "two").mkdir(parents=True)
        result = self.run_initializer("ONE", "TWO", apply=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bot_root / "one").exists())

    def test_add_to_fleet_preview_does_not_change_config(self):
        before = self.config.read_text(encoding="utf-8")

        result = self.run_initializer("ONE", "TWO", add_to_fleet=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fleet config: add one two", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_add_to_fleet_appends_names_after_success(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8") + "FLEET_BOT_NAMES=(existing)\n",
            encoding="utf-8",
        )

        result = self.run_initializer("ONE", "Two", apply=True, add_to_fleet=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        config_text = self.config.read_text(encoding="utf-8")
        self.assertIn("FLEET_BOT_NAMES=(existing)", config_text)
        self.assertIn("FLEET_BOT_NAMES+=(\n  one\n  two\n)", config_text)
        check = subprocess.run(
            ["bash", "-c", f'source "$1"; printf "%s\\n" "${{FLEET_BOT_NAMES[@]}}"', "bash", str(self.config)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(check.stdout.splitlines(), ["existing", "one", "two"])

    def test_add_to_fleet_rejects_duplicate_before_clone(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8") + "FLEET_BOT_NAMES=(one)\n",
            encoding="utf-8",
        )

        result = self.run_initializer("ONE", apply=True, add_to_fleet=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists in FLEET_BOT_NAMES", result.stderr)
        self.assertFalse(self.bot_root.exists())

    def test_add_to_fleet_rejects_explicit_directory_membership(self):
        self.config.write_text(
            self.config.read_text(encoding="utf-8") + 'FLEET_BOT_DIRS=("/tmp/bot")\n',
            encoding="utf-8",
        )

        result = self.run_initializer("ONE", add_to_fleet=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FLEET_BOT_DIRS is configured", result.stderr)


if __name__ == "__main__":
    unittest.main()
