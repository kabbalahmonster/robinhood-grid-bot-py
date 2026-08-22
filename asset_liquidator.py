"""Guarded conversion of every bot-managed ERC-20 balance into native ETH."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from web3 import Web3

from config import load_config
from swap_provider import FallbackSwapProvider, create_swap_provider
from wallet import TransactionResult, Wallet


NATIVE_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"
POSITION_FILES = (Path("data/positions.json"), Path("data/gridless_positions.json"))
AUDIT_FILE = Path("data/asset_liquidations.json")


@dataclass
class LiquidationResult:
    success: bool
    error: str | None = None
    tx_hash: str | None = None
    quoted_output_wei: int = 0


def _managed_assets(config: Any, keep_usdg: bool = False) -> list[tuple[str, str]]:
    """Return configured assets once each; WETH is always handled as WETH."""
    candidates = [
        (getattr(config, "token_symbol", "TOKEN"), config.token_address),
        ("WETH", config.weth_address),
    ]
    if not keep_usdg:
        candidates.insert(1, ("USDG", config.usdg_address))
    weth_key = config.weth_address.lower()
    unique: dict[str, tuple[str, str]] = {}
    for label, address in candidates:
        key = address.lower()
        if key not in unique or key == weth_key:
            unique[key] = ("WETH" if key == weth_key else label, address)
    return list(unique.values())


def _append_audit(record: dict[str, Any], path: Path = AUDIT_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[Any] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, list):
                records = loaded
        except (OSError, json.JSONDecodeError):
            records = []
    records.append(record)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(records, indent=2) + "\n")
    os.replace(temp, path)


def _clear_positions(paths: tuple[Path, ...] | None = None) -> list[str]:
    """Back up and atomically clear all existing position stores."""
    paths = POSITION_FILES if paths is None else paths
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: list[tuple[Path, Path]] = []
    try:
        for path in paths:
            if not path.exists():
                continue
            backup = path.with_name(f"{path.name}.pre-liquidation.{stamp}.bak")
            shutil.copy2(path, backup)
            backups.append((path, backup))
        for path, _ in backups:
            temp = path.with_name(path.name + ".tmp")
            temp.write_text("{}\n")
            os.replace(temp, path)
        return [str(backup) for _, backup in backups]
    except Exception:
        for path, backup in backups:
            if backup.exists():
                shutil.copy2(backup, path)
        raise


def _send_quote(wallet: Wallet, config: Any, quote: Any) -> TransactionResult:
    gas_limit = int((quote.gas or 300000) * max(float(config.gas_limit_multiplier), 1.0))
    source_price = quote.gas_price or wallet.w3.eth.gas_price
    gas_price = int(source_price * max(float(config.gas_price_multiplier), 1.0))
    return wallet._send_transaction({
        "from": Web3.to_checksum_address(wallet.address),
        "to": Web3.to_checksum_address(quote.to),
        "data": quote.data,
        "value": quote.value or 0,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "nonce": wallet.w3.eth.get_transaction_count(wallet.address),
        "chainId": config.chain_id,
    })


def _execute_swap_attempt(provider: Any, wallet: Wallet, config: Any, token: str, amount: int) -> LiquidationResult:
    if provider.capabilities.api_managed_approval:
        quote = provider.execute_sell_with_approval(
            sell_token=token,
            buy_token=NATIVE_ETH_ADDRESS,
            sell_amount=amount,
            taker_address=wallet.address,
            wallet=wallet,
            slippage_percentage=0.02,
        )
    else:
        quote = provider.build_swap_transaction(
            sell_token=token,
            buy_token=NATIVE_ETH_ADDRESS,
            sell_amount=amount,
            taker_address=wallet.address,
            slippage_percentage=0.02,
        )
        if quote.success:
            spender = quote.allowance_target or config.zero_x_proxy
            if wallet.check_allowance(token, spender, use_permit2=False) < amount:
                approval = wallet.approve_token(token, spender, 2**256 - 1)
                if not approval.success:
                    return LiquidationResult(False, f"approval failed: {approval.error}")
                if provider.capabilities.refresh_after_approval:
                    quote = provider.refresh_quote(
                        sell_token=token,
                        buy_token=NATIVE_ETH_ADDRESS,
                        sell_amount=amount,
                        taker_address=wallet.address,
                        slippage_percentage=0.02,
                    )
            if quote.success and provider.capabilities.quote_requires_preparation:
                quote = provider.prepare_swap(quote)
    if not quote.success:
        return LiquidationResult(False, quote.error or "quote/swap preparation failed")
    sent = _send_quote(wallet, config, quote)
    return LiquidationResult(
        sent.success,
        sent.error,
        sent.tx_hash,
        int(quote.buy_amount or 0),
    )


def _execute_swap(provider: Any, wallet: Wallet, config: Any, token: str, amount: int) -> LiquidationResult:
    operation = lambda: _execute_swap_attempt(provider, wallet, config, token, amount)
    if isinstance(provider, FallbackSwapProvider):
        return provider.run_with_fallback(operation, "asset liquidation")
    return operation()


def _plan_quote(provider: Any, wallet: Wallet, token: str, amount: int) -> Any:
    operation = lambda: provider.build_swap_transaction(
        sell_token=token,
        buy_token=NATIVE_ETH_ADDRESS,
        sell_amount=amount,
        taker_address=wallet.address,
        slippage_percentage=0.02,
    )
    if isinstance(provider, FallbackSwapProvider):
        return provider.run_with_fallback(operation, "asset liquidation quote")
    return operation()


def run_asset_liquidation(args: Any) -> int:
    """Plan or execute liquidation, clearing positions only after verification."""
    if not args.confirm_liquidate_assets:
        print("ERROR: --liquidate-assets requires --confirm-liquidate-assets")
        return 2
    if args.execute and not args.confirm_bot_stopped:
        print("ERROR: --execute requires --confirm-bot-stopped")
        return 2

    try:
        config = load_config()
        wallet = Wallet(config)
        provider = create_swap_provider(config)
        keep_usdg = bool(getattr(args, "keep_usdg", False))
        assets = _managed_assets(config, keep_usdg=keep_usdg)
        balances: list[tuple[str, str, int, int]] = []
        for label, address in assets:
            info = wallet.get_token_info(address)
            _, raw = wallet.get_token_balance(address)
            balances.append((label or info.symbol, address, info.decimals, int(raw)))

        print(f"Managed-asset liquidation: {'EXECUTE' if args.execute else 'DRY RUN'}")
        print(f"Wallet: {wallet.address}")
        print("Destination: native ETH in the same wallet")
        print("Unknown/airdrop tokens: ignored")
        print(f"USDG: {'kept unchanged' if keep_usdg else 'included in liquidation'}")

        plans: list[dict[str, Any]] = []
        for label, address, decimals, raw in balances:
            human = raw / (10**decimals)
            if raw == 0:
                print(f"- {label}: 0 (skip)")
                continue
            if address.lower() == config.weth_address.lower():
                print(f"- {label}: unwrap {human:.{min(decimals, 12)}f} to native ETH")
                plans.append({"label": label, "address": address, "amount_raw": raw, "action": "unwrap"})
            else:
                quote = _plan_quote(provider, wallet, address, raw)
                if not quote.success:
                    raise RuntimeError(f"{label} quote failed: {quote.error}")
                print(f"- {label}: sell {human:.{min(decimals, 12)}f}; quoted native output {int(quote.buy_amount or 0) / 1e18:.12f} ETH")
                plans.append({"label": label, "address": address, "amount_raw": raw, "action": "swap"})

        if not args.execute:
            print("DRY RUN ONLY: no approval, swap, unwrap, or position-file change was made.")
            print("Successful execution will back up and clear both position stores.")
            return 0

        receipts: list[dict[str, Any]] = []
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        for plan in plans:
            if plan["action"] == "unwrap":
                tx = wallet.build_weth_withdraw_transaction(plan["address"], plan["amount_raw"])
                sent = wallet.unwrap_weth(tx)
                result = LiquidationResult(sent.success, sent.error, sent.tx_hash, plan["amount_raw"])
            else:
                result = _execute_swap(provider, wallet, config, plan["address"], plan["amount_raw"])
            receipt_record = {
                **plan,
                "success": result.success,
                "tx_hash": result.tx_hash,
                "error": result.error,
                "quoted_output_wei": result.quoted_output_wei,
            }
            receipts.append(receipt_record)
            _append_audit({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "wallet": wallet.address,
                "event": "asset_result",
                "positions_cleared": False,
                "asset": receipt_record,
            })
            if not result.success:
                raise RuntimeError(f"{plan['label']} liquidation failed: {result.error}")

        residuals = []
        for label, address, _, _ in balances:
            _, raw = wallet.get_token_balance(address)
            if int(raw) != 0:
                residuals.append(f"{label}={raw} raw units")
        if residuals:
            raise RuntimeError("final managed-token balances are not zero: " + ", ".join(residuals))

        backups = _clear_positions()
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "wallet": wallet.address,
            "event": "complete",
            "success": True,
            "assets": receipts,
            "position_backups": backups,
            "positions_cleared": True,
        }
        _append_audit(record)
        print("SUCCESS: every nonzero managed asset converted to native ETH and verified at zero.")
        print("Position stores cleared after atomic backups: " + (", ".join(backups) if backups else "no existing stores"))
        return 0
    except Exception as exc:
        if getattr(args, "execute", False):
            try:
                _append_audit({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "run_id": locals().get("run_id"),
                    "wallet": getattr(locals().get("wallet"), "address", None),
                    "event": "failed",
                    "success": False,
                    "error": str(exc),
                    "positions_cleared": False,
                    "assets": locals().get("receipts", []),
                })
            except Exception:
                pass
        print(f"ERROR: {exc}")
        print("Position data was NOT cleared.")
        return 2
