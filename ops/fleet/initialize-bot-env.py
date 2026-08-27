#!/usr/bin/env python3
"""Generate one wallet and build a bot .env without exposing its private key."""

import argparse
import importlib.util
import os
import re
import stat
import tempfile
from pathlib import Path


PRIVATE_KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def load_generator(path: Path):
    spec = importlib.util.spec_from_file_location("fleet_wallet_generator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load wallet generator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "generate_wallet", None)):
        raise RuntimeError(f"Wallet generator has no generate_wallet(): {path}")
    if not callable(getattr(module, "save_wallet", None)):
        raise RuntimeError(f"Wallet generator has no save_wallet(): {path}")
    return module


def replace_assignments(text: str, assignments: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    for name, value in assignments.items():
        pattern = re.compile(rf"^(\s*(?:export\s+)?{re.escape(name)}\s*=)(.*?)(\r?\n)?$")
        matches = [index for index, line in enumerate(lines) if pattern.match(line)]
        if len(matches) > 1:
            raise ValueError(f"Template defines {name} more than once")
        if matches:
            index = matches[0]
            match = pattern.match(lines[index])
            assert match is not None
            lines[index] = f"{match.group(1)}{value}{match.group(3) or os.linesep}"
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += os.linesep
            lines.append(f"{name}={value}{os.linesep}")
    return "".join(lines)


def write_exclusive(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite: {path}")
        os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--env-output", required=True, type=Path)
    parser.add_argument("--wallet-generator", required=True, type=Path)
    parser.add_argument("--wallet-output", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--address", default="")
    parser.add_argument("--reveal-private-key", action="store_true")
    args = parser.parse_args()

    if args.address and not ADDRESS_RE.fullmatch(args.address):
        raise ValueError("Token address must be blank or a 20-byte 0x-prefixed EVM address")
    if args.env_output.exists() or args.wallet_output.exists():
        raise FileExistsError("Refusing to overwrite an existing .env or wallet file")

    template = args.template.read_text(encoding="utf-8")
    generator = load_generator(args.wallet_generator)
    wallet = generator.generate_wallet()
    private_key = str(wallet.get("private_key", ""))
    public_address = str(wallet.get("address", ""))
    if not PRIVATE_KEY_RE.fullmatch(private_key):
        raise ValueError("Wallet generator returned an invalid private key")
    if not ADDRESS_RE.fullmatch(public_address):
        raise ValueError("Wallet generator returned an invalid public address")

    generator.save_wallet(wallet, str(args.wallet_output), chmod=True)
    os.chmod(args.wallet_output, stat.S_IRUSR | stat.S_IWUSR)
    env_text = replace_assignments(
        template,
        {
            "PRIVATE_KEY": private_key,
            "TOKEN_SYMBOL": args.symbol,
            "TOKEN_ADDRESS": args.address,
        },
    )
    write_exclusive(args.env_output, env_text)
    if args.reveal_private_key:
        print(f"{public_address}\t{private_key}")
    else:
        print(public_address)


if __name__ == "__main__":
    main()
