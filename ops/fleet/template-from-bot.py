#!/usr/bin/env python3
"""Safely derive an initialize-bots dotenv template from one bot environment."""

import argparse
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SANITIZED_NAMES = {"PRIVATE_KEY", "TOKEN_SYMBOL", "TOKEN_ADDRESS"}
ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=.*)$")


def sanitized(text: str):
    kept, removed = [], []
    for line in text.splitlines(keepends=True):
        match = ASSIGNMENT.match(line.rstrip("\r\n"))
        if match and match.group(2) in SANITIZED_NAMES:
            removed.append(match.group(2))
            continue
        kept.append(line)
    if text and not text.endswith(("\n", "\r")) and kept:
        kept[-1] = kept[-1] + "\n"
    return "".join(kept), sorted(set(removed))


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve(strict=False)
    if source == destination:
        raise SystemExit("Refusing to use a bot .env as its own shared template destination")
    if not source.is_file():
        raise SystemExit(f"Source .env is not a readable file: {source}")
    content, removed = sanitized(source.read_text(encoding="utf-8"))
    missing = SANITIZED_NAMES.difference(removed)
    print(f"Source: {source}\nDestination: {destination}")
    print("Removed from template: " + ", ".join(removed or ["none found"]))
    if missing:
        print("Not present in source: " + ", ".join(sorted(missing)))
    if not args.apply:
        print("PREVIEW ONLY: destination unchanged.")
        return
    backup = None
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = destination.with_name(f"{destination.name}.bak.{stamp}")
        if backup.exists():
            raise SystemExit(f"Refusing to overwrite existing backup: {backup}")
        backup.write_bytes(destination.read_bytes())
        os.chmod(backup, 0o600)
    atomic_write(destination, content)
    print("TEMPLATE UPDATED: " + str(destination))
    if backup:
        print("Previous template backup: " + str(backup))


if __name__ == "__main__":
    main()
