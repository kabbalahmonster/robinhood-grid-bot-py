#!/usr/bin/env python3
"""Preview or apply a conservative proportional position haircut to wallet reality."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


POSITION_FILES = (Path("data/positions.json"), Path("data/gridless_positions.json"))


def load_mapping(path):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    checkout = Path.cwd().resolve()
    sys.path.insert(0, str(checkout))
    from config import load_config
    from wallet import Wallet

    config = load_config()
    wallet = Wallet(config)
    _display, wallet_raw = wallet.get_token_balance(config.token_address)
    wallet_raw = int(wallet_raw)

    documents = {path: load_mapping(path) for path in POSITION_FILES}
    active = []
    for path, positions in documents.items():
        for position_id, position in positions.items():
            if not isinstance(position, dict):
                raise ValueError(f"{path} position {position_id} must be an object")
            balance = int(position.get("balance", 0) or 0)
            if balance > 0:
                active.append((path, str(position_id), position, balance))

    tracked_raw = sum(item[3] for item in active)
    deficit_raw = max(0, tracked_raw - wallet_raw)
    result = {
        "checkout": str(checkout), "token_symbol": config.token_symbol,
        "wallet_raw": wallet_raw, "tracked_raw": tracked_raw, "deficit_raw": deficit_raw,
        "apply": args.apply, "changes": [],
    }
    if deficit_raw == 0:
        print(json.dumps(result, separators=(",", ":")))
        return 0
    if wallet_raw < 0 or not active:
        raise ValueError("invalid wallet/position state")

    # Allocate the real wallet balance proportionally. Cost basis is deliberately
    # preserved so missing tokens remain accounted for as economic loss.
    allocations = []
    allocated = 0
    for path, position_id, position, old_balance in active:
        numerator = wallet_raw * old_balance
        new_balance, remainder = divmod(numerator, tracked_raw)
        allocations.append([path, position_id, position, old_balance, new_balance, remainder])
        allocated += new_balance
    for item in sorted(allocations, key=lambda entry: (-entry[5], str(entry[0]), entry[1]))[:wallet_raw - allocated]:
        item[4] += 1

    for path, position_id, position, old_balance, new_balance, _remainder in allocations:
        if new_balance != old_balance:
            result["changes"].append({
                "file": str(path), "position_id": position_id,
                "old_balance_raw": old_balance, "new_balance_raw": new_balance,
                "haircut_raw": old_balance - new_balance,
            })
            if args.apply:
                position["balance"] = new_balance

    if args.apply:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for path, positions in documents.items():
            if not path.exists() or not any(change["file"] == str(path) for change in result["changes"]):
                continue
            backup = path.with_name(path.name + f".bak.reconcile.{timestamp}")
            backup.write_bytes(path.read_bytes())
            atomic_json(path, positions)
        audit_path = Path("data/position_balance_reconciliations.json")
        try:
            audit = json.loads(audit_path.read_text())
            if not isinstance(audit, list):
                audit = []
        except (FileNotFoundError, json.JSONDecodeError):
            audit = []
        result["reconciled_at"] = datetime.now(timezone.utc).isoformat()
        result["method"] = "proportional_wallet_haircut_cost_basis_preserved"
        audit.append(result)
        atomic_json(audit_path, audit[-1000:])

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
