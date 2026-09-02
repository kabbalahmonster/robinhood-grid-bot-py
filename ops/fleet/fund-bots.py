#!/usr/bin/env python3
"""Preview or execute guarded treasury-to-fleet native ETH top-ups."""

import argparse
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import dotenv_values
from eth_account import Account
from web3 import Web3


WEI = Decimal(10**18)


def positive_eth(value, label):
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be decimal ETH") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive decimal ETH")
    return int(parsed * WEI)


def load_env(path):
    mode = Path(path).stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            f"Treasury env permissions are too broad ({mode:o}); run chmod 600 {path}"
        )
    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    private_key = values.get("PRIVATE_KEY", "")
    rpc_url = values.get("RPC_URL", "")
    if not private_key or not rpc_url:
        raise ValueError("Treasury env requires PRIVATE_KEY and RPC_URL")
    return values


def current_gas_price(w3, freshness=Decimal("1.01"), minimum_base_fee=0):
    rpc_price = int(w3.eth.gas_price)
    latest = w3.eth.get_block("latest")
    pending = w3.eth.get_block("pending")
    base_fee = max(
        int(latest.get("baseFeePerGas") or 0),
        int(pending.get("baseFeePerGas") or 0),
    )
    return int(Decimal(max(rpc_price, base_fee, int(minimum_base_fee))) * freshness)


def buffered_gas_limit(w3, source, recipient, amount_wei, multiplier=Decimal("1.05")):
    estimate = int(w3.eth.estimate_gas({
        "from": source,
        "to": Web3.to_checksum_address(recipient),
        "value": amount_wei,
    }))
    return max(estimate, int(Decimal(estimate) * multiplier))


def stale_base_fee_error(exc):
    message = str(exc).lower()
    return (
        "max fee per gas less than block base fee" in message
        or "fee cap less than block base fee" in message
    )


def base_fee_from_error(exc):
    matches = re.findall(r"basefee\s*:\s*(\d+)", str(exc), flags=re.IGNORECASE)
    return int(matches[-1]) if matches else 0


def destination_from_env(path):
    values = dotenv_values(path)
    private_key = values.get("PRIVATE_KEY", "")
    if not private_key:
        raise ValueError(f"Bot env has no PRIVATE_KEY: {path}")
    return Account.from_key(private_key).address


def append_receipt(record, path="data/fleet_funding.json"):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if target.exists():
        try:
            records = json.loads(target.read_text())
        except (json.JSONDecodeError, OSError):
            records = []
    records.append(record)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(records, indent=2) + "\n")
    os.replace(temporary, target)


def send_top_up(
    w3,
    account,
    recipient,
    amount_wei,
    chain_id,
    reserve_wei,
    gas_limit_multiplier=Decimal("1.05"),
):
    """Send once, rebuilding exactly once after a pre-broadcast stale-fee rejection."""
    rejected_base_fee = 0
    for attempt in range(2):
        gas_price = current_gas_price(w3, minimum_base_fee=rejected_base_fee)
        gas = buffered_gas_limit(
            w3, account.address, recipient, amount_wei, gas_limit_multiplier
        )
        if amount_wei + gas * gas_price + reserve_wei > int(w3.eth.get_balance(account.address)):
            raise ValueError("Treasury balance no longer covers transfer gas and reserve")
        tx = {
            "from": account.address,
            "to": Web3.to_checksum_address(recipient),
            "value": amount_wei,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": chain_id,
            "gas": gas,
            "gasPrice": gas_price,
        }
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", getattr(signed, "rawTransaction", None))
        try:
            tx_hash = w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            if attempt == 0 and stale_base_fee_error(exc):
                reported_base_fee = base_fee_from_error(exc)
                rejected_base_fee = (reported_base_fee * 102 + 99) // 100
                print("  Gas became stale before broadcast; rebuilding once.")
                continue
            raise
        tx_hash_hex = tx_hash.hex()
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if int(receipt["status"]) != 1:
            raise RuntimeError(f"Transaction {tx_hash_hex} was mined with status=0")
        return tx_hash_hex, tx
    raise AssertionError("unreachable")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-env", required=True)
    parser.add_argument("--target-balance", required=True)
    parser.add_argument("--treasury-reserve")
    parser.add_argument("--confirm-source")
    parser.add_argument("--bot", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    values = load_env(args.from_env)
    account = Account.from_key(values["PRIVATE_KEY"])
    source = account.address
    if args.execute and (args.confirm_source or "").lower() != source.lower():
        raise ValueError(f"Execution requires --confirm-source {source}")
    chain_id = int(values.get("CHAIN_ID", "4663"))
    reserve_text = args.treasury_reserve or values.get("ETH_GAS_RESERVE", "")
    if not reserve_text:
        raise ValueError("Set ETH_GAS_RESERVE in treasury env or pass --treasury-reserve")
    reserve_wei = positive_eth(reserve_text, "treasury reserve")
    target_wei = positive_eth(args.target_balance, "target balance")
    try:
        gas_limit_multiplier = Decimal(values.get("GAS_LIMIT_MULTIPLIER", "1.05"))
    except InvalidOperation as exc:
        raise ValueError("GAS_LIMIT_MULTIPLIER must be decimal") from exc
    if not gas_limit_multiplier.is_finite() or gas_limit_multiplier < 1:
        raise ValueError("GAS_LIMIT_MULTIPLIER must be at least 1")

    w3 = Web3(Web3.HTTPProvider(values["RPC_URL"], request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError("Could not connect to treasury RPC_URL")
    if int(w3.eth.chain_id) != chain_id:
        raise ValueError(f"RPC chain ID {w3.eth.chain_id} does not match CHAIN_ID={chain_id}")

    destinations = []
    seen = set()
    for item in args.bot:
        name, separator, path = item.partition("=")
        if not separator or not name or not path:
            raise ValueError("--bot must be NAME=ENV_PATH")
        recipient = destination_from_env(path)
        if recipient.lower() == source.lower():
            raise ValueError(f"Refusing self-funding target: {name}")
        if recipient.lower() in seen:
            raise ValueError(f"Duplicate destination wallet: {name} ({recipient})")
        seen.add(recipient.lower())
        balance = int(w3.eth.get_balance(recipient))
        amount = max(0, target_wei - balance)
        gas_limit = (
            buffered_gas_limit(w3, source, recipient, amount, gas_limit_multiplier)
            if amount else 0
        )
        destinations.append((name, recipient, balance, amount, gas_limit))
    if not destinations:
        raise ValueError("At least one --bot is required")

    gas_price = current_gas_price(w3)
    total_send = sum(item[3] for item in destinations)
    maximum_fees = sum(item[4] * gas_price for item in destinations)
    source_balance = int(w3.eth.get_balance(source))
    required = total_send + maximum_fees + reserve_wei

    print(f"FLEET FUNDING PLAN: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Source: {source}")
    print(f"Source balance: {Decimal(source_balance) / WEI} ETH")
    print(f"Target balance per bot: {Decimal(target_wei) / WEI} ETH")
    print(f"Treasury reserve: {Decimal(reserve_wei) / WEI} ETH")
    for name, recipient, balance, amount, gas_limit in destinations:
        print(
            f"- {name}: {recipient} balance={Decimal(balance) / WEI} "
            f"top_up={Decimal(amount) / WEI} ETH gas_limit={gas_limit}"
        )
    print(f"Total top-ups: {Decimal(total_send) / WEI} ETH")
    print(f"Maximum planned gas: {Decimal(maximum_fees) / WEI} ETH")
    print(f"Maximum required with reserve: {Decimal(required) / WEI} ETH")
    if required > source_balance:
        raise ValueError("Treasury balance cannot cover top-ups, planned gas, and reserve")
    if not args.execute:
        print(f"DRY RUN: no broadcasts. Execute with --confirm-source {source} --execute.")
        return 0

    completed = 0
    for name, recipient, _balance, _planned_amount, _planned_gas in destinations:
        current_balance = int(w3.eth.get_balance(recipient))
        amount = max(0, target_wei - current_balance)
        if amount == 0:
            print(f"SKIP {name}: already at or above target")
            continue
        refreshed_gas = buffered_gas_limit(
            w3, source, recipient, amount, gas_limit_multiplier
        )
        refreshed_fee = refreshed_gas * current_gas_price(w3)
        current_source_balance = int(w3.eth.get_balance(source))
        if amount + refreshed_fee + reserve_wei > current_source_balance:
            raise RuntimeError(
                f"Funding stopped after {completed} confirmed transfer(s); treasury can no "
                f"longer fund {name} while preserving its reserve"
            )
        print(f"FUND {name}: {Decimal(amount) / WEI} ETH -> {recipient}")
        try:
            tx_hash, tx = send_top_up(
                w3,
                account,
                recipient,
                amount,
                chain_id,
                reserve_wei,
                gas_limit_multiplier,
            )
        except Exception as exc:
            append_receipt({
                "timestamp": datetime.now().astimezone().isoformat(),
                "source": source,
                "bot": name,
                "recipient": recipient,
                "amount_eth": str(Decimal(amount) / WEI),
                "success": False,
                "error": str(exc),
            })
            raise RuntimeError(
                f"Funding stopped after {completed} confirmed transfer(s); {name} failed: {exc}"
            ) from exc
        append_receipt({
            "timestamp": datetime.now().astimezone().isoformat(),
            "source": source,
            "bot": name,
            "recipient": recipient,
            "amount_eth": str(Decimal(amount) / WEI),
            "estimated_max_gas_eth": str(Decimal(tx["gas"] * tx["gasPrice"]) / WEI),
            "success": True,
            "tx_hash": tx_hash,
        })
        completed += 1
        print(f"CONFIRMED {name}: {tx_hash}")
    print(f"Fleet funding complete: {completed} transfer(s) confirmed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FLEET FUNDING REFUSED: {exc}")
        raise SystemExit(1)
