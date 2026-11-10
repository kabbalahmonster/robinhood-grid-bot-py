#!/usr/bin/env python3
"""Preview or execute a guarded same-nonce cancellation of an absent broadcast."""

import argparse
import json
import os
import time
from pathlib import Path

from web3 import Web3


MINIMUM_GUARD_AGE_SECONDS = 900
CANCELLATION_GAS = 21_000
MINIMUM_CANCELLATION_GAS_PRICE_WEI = 1_000_000_000


def _int(value):
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def _find_transaction(w3, tx_hash):
    finder = getattr(w3, "find_transaction", None)
    return finder(tx_hash) if callable(finder) else w3.eth.get_transaction(tx_hash)


def _find_receipt(w3, tx_hash):
    finder = getattr(w3, "find_transaction_receipt", None)
    return finder(tx_hash) if callable(finder) else w3.eth.get_transaction_receipt(tx_hash)


def build_cancellation_plan(wallet, config, now=None):
    """Return a cancellation plan only for an old, absent, still-unused nonce."""
    guard = getattr(wallet, "unresolved_broadcast", None)
    if not isinstance(guard, dict) or not guard.get("tx_hash"):
        raise ValueError("no valid unresolved-broadcast guard exists")
    if guard.get("guard_type") or guard.get("broadcast_state") == "not_attempted":
        raise ValueError("guard is not an outcome-unknown signed broadcast")
    required = ("recorded_at_unix", "chain_id", "wallet", "nonce", "to", "value_wei")
    missing = [key for key in required if guard.get(key) is None]
    if missing:
        raise ValueError("guard lacks fingerprint fields: " + ", ".join(missing))

    tx_hash = str(guard["tx_hash"]).lower()
    if guard.get("cancellation_tx_hash"):
        cancel_hash = str(guard["cancellation_tx_hash"]).lower()
        receipt = _find_receipt(wallet.w3, cancel_hash)
        if receipt is not None and _int(receipt.get("status", 0)) == 1:
            raise ValueError(
                f"recorded cancellation {cancel_hash} succeeded; archive recovery is required"
            )
        raise ValueError(f"cancellation attempt already recorded: {cancel_hash}")
    if _find_transaction(wallet.w3, tx_hash) is not None or _find_receipt(wallet.w3, tx_hash) is not None:
        raise ValueError("guarded transaction is visible on-chain; use receipt reconciliation")

    chain_id = _int(guard["chain_id"])
    nonce = _int(guard["nonce"])
    if chain_id != int(config.chain_id):
        raise ValueError("guard chain does not match configured chain")
    if str(guard["wallet"]).lower() != wallet.address.lower():
        raise ValueError("guard wallet does not match configured wallet")
    if not Web3.is_address(guard["to"]):
        raise ValueError("guard destination is invalid")

    now = int(time.time() if now is None else now)
    age_seconds = now - _int(guard["recorded_at_unix"])
    if age_seconds < MINIMUM_GUARD_AGE_SECONDS:
        raise ValueError(
            f"guard is only {age_seconds}s old; wait at least {MINIMUM_GUARD_AGE_SECONDS}s"
        )
    latest_nonce = int(wallet.w3.eth.get_transaction_count(wallet.address, "latest"))
    pending_nonce = int(wallet.w3.eth.get_transaction_count(wallet.address, "pending"))
    if latest_nonce != nonce or pending_nonce != nonce:
        raise ValueError(
            f"guard nonce is {nonce}, but latest/pending nonces are "
            f"{latest_nonce}/{pending_nonce}; replacement discovery is required"
        )

    normal_gas_price = int(wallet.normal_gas_price())
    gas_price = max(
        normal_gas_price * 2,
        MINIMUM_CANCELLATION_GAS_PRICE_WEI,
    )
    maximum_fee_wei = CANCELLATION_GAS * gas_price
    balance_wei = int(wallet.get_eth_balance_wei())
    if balance_wei < maximum_fee_wei:
        raise ValueError(
            f"wallet has {balance_wei} wei but cancellation may require {maximum_fee_wei} wei"
        )
    return {
        "guarded_tx_hash": tx_hash,
        "chain_id": chain_id,
        "wallet": wallet.address,
        "nonce": nonce,
        "age_seconds": age_seconds,
        "guarded_to": str(guard["to"]),
        "guarded_value_wei": _int(guard["value_wei"]),
        "latest_nonce": latest_nonce,
        "pending_nonce": pending_nonce,
        "gas": CANCELLATION_GAS,
        "gas_price_wei": gas_price,
        "maximum_fee_wei": maximum_fee_wei,
        "wallet_balance_wei": balance_wei,
    }


def _archive_cancelled_guard(wallet, plan, cancellation_hash, receipt):
    record = dict(wallet.unresolved_broadcast)
    if str(record.get("tx_hash") or "").lower() != plan["guarded_tx_hash"]:
        raise ValueError("unresolved guard changed during cancellation")
    record.update({
        "cancellation_tx_hash": cancellation_hash,
        "cancellation_confirmed_at_unix": int(time.time()),
        "cancellation_block_number": _int(receipt.get("blockNumber", 0)),
        "cancellation_status": "confirmed",
    })
    wallet._persist_transaction_guard(record)
    path = Path(wallet.unresolved_broadcast_path)
    timestamp = int(time.time())
    archived = path.with_name(path.name + f".cancelled.{timestamp}")
    suffix = 1
    while archived.exists():
        archived = path.with_name(path.name + f".cancelled.{timestamp}.{suffix}")
        suffix += 1
    os.replace(path, archived)
    wallet.unresolved_broadcast = None
    return str(archived)


def finalize_recorded_cancellation(wallet, config, confirm_tx_hash):
    """Archive an original guard after its recorded cancellation is proven."""
    guard = getattr(wallet, "unresolved_broadcast", None)
    if not isinstance(guard, dict) or not guard.get("cancellation_tx_hash"):
        raise ValueError("no recorded cancellation attempt exists")
    guarded_hash = str(guard.get("tx_hash") or "").lower()
    if str(confirm_tx_hash or "").lower() != guarded_hash:
        raise ValueError("--confirm-tx-hash must exactly match the guarded hash")
    if _int(guard.get("chain_id", -1)) != int(config.chain_id):
        raise ValueError("guard chain does not match configured chain")
    cancellation_hash = str(guard["cancellation_tx_hash"]).lower()
    tx = _find_transaction(wallet.w3, cancellation_hash)
    receipt = _find_receipt(wallet.w3, cancellation_hash)
    if tx is None or receipt is None:
        raise ValueError("recorded cancellation is not yet receipt-proven")
    if _int(receipt.get("status", 0)) != 1:
        raise ValueError("recorded cancellation did not succeed")
    fingerprint = (
        str(tx.get("from") or "").lower() == wallet.address.lower()
        and str(tx.get("to") or "").lower() == wallet.address.lower()
        and _int(tx.get("nonce", -1)) == _int(guard.get("nonce", -2))
        and _int(tx.get("value", -1)) == 0
        and _int(tx.get("chainId", -1)) == int(config.chain_id)
    )
    if not fingerprint:
        raise ValueError("recorded cancellation transaction fingerprint is invalid")
    plan = {
        "guarded_tx_hash": guarded_hash,
        "chain_id": int(config.chain_id),
        "wallet": wallet.address,
        "nonce": _int(guard["nonce"]),
    }
    archived = _archive_cancelled_guard(
        wallet, plan, cancellation_hash, receipt,
    )
    return {**plan, "cancellation_tx_hash": cancellation_hash, "archived_path": archived}


def execute_cancellation(wallet, config, confirm_tx_hash, now=None):
    """Persist intent, broadcast a zero-value self replacement, and archive on receipt."""
    guard = getattr(wallet, "unresolved_broadcast", None)
    if isinstance(guard, dict) and guard.get("cancellation_tx_hash"):
        return finalize_recorded_cancellation(wallet, config, confirm_tx_hash)
    plan = build_cancellation_plan(wallet, config, now=now)
    if str(confirm_tx_hash or "").lower() != plan["guarded_tx_hash"]:
        raise ValueError("--confirm-tx-hash must exactly match the guarded hash")
    transaction = {
        "chainId": plan["chain_id"],
        "nonce": plan["nonce"],
        "to": Web3.to_checksum_address(wallet.address),
        "value": 0,
        "gas": plan["gas"],
        "gasPrice": plan["gas_price_wei"],
    }
    signed = wallet.account.sign_transaction(transaction)
    raw = getattr(signed, "raw_transaction", getattr(signed, "rawTransaction", None))
    if raw is None:
        raise RuntimeError("signed cancellation has no raw transaction")
    signed_hash = getattr(signed, "hash", None)
    cancellation_hash = (
        signed_hash.hex() if signed_hash is not None else Web3.keccak(raw).hex()
    ).lower()

    # Persist the cancellation identity before the broadcast boundary. If the
    # RPC response is lost, the original guard remains and the exact cancel hash
    # can be receipt-checked without ever retrying automatically.
    record = dict(wallet.unresolved_broadcast)
    record.update({
        "cancellation_tx_hash": cancellation_hash,
        "cancellation_attempted_at_unix": int(time.time()),
        "cancellation_gas_price_wei": plan["gas_price_wei"],
        "cancellation_status": "submission_unknown",
    })
    wallet._persist_transaction_guard(record)
    try:
        returned = wallet.w3.eth.send_raw_transaction(raw)
        if returned.hex().lower() != cancellation_hash:
            raise RuntimeError("RPC returned a different cancellation hash")
        receipt = wallet.w3.eth.wait_for_transaction_receipt(returned, timeout=120)
    except Exception as exc:
        raise RuntimeError(
            f"cancellation outcome is unknown; guard retained with hash {cancellation_hash}: {exc}"
        ) from exc
    if _int(receipt.get("status", 0)) != 1:
        raise RuntimeError(
            f"cancellation {cancellation_hash} failed; original guard remains active"
        )
    archived = _archive_cancelled_guard(
        wallet, plan, cancellation_hash, receipt,
    )
    return {**plan, "cancellation_tx_hash": cancellation_hash, "archived_path": archived}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-bot-stopped", action="store_true")
    parser.add_argument("--confirm-tx-hash")
    args = parser.parse_args()
    if args.execute and not args.confirm_bot_stopped:
        parser.error("--execute requires --confirm-bot-stopped")
    if args.execute and not args.confirm_tx_hash:
        parser.error("--execute requires --confirm-tx-hash")

    from config import load_config
    from wallet import Wallet

    config = load_config()
    wallet = Wallet(config)
    if args.execute:
        result = execute_cancellation(wallet, config, args.confirm_tx_hash)
        print(json.dumps({"mode": "EXECUTED", **result}, separators=(",", ":")))
    else:
        plan = build_cancellation_plan(wallet, config)
        print(json.dumps({"mode": "PREVIEW", **plan}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
