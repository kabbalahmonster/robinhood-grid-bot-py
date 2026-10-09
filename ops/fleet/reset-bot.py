#!/usr/bin/env python3
"""Preview or reset one bot's mutable trading state without reading secrets."""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REMOVE = object()


SCOPES = {
    "history": {
        "data/dashboard_trades.json": [],
        "data/dashboard_events.json": [],
    },
    "positions": {
        "data/positions.json": {},
        "data/gridless_positions.json": {},
        # A ladder is inseparable from its paired gridless position state.  An
        # empty JSON object is not valid ladder data, so remove it after backup
        # rather than replacing it with an invalid placeholder.
        "data/gridless_ladder.json": REMOVE,
    },
    "accounting": {
        "data/profit_totals.json": None,
        "data/profit_fee_accrual.json": {"pending_wei": 0, "sale_tx_hashes": []},
    },
    "learning": {
        "data/token_tax_detection.json": {},
    },
}


def dotenv_value(path: Path, name: str):
    if not path.is_file():
        return None
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*?)\s*$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw)
        if not match:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def state_file(bot_dir: Path) -> str:
    value = dotenv_value(bot_dir / ".env", "STATE_FILE") or "data/positions.json"
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("STATE_FILE must be a relative path inside the bot checkout")
    return str(candidate)


def selected_files(bot_dir: Path, scope: str):
    names = [scope] if scope != "all" else list(SCOPES)
    files = {}
    for name in names:
        files.update(SCOPES[name])
    configured_state = state_file(bot_dir)
    if "positions" in names and configured_state != "data/positions.json":
        files.pop("data/positions.json", None)
        files[configured_state] = {}
    return files


def empty_value(relative: str, configured):
    if relative != "data/profit_totals.json":
        return configured
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "tracking_started_at": now,
        "last_updated_at": None,
        "realized_profit_wei": 0,
        "realized_sales": 0,
        "profitable_sales": 0,
        "losing_sales": 0,
        "baseline_profit_wei": 0,
        "baseline_sales": 0,
        "baseline_at": None,
        "recent_tx_hashes": [],
        "profit_history": [],
    }


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-dir", required=True, type=Path)
    parser.add_argument("--scope", choices=(*SCOPES, "all"), default="all")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-stamp")
    args = parser.parse_args()

    bot_dir = args.bot_dir.resolve()
    files = selected_files(bot_dir, args.scope)
    print(f"Reset scope: {args.scope}")
    for relative, configured_value in files.items():
        target = bot_dir / relative
        if configured_value is REMOVE:
            print(f"  {relative}: {'remove' if target.exists() else 'already absent'}")
            continue
        value = empty_value(relative, configured_value)
        print(f"  {relative}: {'replace' if target.exists() else 'create'} with {type(value).__name__}")
    print("Preserved: .env, wallet balances, source code, treasury transfers, and liquidation audit data.")
    if not args.apply:
        print("PREVIEW ONLY: no files changed.")
        return

    stamp = args.backup_stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = bot_dir / "data" / "reset-backups" / stamp
    if backup_root.exists():
        raise SystemExit(f"Refusing to reuse existing backup directory: {backup_root}")
    backup_root.mkdir(parents=True)
    manifest = {"scope": args.scope, "created_at": stamp, "files": []}
    try:
        for relative, configured_value in files.items():
            target = bot_dir / relative
            backup = backup_root / relative
            existed = target.exists()
            if existed:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            manifest["files"].append({"path": relative, "existed": existed})
            if configured_value is REMOVE:
                target.unlink(missing_ok=True)
            else:
                atomic_json(target, empty_value(relative, configured_value))
        atomic_json(backup_root / "manifest.json", manifest)
    except BaseException:
        for record in reversed(manifest["files"]):
            target = bot_dir / record["path"]
            backup = backup_root / record["path"]
            if record["existed"] and backup.exists():
                shutil.copy2(backup, target)
            elif not record["existed"]:
                target.unlink(missing_ok=True)
        raise
    print(f"RESET COMPLETE: backup saved at {backup_root}")


if __name__ == "__main__":
    main()
