#!/usr/bin/env python3
"""Read-only doctor and inventory probe executed with each bot's Python."""

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def git_value(*args):
    result = subprocess.run(["git", *args], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def load_json(path, default):
    try:
        value = json.loads(Path(path).read_text())
        return value
    except (OSError, json.JSONDecodeError):
        return default


def last_timestamp(path):
    records = load_json(path, [])
    if not isinstance(records, list):
        return None
    values = [str(item.get("timestamp")) for item in records if isinstance(item, dict) and item.get("timestamp")]
    return max(values) if values else None


def add_check(result, name, status, detail):
    result["checks"].append({"name": name, "status": status, "detail": str(detail)})
    if status == "fail":
        result["status"] = "fail"
    elif status == "warn" and result["status"] == "pass":
        result["status"] = "warn"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("doctor", "inventory"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--no-quote", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checkout = Path.cwd().resolve()
    sys.path.insert(0, str(checkout))
    result = {
        "name": args.name,
        "checkout": str(checkout),
        "mode": args.mode,
        "status": "pass",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
    }

    branch = git_value("-C", str(checkout), "symbolic-ref", "--quiet", "--short", "HEAD")
    commit = git_value("-C", str(checkout), "rev-parse", "--short", "HEAD")
    upstream = git_value("-C", str(checkout), "rev-parse", "--abbrev-ref", "@{upstream}")
    dirty = bool(git_value("-C", str(checkout), "status", "--porcelain"))
    ahead = behind = None
    if upstream:
        counts = git_value("-C", str(checkout), "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
        if len(counts) == 2:
            ahead, behind = map(int, counts)
    result["git"] = {"branch": branch or None, "commit": commit or None, "upstream": upstream or None,
                     "dirty": dirty, "ahead": ahead, "behind": behind}

    env_path = checkout / ".env"
    if not env_path.is_file():
        add_check(result, "env", "fail", ".env is missing")
    else:
        mode = stat.S_IMODE(env_path.stat().st_mode)
        result["env_mode"] = f"{mode:04o}"
        add_check(result, "env", "pass" if mode & 0o077 == 0 else "warn", f"mode {mode:04o}")
    add_check(result, "git", "warn" if dirty or not branch or not upstream else "pass",
              f"branch={branch or 'detached'} commit={commit or '?'} upstream={upstream or 'none'} "
              f"ahead={ahead if ahead is not None else '?'} behind={behind if behind is not None else '?'} dirty={dirty}")

    try:
        from config import load_config
        from wallet import Wallet
        from swap_provider import FallbackSwapProvider, create_swap_provider, resolve_provider_name

        config = load_config()
        add_check(result, "config", "pass", "validated")
        result.update({
            "bot_id": config.bot_id,
            "dashboard_name": config.dashboard_name,
            "chain_id": config.chain_id,
            "chain_name": config.chain_name,
            "provider": resolve_provider_name(config),
            "fallback_provider": config.swap_fallback_provider or None,
            "gas_reserve_eth": config.eth_gas_reserve,
        })
        wallet = Wallet(config)
        result["wallet"] = wallet.address
        actual_chain = int(wallet.w3.eth.chain_id)
        add_check(result, "rpc_chain", "pass" if actual_chain == config.chain_id else "fail",
                  f"configured={config.chain_id} actual={actual_chain}")

        assets = []
        seen = set()
        for label, address in ((config.token_symbol, config.token_address), ("USDG", config.usdg_address), ("WETH", config.weth_address)):
            if not address or address.lower() in seen:
                continue
            seen.add(address.lower())
            try:
                code = wallet.w3.eth.get_code(address)
                add_check(result, f"contract_{label}", "pass" if code else "fail", address)
                info = wallet.get_token_info(address)
                balance, raw = wallet.get_token_balance(address)
                assets.append({"label": label, "symbol": info.symbol, "address": address, "decimals": info.decimals,
                               "balance": str(balance), "balance_raw": str(raw)})
            except Exception as exc:
                add_check(result, f"contract_{label}", "fail", exc)
        result["assets"] = assets
        eth_wei = wallet.get_eth_balance_wei()
        reserve_wei = int(float(config.eth_gas_reserve) * 10**18)
        result["native_eth"] = str(eth_wei / 10**18)
        result["native_eth_wei"] = str(eth_wei)
        result["spendable_eth"] = str(max(eth_wei - reserve_wei, 0) / 10**18)

        dashboard_status = "pass" if config.dashboard_url and config.dashboard_api_key else "warn"
        add_check(result, "dashboard", dashboard_status,
                  "configured" if dashboard_status == "pass" else "URL or API key absent")

        if args.mode == "doctor" and not args.no_quote and config.token_address.lower() != config.weth_address.lower():
            try:
                provider = create_swap_provider(config)
                operation = lambda: provider.build_swap_transaction(
                    sell_token=config.weth_address,
                    buy_token=config.token_address,
                    sell_amount=10**15,
                    taker_address=wallet.address,
                    slippage_percentage=0.02,
                )
                quote = provider.run_with_fallback(operation, "doctor route quote") if isinstance(provider, FallbackSwapProvider) else operation()
                add_check(result, "route_quote", "pass" if quote.success else "fail",
                          f"{provider.name}: buy_amount={quote.buy_amount}" if quote.success else quote.error)
            except Exception as exc:
                add_check(result, "route_quote", "fail", exc)

        classic = load_json("data/positions.json", {})
        gridless = load_json("data/gridless_positions.json", {})
        result["positions"] = {
            "classic": len(classic) if isinstance(classic, dict) else None,
            "gridless": len(gridless) if isinstance(gridless, dict) else None,
        }
        result["last_treasury_receipt"] = last_timestamp("data/treasury_transfers.json")
        result["last_liquidation_event"] = last_timestamp("data/asset_liquidations.json")
    except Exception as exc:
        add_check(result, "runtime", "fail", exc)

    if args.json:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(f"{args.name}: {result['status'].upper()} — {checkout}")
        if args.mode == "inventory" and result.get("wallet"):
            print(f"  {result.get('chain_name')} ({result.get('chain_id')})  wallet={result['wallet']}")
            print(f"  ETH={result.get('native_eth')}  spendable={result.get('spendable_eth')}  reserve={result.get('gas_reserve_eth')}")
            for asset in result.get("assets", []):
                print(f"  {asset['label']}={asset['balance']} ({asset['address']})")
            print(f"  positions classic={result['positions']['classic']} gridless={result['positions']['gridless']}")
        else:
            for check in result["checks"]:
                print(f"  {check['status'].upper():4} {check['name']}: {check['detail']}")
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
