"""Receipt-verified recovery of successful buys omitted from gridless state."""

import json
import os
import shutil
import time
from pathlib import Path

from web3 import Web3

import gridless
from config import load_config
from wallet import Wallet


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f1639c4a11628f55a4df523b3ef"
JOURNAL_FILE = Path("data/reconciled_gridless_buys.json")


def _hex(value):
    if hasattr(value, "hex"):
        value = value.hex()
    value = str(value)
    return value if value.startswith("0x") else "0x" + value


def _int(value):
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def _received_from_logs(receipt, token_address, wallet_address):
    token_address = token_address.lower()
    wallet_topic = "0x" + wallet_address.lower().removeprefix("0x").rjust(64, "0")
    total = 0
    for entry in receipt.get("logs", []):
        if str(entry.get("address", "")).lower() != token_address:
            continue
        topics = [_hex(topic).lower() for topic in entry.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC or topics[2] != wallet_topic:
            continue
        total += _int(entry.get("data", 0))
    return total


def inspect_buy(w3, tx_hash, token_address, wallet_address):
    """Return exact position economics after validating chain evidence."""
    tx_hash = tx_hash.lower()
    tx = w3.eth.get_transaction(tx_hash)
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    if _int(receipt.get("status", 0)) != 1:
        raise ValueError(f"{tx_hash}: transaction did not succeed")
    if str(tx.get("from", "")).lower() != wallet_address.lower():
        raise ValueError(f"{tx_hash}: sender is not the configured wallet")
    value_wei = _int(tx.get("value", 0))
    if value_wei <= 0:
        raise ValueError(f"{tx_hash}: transaction spent no native ETH principal")
    received_raw = _received_from_logs(receipt, token_address, wallet_address)
    if received_raw <= 0:
        raise ValueError(f"{tx_hash}: receipt has no configured-token transfer to wallet")
    gas_used = _int(receipt.get("gasUsed", 0))
    gas_price = _int(receipt.get("effectiveGasPrice", tx.get("gasPrice", 0)))
    if gas_used <= 0 or gas_price <= 0:
        raise ValueError(f"{tx_hash}: receipt is missing confirmed gas economics")
    return {
        "tx_hash": tx_hash,
        "block_number": _int(receipt.get("blockNumber", 0)),
        "to": str(tx.get("to", "")),
        "principal_wei": value_wei,
        "gas_wei": gas_used * gas_price,
        "cost_wei": value_wei + gas_used * gas_price,
        "balance": received_raw,
    }


def _load_journal():
    try:
        with JOURNAL_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("journal root is not an object")
        return data
    except FileNotFoundError:
        return {}


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def run_gridless_reconciliation(tx_hashes, apply=False, confirm_bot_stopped=False):
    """Preview or atomically add receipt-proven buys to gridless positions."""
    if apply and not confirm_bot_stopped:
        raise ValueError("--apply-reconciliation requires --confirm-bot-stopped")
    normalized = [Web3.to_hex(hexstr=value).lower() for value in tx_hashes]
    if len(normalized) != len(set(normalized)):
        raise ValueError("duplicate transaction hash supplied")

    config = load_config()
    wallet = Wallet(config)
    positions = gridless.load_positions()
    journal = _load_journal()
    recorded = {
        str(position.get("reconciliation_tx_hash", "")).lower()
        for position in positions.values()
    } | {str(value).lower() for value in journal}
    duplicates = [value for value in normalized if value in recorded]
    if duplicates:
        raise ValueError("already reconciled: " + ", ".join(duplicates))

    plans = [
        inspect_buy(wallet.w3, value, config.token_address, wallet.address)
        for value in normalized
    ]
    if len(positions) + len(plans) > config.max_active_positions:
        raise ValueError("reconciliation would exceed MAX_ACTIVE_POSITIONS")
    _, wallet_raw = wallet.get_token_balance(config.token_address)
    allocated_raw = sum(int(position["balance"]) for position in positions.values())
    import_raw = sum(plan["balance"] for plan in plans)
    if wallet_raw - allocated_raw < import_raw:
        raise ValueError("wallet does not contain enough unallocated tokens for these receipts")

    existing_ids = {int(value) for value in positions}
    for plan in plans:
        next_id = 0
        while next_id in existing_ids:
            next_id += 1
        existing_ids.add(next_id)
        plan["position_id"] = str(next_id)

    print("GRIDLESS BUY RECONCILIATION: " + ("APPLY" if apply else "DRY RUN"))
    for plan in plans:
        print(
            f"Position #{plan['position_id']}: tx={plan['tx_hash']} "
            f"principal={plan['principal_wei'] / 10**18:.18f} ETH "
            f"gas={plan['gas_wei'] / 10**18:.18f} ETH "
            f"cost={plan['cost_wei'] / 10**18:.18f} ETH "
            f"tokens_raw={plan['balance']}"
        )
    if not apply:
        print("No files changed. Re-run with --apply-reconciliation --confirm-bot-stopped.")
        return 0

    positions_path = Path(gridless.POSITIONS_FILE)
    backup_path = positions_path.with_name(
        positions_path.name + f".pre-reconcile-{int(time.time())}.bak"
    )
    if positions_path.exists():
        shutil.copy2(positions_path, backup_path)
    for plan in plans:
        positions[plan["position_id"]] = {
            "cost_wei": plan["cost_wei"],
            "balance": plan["balance"],
            "reconciliation_tx_hash": plan["tx_hash"],
        }
        journal[plan["tx_hash"]] = {
            **plan,
            "reconciled_at": int(time.time()),
        }
    gridless.save_positions(positions)
    _atomic_json(JOURNAL_FILE, journal)

    reloaded = gridless.load_positions()
    for plan in plans:
        saved = reloaded.get(plan["position_id"], {})
        if saved.get("reconciliation_tx_hash") != plan["tx_hash"]:
            raise RuntimeError("post-write verification failed; keep bot stopped and restore backup")
    print(f"Applied {len(plans)} position(s). Backup: {backup_path if backup_path.exists() else 'new file'}")
    return 0
