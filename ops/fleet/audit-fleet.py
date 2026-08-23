#!/usr/bin/env python3
"""Reconcile local fleet transaction logs without retrying anything."""

import argparse
import json
import re
from pathlib import Path


EXPLORERS = {1: "https://etherscan.io/tx/", 8453: "https://basescan.org/tx/", 4663: "https://robinhoodchain.blockscout.com/tx/"}


def read_list(path):
    try:
        value = json.loads(path.read_text())
        return (value, None) if isinstance(value, list) else ([], f"audit log is not a JSON list: {path}")
    except FileNotFoundError:
        return [], None
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"cannot read audit log {path}: {exc}"


def chain_id(checkout):
    try:
        text = (checkout / ".env").read_text()
    except OSError:
        return None
    match = re.search(r"(?m)^\s*CHAIN_ID\s*=\s*([0-9]+)\s*(?:#.*)?$", text)
    return int(match.group(1)) if match else 4663


def tx_link(chain, tx_hash):
    return EXPLORERS.get(chain, "") + tx_hash if tx_hash and chain in EXPLORERS else tx_hash


def audit_bot(name, checkout):
    chain = chain_id(checkout)
    treasury, treasury_error = read_list(checkout / "data/treasury_transfers.json")
    liquidations, liquidation_error = read_list(checkout / "data/asset_liquidations.json")
    events = []
    attention = [error for error in (treasury_error, liquidation_error) if error]
    for record in treasury:
        if not isinstance(record, dict):
            continue
        item = {
            "kind": "treasury",
            "timestamp": record.get("timestamp"),
            "success": bool(record.get("success")),
            "asset": record.get("token"),
            "amount": record.get("amount"),
            "recipient": record.get("recipient"),
            "tx_hash": record.get("tx_hash"),
            "explorer": tx_link(chain, record.get("tx_hash")),
            "error": record.get("error"),
        }
        events.append(item)
        if not item["success"]:
            attention.append("failed treasury transfer")

    runs = {}
    for record in liquidations:
        if not isinstance(record, dict):
            continue
        run_id = record.get("run_id") or "legacy"
        runs.setdefault(run_id, []).append(record)
        event = record.get("event") or ("complete" if record.get("success") else "failed")
        if event == "asset_result":
            asset = record.get("asset") if isinstance(record.get("asset"), dict) else {}
            events.append({
                "kind": "liquidation_asset",
                "timestamp": record.get("timestamp"),
                "run_id": run_id,
                "success": bool(asset.get("success")),
                "asset": asset.get("label"),
                "tx_hash": asset.get("tx_hash"),
                "explorer": tx_link(chain, asset.get("tx_hash")),
                "error": asset.get("error"),
            })
        if event in {"complete", "failed"}:
            events.append({
                "kind": "liquidation",
                "timestamp": record.get("timestamp"),
                "run_id": run_id,
                "success": bool(record.get("success")),
                "positions_cleared": bool(record.get("positions_cleared")),
                "error": record.get("error"),
            })
    for run_id, records in runs.items():
        terminal = [r for r in records if r.get("event") in {"complete", "failed"}]
        if not terminal:
            attention.append(f"incomplete liquidation run {run_id}")
        elif terminal[-1].get("event") == "failed" or not terminal[-1].get("success"):
            attention.append(f"failed liquidation run {run_id}")
        elif not terminal[-1].get("positions_cleared"):
            attention.append(f"positions not cleared for liquidation run {run_id}")

    return {
        "name": name,
        "checkout": str(checkout),
        "chain_id": chain,
        "status": "attention" if attention else "ok",
        "attention": sorted(set(attention)),
        "events": sorted(events, key=lambda e: str(e.get("timestamp") or "")),
        "treasury_records": len(treasury),
        "liquidation_records": len(liquidations),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--failures-only", action="store_true")
    parser.add_argument("--emit-only", action="store_true", help="Print comma-separated bot names requiring attention")
    args = parser.parse_args()
    results = []
    for spec in args.bot:
        name, separator, path = spec.partition("=")
        if not separator:
            parser.error("--bot requires NAME=PATH")
        results.append(audit_bot(name, Path(path)))
    attention_names = [item["name"] for item in results if item["status"] == "attention"]
    if args.emit_only:
        print(",".join(attention_names))
        return 0
    shown = [item for item in results if not args.failures_only or item["status"] == "attention"]
    if args.json:
        print(json.dumps(shown, indent=2))
    else:
        for item in shown:
            print(f"{item['name']}: {item['status'].upper()} — treasury={item['treasury_records']} liquidation={item['liquidation_records']}")
            for warning in item["attention"]:
                print(f"  ATTENTION: {warning}")
            for event in item["events"]:
                if event["kind"] == "treasury":
                    state = "CONFIRMED" if event["success"] else "FAILED"
                    print(f"  {state} treasury {event.get('asset')} {event.get('amount')} -> {event.get('recipient')} {event.get('explorer') or event.get('error') or ''}")
                elif event["kind"] == "liquidation_asset":
                    state = "CONFIRMED" if event["success"] else "FAILED"
                    print(f"  {state} liquidation asset {event.get('asset')} {event.get('explorer') or event.get('error') or ''}")
                else:
                    state = "COMPLETE" if event["success"] else "FAILED"
                    print(f"  {state} liquidation {event.get('run_id')} positions_cleared={event.get('positions_cleared')}")
    return 1 if attention_names else 0


if __name__ == "__main__":
    raise SystemExit(main())
