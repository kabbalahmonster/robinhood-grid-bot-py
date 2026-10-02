#!/usr/bin/env python3
"""Render a fleet-wide balance report from read-only bot probe results."""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


def decimal_text(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def asset_for_role(report, role):
    assets = report.get("assets") or []
    explicit = next(
        (
            asset for asset in assets
            if asset.get("role") == role or role in (asset.get("roles") or [])
        ),
        None,
    )
    if explicit is not None:
        return explicit
    # Older fleet checkouts do not emit role yet. Preserve compatibility with
    # their labels while they are being rolled forward.
    if role == "usdg":
        return next((asset for asset in assets if asset.get("label") == "USDG"), None)
    if role == "managed":
        return next(
            (asset for asset in assets if asset.get("label") not in {"USDG", "WETH"}),
            None,
        )
    return None


def public_asset(asset):
    if asset is None:
        return None
    return {
        "symbol": asset.get("symbol") or asset.get("label") or "?",
        "address": asset.get("address"),
        "decimals": asset.get("decimals"),
        "balance": str(asset.get("balance", "0")),
        "balance_raw": str(asset.get("balance_raw", "0")),
    }


def failed_checks(report):
    return [
        {"name": item.get("name"), "detail": item.get("detail")}
        for item in report.get("checks", [])
        if item.get("status") == "fail"
    ]


def build_report(probes):
    bots = []
    native_totals = defaultdict(int)
    asset_totals = {"usdg": {}, "managed": {}}

    for probe in probes:
        chain_id = probe.get("chain_id")
        chain_name = probe.get("chain_name") or "unknown"
        native_wei = probe.get("native_eth_wei")
        if chain_id is not None and native_wei is not None:
            native_totals[(chain_id, chain_name)] += int(native_wei)

        selected = {}
        for role in ("usdg", "managed"):
            asset = public_asset(asset_for_role(probe, role))
            selected[role] = asset
            if asset is None or chain_id is None or not asset.get("address"):
                continue
            key = (
                chain_id,
                chain_name,
                asset["address"].lower(),
                asset["symbol"],
                int(asset["decimals"]),
            )
            current = asset_totals[role].setdefault(key, 0)
            asset_totals[role][key] = current + int(asset["balance_raw"])

        bots.append({
            "name": probe.get("name") or "?",
            "status": probe.get("status") or "fail",
            "chain_id": chain_id,
            "chain_name": chain_name,
            "wallet": probe.get("wallet"),
            "eth": {
                "balance": str(probe.get("native_eth", "unknown")),
                "balance_wei": str(native_wei) if native_wei is not None else None,
            },
            "usdg": selected["usdg"],
            "managed_token": selected["managed"],
            "errors": failed_checks(probe),
        })

    def native_rows():
        return [
            {
                "chain_id": chain_id,
                "chain_name": chain_name,
                "balance": decimal_text(Decimal(value) / Decimal(10**18)),
                "balance_wei": str(value),
            }
            for (chain_id, chain_name), value in sorted(native_totals.items())
        ]

    def asset_rows(role):
        rows = []
        for (chain_id, chain_name, address, symbol, decimals), value in sorted(
            asset_totals[role].items()
        ):
            rows.append({
                "chain_id": chain_id,
                "chain_name": chain_name,
                "symbol": symbol,
                "address": address,
                "decimals": decimals,
                "balance": decimal_text(Decimal(value) / (Decimal(10) ** decimals)),
                "balance_raw": str(value),
            })
        return rows

    statuses = {bot["status"] for bot in bots}
    overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": overall,
        "bots": bots,
        "totals": {
            "native_eth": native_rows(),
            "usdg": asset_rows("usdg"),
            "managed_tokens": asset_rows("managed"),
        },
    }


def render_asset(asset, default_symbol):
    if asset is None:
        return f"{default_symbol}=unavailable"
    return f"{asset['symbol']}={decimal_text(asset['balance'])}"


def render_progress(probe):
    """Render one compact line after a bot's live RPC probe completes."""
    bot = build_report([probe])["bots"][0]
    line = (
        f"{bot['status'].upper()} | ETH={decimal_text(bot['eth']['balance'])} | "
        f"{render_asset(bot['usdg'], 'USDG')} | "
        f"{render_asset(bot['managed_token'], 'managed')}"
    )
    if bot["errors"]:
        error = bot["errors"][0]
        line += f" | {error['name']}: {error['detail']}"
    return line


def render_human(report):
    lines = [
        f"Fleet balance report: {report['status'].upper()}",
        f"Bots: {len(report['bots'])}  Checked: {report['generated_at']}",
        "",
    ]
    for bot in report["bots"]:
        lines.append(
            f"{bot['name']} [{bot['status'].upper()}] | "
            f"ETH={decimal_text(bot['eth']['balance'])} | "
            f"{render_asset(bot['usdg'], 'USDG')} | "
            f"{render_asset(bot['managed_token'], 'managed')}"
        )
        lines.append(
            f"  {bot['chain_name']} ({bot['chain_id'] or '?'})  wallet={bot['wallet'] or 'unavailable'}"
        )
        for error in bot["errors"]:
            lines.append(f"  ERROR {error['name']}: {error['detail']}")

    lines.extend(["", "Fleet totals:"])
    for item in report["totals"]["native_eth"]:
        lines.append(f"  {item['chain_name']} ({item['chain_id']}) ETH={item['balance']}")
    for heading, key in (("USDG", "usdg"), ("managed", "managed_tokens")):
        for item in report["totals"][key]:
            lines.append(
                f"  {item['chain_name']} ({item['chain_id']}) {heading} "
                f"{item['symbol']}={item['balance']} ({item['address']})"
            )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--progress", action="store_true")
    parser.add_argument("reports", nargs="+")
    args = parser.parse_args()
    probes = []
    for path in args.reports:
        try:
            probes.append(json.loads(Path(path).read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read probe report {path}: {exc}", file=sys.stderr)
            return 2
    if args.progress:
        if len(probes) != 1:
            print("--progress requires exactly one probe report", file=sys.stderr)
            return 2
        print(render_progress(probes[0]))
        return 0
    report = build_report(probes)
    if args.json:
        print(json.dumps(report, separators=(",", ":")))
    else:
        print(render_human(report))
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
