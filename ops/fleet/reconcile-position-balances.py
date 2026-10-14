#!/usr/bin/env python3
"""Preview or apply a conservative proportional position haircut to wallet reality."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3


POSITION_FILES = (Path("data/positions.json"), Path("data/gridless_positions.json"))
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PRE_SELL_BALANCE_GUARD = "pre-sell-balance-unavailable"
POSITION_BALANCE_GUARD = "position-balance-mismatch"
REPLACEMENT_LOOKBACK_SECONDS = 300
REPLACEMENT_LOOKAHEAD_SECONDS = 7200
REPLACEMENT_LOG_BLOCK_CHUNK = 50_000


class ExactTransactionAbsent(ValueError):
    """The guarded hash has neither a transaction nor receipt on any RPC."""


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
    transaction_finder = getattr(wallet.w3, "find_transaction", None)
    receipt_finder = getattr(wallet.w3, "find_transaction_receipt", None)
    tx = (transaction_finder(tx_hash) if callable(transaction_finder)
          else wallet.w3.eth.get_transaction(tx_hash))
    receipt = (receipt_finder(tx_hash) if callable(receipt_finder)
               else wallet.w3.eth.get_transaction_receipt(tx_hash))
    if tx is None and receipt is None:
        raise ExactTransactionAbsent(
            "exact transaction and receipt are absent across all RPC endpoints"
        )
    if tx is None or receipt is None:
        raise ValueError("exact transaction or receipt is absent across all RPC endpoints")
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


def block_at_or_after_timestamp(w3, timestamp):
    """Find the first available block whose timestamp is at least ``timestamp``."""
    latest = w3.eth.get_block("latest")
    high = _int(latest["number"])
    if _int(latest["timestamp"]) < timestamp:
        return high + 1
    low = 0
    while low < high:
        middle = (low + high) // 2
        block = w3.eth.get_block(middle)
        if _int(block["timestamp"]) < timestamp:
            low = middle + 1
        else:
            high = middle
    return low


def discover_replacement_sell(wallet, config, guard):
    """Find one successful same-nonce replacement through token outflow logs.

    This intentionally handles sells only: a managed-token Transfer emitted
    from the configured wallet provides a narrow candidate set without relying
    on explorer indexing.  Every candidate is then checked against the durable
    transaction fingerprint before its receipt is trusted.
    """
    if not isinstance(guard, dict):
        raise ValueError("replacement discovery requires a valid guard")
    try:
        nonce = _int(guard["nonce"])
        recorded_at = _int(guard["recorded_at_unix"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("guard lacks nonce or recording time") from exc
    guarded_wallet = str(guard.get("wallet") or "").lower()
    if guarded_wallet != wallet.address.lower():
        raise ValueError("guard wallet does not match the configured wallet")
    guarded_chain = guard.get("chain_id")
    if guarded_chain is not None and _int(guarded_chain) != int(config.chain_id):
        raise ValueError("guard chain does not match the configured chain")

    start_block = block_at_or_after_timestamp(
        wallet.w3, max(0, recorded_at - REPLACEMENT_LOOKBACK_SECONDS),
    )
    end_block = block_at_or_after_timestamp(
        wallet.w3, recorded_at + REPLACEMENT_LOOKAHEAD_SECONDS,
    )
    latest_block = _int(wallet.w3.eth.get_block("latest")["number"])
    end_block = min(end_block, latest_block)
    if start_block > end_block:
        raise ValueError("replacement search window has no mined blocks yet")

    wallet_topic = "0x" + wallet.address.lower().removeprefix("0x").rjust(64, "0")
    token_address = Web3.to_checksum_address(config.token_address)
    transaction_hashes = set()
    for first in range(start_block, end_block + 1, REPLACEMENT_LOG_BLOCK_CHUNK):
        last = min(end_block, first + REPLACEMENT_LOG_BLOCK_CHUNK - 1)
        logs = wallet.w3.eth.get_logs({
            "fromBlock": first,
            "toBlock": last,
            "address": token_address,
            "topics": [TRANSFER_TOPIC, wallet_topic],
        })
        transaction_hashes.update(
            _hex(entry["transactionHash"]).lower() for entry in logs
        )

    transaction_finder = getattr(wallet.w3, "find_transaction", None)
    candidates = []
    for tx_hash in sorted(transaction_hashes):
        tx = (transaction_finder(tx_hash) if callable(transaction_finder)
              else wallet.w3.eth.get_transaction(tx_hash))
        if tx is None:
            continue
        if str(tx.get("from") or "").lower() != wallet.address.lower():
            continue
        if _int(tx.get("nonce", -1)) != nonce:
            continue
        guarded_to = str(guard.get("to") or "").lower()
        if guarded_to and str(tx.get("to") or "").lower() != guarded_to:
            continue
        if _int(tx.get("value", 0) or 0) != _int(guard.get("value_wei", 0) or 0):
            continue
        outgoing_raw = verified_outgoing_tokens(
            wallet, config.token_address, tx_hash,
        )
        if outgoing_raw > 0:
            candidates.append((tx_hash, outgoing_raw))

    if len(candidates) != 1:
        raise ValueError(
            f"replacement discovery found {len(candidates)} matching successful sells"
        )
    return candidates[0]


def guarded_outgoing_tokens(wallet, config, guard, allow_replacement=False):
    """Verify the guarded hash, optionally resolving one proven replacement."""
    guarded_hash = str(guard["tx_hash"])
    try:
        return guarded_hash, verified_outgoing_tokens(
            wallet, config.token_address, guarded_hash,
        ), None
    except ExactTransactionAbsent:
        if not allow_replacement:
            raise
    replacement_hash, outgoing_raw = discover_replacement_sell(
        wallet, config, guard,
    )
    return replacement_hash, outgoing_raw, guarded_hash


def adopt_replacement(wallet, old_hash, replacement_hash):
    """Persist a verified replacement or fail without modifying the guard."""
    if old_hash is None:
        return True
    replacer = getattr(wallet, "replace_unresolved_broadcast_hash", None)
    if not callable(replacer) or not replacer(old_hash, replacement_hash):
        raise ValueError("could not atomically persist replacement transaction hash")
    return True


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


def recovered_prebroadcast_guard(record):
    """Return a known never-submitted guard once prerequisite RPC reads work."""
    if not isinstance(record, dict):
        return None
    guard_type = str(record.get("guard_type") or record.get("tx_hash") or "")
    if guard_type != PRE_SELL_BALANCE_GUARD:
        return None
    if record.get("broadcast_state") == "not_attempted":
        return guard_type
    # Backward compatibility for guards written before broadcast_state existed.
    if str(record.get("error") or "").startswith(
            "cannot snapshot token balance before sell"):
        return guard_type
    return None


def recovered_position_balance_guard(record):
    """Return the synthetic guard created from an observed position deficit."""
    if not isinstance(record, dict):
        return None
    guard_type = str(record.get("guard_type") or record.get("tx_hash") or "")
    if guard_type != POSITION_BALANCE_GUARD:
        return None
    if record.get("broadcast_state") == "not_attempted":
        return guard_type
    # Backward compatibility for guards written before the mismatch was
    # correctly classified as a non-broadcast state-reconciliation guard.
    if str(record.get("error") or "").startswith(
            "gridless tracked balance exceeds wallet balance"):
        return guard_type
    return None


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
    guard = getattr(wallet, "unresolved_broadcast", None)
    prebroadcast_guard = recovered_prebroadcast_guard(guard)
    position_balance_guard = recovered_position_balance_guard(guard)
    if args.automatic and prebroadcast_guard:
        archived = wallet.archive_unsubmitted_guard(prebroadcast_guard)
        if not archived:
            result["unresolved_broadcast_match"] = {
                "tx_hash": prebroadcast_guard,
                "status": "prebroadcast_guard_archive_failed",
            }
            print(json.dumps(result, separators=(",", ":")))
            return 2
        result["unresolved_broadcast_match"] = {
            "tx_hash": prebroadcast_guard,
            "status": "recovered_prebroadcast_guard",
            "archived_path": archived,
        }
        print(json.dumps(result, separators=(",", ":")))
        return 0
    if deficit_raw == 0:
        if args.automatic and position_balance_guard:
            archived = wallet.archive_unsubmitted_guard(position_balance_guard)
            result["unresolved_broadcast_match"] = {
                "tx_hash": position_balance_guard,
                "status": (
                    "state_guard_recovered_no_deficit"
                    if archived else "state_guard_archive_failed"
                ),
            }
            if archived:
                result["unresolved_broadcast_match"]["archived_path"] = archived
            print(json.dumps(result, separators=(",", ":")))
            return 0 if archived else 2
        # Recovery for a haircut applied by an older release: re-check the most
        # recent local audit and archive only an exact receipt-proven match.
        audit_path = Path("data/position_balance_reconciliations.json")
        recovered_prior_reconciliation = False
        if args.apply and isinstance(guard, dict) and guard.get("tx_hash"):
            outgoing_raw = None
            verified_hash = str(guard["tx_hash"])
            superseded_hash = None
            try:
                verified_hash, outgoing_raw, superseded_hash = guarded_outgoing_tokens(
                    wallet, config, guard, allow_replacement=args.automatic,
                )
                try:
                    audit = json.loads(audit_path.read_text())
                    prior = next(
                        entry for entry in reversed(audit)
                        if isinstance(entry, dict)
                        and entry.get("checkout") == str(checkout)
                        and entry.get("token_symbol") == config.token_symbol
                        and int(entry.get("deficit_raw", 0)) > 0
                    )
                except (FileNotFoundError, StopIteration, TypeError, ValueError):
                    prior = None
                if prior is not None and outgoing_raw == int(prior["deficit_raw"]):
                    adopt_replacement(wallet, superseded_hash, verified_hash)
                    archived = wallet.archive_reconciled_broadcast(verified_hash)
                    result["unresolved_broadcast_match"] = {
                        "tx_hash": verified_hash, "outgoing_raw": outgoing_raw,
                        "status": "receipt_verified_against_prior_reconciliation",
                        "archived_path": archived,
                    }
                    if superseded_hash:
                        result["unresolved_broadcast_match"]["superseded_tx_hash"] = superseded_hash
                    recovered_prior_reconciliation = bool(archived)
                elif outgoing_raw > 0:
                    # A managed-token outflow proves this is a sell, not a buy.
                    # With no current deficit, every tracked position remains
                    # fully collateralized by the post-transaction wallet
                    # balance, so no ledger haircut is required.  Archiving the
                    # exact guard is safe and avoids routing a successful sell
                    # through the gridless buy reconciler.
                    adopt_replacement(wallet, superseded_hash, verified_hash)
                    archived = wallet.archive_reconciled_broadcast(verified_hash)
                    result["unresolved_broadcast_match"] = {
                        "tx_hash": verified_hash, "outgoing_raw": outgoing_raw,
                        "status": "receipt_verified_sell_inventory_collateralized",
                        "archived_path": archived,
                    }
                    if superseded_hash:
                        result["unresolved_broadcast_match"]["superseded_tx_hash"] = superseded_hash
                    recovered_prior_reconciliation = bool(archived)
            except Exception as exc:
                result["unresolved_broadcast_match"] = {
                    "tx_hash": str(guard["tx_hash"]), "status": "verification_failed",
                    "error": str(exc),
                }
        if (args.automatic and not recovered_prior_reconciliation
                and not (isinstance(outgoing_raw, int) and outgoing_raw > 0)
                and getattr(config, "use_gridless", False)
                and isinstance(guard, dict) and guard.get("tx_hash")):
            # A successful unresolved gridless buy produces wallet surplus, not
            # the position deficit handled above. Reuse the existing receipt-
            # verified buy reconciler so principal, confirmed gas, and exact
            # token inflow become a real position before the guard is removed.
            candidate_hash = str(guard["tx_hash"])
            try:
                from gridless_reconciler import run_gridless_reconciliation
                recovery_code = run_gridless_reconciliation(
                    [candidate_hash], apply=True, safety_halted=True,
                )
                guard_path = Path(getattr(
                    wallet, "unresolved_broadcast_path",
                    "data/unresolved_broadcast.json",
                ))
                if recovery_code == 0 and not guard_path.exists():
                    recovered_prior_reconciliation = True
                    result["unresolved_broadcast_match"] = {
                        "tx_hash": candidate_hash,
                        "status": "receipt_verified_gridless_buy",
                    }
                else:
                    result["unresolved_broadcast_match"] = {
                        "tx_hash": candidate_hash,
                        "status": "gridless_buy_guard_not_archived",
                    }
            except Exception as exc:
                result["unresolved_broadcast_match"] = {
                    "tx_hash": candidate_hash,
                    "status": "gridless_buy_verification_failed",
                    "error": str(exc),
                }
        print(json.dumps(result, separators=(",", ":")))
        # Automatic recovery used to be excluded from this zero-deficit branch,
        # so a previously applied haircut left the bot halted forever even when
        # the same receipt and audit could be proven exactly. Resume only after
        # archive_reconciled_broadcast confirms that the matching guard moved.
        return 0 if recovered_prior_reconciliation or not args.automatic else 2
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
    superseded_guard_hash = None
    guard = getattr(wallet, "unresolved_broadcast", None)
    if (args.apply and isinstance(guard, dict) and guard.get("tx_hash")
            and not position_balance_guard):
        candidate_hash = str(guard["tx_hash"])
        try:
            candidate_hash, outgoing_raw, superseded_guard_hash = guarded_outgoing_tokens(
                wallet, config, guard, allow_replacement=args.automatic,
            )
            if outgoing_raw == deficit_raw:
                matching_guard_hash = candidate_hash
                result["unresolved_broadcast_match"] = {
                    "tx_hash": candidate_hash, "outgoing_raw": outgoing_raw,
                    "status": "receipt_verified_exact_deficit",
                }
                if superseded_guard_hash:
                    result["unresolved_broadcast_match"]["superseded_tx_hash"] = (
                        superseded_guard_hash
                    )
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

    if args.automatic and not (matching_guard_hash or position_balance_guard):
        result["automatic_reconciliation"] = "refused_without_exact_receipt_match"
        print(json.dumps(result, separators=(",", ":")))
        return 2

    if args.apply:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if matching_guard_hash:
            # Make the verified replacement durable before changing position
            # state. If a later write fails, the next run can still resolve the
            # exact mined hash instead of repeating replacement discovery.
            adopt_replacement(
                wallet, superseded_guard_hash, matching_guard_hash,
            )
        for path, positions in documents.items():
            if not path.exists() or not any(change["file"] == str(path) for change in result["changes"]):
                continue
            backup = path.with_name(path.name + f".bak.reconcile.{timestamp}")
            backup.write_bytes(path.read_bytes())
            atomic_json(path, positions)
        state_guard_archived = None
        if matching_guard_hash:
            archived = wallet.archive_reconciled_broadcast(matching_guard_hash)
            result["unresolved_broadcast_match"]["archived_path"] = archived
        elif position_balance_guard:
            state_guard_archived = wallet.archive_unsubmitted_guard(
                position_balance_guard
            )
            result["unresolved_broadcast_match"] = {
                "tx_hash": position_balance_guard,
                "status": (
                    "state_verified_position_haircut"
                    if state_guard_archived else "state_guard_archive_failed"
                ),
            }
            if state_guard_archived:
                result["unresolved_broadcast_match"]["archived_path"] = (
                    state_guard_archived
                )
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

        if position_balance_guard and not state_guard_archived:
            print(json.dumps(result, separators=(",", ":")))
            return 2

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
