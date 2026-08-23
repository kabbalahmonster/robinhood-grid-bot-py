#!/usr/bin/env python3
"""Create a non-overwriting, owner-readable fleet private-key backup."""

import argparse
import datetime
import json
import os
import re


KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def read_fields(path):
    found = {"PRIVATE_KEY": [], "TOKEN_SYMBOL": []}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            match = ASSIGNMENT_RE.match(line.rstrip("\r\n"))
            if match and match.group(1) in found:
                found[match.group(1)].append(unquote(match.group(2)))
    for field, values in found.items():
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{path}: expected exactly one non-empty {field}")
    private_key = found["PRIVATE_KEY"][0]
    symbol = found["TOKEN_SYMBOL"][0]
    if not KEY_RE.fullmatch(private_key):
        raise ValueError(f"{path}: PRIVATE_KEY is not a 32-byte hexadecimal key")
    if any(char.isspace() for char in symbol):
        raise ValueError(f"{path}: TOKEN_SYMBOL must not contain whitespace")
    return private_key, symbol


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--entry", nargs=2, action="append", metavar=("BOT", "ENV"), required=True)
    args = parser.parse_args()

    entries = []
    for bot_name, env_path in args.entry:
        private_key, symbol = read_fields(env_path)
        entries.append({"bot": bot_name, "symbol": symbol, "private_key": private_key})

    document = {
        "format": "rh-grid-bot-private-key-backup-v1",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "entries": entries,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(os.path.abspath(args.output), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(os.path.abspath(args.output))
        except FileNotFoundError:
            pass
        raise
    print(f"Private-key backup created with mode 0600: {os.path.abspath(args.output)} ({len(entries)} entries)")


if __name__ == "__main__":
    main()
