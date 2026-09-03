"""Guarded sale of token balance not allocated to an open position."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web3 import Web3

from config import load_config
from swap_provider import FallbackSwapProvider, create_swap_provider
from wallet import Wallet


UNISWAP_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

@dataclass(frozen=True)
class MoonbagAllocation:
    wallet_raw: int
    allocated_raw: int
    moonbag_raw: int


def _position_path(config: Any) -> Path:
    return Path("data/gridless_positions.json" if getattr(config, "use_gridless", False) else "data/positions.json")


def _allocated_position_balance(path: Path) -> int:
    """Read the authoritative position store and fail closed on malformed data."""
    if not path.exists():
        raise ValueError(f"position store does not exist: {path}")
    try:
        positions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read position store {path}: {exc}") from exc
    if not isinstance(positions, dict):
        raise ValueError(f"position store must contain an object: {path}")

    total = 0
    for position_id, position in positions.items():
        if not isinstance(position, dict):
            raise ValueError(f"position {position_id!r} is not an object")
        balance = position.get("balance", 0)
        if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
            raise ValueError(f"position {position_id!r} has an invalid raw balance")
        total += balance
    return total


def calculate_moonbag(wallet_raw: int, allocated_raw: int) -> MoonbagAllocation:
    wallet_raw = int(wallet_raw)
    allocated_raw = int(allocated_raw)
    if wallet_raw < 0 or allocated_raw < 0:
        raise ValueError("wallet and allocated balances must be non-negative")
    if allocated_raw > wallet_raw:
        raise ValueError(
            "position accounting exceeds the wallet token balance; reconcile positions before selling"
        )
    return MoonbagAllocation(wallet_raw, allocated_raw, wallet_raw - allocated_raw)


def _slippage_fraction(config: Any) -> float:
    market = float(getattr(config, "slippage_tolerance", 2.0))
    transfer_fee = float(getattr(config, "token_transfer_fee_percent", 0.0)) if getattr(config, "taxed_token", False) else 0.0
    buffer = float(getattr(config, "taxed_token_slippage_buffer_percent", 0.0)) if transfer_fee > 0 else 0.0
    return min(15.0, max(0.0, transfer_fee + buffer if transfer_fee > 0 else market)) / 100.0


def _effective_quote_output(config: Any, quote: Any) -> int:
    output = int(quote.buy_amount or 0)
    if not getattr(config, "taxed_token", False):
        return output
    fee = float(getattr(config, "token_transfer_fee_percent", 0.0)) / 100.0
    return int(output * (1.0 - fee))


def _gas_fields(wallet: Wallet, config: Any, quote: Any) -> tuple[int, int]:
    gas_limit = int((quote.gas or 300000) * max(float(config.gas_limit_multiplier), 1.0))
    normal_gas_price = int(wallet.normal_gas_price())
    gas_price = int(
        normal_gas_price
        * max(float(config.gas_price_multiplier), 1.0)
        * max(float(getattr(config, "gas_price_freshness_multiplier", 1.01)), 1.0)
    )
    return gas_limit, gas_price


def _validate_economics(
    wallet: Wallet,
    config: Any,
    quote: Any,
    setup_gas_wei: int = 0,
    reserve_setup_gas_wei: int | None = None,
) -> tuple[int, int, int]:
    gas_limit, gas_price = _gas_fields(wallet, config, quote)
    gas_wei = gas_limit * gas_price
    cap_eth = float(getattr(config, "max_sell_gas_eth", getattr(config, "max_swap_gas_eth", 0.00004)))
    if cap_eth > 0 and gas_wei > int(cap_eth * 10**18):
        raise ValueError(
            f"projected sell gas {gas_wei / 1e18:.8f} ETH exceeds cap {cap_eth:.8f} ETH"
        )
    quoted_output = _effective_quote_output(config, quote)
    if quoted_output <= gas_wei + int(setup_gas_wei):
        raise ValueError("quoted output does not exceed projected setup and swap gas")
    reserve_wei = int(float(getattr(config, "eth_gas_reserve", 0.0005)) * 10**18)
    reserve_setup = int(setup_gas_wei) if reserve_setup_gas_wei is None else int(reserve_setup_gas_wei)
    if wallet.get_eth_balance_wei() - gas_wei - reserve_setup < reserve_wei:
        raise ValueError("sale would breach ETH_GAS_RESERVE")
    return gas_limit, gas_price, gas_wei


def _build_quote(provider: Any, wallet: Wallet, config: Any, amount: int) -> Any:
    buy_token = UNISWAP_ETH_ADDRESS if getattr(config, "use_eth_trading", False) else config.weth_address
    quote = provider.build_swap_transaction(
        sell_token=config.token_address,
        buy_token=buy_token,
        sell_amount=amount,
        taker_address=wallet.address,
        slippage_percentage=_slippage_fraction(config),
    )
    return quote


def _select_provider_quote(provider: Any, wallet: Wallet, config: Any, amount: int) -> tuple[Any, Any]:
    """Select one provider before any approval so calldata never crosses providers."""
    candidates = [provider.primary, provider.fallback] if isinstance(provider, FallbackSwapProvider) else [provider]
    errors = []
    for candidate in candidates:
        quote = _build_quote(candidate, wallet, config, amount)
        if quote.success:
            return candidate, quote
        errors.append(f"{candidate.name}: {quote.error or 'quote failed'}")
    raise ValueError("; ".join(errors))


def _send_quote(wallet: Wallet, config: Any, quote: Any, gas_limit: int, gas_price: int):
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


def _treasury_recipient(wallet: Wallet, config: Any, args: Any) -> str:
    recipient_text = getattr(args, "recipient", None)
    if not recipient_text or not Web3.is_address(recipient_text):
        raise ValueError("--send-to-treasury requires a valid --recipient")
    recipient = Web3.to_checksum_address(recipient_text)
    if recipient.lower() == wallet.address.lower():
        raise ValueError("treasury recipient cannot be the bot wallet")
    allowed = {str(address).lower() for address in getattr(config, "treasury_allowed_recipients", [])}
    if recipient.lower() not in allowed and str(getattr(args, "confirm_recipient", "") or "").lower() != recipient.lower():
        raise ValueError(
            "treasury recipient is not allowlisted; repeat it exactly with --confirm-recipient"
        )
    if wallet.address_has_code(recipient):
        raise ValueError("treasury forwarding requires an externally owned recipient")
    return recipient


def _forward_actual_proceeds(
    wallet: Wallet,
    config: Any,
    recipient: str,
    eth_before_sale: int,
    weth_before_sale: int | None,
) -> tuple[Any, int]:
    """Forward only net ETH created by this sale, preserving prior wallet ETH."""
    if weth_before_sale is not None:
        _, weth_after_sale = wallet.get_token_balance(config.weth_address)
        received_weth = int(weth_after_sale) - int(weth_before_sale)
        if received_weth <= 0:
            raise ValueError("sale produced no measurable WETH to unwrap")
        unwrap_tx = wallet.build_weth_withdraw_transaction(config.weth_address, received_weth)
        reserve_wei = int(float(getattr(config, "eth_gas_reserve", 0.0005)) * 10**18)
        unwrap_max_fee = int(unwrap_tx["gas"]) * int(unwrap_tx["gasPrice"])
        if wallet.get_eth_balance_wei() - unwrap_max_fee < reserve_wei:
            raise ValueError("WETH unwrap gas would breach ETH_GAS_RESERVE")
        unwrap_result = wallet.unwrap_weth(unwrap_tx, wait_for_receipt=True)
        if not unwrap_result.success:
            raise ValueError(f"WETH unwrap failed: {unwrap_result.error}")

    eth_after_settlement = int(wallet.get_eth_balance_wei())
    net_sale_proceeds = eth_after_settlement - int(eth_before_sale)
    if net_sale_proceeds <= 0:
        raise ValueError("sale produced no net native ETH after execution gas")

    transfer_tx = wallet.build_eth_transfer_transaction(recipient, 1)
    transfer_max_fee = int(transfer_tx["gas"]) * int(transfer_tx["gasPrice"])
    amount_wei = net_sale_proceeds - transfer_max_fee
    if amount_wei <= 0:
        raise ValueError("net moonbag proceeds do not cover treasury transfer gas")
    reserve_wei = int(float(getattr(config, "eth_gas_reserve", 0.0005)) * 10**18)
    if eth_after_settlement - amount_wei - transfer_max_fee < reserve_wei:
        raise ValueError("treasury forwarding would breach ETH_GAS_RESERVE")
    transfer_tx["value"] = amount_wei
    result = wallet.transfer_eth(transfer_tx, wait_for_receipt=True)
    if not result.success:
        raise ValueError(f"treasury transfer failed: {result.error}")
    return result, amount_wei


def _build_api_transaction(wallet: Wallet, config: Any, api_tx: dict[str, Any]) -> dict[str, Any]:
    """Build an API-supplied approval/reset with fresh EIP-1559 fees."""
    latest_block = wallet.w3.eth.get_block("latest")
    base_fee = int(latest_block.get("baseFeePerGas", 0))
    try:
        priority_fee = max(int(wallet.w3.eth.max_priority_fee), 1_000_000)
    except Exception:
        priority_fee = 1_000_000
    tx = {
        "from": Web3.to_checksum_address(api_tx.get("from", wallet.address)),
        "to": Web3.to_checksum_address(api_tx["to"]),
        "data": api_tx.get("data", "0x"),
        "value": int(api_tx.get("value", "0x0"), 16) if isinstance(api_tx.get("value"), str) else int(api_tx.get("value", 0)),
        "chainId": int(api_tx.get("chainId", config.chain_id)),
        "nonce": wallet.w3.eth.get_transaction_count(wallet.address, "pending"),
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": base_fee * 2 + priority_fee,
        "type": 2,
    }
    try:
        tx["gas"] = int(wallet.w3.eth.estimate_gas(tx) * 1.2)
    except Exception:
        tx["gas"] = int(api_tx.get("gas", 100000))
    return tx


def _receipt_gas_wei(result: Any, fallback: int) -> int:
    receipt = getattr(result, "receipt", None) or {}
    gas_used = receipt.get("gasUsed", getattr(result, "gas_used", None))
    gas_price = receipt.get("effectiveGasPrice", getattr(result, "effective_gas_price", None))
    return int(gas_used) * int(gas_price) if gas_used is not None and gas_price is not None else int(fallback)


def _execute_quote(provider: Any, wallet: Wallet, config: Any, amount: int, quote: Any):
    _validate_economics(wallet, config, quote)
    buy_token = UNISWAP_ETH_ADDRESS if getattr(config, "use_eth_trading", False) else config.weth_address

    if provider.capabilities.api_managed_approval:
        approval_plan = provider.check_approval(
            token=config.token_address, amount=amount, wallet=wallet.address
        )
        if "error" in approval_plan:
            raise ValueError(f"approval check failed: {approval_plan['error']}")
        projected_setup_gas = 0
        prepared_approvals = []
        for label in ("cancel", "approval"):
            if approval_plan.get(label) is None:
                continue
            tx = _build_api_transaction(wallet, config, approval_plan[label])
            projected_setup_gas += int(tx["gas"]) * int(tx["maxFeePerGas"])
            prepared_approvals.append((label, tx))
        _, _, swap_gas = _validate_economics(wallet, config, quote)
        if _effective_quote_output(config, quote) <= projected_setup_gas + swap_gas:
            raise ValueError("quoted output does not exceed projected approval and swap gas")
        reserve_wei = int(float(getattr(config, "eth_gas_reserve", 0.0005)) * 10**18)
        if wallet.get_eth_balance_wei() - projected_setup_gas - swap_gas < reserve_wei:
            raise ValueError("approval and sale would breach ETH_GAS_RESERVE")
        setup_gas_wei = 0
        for label, tx in prepared_approvals:
            sent = wallet._send_transaction(tx)
            if not sent.success:
                raise ValueError(f"{label} transaction failed: {sent.error}")
            setup_gas_wei += _receipt_gas_wei(sent, int(tx["gas"]) * int(tx["maxFeePerGas"]))
            seal = getattr(provider, "seal_current_operation", None)
            if seal is not None:
                seal()
        quote = provider.get_quote(
            sell_token=config.token_address,
            buy_token=buy_token,
            sell_amount=amount,
            taker_address=wallet.address,
            slippage_percentage=_slippage_fraction(config),
        )
        if not quote.success:
            raise ValueError(quote.error or "moonbag quote refresh failed")
        quote = provider.prepare_swap(quote)
        if not quote.success:
            raise ValueError(quote.error or "moonbag swap preparation failed")
    else:
        setup_gas_wei = 0
        spender = quote.allowance_target or config.zero_x_proxy
        if wallet.check_allowance(config.token_address, spender, use_permit2=False) < amount:
            approval_gas_wei = 100000 * int(wallet.normal_gas_price())
            _validate_economics(wallet, config, quote, approval_gas_wei)
            approval = wallet.approve_token(config.token_address, spender, 2**256 - 1)
            if not approval.success:
                raise ValueError(f"approval failed: {approval.error}")
            setup_gas_wei = _receipt_gas_wei(approval, approval_gas_wei)
            seal = getattr(provider, "seal_current_operation", None)
            if seal is not None:
                seal()
            if provider.capabilities.refresh_after_approval:
                quote = provider.refresh_quote(
                    sell_token=config.token_address,
                    buy_token=buy_token,
                    sell_amount=amount,
                    taker_address=wallet.address,
                    slippage_percentage=_slippage_fraction(config),
                )
                if not quote.success:
                    raise ValueError(quote.error or "moonbag quote refresh failed")
        if provider.capabilities.quote_requires_preparation:
            quote = provider.prepare_swap(quote)
            if not quote.success:
                raise ValueError(quote.error or "moonbag swap preparation failed")

    gas_limit, gas_price, _ = _validate_economics(
        wallet, config, quote, setup_gas_wei, reserve_setup_gas_wei=0
    )
    return _send_quote(wallet, config, quote, gas_limit, gas_price)


def run_moonbag_sale(args: Any) -> int:
    """Plan or execute sale of the current bot's unallocated token balance."""
    try:
        if args.execute and not args.confirm_sell_moonbag:
            raise ValueError("--execute requires --confirm-sell-moonbag")
        if args.execute and not args.confirm_bot_stopped:
            raise ValueError("--execute requires --confirm-bot-stopped")
        send_to_treasury = bool(getattr(args, "send_to_treasury", False))
        if args.execute and send_to_treasury and not getattr(args, "confirm_send_to_treasury", False):
            raise ValueError("--send-to-treasury execution requires --confirm-send-to-treasury")

        config = load_config()
        wallet = Wallet(config)
        recipient = _treasury_recipient(wallet, config, args) if send_to_treasury else None
        provider = create_swap_provider(config)
        token_info = wallet.get_token_info(config.token_address)
        _, wallet_raw = wallet.get_token_balance(config.token_address)
        path = _position_path(config)
        allocation = calculate_moonbag(wallet_raw, _allocated_position_balance(path))
        unit = 10 ** token_info.decimals

        print(f"MOONBAG SALE: {'EXECUTE' if args.execute else 'DRY RUN'}")
        print(f"Coin:       {token_info.symbol} ({config.token_address})")
        print(f"Wallet:     {allocation.wallet_raw / unit:.{min(token_info.decimals, 12)}f}")
        print(f"Allocated:  {allocation.allocated_raw / unit:.{min(token_info.decimals, 12)}f}")
        print(f"Moonbag:    {allocation.moonbag_raw / unit:.{min(token_info.decimals, 12)}f}")
        if allocation.moonbag_raw == 0:
            print("SKIP: no unallocated token balance to sell.")
            return 0

        selected_provider, quote = _select_provider_quote(provider, wallet, config, allocation.moonbag_raw)
        _, _, gas_wei = _validate_economics(wallet, config, quote)
        settlement = "ETH" if getattr(config, "use_eth_trading", False) else "WETH"
        output_wei = _effective_quote_output(config, quote)
        print(f"Provider:   {selected_provider.name}")
        print(f"Quoted:     {output_wei / 1e18:.12f} {settlement}")
        print(f"Swap gas:   {gas_wei / 1e18:.12f} ETH projected")
        print(f"Net est.:   {(output_wei - gas_wei) / 1e18:.12f} {settlement} after swap gas")
        if send_to_treasury:
            print(f"Treasury:   {recipient} (forward actual net proceeds only)")
        if not args.execute:
            print("DRY RUN: no approval, swap, unwrap, or treasury transfer was broadcast.")
            return 0

        eth_before_sale = int(wallet.get_eth_balance_wei()) if send_to_treasury else None
        weth_before_sale = None
        if send_to_treasury and not getattr(config, "use_eth_trading", False):
            _, weth_before_sale = wallet.get_token_balance(config.weth_address)
        result = _execute_quote(selected_provider, wallet, config, allocation.moonbag_raw, quote)
        if not result.success:
            raise ValueError(result.error or "moonbag swap failed")
        _, remaining_raw = wallet.get_token_balance(config.token_address)
        if int(remaining_raw) < allocation.allocated_raw:
            raise ValueError("post-sale wallet balance fell below the amount allocated to positions")
        print(f"CONFIRMED: {result.tx_hash}")
        print(f"Protected position allocation: {allocation.allocated_raw / unit:.{min(token_info.decimals, 12)}f}")
        if send_to_treasury:
            try:
                treasury_result, amount_wei = _forward_actual_proceeds(
                    wallet, config, recipient, int(eth_before_sale), weth_before_sale
                )
            except Exception as exc:
                raise ValueError(f"sale confirmed but treasury forwarding failed: {exc}") from exc
            print(f"TREASURY CONFIRMED: {treasury_result.tx_hash}")
            print(f"Treasury net: {amount_wei / 1e18:.12f} ETH")
        return 0
    except Exception as exc:
        print(f"MOONBAG SALE REFUSED: {exc}")
        return 2
