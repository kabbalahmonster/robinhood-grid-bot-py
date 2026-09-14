#!/usr/bin/env python3
"""Preview or apply a conservative proportional position haircut to wallet reality."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


POSITION_FILES = (Path("data/positions.json"), Path("data/gridless_positions.json"))
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _hex(value):
    if hasattr(value, "hex"):
        value = value.hex()
    value = str(value)
    return value if value.startswith("0x") else "0x" + value


def _int(value):
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, byteorder="big")
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def verified_outgoing_tokens(wallet, token_address, tx_hash):
    """Return receipt-proven managed-token outflow for a successful wallet tx."""
    tx = wallet.w3.eth.get_transaction(tx_hash)
    receipt = wallet.w3.eth.get_transaction_receipt(tx_hash)
    if _int(receipt.get("status", 0)) != 1:
        raise ValueError("unresolved broadcast receipt is not successful")
    if str(tx.get("from", "")).lower() != wallet.address.lower():
        raise ValueError("unresolved broadcast sender is not the configured wallet")
    wallet_topic = "0x" + wallet.address.lower().removeprefix("0x").rjust(64, "0")
    total = 0
    for entry in receipt.get("logs", []):
        if str(entry.get("address", "")).lower() != token_address.lower():
            continue
        topics = [_hex(topic).lower() for topic in entry.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC or topics[1] != wallet_topic:
            continue
        total += _int(entry.get("data", 0))
    return total


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
    parser.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.automatic:
        args.apply = True

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
        # Recovery for a haircut applied by an older release: re-check the most
        # recent local audit and archive only an exact receipt-proven match.
        guard = getattr(wallet, "unresolved_broadcast", None)
        audit_path = Path("data/position_balance_reconciliations.json")
        if args.apply and not args.automatic and isinstance(guard, dict) and guard.get("tx_hash"):
            try:
                audit = json.loads(audit_path.read_text())
                prior = next(
                    entry for entry in reversed(audit)
                    if isinstance(entry, dict)
                    and entry.get("checkout") == str(checkout)
                    and entry.get("token_symbol") == config.token_symbol
                    and int(entry.get("deficit_raw", 0)) > 0
                )
                outgoing_raw = verified_outgoing_tokens(
                    wallet, config.token_address, str(guard["tx_hash"]),
                )
                if outgoing_raw == int(prior["deficit_raw"]):
                    archived = wallet.archive_reconciled_broadcast(str(guard["tx_hash"]))
                    result["unresolved_broadcast_match"] = {
                        "tx_hash": str(guard["tx_hash"]), "outgoing_raw": outgoing_raw,
                        "status": "receipt_verified_against_prior_reconciliation",
                        "archived_path": archived,
                    }
            except (FileNotFoundError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
                result["unresolved_broadcast_match"] = {
                    "tx_hash": str(guard["tx_hash"]), "status": "verification_failed",
                    "error": str(exc),
                }
        print(json.dumps(result, separators=(",", ":")))
        return 2 if args.automatic else 0
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

    matching_guard_hash = None
    guard = getattr(wallet, "unresolved_broadcast", None)
    if args.apply and isinstance(guard, dict) and guard.get("tx_hash"):
        candidate_hash = str(guard["tx_hash"])
        try:
            outgoing_raw = verified_outgoing_tokens(
                wallet, config.token_address, candidate_hash,
            )
            if outgoing_raw == deficit_raw:
                matching_guard_hash = candidate_hash
                result["unresolved_broadcast_match"] = {
                    "tx_hash": candidate_hash, "outgoing_raw": outgoing_raw,
                    "status": "receipt_verified_exact_deficit",
                }
            else:
                result["unresolved_broadcast_match"] = {
                    "tx_hash": candidate_hash, "outgoing_raw": outgoing_raw,
                    "status": "outflow_does_not_match_deficit",
                }
        except Exception as exc:
            result["unresolved_broadcast_match"] = {
                "tx_hash": candidate_hash, "status": "verification_failed",
                "error": str(exc),
            }

    if args.automatic and not matching_guard_hash:
        result["automatic_reconciliation"] = "refused_without_exact_receipt_match"
        print(json.dumps(result, separators=(",", ":")))
        return 2

    if args.apply:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for path, positions in documents.items():
            if not path.exists() or not any(change["file"] == str(path) for change in result["changes"]):
                continue
            backup = path.with_name(path.name + f".bak.reconcile.{timestamp}")
            backup.write_bytes(path.read_bytes())
            atomic_json(path, positions)
        if matching_guard_hash:
            archived = wallet.archive_reconciled_broadcast(matching_guard_hash)
            result["unresolved_broadcast_match"]["archived_path"] = archived
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
