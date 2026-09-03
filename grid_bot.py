#!/usr/bin/env python3
"""
Robinhood Chain Grid Trading Bot
Uses positions.json format compatible with original bot
"""

import json
import time
import logging
import os
import re
import threading
import sys
import argparse
import random
from functools import wraps
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from profit_tracker import ProfitTracker
from token_tax_detector import TokenTaxDetector


def _with_swap_provider_fallback(method):
    """Retry one complete pre-broadcast operation with the fallback provider."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        runner = getattr(self.provider, "run_with_fallback", None)
        if runner is None:
            return method(self, *args, **kwargs)
        return runner(
            lambda: method(self, *args, **kwargs),
            operation_name=method.__name__,
        )
    return wrapped


def _reset_json_history(path, label):
    """Atomically replace a bot-owned history file with an empty list."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_file = path + ".tmp"
    with open(temp_file, "w") as handle:
        json.dump([], handle, indent=2)
        handle.write("\n")
    os.replace(temp_file, path)
    print(f"{label} reset: {path}")


def _run_lightweight_maintenance():
    """Handle reset commands before importing Web3/provider dependencies."""
    if len(sys.argv) != 2:
        return
    command = sys.argv[1]
    if command == "--reset-profit-baseline":
        tracker = ProfitTracker()
        tracker.reset_baseline()
        print(f"Profit baseline reset at {tracker.tracking_started_at}")
        raise SystemExit(0)
    if command == "--reset-event-data":
        _reset_json_history("data/dashboard_events.json", "Dashboard event data")
        raise SystemExit(0)
    if command == "--reset-trade-history":
        _reset_json_history("data/dashboard_trades.json", "Dashboard trade history")
        raise SystemExit(0)


def _append_treasury_receipt(record):
    """Atomically retain a bounded, local audit trail of sweep attempts."""
    path = "data/treasury_transfers.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r") as handle:
            history = json.load(handle)
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        history = []
    history.append(record)
    history = history[-200:]
    temp_file = path + ".tmp"
    with open(temp_file, "w") as handle:
        json.dump(history, handle, indent=2)
        handle.write("\n")
    os.replace(temp_file, path)


def _total_successful_treasury_sent_usdg(usdg_address, path="data/treasury_transfers.json"):
    """Return the all-time USDG amount confirmed by the local sweep audit log.

    Treasury receipts are the durable source of truth for this display metric:
    dry runs and refused commands never create a receipt, and failed broadcasts
    are explicitly excluded.  The dashboard uses a float only for rendering;
    transfer amounts remain strings in the audit log so their exact decimal
    representation is preserved there.
    """
    if not usdg_address:
        return 0.0
    try:
        with open(path, "r") as handle:
            history = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0.0
    if not isinstance(history, list):
        return 0.0

    total = Decimal(0)
    for record in history:
        if not isinstance(record, dict) or not record.get("success"):
            continue
        if str(record.get("token_address", "")).lower() != usdg_address.lower():
            continue
        try:
            amount = Decimal(str(record.get("amount", "")))
        except Exception:
            continue
        if amount.is_finite() and amount > 0:
            total += amount
    return float(total)


def _resolve_transfer_token(config, token):
    if token.upper() == "USDG":
        if not config.usdg_address or config.usdg_address == "0x...":
            raise ValueError("USDG_ADDRESS is not configured")
        return config.usdg_address
    if not Web3.is_address(token):
        raise ValueError("--transfer-token must be USDG or an ERC-20 contract address")
    return Web3.to_checksum_address(token)


def _validated_treasury_recipient(config, wallet, args):
    """Validate a guarded transfer recipient and return its policy status."""
    if not Web3.is_address(args.recipient):
        raise ValueError("Recipient is not a valid EVM address")
    recipient = Web3.to_checksum_address(args.recipient)
    if recipient.lower() == wallet.address.lower():
        raise ValueError("Recipient cannot be the bot wallet")

    allowed = {address.lower() for address in config.treasury_allowed_recipients}
    recipient_is_allowed = recipient.lower() in allowed
    if not recipient_is_allowed:
        confirmation = (args.confirm_recipient or "").lower()
        if confirmation != recipient.lower():
            raise ValueError(
                "Recipient is not allowlisted. Repeat it exactly with "
                "--confirm-recipient <recipient> to authorize this one transfer."
            )
    return recipient, recipient_is_allowed


def run_treasury_transfer(args):
    """Plan or execute one deliberately guarded ERC-20 treasury transfer."""
    try:
        config = load_config()
        token_address = _resolve_transfer_token(config, args.transfer_token)
        wallet = Wallet(config)
        recipient, recipient_is_allowed = _validated_treasury_recipient(config, wallet, args)

        token_info = wallet.get_token_info(token_address)
        balance, balance_raw = wallet.get_token_balance(token_address)
        if args.amount == "all":
            amount_raw = balance_raw
        else:
            requested = Decimal(args.amount)
            if not requested.is_finite() or requested <= 0:
                raise ValueError("--amount must be a positive decimal amount or 'all'")
            amount_raw = int(requested * (Decimal(10) ** token_info.decimals))
            if amount_raw <= 0:
                raise ValueError("--amount is below one smallest token unit")
        if amount_raw > balance_raw:
            raise ValueError("Requested amount exceeds the current token balance")
        if amount_raw <= 0:
            raise ValueError("There is no token balance available to transfer")

        amount = Decimal(amount_raw) / (Decimal(10) ** token_info.decimals)
        print("TREASURY TRANSFER PLAN")
        print(f"Wallet:    {wallet.address}")
        print(f"Token:     {token_info.symbol} ({token_address})")
        print(f"Balance:   {balance}")
        print(f"Send:      {amount}")
        print(f"Recipient: {recipient} ({'allowlisted' if recipient_is_allowed else 'one-time confirmed'})")

        if not args.execute:
            print("DRY RUN: no transaction broadcast. Add --execute after reviewing this plan.")
            return 0
        if not args.confirm_bot_stopped:
            raise ValueError(
                "Refusing to broadcast while a trading bot may share this wallet. "
                "Stop it first, then pass --confirm-bot-stopped."
            )

        result = wallet.transfer_erc20(token_address, recipient, amount_raw, wait_for_receipt=True)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "wallet": wallet.address,
            "token": token_info.symbol,
            "token_address": token_address,
            "amount": str(amount),
            "recipient": recipient,
            "success": result.success,
            "tx_hash": result.tx_hash,
            "error": result.error,
        }
        _append_treasury_receipt(record)
        if not result.success:
            print(f"TRANSFER FAILED: {result.error}")
            return 1
        print(f"TRANSFER CONFIRMED: {_terminal_transaction_link(config.chain_id, result.tx_hash)}")
        return 0
    except Exception as exc:
        print(f"TREASURY TRANSFER REFUSED: {exc}")
        return 2


def run_native_treasury_transfer(args):
    """Plan or execute one reserve-preserving native-ETH transfer."""
    try:
        config = load_config()
        wallet = Wallet(config)
        recipient, recipient_is_allowed = _validated_treasury_recipient(config, wallet, args)

        liquidate = args.amount == "all"
        sweep_available = args.amount == "available"
        if liquidate and not args.confirm_liquidate:
            raise ValueError("Native ETH 'all' requires --confirm-liquidate")
        if sweep_available and args.confirm_liquidate:
            raise ValueError("--confirm-liquidate is valid only with --amount all")
        if liquidate or sweep_available:
            if wallet.address_has_code(recipient):
                raise ValueError(
                    "Calculated native ETH transfer to a contract is forbidden because its "
                    "receive-gas requirement may differ from the estimate"
                )
            requested_amount_wei = None
        else:
            if args.confirm_liquidate:
                raise ValueError("--confirm-liquidate is valid only with --amount all")
            requested = Decimal(args.amount)
            if not requested.is_finite() or requested <= 0:
                raise ValueError("--amount must be a positive decimal ETH amount or 'all'")
            requested_amount_wei = int(requested * Decimal(10**18))
            if requested_amount_wei <= 0:
                raise ValueError("--amount is below one wei")

        def build_plan(minimum_base_fee=0):
            balance_wei = wallet.get_eth_balance_wei()
            # A one-wei EOA transfer provides the gas estimate without first
            # constructing an unaffordable balance-sized transaction.
            initial_value = 1 if liquidate or sweep_available else requested_amount_wei
            tx = wallet.build_eth_transfer_transaction(
                recipient, initial_value, minimum_base_fee=minimum_base_fee
            )
            fee_wei = int(tx["gas"]) * int(tx["gasPrice"])
            reserve_wei = (
                0 if liquidate
                else int(Decimal(str(config.eth_gas_reserve)) * Decimal(10**18))
            )
            if liquidate:
                amount_wei = balance_wei - fee_wei
                if amount_wei <= 0:
                    raise ValueError("ETH balance does not cover the estimated maximum transfer fee")
                tx["value"] = amount_wei
            elif sweep_available:
                amount_wei = balance_wei - fee_wei - reserve_wei
                if amount_wei <= 0:
                    return None
                tx["value"] = amount_wei
            else:
                amount_wei = requested_amount_wei
            if amount_wei + fee_wei + reserve_wei > balance_wei:
                raise ValueError(
                    "Insufficient ETH to send the requested amount while paying the "
                    f"estimated maximum fee and retaining ETH_GAS_RESERVE={config.eth_gas_reserve}"
                )
            return tx, balance_wei, amount_wei, fee_wei, reserve_wei

        plan = build_plan()
        if plan is None:
            print(
                "NATIVE ETH TREASURY TRANSFER SKIPPED: balance does not exceed "
                "the estimated maximum transfer fee plus "
                f"ETH_GAS_RESERVE={config.eth_gas_reserve}"
            )
            return 0
        tx, balance_wei, amount_wei, fee_wei, reserve_wei = plan

        amount = Decimal(amount_wei) / Decimal(10**18)
        balance = Decimal(balance_wei) / Decimal(10**18)
        fee = Decimal(fee_wei) / Decimal(10**18)
        remaining = Decimal(balance_wei - amount_wei - fee_wei) / Decimal(10**18)
        print("NATIVE ETH TREASURY TRANSFER PLAN")
        print(f"Wallet:       {wallet.address}")
        print(f"Balance:      {balance} ETH")
        print(f"Send:         {amount} ETH")
        print(f"Max gas cost: {fee} ETH")
        print(f"Reserve:      {config.eth_gas_reserve if not liquidate else 0} ETH")
        print(f"Min remaining after gas: {remaining} ETH")
        print(f"Liquidation:  {'YES — configured reserve intentionally bypassed' if liquidate else 'no'}")
        print(f"Sweep mode:   {'all unreserved ETH' if sweep_available else 'no'}")
        print(f"Recipient:    {recipient} ({'allowlisted' if recipient_is_allowed else 'one-time confirmed'})")

        if not args.execute:
            print("DRY RUN: no transaction broadcast. Add --execute after reviewing this plan.")
            return 0
        if not args.confirm_bot_stopped:
            raise ValueError(
                "Refusing to broadcast while a trading bot may share this wallet. "
                "Stop it first, then pass --confirm-bot-stopped."
            )

        result = wallet.transfer_eth(tx, wait_for_receipt=True)
        if (
            not result.success
            and not result.tx_hash
            and wallet.is_base_fee_too_low_error(result.error)
        ):
            print(
                "Transfer gas became stale before broadcast; rebuilding once with "
                "the current base fee and rechecking the reserve."
            )
            rejected_base_fee = wallet.base_fee_from_error(result.error)
            retry_floor = (rejected_base_fee * 102 + 99) // 100
            plan = build_plan(retry_floor)
            if plan is None:
                print(
                    "NATIVE ETH TREASURY TRANSFER SKIPPED: refreshed gas leaves no "
                    "surplus above ETH_GAS_RESERVE"
                )
                return 0
            tx, balance_wei, amount_wei, fee_wei, reserve_wei = plan
            amount = Decimal(amount_wei) / Decimal(10**18)
            fee = Decimal(fee_wei) / Decimal(10**18)
            result = wallet.transfer_eth(tx, wait_for_receipt=True)
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "wallet": wallet.address,
            "token": "ETH",
            "token_address": "native",
            "amount": str(amount),
            "recipient": recipient,
            "estimated_max_gas_eth": str(fee),
            "gas_reserve_eth": str(config.eth_gas_reserve if not liquidate else 0),
            "liquidation": liquidate,
            "sweep_available": sweep_available,
            "success": result.success,
            "tx_hash": result.tx_hash,
            "error": result.error,
        }
        _append_treasury_receipt(record)
        if not result.success:
            print(f"TRANSFER FAILED: {result.error}")
            return 1
        print(f"TRANSFER CONFIRMED: {_terminal_transaction_link(config.chain_id, result.tx_hash)}")
        return 0
    except Exception as exc:
        print(f"NATIVE ETH TRANSFER REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    _run_lightweight_maintenance()


import requests
from web3 import Web3

from config import load_config
from wallet import Wallet
from swap_provider import create_swap_provider, resolve_provider_name
from dashboard_reporter import DashboardReporter, create_reporter_from_config

# Native ETH address for 0x API (used when trading with native ETH instead of WETH)
# Native ETH address for 0x API
ETH_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
# Native ETH address for Uniswap API (zero address)
UNISWAP_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

# Global logger - will be configured by GridBot
logger = logging.getLogger('grid_bot')

_PRIVATE_KEY_RE = re.compile(r'(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])')
_SECRET_PARAM_RE = re.compile(r'(?i)(api[_-]?key|token|secret|authorization)([=:]\s*)([^\s&,]+)')
_REQUEST_ID_RE = re.compile(r'(?i)(["\']?request[_-]?id["\']?\s*[:=]\s*)["\']?[^"\',}\s]+["\']?')
_TX_HASH_RE = re.compile(r'^0x[0-9a-fA-F]{64}$')
_TRANSACTION_EXPLORER_BASES = {
    1: "https://etherscan.io/tx/",
    4663: "https://robinhoodchain.blockscout.com/tx/",
    8453: "https://basescan.org/tx/",
}


def _terminal_transaction_link(chain_id, tx_hash):
    """Return a terminal hyperlink with the transaction hash as its label.

    OSC 8 links are supported by current terminal emulators and remain a plain,
    readable hash in terminals that do not support hyperlinks. The URL is never
    printed visibly, so fleet output stays compact.
    """
    tx_hash = str(tx_hash)
    base_url = _TRANSACTION_EXPLORER_BASES.get(chain_id)
    if not base_url or not _TX_HASH_RE.fullmatch(tx_hash):
        return tx_hash
    url = f"{base_url}{tx_hash}"
    return f"\033]8;;{url}\033\\{tx_hash}\033]8;;\033\\"


def _safe_event_message(message):
    """Bound and redact log text before it leaves the bot."""
    text = _PRIVATE_KEY_RE.sub('[REDACTED]', str(message))
    text = _SECRET_PARAM_RE.sub(r'\1\2[REDACTED]', text)
    text = _REQUEST_ID_RE.sub(r'\1[REDACTED]', text)
    return text[:500]


def _dashboard_root_url(status_url):
    parsed = urlsplit(status_url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else status_url


MERCURY_EVOCATION = """☿ EVOCATION OF MERCURY ☿

Mercury, fleet-footed keeper of roads,
Lord of exchange, measure, message, and wit—
Attend this engine of number and motion.

Make clear its signals,
Make swift its passage,
Make honest its measures,
And turn error, delay, and false quotation aside.

Guide each bargain toward fair advantage;
Let no key be exposed, no nonce be crossed,
No allowance be granted beyond its purpose,
And no trade be taken without profit’s promise.

By ledger and wire,
By token and flame,
By the turning wheel of price:
May this bot trade cunningly,
Bank faithfully,
And return bearing increase.

☿ The road is open. The market is awake. ☿

MERCURY INVOKED · ROUTES OPEN · PROFIT SOUGHT · RISK BOUNDED"""


def invoke_mercury(enabled, emit=print):
    """Emit the trading evocation once when enabled."""
    if not enabled:
        return False
    emit(MERCURY_EVOCATION)
    return True


def check_config():
    """Validate configuration and read-only dependencies without trading."""
    try:
        config = load_config()
    except Exception as exc:
        print(f"FAIL configuration: {exc}")
        return 1

    provider = resolve_provider_name(config)
    selection = "explicit SWAP_PROVIDER" if config.swap_provider else "legacy provider flags/default"
    fallback = (getattr(config, "swap_fallback_provider", "") or "").strip().lower()
    print(f"PASS configuration: {config.token_symbol} on {config.chain_name} ({config.chain_id})")
    print(f"PASS provider: {provider} ({selection})")
    if fallback and fallback != provider:
        print(f"PASS fallback provider: {fallback}")
    if getattr(config, "taxed_token", False):
        total_tolerance = (
            config.token_transfer_fee_percent
            + config.taxed_token_slippage_buffer_percent
        )
        print(
            "PASS taxed-token mode: "
            f"fee {config.token_transfer_fee_percent:g}% + "
            f"market buffer {config.taxed_token_slippage_buffer_percent:g}% = "
            f"{total_tolerance:g}% total tolerance"
        )

    try:
        wallet = Wallet(config)
        chain_id = wallet.w3.eth.chain_id
        if chain_id != config.chain_id:
            raise ValueError(f"RPC chain ID {chain_id} does not match configured {config.chain_id}")
        print(f"PASS RPC: connected to chain {chain_id}")
        print(f"PASS wallet: {wallet.address} ({wallet.get_eth_balance():.8f} ETH)")
        token_info = wallet._load_token_info(config.token_address)
        token_balance, _ = wallet.get_token_balance(config.token_address)
        print(f"PASS token: {token_info.symbol} at {config.token_address} ({token_balance:.8f})")
        if config.usdg_address and config.usdg_address != "0x...":
            usdg_balance, _ = wallet.get_token_balance(config.usdg_address)
            print(f"PASS USDG: {usdg_balance:.6f}")
        else:
            print("SKIP USDG: USDG_ADDRESS is not configured")
    except Exception as exc:
        print(f"FAIL chain/wallet/token: {exc}")
        return 1

    if not config.dashboard_url:
        print("SKIP dashboard: DASHBOARD_URL is not configured")
    else:
        if not config.dashboard_api_key:
            print("FAIL dashboard: DASHBOARD_API_KEY is not configured")
            return 1
        try:
            response = requests.get(_dashboard_root_url(config.dashboard_url), timeout=5)
            response.raise_for_status()
            print(f"PASS dashboard: reachable at {_dashboard_root_url(config.dashboard_url)}")
        except requests.RequestException as exc:
            print(f"FAIL dashboard: {exc}")
            return 1

    print("PASS check complete: no quote requested and no transaction broadcast")
    return 0


class DashboardEventHandler(logging.Handler):
    """Convert warning/error log records into structured dashboard events."""

    def __init__(self, callback):
        super().__init__(level=logging.WARNING)
        self.callback = callback

    def emit(self, record):
        try:
            self.callback(
                record.levelname.lower(),
                'log_error' if record.levelno >= logging.ERROR else 'log_warning',
                record.getMessage(),
                source=record.name,
            )
        except Exception:
            pass

class GridBot:
    def __init__(self):
        self.config = load_config()
        self.dashboard_events_file = "data/dashboard_events.json"
        self._dashboard_event_lock = threading.Lock()
        self.dashboard_events = self._load_dashboard_events()
        
        # Setup logging FIRST so we capture all initialization logs
        self._setup_logging()

        self.tax_detector = TokenTaxDetector(
            path="data/token_tax_detection.json",
            chain_id=self.config.chain_id,
            token_address=self.config.token_address,
            enabled=(
                self.config.auto_detect_token_transfer_fee
                and not self.config.taxed_token
            ),
            max_fee_percent=self.config.auto_detect_token_transfer_fee_max_percent,
        )
        
        self.wallet = Wallet(self.config)
        token_info = self.wallet.get_token_info(self.config.token_address)
        self.token_decimals = token_info.decimals
        self.token_unit = 10 ** self.token_decimals
        # Strategy helpers receive config rather than the bot instance. Attach
        # discovered metadata at runtime; position files continue storing raw
        # integer balances and therefore require no migration.
        self.config.token_decimals = self.token_decimals
        logger.info(
            f"Trading token: {token_info.symbol} ({self.token_decimals} decimals)"
        )
        
        self.provider = create_swap_provider(self.config)
        self.api_client = self.provider  # compatibility alias during refactor
        logger.info(f"Using {self.provider.name} provider for swaps")
        
        self.positions_file = "data/positions.json"
        self.positions = {}
        self.running = True
        self.round_count = 0
        self.start_time = time.time()
        self.session_buys = 0
        self.session_sells = 0
        self.session_profit_weth = 0.0
        # Remember the last executable sell quote per position. A provider
        # handoff must be observed for one full poll before it may authorize a
        # sale; materially divergent providers remain visibly blocked.
        self._last_sell_quotes = {}
        self.profit_tracker = ProfitTracker()
        self.dashboard_trades_file = "data/dashboard_trades.json"
        self.dashboard_trades = self._load_dashboard_trades()
        self.profit_tracker.seed_profit_history(self.dashboard_trades)
        
        # Cooldown tracking for gridless buys
        self.last_buy_time = 0
        self.gridless_buy_cooldown = getattr(self.config, 'gridless_buy_cooldown_seconds', 300)  # Default 5 min
        self.last_taxed_token_failure_time = 0
        # Funding warnings are discovered after the dashboard report and then
        # carried into the following cycle. Initialize the first-cycle value so
        # a fresh bot can report before any buy check has created a warning.
        self._funding_warning = None
        
        # Trading token setup (WETH or native ETH)
        if getattr(self.config, 'use_eth_trading', False):
            # Use native ETH address (zero address for Uniswap API)
            self.trade_token_address = UNISWAP_ETH_ADDRESS
            self.trade_token_name = "ETH"
            logger.info("Trading mode: Native ETH")
        else:
            self.trade_token_address = self.config.weth_address
            self.trade_token_name = "WETH"
            logger.info("Trading mode: WETH")

        if self._taxed_token_active():
            source = "declared" if self.config.taxed_token else "auto-detected"
            logger.warning(
                "Taxed-token mode enabled (%s): fee %.2f%%, market-slippage "
                "buffer %.2f%%, total swap tolerance %.2f%%",
                source,
                self._effective_token_transfer_fee_percent(),
                self.config.taxed_token_slippage_buffer_percent,
                self._swap_slippage_fraction() * 100,
            )
        
        logger.info(f"Grid Bot initialized")
        
        # Dashboard reporter (None when DASHBOARD_URL is not configured)
        self._reporter: Optional[DashboardReporter] = create_reporter_from_config(self.config)
        logger.info(f"DEBUG: dashboard_url={self.config.dashboard_url!r}, reporter={self._reporter}")
        if self._reporter:
            logger.info(f"Dashboard reporting enabled (bot_id={self._reporter.bot_id})")

    def _effective_token_transfer_fee_percent(self):
        if getattr(self.config, 'taxed_token', False):
            return float(self.config.token_transfer_fee_percent)
        detector = getattr(self, "tax_detector", None)
        return float(detector.detected_fee_percent) if detector is not None else 0.0

    def _taxed_token_active(self):
        return self._effective_token_transfer_fee_percent() > 0

    def _observe_token_tax_failure(self, quote, *, direction, position_id=None):
        """Record bounded simulation evidence without broadcasting or editing .env."""
        if self.config.taxed_token:
            return None
        detection = self.tax_detector.observe(
            getattr(quote, "error", quote),
            direction=direction,
        )
        if not detection:
            return None
        self._sell_attempt = {
            "status": "token_tax_detected" if detection["confirmed"] else "token_tax_observed",
            "position_id": str(position_id) if position_id is not None else None,
            "observed_fee_percent": detection["observed_fee_percent"],
            "matching_observations": detection["matching_observations"],
            "confirmations_required": detection["confirmations_required"],
            "detected_fee_percent": detection["detected_fee_percent"],
        }
        if detection["newly_confirmed"]:
            logger.warning(
                "Probable %.1f%% token transfer fee auto-detected after %d consistent "
                "simulations; using %.1f%% total execution tolerance without changing .env",
                detection["detected_fee_percent"],
                detection["matching_observations"],
                detection["detected_fee_percent"]
                + self.config.taxed_token_slippage_buffer_percent,
            )
        else:
            logger.warning(
                "Possible %.3f%% token transfer fee observed (%d/%d confirmations); "
                "no execution tolerance changed yet",
                detection["observed_fee_percent"],
                detection["matching_observations"],
                detection["confirmations_required"],
            )
        return detection

    def _swap_slippage_fraction(self):
        """Return bounded provider slippage, including a declared token fee."""
        if self._taxed_token_active():
            total_percent = (
                self._effective_token_transfer_fee_percent()
                + self.config.taxed_token_slippage_buffer_percent
            )
        else:
            total_percent = self.config.slippage_tolerance
        return total_percent / 100.0

    def _raw_token_balance(self, token_address):
        """Return the wallet's integer ERC-20 balance."""
        _, raw_balance = self.wallet.get_token_balance(token_address)
        return int(raw_balance)

    def _wallet_can_cover_sell(self, sell_amount, position_id):
        """Fail closed when local position accounting exceeds wallet reality."""
        try:
            wallet_balance = self._raw_token_balance(self.config.token_address)
        except Exception as exc:
            logger.error(
                "SELL SAFETY CHECK FAILED for position #%s: could not read wallet "
                "token balance: %s",
                position_id,
                exc,
            )
            return False

        sell_amount = int(sell_amount)
        if wallet_balance >= sell_amount:
            return True

        deficit = sell_amount - wallet_balance
        self._sell_attempt = {
            "status": "position_balance_mismatch",
            "position_id": str(position_id),
            "tracked_sell_amount_raw": sell_amount,
            "wallet_balance_raw": wallet_balance,
            "deficit_raw": deficit,
        }
        logger.error(
            "POSITION BALANCE MISMATCH for #%s: tracked sell amount %s exceeds "
            "wallet balance %s by %s raw units; blocking sell before quote",
            position_id,
            sell_amount,
            wallet_balance,
            deficit,
        )
        return False

    def _sell_quote_consistency_guard(self, position_id, provider, return_wei, now=None):
        """Require one confirmation after an executable quote provider changes."""
        now = time.time() if now is None else float(now)
        position_id = str(position_id)
        provider = str(provider or "unknown")
        return_wei = int(return_wei)
        cache = getattr(self, "_last_sell_quotes", None)
        if cache is None:
            cache = self._last_sell_quotes = {}
        previous = cache.get(position_id)
        cache[position_id] = {
            "provider": provider,
            "return_wei": return_wei,
            "timestamp": now,
        }
        if not previous or now - previous["timestamp"] > 120:
            return None
        if previous["provider"] == provider:
            return None

        larger = max(previous["return_wei"], return_wei, 1)
        divergence_percent = abs(previous["return_wei"] - return_wei) * 100 / larger
        return {
            "status": (
                "quote_provider_disagreement"
                if divergence_percent > 8.0
                else "quote_provider_changed"
            ),
            "position_id": position_id,
            "quote_provider": provider,
            "previous_quote_provider": previous["provider"],
            "quoted_return_eth": round(return_wei / 10**18, 8),
            "previous_quoted_return_eth": round(previous["return_wei"] / 10**18, 8),
            "quote_divergence_percent": round(divergence_percent, 2),
        }

    def _raw_trade_balance(self):
        if getattr(self.config, 'use_eth_trading', False):
            return int(self.wallet.get_eth_balance_wei())
        return self._raw_token_balance(self.config.weth_address)

    def _receipt_token_received_raw(self, result):
        """Recover managed-token output from Transfer logs when RPC state lags."""
        receipt = getattr(result, 'receipt', None) or {}
        token_address = str(self.config.token_address).lower()
        wallet_topic = "0x" + str(self.wallet.address).lower().removeprefix("0x").rjust(64, "0")
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f1639c4a11628f55a4df523b3ef"
        total = 0
        for entry in receipt.get('logs') or []:
            if str(entry.get('address', '')).lower() != token_address:
                continue
            topics = [topic.hex() if hasattr(topic, 'hex') else str(topic) for topic in entry.get('topics', [])]
            if len(topics) < 3 or topics[0].lower() != transfer_topic or topics[2].lower() != wallet_topic:
                continue
            data = entry.get('data', 0)
            total += int(data.hex(), 16) if hasattr(data, 'hex') else int(data, 16) if isinstance(data, str) else int(data)
        return total

    def _measured_token_received_raw(self, before_balance, result=None, expected_raw=0):
        """Measure every buy from wallet reality, with receipt-log stale-RPC recovery."""
        expected_raw = max(0, int(expected_raw or 0))
        plausible_floor = int(expected_raw * max(0.0, 1.0 - self._swap_slippage_fraction())) if expected_raw else 0
        attempts = 6 if plausible_floor else 1
        measured = 0
        for attempt in range(attempts):
            measured = max(0, self._raw_token_balance(self.config.token_address) - int(before_balance))
            if not plausible_floor or measured >= plausible_floor:
                return measured
            if attempt < attempts - 1:
                logger.warning("Post-buy token balance appears stale (%s raw; expected about %s); retrying", measured, expected_raw)
                time.sleep(0.5)
        receipt_amount = self._receipt_token_received_raw(result) if result is not None else 0
        if receipt_amount > 0:
            logger.warning("RPC balance remained stale; reconciled %s raw bought tokens from receipt logs", receipt_amount)
            return receipt_amount
        logger.critical("Confirmed buy could not be reconciled from wallet balance or receipt logs; refusing to create a position")
        return 0

    @staticmethod
    def _receipt_gas_cost_wei(result):
        receipt = getattr(result, 'receipt', None) or {}
        gas_used = receipt.get('gasUsed') or getattr(result, 'gas_used', None) or 0
        gas_price = receipt.get('effectiveGasPrice') or getattr(result, 'effective_gas_price', None) or 0
        return int(gas_used) * int(gas_price)

    def _projected_gas_cost_wei(self, quote, default_gas=300000):
        """Conservative native-ETH cost of broadcasting a prepared swap."""
        gas_limit, gas_price = self._swap_gas_fields(quote, default_gas)
        return gas_limit * gas_price

    def _swap_gas_fields(self, quote, default_gas=300000):
        """Return the exact gas limit/price used for economics and broadcast.

        Robinhood Chain's sequencer is first-come-first-served, so paying a
        provider's "fast" recommendation does not buy useful priority. The RPC
        ``eth_gasPrice`` value is the dynamic normal/network price. A configured
        multiplier remains available for operators, but defaults to no premium.
        """
        gas_limit_mult = getattr(self.config, 'gas_limit_multiplier', 1.05)
        provider_gas = int(quote.gas or default_gas)
        estimated_gas = None
        if getattr(quote, "to", None) and getattr(quote, "data", None):
            try:
                # Simulate the exact executable transaction returned by the quote.
                # Route-level provider estimates can include chain-inappropriate
                # padding and have historically overstated Robinhood Chain sells.
                estimated_gas = int(self.wallet.w3.eth.estimate_gas({
                    "from": Web3.to_checksum_address(self.wallet.address),
                    "to": Web3.to_checksum_address(quote.to),
                    "data": quote.data,
                    "value": int(quote.value or 0),
                }))
            except Exception as exc:
                logger.warning(
                    "Executable quote gas simulation failed; using provider estimate: %s",
                    exc,
                )
        gas_source = "rpc_execution" if estimated_gas is not None else "provider"
        gas_limit = int((estimated_gas if estimated_gas is not None else provider_gas) * gas_limit_mult)
        normal_gas_price = int(self.wallet.w3.eth.gas_price)
        gas_price_mult = getattr(self.config, 'gas_price_multiplier', 1.0)
        freshness_mult = max(
            float(getattr(self.config, 'gas_price_freshness_multiplier', 1.01)), 1.0
        )
        gas_price = int(
            normal_gas_price * max(float(gas_price_mult), 1.0) * freshness_mult
        )
        provider_gas_price = int(quote.gas_price or 0)
        logger.info(
            "Gas plan: strategy=normal source=%s simulated_gas=%s provider_gas=%s "
            "gas_limit=%s gas_limit_multiplier=%.3f "
            "normal_gas_price=%s provider_gas_price=%s gas_price_multiplier=%.3f "
            "freshness_multiplier=%.3f "
            "projected_fee_wei=%s",
            gas_source, estimated_gas, provider_gas, gas_limit, gas_limit_mult,
            normal_gas_price, provider_gas_price,
            gas_price_mult, freshness_mult, gas_limit * gas_price,
        )
        return gas_limit, gas_price

    def _gas_within_hard_cap(self, gas_limit, gas_price, operation):
        legacy_cap = float(getattr(self.config, "max_swap_gas_eth", 0.00004))
        cap_attribute = {
            "buy": "max_buy_gas_eth",
            "sell": "max_sell_gas_eth",
            "fee": "max_fee_transfer_gas_eth",
        }.get(operation)
        cap_eth = float(getattr(self.config, cap_attribute, legacy_cap)) if cap_attribute else legacy_cap
        projected_wei = int(gas_limit) * int(gas_price)
        if cap_eth <= 0 or projected_wei <= int(cap_eth * 10**18):
            return True
        logger.warning(
            "❌ %s blocked by gas cap: projected_fee=%.8f ETH cap=%.8f ETH",
            operation, projected_wei / 10**18, cap_eth,
        )
        return False

    def _seal_provider_fallback(self):
        """Fail closed after an operation has spent gas on an approval/setup tx."""
        seal = getattr(self.provider, "seal_current_operation", None)
        if seal is not None:
            seal()

    def _net_sale_profit_wei(self, received_wei, sold_cost_wei, result, setup_gas_wei=0):
        """Economic profit after position cost basis and confirmed sell gas."""
        return (
            int(received_wei) - int(sold_cost_wei)
            - int(setup_gas_wei) - self._receipt_gas_cost_wei(result)
        )

    def _minimum_gas_aware_return_wei(
        self, sold_cost_wei, quote, min_profit_percent, setup_gas_wei=0,
        projected_gas_cost_wei=None,
    ):
        """Return required to preserve principal, pay sell gas, and earn target profit."""
        minimum_profit_wei = int(int(sold_cost_wei) * (float(min_profit_percent) / 100.0))
        return (
            int(sold_cost_wei) + minimum_profit_wei + int(setup_gas_wei)
            + (
                self._projected_gas_cost_wei(quote)
                if projected_gas_cost_wei is None
                else int(projected_gas_cost_wei)
            )
        )

    def _receipt_trade_received_wei(self, result):
        """Recover trade-token output from receipt logs when an RPC balance is stale."""
        receipt = getattr(result, 'receipt', None) or {}
        logs = receipt.get('logs') or []
        weth_address = str(getattr(self.config, 'weth_address', '')).lower()
        wallet_topic = "0x" + str(self.wallet.address).lower().removeprefix("0x").rjust(64, "0")
        zero_topic = "0x" + "0" * 64
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        output_topic = zero_topic if getattr(self.config, 'use_eth_trading', False) else wallet_topic
        total = 0
        for entry in logs:
            if str(entry.get('address', '')).lower() != weth_address:
                continue
            topics = [topic.hex() if hasattr(topic, 'hex') else str(topic) for topic in entry.get('topics', [])]
            if len(topics) < 3 or topics[0].lower() != transfer_topic or topics[2].lower() != output_topic:
                continue
            data = entry.get('data', 0)
            total += int(data.hex(), 16) if hasattr(data, 'hex') else int(data, 16) if isinstance(data, str) else int(data)
        return total

    def _measured_trade_received_wei(self, before_balance, result, expected_wei=0):
        """Measure proceeds, reconciling stale RPC balances against receipt logs."""
        gas_cost = self._receipt_gas_cost_wei(result) if getattr(self.config, 'use_eth_trading', False) else 0
        expected_wei = max(0, int(expected_wei or 0))
        plausible_floor = int(expected_wei * 0.90) if expected_wei else 0
        attempts = 6 if plausible_floor else 1
        measured = 0
        for attempt in range(attempts):
            after_balance = self._raw_trade_balance()
            measured = max(0, after_balance - int(before_balance) + gas_cost)
            if not plausible_floor or measured >= plausible_floor:
                return measured
            if attempt < attempts - 1:
                logger.warning(
                    "Post-trade balance appears stale (%s wei; expected about %s); retrying",
                    measured,
                    expected_wei,
                )
                time.sleep(0.5)

        receipt_amount = self._receipt_trade_received_wei(result)
        if receipt_amount >= plausible_floor:
            logger.warning(
                "RPC balance remained stale; reconciled %s wei of proceeds from receipt logs",
                receipt_amount,
            )
            return receipt_amount

        # A successful, validated swap must not be finalized as a fabricated loss.
        # The router transaction enforces its quoted minimum, so preserve that
        # conservative amount and surface the reconciliation failure loudly.
        logger.critical(
            "Could not reconcile post-trade balance or receipt logs; using validated quote floor %s wei",
            expected_wei,
        )
        return expected_wei

    def _taxed_quote_return_wei(self, quote):
        """Conservatively fee-adjust a sell quote before the pre-trade guard."""
        quoted = int(quote.buy_amount or 0)
        if not self._taxed_token_active():
            return quoted
        fee_fraction = self._effective_token_transfer_fee_percent() / 100.0
        return int(quoted * (1.0 - fee_fraction))

    def _record_profit_fee(self, entry):
        """Append one fee attempt to the local audit trail atomically."""
        path = "data/profit_fees.json"
        try:
            with open(path, "r") as handle:
                entries = json.load(handle)
            if not isinstance(entries, list):
                entries = []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            entries = []
        entries.append(entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w") as handle:
            json.dump(entries[-1000:], handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)

    def _load_profit_fee_accrual(self):
        """Load durable unpaid fee accrual, tolerating legacy/missing state."""
        path = "data/profit_fee_accrual.json"
        try:
            with open(path, "r") as handle:
                state = json.load(handle)
            pending_wei = max(0, int(state.get("pending_wei", 0)))
            sale_tx_hashes = [str(value) for value in state.get("sale_tx_hashes", []) if value]
            return {"pending_wei": pending_wei, "sale_tx_hashes": sale_tx_hashes[-1000:]}
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return {"pending_wei": 0, "sale_tx_hashes": []}

    def _save_profit_fee_accrual(self, state):
        """Atomically persist unpaid fee accrual before/after transfer attempts."""
        path = "data/profit_fee_accrual.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "pending_wei": max(0, int(state.get("pending_wei", 0))),
            "sale_tx_hashes": [str(value) for value in state.get("sale_tx_hashes", []) if value][-1000:],
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        temp_path = f"{path}.tmp"
        with open(temp_path, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)

    def _charge_profit_fee(self, profit_wei, sale_tx_hash):
        """Send the configured share of positive realized profit."""
        fee_percent = getattr(self.config, "profit_fee_percent", 0)
        if fee_percent <= 0 or profit_wei <= 0:
            return None

        sale_fee_wei = int(Decimal(int(profit_wei)) * Decimal(str(fee_percent)) / Decimal(100))
        if sale_fee_wei <= 0:
            return None

        recipient = self.config.profit_fee_wallet
        if recipient.lower() == self.wallet.address.lower():
            logger.error("Profit fee refused: PROFIT_FEE_WALLET is the bot's own wallet")
            return None
        accrual = self._load_profit_fee_accrual()
        sale_hashes = set(accrual["sale_tx_hashes"])
        if sale_tx_hash not in sale_hashes:
            accrual["pending_wei"] += sale_fee_wei
            accrual["sale_tx_hashes"].append(sale_tx_hash)
            self._save_profit_fee_accrual(accrual)
        fee_wei = int(accrual["pending_wei"])
        minimum_wei = int(Decimal(str(getattr(
            self.config, "min_profit_fee_transfer_eth", 0.0001
        ))) * Decimal(10**18))
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "sale_tx_hash": sale_tx_hash,
            "recipient": recipient,
            "asset": self.trade_token_name,
            "profit_wei": int(profit_wei),
            "fee_percent": float(fee_percent),
            "fee_wei": fee_wei,
            "sale_fee_wei": sale_fee_wei,
            "minimum_transfer_wei": minimum_wei,
            "accrued_sale_count": len(accrual["sale_tx_hashes"]),
            "status": "failed",
        }
        if fee_wei < minimum_wei:
            entry["status"] = "deferred"
            self._record_profit_fee(entry)
            logger.info(
                "💰 Profit fee accrued: %.8f %s pending; minimum transfer is %.8f",
                fee_wei / 10**18,
                self.trade_token_name,
                minimum_wei / 10**18,
            )
            return entry
        try:
            if getattr(self.config, "use_eth_trading", False):
                tx = self.wallet.build_eth_transfer_transaction(recipient, fee_wei)
                fee_gas = int(tx.get("gas", 0)) * int(tx.get("gasPrice", 0))
                if not self._gas_within_hard_cap(
                    tx.get("gas", 0), tx.get("gasPrice", 0), "fee"
                ):
                    raise ValueError("profit-fee transfer blocked by gas cap")
                max_cost = fee_wei + fee_gas
                reserve_wei = int(self.config.eth_gas_reserve * 10**18)
                if self.wallet.get_eth_balance_wei() - max_cost < reserve_wei:
                    raise ValueError("profit-fee transfer would breach ETH_GAS_RESERVE")
                result = self.wallet.transfer_eth(tx, wait_for_receipt=True)
            else:
                fee_gas_limit = 100000
                fee_gas_price = self.wallet.normal_gas_price()
                if not self._gas_within_hard_cap(
                    fee_gas_limit, fee_gas_price, "fee"
                ):
                    raise ValueError("profit-fee transfer blocked by gas cap")
                result = self.wallet.transfer_erc20(
                    self.config.weth_address,
                    recipient,
                    fee_wei,
                    wait_for_receipt=True,
                )
            entry["status"] = "success" if result.success else "failed"
            entry["fee_tx_hash"] = result.tx_hash
            if result.error:
                entry["error"] = result.error
        except Exception as exc:
            entry["error"] = str(exc)
            result = None

        if entry["status"] == "success":
            self._save_profit_fee_accrual({"pending_wei": 0, "sale_tx_hashes": []})

        try:
            self._record_profit_fee(entry)
        except OSError as exc:
            logger.error("Could not persist profit-fee audit record: %s", exc)

        if entry["status"] == "success":
            logger.info(
                "💸 Profit fee sent: %.2f%% = %.8f %s → %s (tx: %s)",
                fee_percent,
                fee_wei / 10**18,
                self.trade_token_name,
                recipient,
                entry.get("fee_tx_hash"),
            )
        else:
            logger.error(
                "Profit fee failed after sell %s: %s. The sale remains recorded; inspect data/profit_fees.json before retrying.",
                sale_tx_hash,
                entry.get("error") or "transaction failed",
            )
        return entry

    def _load_dashboard_trades(self):
        try:
            with open(self.dashboard_trades_file, "r") as handle:
                data = json.load(handle)
            return data[-50:] if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _load_dashboard_events(self):
        try:
            with open(self.dashboard_events_file, "r") as handle:
                data = json.load(handle)
            return data[-50:] if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _record_dashboard_event(self, level, code, message, **context):
        now = datetime.now().astimezone().isoformat()
        event = {
            "timestamp": now,
            "level": level if level in {"success", "warning", "error"} else "warning",
            "code": str(code)[:80],
            "message": _safe_event_message(message),
        }
        for key, value in context.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe_key = str(key)[:80]
                # A confirmed transaction hash is public and intentionally
                # rendered as an explorer link by the dashboard. It has the
                # same 0x + 64-hex shape as a private key, so exempt only this
                # explicitly named field after strict validation.
                if safe_key == "tx_hash" and isinstance(value, str) and _TX_HASH_RE.fullmatch(value):
                    event[safe_key] = value
                else:
                    event[safe_key] = _safe_event_message(value) if isinstance(value, str) else value

        with self._dashboard_event_lock:
            if self.dashboard_events:
                previous = self.dashboard_events[-1]
                same_message = previous.get("code") == event["code"] and previous.get("message") == event["message"]
                same_transaction = not event.get("tx_hash") or previous.get("tx_hash") == event.get("tx_hash")
                if same_message and same_transaction:
                    previous["timestamp"] = now
                    previous["count"] = int(previous.get("count", 1)) + 1
                else:
                    self.dashboard_events.append(event)
            else:
                self.dashboard_events.append(event)
            self.dashboard_events = self.dashboard_events[-50:]
            try:
                os.makedirs(os.path.dirname(self.dashboard_events_file), exist_ok=True)
                temp_file = self.dashboard_events_file + ".tmp"
                with open(temp_file, "w") as handle:
                    json.dump(self.dashboard_events, handle, indent=2)
                os.replace(temp_file, self.dashboard_events_file)
            except OSError:
                pass

    def _record_dashboard_trade(
        self, side, eth_amount, token_amount, price, tx_hash,
        profit_eth=None, gas_fee_eth=None,
    ):
        trade = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "side": side,
            "eth_amount": float(eth_amount),
            "token_amount": float(token_amount),
            "price": float(price),
            "tx_hash": str(tx_hash),
        }
        if profit_eth is not None:
            trade["profit_eth"] = float(profit_eth)
        if gas_fee_eth is not None:
            trade["gas_fee_eth"] = float(gas_fee_eth)
        self.dashboard_trades = (self.dashboard_trades + [trade])[-50:]
        try:
            os.makedirs(os.path.dirname(self.dashboard_trades_file), exist_ok=True)
            temp_file = self.dashboard_trades_file + ".tmp"
            with open(temp_file, "w") as handle:
                json.dump(self.dashboard_trades, handle, indent=2)
            os.replace(temp_file, self.dashboard_trades_file)
        except OSError as exc:
            logger.warning(f"Could not persist dashboard trade history: {exc}")
        logger.info(f"Wallet: {self.wallet.address}")
        logger.info(f"Trading: {self.config.token_symbol}")
        logger.info(f"Max active positions: {self.config.max_active_positions}")
    
    def _setup_logging(self):
        """Configure logging based on config settings."""
        minimal = getattr(self.config, 'minimal_logs', False)
        
        # Setup log file path
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        self.log_filename = os.path.join(log_dir, f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        
        # Set logger level from config
        log_level = getattr(self.config, 'log_level', 'INFO')
        logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        
        # Clear any existing handlers
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # File handler always gets full timestamps
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(file_handler)
        
        # Console handler - minimal or full format
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        if minimal:
            console_handler.setFormatter(logging.Formatter('%(message)s'))
        else:
            console_handler.setFormatter(logging.Formatter(
                '%(asctime)s | %(levelname)s | %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            ))
        logger.addHandler(console_handler)

        event_handler = DashboardEventHandler(self._record_dashboard_event)
        logger.addHandler(event_handler)
        
        logger.info(f"Logging to: {self.log_filename}")
    
    def load_positions(self):
        """Load positions from JSON file."""
        # Check if gridless mode is enabled
        if getattr(self.config, 'use_gridless', False):
            # In gridless mode, initialize empty positions (loaded dynamically)
            self.positions = {}
            logger.info("Gridless mode: positions loaded dynamically")
            return
        
        # Classic grid mode - load from file
        try:
            with open(self.positions_file, 'r') as f:
                self.positions = json.load(f)
            logger.info(f"Loaded {len(self.positions)} positions")
        except FileNotFoundError:
            logger.error(f"No positions file found. Run generate_grid.py first!")
            raise
    
    def save_positions(self):
        """Save positions to JSON file."""
        with open(self.positions_file, 'w') as f:
            json.dump(self.positions, f, indent=2)
    
    @_with_swap_provider_fallback
    def get_token_price(self):
        """Get current token price in ETH/WETH using the lighter /price endpoint."""
        # Use /price endpoint for price discovery (doesn't count against quote-to-trade metrics)
        if self.provider.capabilities.price_requires_taker:
            result = self.api_client.get_quote(
                sell_token=self.trade_token_address,
                buy_token=self.config.token_address,
                sell_amount=10**15,  # 0.001 ETH/WETH
                taker_address=self.wallet.address,
                apply_jitter_to_price=False,
            )
            if result.success and result.price:
                price = result.price * self.token_unit / 10**18
                logger.debug(f"API price: {price}")
                return price
            else:
                logger.debug(f"API price failed: success={result.success}, price={result.price}, error={result.error}")
            return None
        else:
            raw_price = self.provider.get_price(
                sell_token=self.trade_token_address,
                buy_token=self.config.token_address,
                sell_amount=10**15,  # 0.001 ETH/WETH
            )
            return raw_price * self.token_unit / 10**18 if raw_price is not None else None
    
    def check_buys(self, price):
        """Check for buy opportunities."""
        # Gridless mode
        if getattr(self.config, 'use_gridless', False):
            return self._check_buys_gridless(price)
        
        # Get available ETH/WETH
        if getattr(self.config, 'use_eth_trading', False):
            trade_balance = self.wallet.get_eth_balance()
            gas_reserve = getattr(self.config, 'eth_gas_reserve', 0.001)
            trade_balance = max(0, trade_balance - gas_reserve)
        else:
            trade_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        
        if trade_balance < 0.001:
            logger.warning(f"Low {self.trade_token_name} balance: {trade_balance:.6f}")
            return
        
        # Check max active positions limit
        active_positions = sum(1 for p in self.positions.values() if p['balance'] > 0)
        if active_positions >= self.config.max_active_positions:
            logger.debug(f"Max active positions reached ({active_positions}/{self.config.max_active_positions})")
            return
        
        # Find empty positions where price is in buy range
        for pos_id, pos in self.positions.items():
            if pos['balance'] == 0:  # Empty position
                # Scale: 10^9 (nano-WETH)
                buy_min = pos['buyMin'] / 10**9
                buy_max = pos['buyMax'] / 10**9
                
                # Handle first position with buyMin = 0
                if buy_min == 0:
                    buy_min = 0
                
                if buy_min <= price <= buy_max:
                    logger.info(f"Buy trigger: Position {pos_id} at price {price:.10f} (range: {buy_min:.10f} - {buy_max:.10f})")
                    self.execute_buy(pos_id, price)
                    return  # One buy per cycle
    
    def check_sells(self, price):
        """Check for sell opportunities."""
        # Gridless mode
        if getattr(self.config, 'use_gridless', False):
            return self._check_sells_gridless(price)
        
        min_profit_percent = getattr(self.config, 'min_profit_percent', 2.0)  # Default 2% minimum profit
        slippage_buffer = 1.5  # Require extra 1.5% to cover slippage
        effective_min_profit = min_profit_percent + slippage_buffer
        fast_profit = getattr(self.config, 'fast_profit', False)
        
        for pos_id, pos in self.positions.items():
            if pos['balance'] > 0:  # Has tokens
                # Scale: 10^9 (nano-WETH)
                sell_min = pos['sellMin'] / 10**9
                
                # Calculate actual profit %
                tokens = pos['balance'] / self.token_unit
                cost_weth = pos['cost'] / 10**9
                buy_price = cost_weth / tokens if tokens > 0 else 0
                current_profit = ((price - buy_price) / buy_price * 100) if buy_price > 0 else 0
                
                # FAST PROFIT MODE: Sell if profit exceeds minimum, regardless of sellMin
                if fast_profit and current_profit >= effective_min_profit:
                    logger.info(f"🚀 Fast profit trigger: Position {pos_id} at {price:.10f} (profit: {current_profit:.2f}%, sellMin: {sell_min:.10f})")
                    self.execute_sell(pos_id, price)
                    return  # One sell per cycle
                
                # STANDARD MODE: Use higher of sellMin or min profit + buffer
                required_price = max(sell_min, buy_price * (1 + effective_min_profit / 100))
                
                if price >= required_price:
                    logger.info(f"Sell trigger: Position {pos_id} at price {price:.10f} (required: {required_price:.10f}, profit: {current_profit:.2f}%)")
                    self.execute_sell(pos_id, price)
                    return  # One sell per cycle
                elif price >= sell_min:
                    logger.info(f"Sell blocked: Position {pos_id} at {price:.10f} - profit {current_profit:.2f}% < required {effective_min_profit}% (buffer for slippage)")
                    return  # Blocked - do not sell
    
    def _check_buys_gridless(self, price):
        """Gridless buy logic - buy when no positions or top position P&L <= threshold."""
        from gridless import should_buy, load_positions, add_position

        if self._taxed_token_active():
            failure_cooldown = self.config.taxed_token_failure_cooldown_seconds
            since_failure = time.time() - self.last_taxed_token_failure_time
            if self.last_taxed_token_failure_time and since_failure < failure_cooldown:
                logger.debug(
                    "Gridless: Taxed-token failure cooldown active (%.0fs < %ss)",
                    since_failure,
                    failure_cooldown,
                )
                return
        
        # Check cooldown
        time_since_last_buy = time.time() - self.last_buy_time
        if time_since_last_buy < self.gridless_buy_cooldown:
            logger.debug(f"Gridless: Buy cooldown active ({time_since_last_buy:.0f}s < {self.gridless_buy_cooldown}s)")
            return
        
        # Load gridless positions
        gridless_positions = load_positions()
        
        # Check if we should buy
        should_buy_flag, reason = should_buy(gridless_positions, price, self.config)
        if not should_buy_flag:
            logger.debug(f"Gridless: No buy - {reason}")
            return
        
        # Get available ETH/WETH
        if getattr(self.config, 'use_eth_trading', False):
            eth_balance = self.wallet.get_eth_balance()
            gas_reserve = getattr(self.config, 'eth_gas_reserve', 0.001)
            trade_balance = max(0, eth_balance - gas_reserve)
        else:
            trade_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        
        if trade_balance < 0.001:
            logger.warning(f"Gridless: Low {self.trade_token_name} balance: {trade_balance:.6f}")
            self._funding_warning = {
                "asset": self.trade_token_name,
                "trade_balance": trade_balance,
                "minimum_trade_balance": 0.001,
                "available_slots": max(0, self.config.max_active_positions - len(gridless_positions)),
                "reason": reason,
            }
            return
        
        # Calculate buy amount
        active_count = len(gridless_positions)
        available_slots = self.config.max_active_positions - active_count
        if available_slots <= 0:
            logger.debug(f"Gridless: Max positions reached ({active_count}/{self.config.max_active_positions})")
            return
        
        tradeable_pct = getattr(self.config, 'tradeable_balance_percent', 90.0) / 100.0
        buy_amount_eth = (trade_balance * tradeable_pct) / available_slots
        buy_amount_wei = int(buy_amount_eth * 10**18)
        
        logger.info(f"🎯 Gridless buy triggered: {reason}")
        logger.info(f"   Amount: {buy_amount_eth:.6f} {self.trade_token_name} ({trade_balance:.6f} × {tradeable_pct*100:.0f}% / {available_slots} slots)")
        
        # Execute the buy via execute_buy_gridless
        is_leading_edge_buy = "Leading edge" in reason
        self._execute_buy_gridless(buy_amount_eth, buy_amount_wei, price, is_leading_edge_buy)
    
    @_with_swap_provider_fallback
    def _execute_buy_gridless(self, buy_amount_eth, buy_amount_wei, price, is_leading_edge_buy=False):
        """Execute a gridless buy order."""
        from gridless import add_position
        
        # Get quote
        quote = self.api_client.build_swap_transaction(
            sell_token=self.trade_token_address,
            buy_token=self.config.token_address,
            sell_amount=buy_amount_wei,
            taker_address=self.wallet.address,
            slippage_percentage=self._swap_slippage_fraction(),
        )
        
        if not quote.success:
            logger.error(f"Gridless buy quote failed: {quote.error}")
            self._observe_token_tax_failure(quote, direction="buy")
            if self._taxed_token_active():
                self.last_taxed_token_failure_time = time.time()
            return
        
        # Load positions for execution margin check
        from gridless import load_positions as reload_positions
        gridless_positions = reload_positions()
        
        # Validate execution price is still within buy threshold margin
        # Skip for leading edge buys (buying into strength with single position)
        execution_margin_pct = getattr(self.config, 'gridless_buy_execution_margin', 50.0)  # Default 50%
        
        # Skip execution margin check for leading edge buys
        if not is_leading_edge_buy and quote.buy_amount and quote.buy_amount > 0:
            from gridless import get_buy_price, calculate_pnl
            top = None
            if gridless_positions:
                top_id, top_pos, top_price = None, None, float('inf')
                for pos_id, pos in gridless_positions.items():
                    buy_price = get_buy_price(pos, self.token_decimals)
                    if buy_price > 0 and buy_price < top_price:
                        top_price, top_id, top_pos = buy_price, pos_id, pos
                top = (top_id, top_pos) if top_id else None
            
            if top:
                # Calculate what the P&L would be at the quoted price
                tokens_at_quote = quote.buy_amount / self.token_unit
                quote_buy_price = buy_amount_eth / tokens_at_quote if tokens_at_quote > 0 else 0
                pnl_at_quote = calculate_pnl(top[1], quote_buy_price, self.token_decimals)
                buy_threshold = getattr(self.config, 'gridless_buy_threshold', -10.0)
                
                # Calculate block threshold as percentage of threshold distance from 0
                # e.g., -10% threshold with 50% margin = -10 + (10 * 0.5) = -5%
                distance_from_zero = abs(buy_threshold)
                max_recovery = distance_from_zero * (execution_margin_pct / 100.0)
                block_threshold = buy_threshold + max_recovery
                
                # Block if price recovered too much (quote P&L above block threshold)
                if pnl_at_quote > block_threshold:
                    logger.info(f"⏸️ Buy aborted: Quote P&L ({pnl_at_quote:.1f}%) recovered past {execution_margin_pct}% margin (block above {block_threshold:.1f}%)")
                    logger.info(f"   Price moved from trigger. Buy price: {quote_buy_price:.10f}, Top position buy: {get_buy_price(top[1], self.token_decimals):.10f}")
                    return

        initial_gas_limit, initial_gas_price = self._swap_gas_fields(quote, 350000)
        if not self._gas_within_hard_cap(initial_gas_limit, initial_gas_price, "buy"):
            return
        
        buy_setup_gas_wei = 0
        # Determine approval spender
        spender = quote.allowance_target or self.config.zero_x_proxy
        
        # Check/approve WETH
        # Check/approve WETH (skip for native ETH - it doesn't need approval)
        if not getattr(self.config, 'use_eth_trading', False):
            allowance = self.wallet.check_allowance(self.config.weth_address, spender, use_permit2=False)
            if allowance < buy_amount_wei:
                logger.info(f"Approving WETH to {spender[:20]}...")
                result = self.wallet.approve_token(self.config.weth_address, spender, 2**256 - 1)
                if not result.success:
                    logger.error(f"Approval failed: {result.error}")
                    return
                buy_setup_gas_wei += self._receipt_gas_cost_wei(result)
                self._seal_provider_fallback()
                # Refresh quote after approval for LI.FI
                if self.provider.capabilities.refresh_after_approval:
                    quote = self.api_client.refresh_quote(
                        sell_token=self.config.weth_address,
                        buy_token=self.config.token_address,
                        sell_amount=buy_amount_wei,
                        taker_address=self.wallet.address,
                    slippage_percentage=self._swap_slippage_fraction(),
                    )
                    if not quote.success:
                        logger.error(f"Refreshed quote failed: {quote.error}")
                        return
        
        # Some providers return pricing first and executable calldata separately.
        if self.provider.capabilities.quote_requires_preparation:
            swap_result = self.provider.prepare_swap(quote)
            if not swap_result.success:
                logger.error(f"{self.provider.name} swap preparation failed: {swap_result.error}")
                return
            quote = swap_result
        
        # Execute swap with configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit, gas_price = self._swap_gas_fields(quote, 350000)
        if not self._gas_within_hard_cap(gas_limit, gas_price, "buy"):
            return
        
        from web3 import Web3
        tx_params = {
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        }
        
        token_balance_before = self._raw_token_balance(self.config.token_address)
        result = self.wallet._send_transaction(tx_params)
        
        if result.success:
            # Record position in gridless format
            tokens_received = self._measured_token_received_raw(token_balance_before, result, quote.buy_amount)
            logger.info("Measured post-buy token receipt: %s raw units", tokens_received)
            if tokens_received <= 0:
                logger.error("Buy confirmed but no token balance increase could be reconciled")
                return
            # Use the actual sell amount from the quote in wei for precision
            principal_cost_wei = quote.sell_amount if quote.sell_amount else buy_amount_wei
            buy_gas_wei = buy_setup_gas_wei + self._receipt_gas_cost_wei(result)
            cost_wei = principal_cost_wei + buy_gas_wei
            
            logger.debug(
                "Recording position: cost_wei=%s (principal=%s + buy_gas=%s), tokens_received=%s",
                cost_wei, principal_cost_wei, buy_gas_wei, tokens_received,
            )
            logger.debug(f"Quote buy_amount: {quote.buy_amount}, sell_amount: {quote.sell_amount}")
            
            pos_id = add_position(cost_wei, tokens_received)
            
            tokens = tokens_received / self.token_unit
            economic_cost_eth = cost_wei / 10**18
            buy_price = economic_cost_eth / tokens if tokens > 0 else 0
            self.session_buys += 1
            self.last_buy_time = time.time()  # Update cooldown timer
            self._record_dashboard_trade(
                "buy", economic_cost_eth, tokens, buy_price, result.tx_hash,
                gas_fee_eth=buy_gas_wei / 10**18,
            )
            
            logger.info(f"✅ Gridless buy successful! Position #{pos_id}")
            logger.info(f"   Tokens: {tokens:.6f} {self.config.token_symbol}")
            logger.info(
                "   Economic cost: %.6f %s (principal %.6f + gas %.6f)",
                economic_cost_eth, self.trade_token_name, principal_cost_wei / 10**18, buy_gas_wei / 10**18,
            )
            logger.info(f"   Buy price: {buy_price:.10f} {self.trade_token_name}/token")
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Gridless buy failed: {result.error}")
    
    @_with_swap_provider_fallback
    def _check_sells_gridless(self, price):
        """Gridless sell logic - sell when P&L >= threshold or stoploss triggered."""
        from gridless import load_positions, find_sell_candidate, calculate_pnl, remove_position, get_buy_price
        
        # Load gridless positions
        gridless_positions = load_positions()
        if not gridless_positions:
            return
        
        # Find best sell candidate based on P&L only (quote checked at execution)
        # This allows profitable positions to sell even when aggregate portfolio is down
        sell_threshold = getattr(self.config, 'gridless_sell_threshold', 5.0)
        stoploss_enabled = getattr(self.config, 'gridless_stoploss_enabled', False)
        stoploss_threshold = getattr(self.config, 'gridless_stoploss_threshold', -25.0)
        
        best_candidate = None
        best_priority = 999
        best_pnl = float('-inf')
        
        for pos_id, pos in gridless_positions.items():
            pnl = calculate_pnl(pos, price, self.token_decimals)
            
            # Check stoploss first (highest priority)
            if stoploss_enabled and pnl <= stoploss_threshold:
                if best_priority > 0 or pnl > best_pnl:
                    best_candidate = (pos_id, pos, f"STOPLOSS: {pnl:.1f}%")
                    best_priority = 0
                    best_pnl = pnl
            # Check profit target
            elif pnl >= sell_threshold:
                if best_priority > 1 or pnl > best_pnl:
                    best_candidate = (pos_id, pos, f"PROFIT: {pnl:.1f}%")
                    best_priority = 1
                    best_pnl = pnl
        
        if best_candidate is None:
            return
        
        pos_id, pos, reason = best_candidate
        
        # Verify with individual position quote before executing
        balance = pos.get('balance', 0)
        if balance <= 0:
            return
        if not self._wallet_can_cover_sell(balance, pos_id):
            return
            
        quote = self.api_client.build_swap_transaction(
            sell_token=self.config.token_address,
            buy_token=self.trade_token_address,
            sell_amount=balance,
            taker_address=self.wallet.address,
            slippage_percentage=self._swap_slippage_fraction(),
        )
        
        if not quote.success:
            logger.warning(f"Sell candidate #{pos_id} but quote failed: {quote.error}")
            self._observe_token_tax_failure(
                quote,
                direction="sell",
                position_id=pos_id,
            )
            return

        quote_provider = self.api_client.name
        quote_return_wei = self._taxed_quote_return_wei(quote)
        consistency_block = self._sell_quote_consistency_guard(
            pos_id, quote_provider, quote_return_wei,
        )
        if consistency_block:
            self._sell_attempt = consistency_block
            logger.warning(
                "Sell quote provider changed for position #%s (%s -> %s, %.2f%% divergence); "
                "blocking this poll pending a consistent executable quote",
                pos_id,
                consistency_block["previous_quote_provider"],
                quote_provider,
                consistency_block["quote_divergence_percent"],
            )
            return
        
        # Check min profit requirement against individual position quote
        # Support both cost_wei (new) and cost (legacy nano-ETH)
        cost_wei = pos.get('cost_wei', 0)
        if cost_wei <= 0 and 'cost' in pos:
            old_cost = pos.get('cost', 0)
            if old_cost > 0:
                cost_wei = old_cost * 10**9
        cost_eth = cost_wei / 1e18
        min_profit = getattr(self.config, 'min_profit_percent', 1.5)
        min_profit_eth = cost_eth * (min_profit / 100)
        quote_return_eth = quote_return_wei / 10**18
        quote_profit_eth = quote_return_eth - cost_eth
        projected_gas_eth = self._projected_gas_cost_wei(quote) / 10**18
        projected_net_profit_eth = quote_profit_eth - projected_gas_eth
        
        if projected_net_profit_eth < min_profit_eth:
            buy_price = get_buy_price(pos, self.token_decimals)
            pnl_at_check = calculate_pnl(pos, price, self.token_decimals)
            logger.info(
                "⏸️  Position #%s at %.1f%% P&L but projected net profit "
                "(%.6f after %.6f gas) < min (%.6f) - skipping",
                pos_id, pnl_at_check, projected_net_profit_eth, projected_gas_eth, min_profit_eth,
            )
            self._sell_attempt = {
                "status": "quote_below_minimum",
                "position_id": str(pos_id),
                "quote_provider": quote_provider,
                "pnl_percent": round(pnl_at_check, 2),
                "quoted_profit_eth": round(quote_profit_eth, 8),
                "projected_gas_eth": round(projected_gas_eth, 8),
                "projected_net_profit_eth": round(projected_net_profit_eth, 8),
                "minimum_profit_eth": round(min_profit_eth, 8),
            }
            return
        
        logger.info(f"🎯 Gridless sell trigger: Position #{pos_id} - {reason}")
        self._execute_sell_gridless(pos_id, pos, price, quote)
    
    def _execute_sell_gridless(self, pos_id, pos, price, pre_fetched_quote=None):
        """Execute a gridless sell order."""
        from gridless import remove_position, calculate_pnl
        
        balance = pos.get('balance', 0)
        # Support both cost_wei (new) and cost (legacy nano-ETH)
        cost_wei = pos.get('cost_wei', 0)
        if cost_wei <= 0 and 'cost' in pos:
            old_cost = pos.get('cost', 0)
            if old_cost > 0:
                cost_wei = old_cost * 10**9
        
        if balance <= 0 or cost_wei <= 0:
            logger.warning(f"Invalid position #{pos_id}: balance={balance}, cost_wei={cost_wei}")
            return
        
        tokens = balance / self.token_unit
        cost_eth = cost_wei / 1e18
        buy_price = cost_eth / tokens if tokens > 0 else 0
        pnl = calculate_pnl(pos, price, self.token_decimals)
        
        # Moonbag logic
        moonbag_pct = getattr(self.config, 'moonbag_percentage', 0)
        if moonbag_pct > 0:
            moonbag_tokens = int(balance * moonbag_pct / 100)
            sell_amount = balance - moonbag_tokens
            sell_tokens = sell_amount / self.token_unit
            logger.info(f"🌙 Moonbag: Keeping {moonbag_tokens/self.token_unit:.4f} ({moonbag_pct}%), selling {sell_tokens:.4f}")
        else:
            sell_amount = balance
            sell_tokens = tokens
            moonbag_tokens = 0
        
        sold_cost_eth = cost_eth * (sell_tokens / tokens) if tokens > 0 else 0
        expected_eth = sell_tokens * price
        profit_eth = expected_eth - sold_cost_eth
        
        logger.info(f"💰 Gridless sell position #{pos_id}:")
        logger.info(f"   Position data: cost_wei={cost_wei}, balance={balance} wei")
        logger.info(f"   Calculated: cost={cost_eth:.6f} {self.trade_token_name}, tokens={tokens:.6f}, buy_price={buy_price:.10f}")
        logger.info(f"   Selling: {sell_tokens:.6f} tokens")
        logger.info(f"   Buy price: {buy_price:.10f}, Current: {price:.10f}")
        logger.info(f"   Expected: {expected_eth:.6f} {self.trade_token_name}, Profit: {profit_eth:.6f} ({pnl:+.2f}%)")
        
        # Use pre-fetched quote if available (for moonbag, need to re-quote with different amount)
        if pre_fetched_quote and moonbag_pct == 0 and sell_amount == balance:
            quote = pre_fetched_quote
        else:
            # Get fresh quote (for moonbag or if no pre-fetched quote)
            quote = self.api_client.build_swap_transaction(
                sell_token=self.config.token_address,
                buy_token=self.trade_token_address,
                sell_amount=sell_amount,
                taker_address=self.wallet.address,
                slippage_percentage=self._swap_slippage_fraction(),
            )
        
        if not quote.success:
            logger.error(f"Gridless sell quote failed: {quote.error}")
            self._observe_token_tax_failure(quote, direction="sell", position_id=pos_id)
            return
        
        # Validate minimum profit
        min_profit = getattr(self.config, 'min_profit_percent', 1.5)
        min_profit_eth = sold_cost_eth * (min_profit / 100)
        min_return_eth = self._minimum_gas_aware_return_wei(
            int(round(sold_cost_eth * 10**18)), quote, min_profit
        ) / 10**18
        quote_return_eth = self._taxed_quote_return_wei(quote) / 10**18
        
        # Skip min_profit check for stoploss
        stoploss_enabled = getattr(self.config, 'gridless_stoploss_enabled', False)
        stoploss_threshold = getattr(self.config, 'gridless_stoploss_threshold', -25.0)
        is_stoploss = stoploss_enabled and pnl <= stoploss_threshold
        
        if not is_stoploss and quote_return_eth < min_return_eth:
            logger.warning(f"❌ Sell aborted: Quote ({quote_return_eth:.6f}) < min ({min_return_eth:.6f})")
            return

        initial_gas_limit, initial_gas_price = self._swap_gas_fields(quote, 300000)
        if not self._gas_within_hard_cap(initial_gas_limit, initial_gas_price, "sell"):
            return
        
        sell_setup_gas_wei = 0
        # Providers with API-managed approvals return the required approval txs.
        if self.provider.capabilities.api_managed_approval:
                # Step 1: Check approval via Uniswap API
                approval_result = self.api_client.check_approval(
                    token=self.config.token_address,
                    amount=sell_amount,
                    wallet=self.wallet.address,
                )
                
                if "error" in approval_result:
                    logger.error(f"Approval check failed: {approval_result.get('error')}")
                    return
                
                # Step 2: Execute approval transactions if needed
                # Uniswap check_approval returns {"approval": tx} when approval is needed, null otherwise
                cancel_tx = approval_result.get("cancel")
                approval_tx = approval_result.get("approval")
                
                # Helper function to build EIP-1559 transaction with fresh fees
                def build_eip1559_tx(api_tx):
                    from web3 import Web3
                    # Get fresh block data
                    latest_block = self.wallet.w3.eth.get_block("latest")
                    base_fee = int(latest_block.get("baseFeePerGas", 0))
                    
                    # Get priority fee (with fallback)
                    try:
                        priority_fee = int(self.wallet.w3.eth.max_priority_fee)
                    except Exception:
                        priority_fee = 1_000_000  # 0.001 gwei fallback
                    priority_fee = max(priority_fee, 1_000_000)
                    
                    # Calculate max fee with 2x headroom
                    max_fee = base_fee * 2 + priority_fee
                    
                    # Build transaction
                    tx = {
                        "from": Web3.to_checksum_address(api_tx.get("from", self.wallet.address)),
                        "to": Web3.to_checksum_address(api_tx.get("to")),
                        "data": api_tx.get("data"),
                        "value": int(api_tx.get("value", "0x0"), 16) if isinstance(api_tx.get("value"), str) else int(api_tx.get("value", 0)),
                        "chainId": int(api_tx.get("chainId", self.config.chain_id)),
                        "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address, "pending"),
                        "maxPriorityFeePerGas": priority_fee,
                        "maxFeePerGas": max_fee,
                        "type": 2,  # EIP-1559
                    }
                    
                    # Estimate gas with headroom
                    try:
                        estimated_gas = self.wallet.w3.eth.estimate_gas(tx)
                        tx["gas"] = int(estimated_gas * 1.2)  # 20% headroom
                    except Exception as e:
                        logger.warning(f"Gas estimation failed: {e}, using default")
                        tx["gas"] = int(api_tx.get("gas", 100000))
                    
                    logger.info(f"Approval fees: base={base_fee} priority={priority_fee} max={max_fee} nonce={tx['nonce']} gas={tx['gas']}")
                    return tx
                
                # Handle cancel transaction first (if present)
                if cancel_tx is not None:
                    logger.info("Approval cancel/reset transaction required")
                    tx = build_eip1559_tx(cancel_tx)
                    result = self.wallet._send_transaction(tx)
                    if not result.success:
                        logger.error(f"Cancel transaction failed: {result.error}")
                        return
                    sell_setup_gas_wei += self._receipt_gas_cost_wei(result)
                    self._seal_provider_fallback()
                    logger.info(f"Cancel transaction confirmed: {result.tx_hash}")
                    # Wait for confirmation
                    import time
                    time.sleep(3)
                
                # Handle approval transaction
                if approval_tx is not None:
                    logger.info("ERC20 approval transaction required")
                    tx = build_eip1559_tx(approval_tx)
                    result = self.wallet._send_transaction(tx)
                    if not result.success:
                        logger.error(f"Approval transaction failed: {result.error}")
                        return
                    sell_setup_gas_wei += self._receipt_gas_cost_wei(result)
                    self._seal_provider_fallback()
                    logger.info(f"Approval transaction confirmed: {result.tx_hash}")
                    # Wait for confirmation
                    import time
                    time.sleep(3)
                else:
                    logger.info("No approval transaction required")
                
                # Step 3b: Verify allowance if approval was sent
                if approval_tx is not None:
                    # Decode spender from approval calldata (0x095ea7b3 = approve(address,uint256))
                    import binascii
                    data = approval_tx.get("data", "")
                    if data.startswith("0x095ea7b3") or data.startswith("095ea7b3"):
                        # spender is the first 32-byte word after selector
                        clean_data = data[10:] if data.startswith("0x") else data[8:]
                        spender_word = clean_data[:64]
                        spender_addr = "0x" + spender_word[-40:]
                        logger.info(f"Decoded spender from approval: {spender_addr}")
                        
                        # Check allowance on-chain
                        from web3 import Web3
                        token_contract = self.wallet.w3.eth.contract(
                            address=Web3.to_checksum_address(self.config.token_address),
                            abi=[{
                                "name": "allowance",
                                "type": "function",
                                "stateMutability": "view",
                                "inputs": [
                                    {"name": "owner", "type": "address"},
                                    {"name": "spender", "type": "address"},
                                ],
                                "outputs": [{"name": "", "type": "uint256"}],
                            }]
                        )
                        confirmed_allowance = token_contract.functions.allowance(
                            self.wallet.address,
                            Web3.to_checksum_address(spender_addr),
                        ).call()
                        logger.info(f"Confirmed allowance: {confirmed_allowance} >= required {sell_amount}")
                        if confirmed_allowance < sell_amount:
                            logger.error("Approval succeeded but allowance is insufficient!")
                            return
                
                # Step 4: Get fresh quote after approval
                quote = self.api_client.get_quote(
                    sell_token=self.config.token_address,
                    buy_token=self.trade_token_address,
                    sell_amount=sell_amount,
                    taker_address=self.wallet.address,
                        slippage_percentage=self._swap_slippage_fraction(),
                )
                if not quote.success:
                    logger.error(f"Fresh quote after approval failed: {quote.error}")
                    return
                
                # Step 5: Get swap transaction
                swap_result = self.provider.prepare_swap(quote)
                if not swap_result.success:
                    logger.error(f"{self.provider.name} swap preparation failed: {swap_result.error}")
                    return
                quote = swap_result
        else:
            # Providers without API-managed approvals use standard ERC-20 approval.
            spender = quote.allowance_target or self.config.zero_x_proxy
            token_allowance = self.wallet.check_allowance(self.config.token_address, spender, use_permit2=False)
            if token_allowance < sell_amount:
                logger.info(f"Approving {self.config.token_symbol} to {spender[:20]}...")
                result = self.wallet.approve_token(self.config.token_address, spender, 2**256 - 1)
                if not result.success:
                    logger.error(f"Approval failed: {result.error}")
                    return
                sell_setup_gas_wei += self._receipt_gas_cost_wei(result)
                self._seal_provider_fallback()
            
                # Refresh provider routes after approval when required.
                if self.provider.capabilities.refresh_after_approval:
                    quote = self.api_client.refresh_quote(
                        sell_token=self.config.token_address,
                        buy_token=self.trade_token_address,
                        sell_amount=sell_amount,
                        taker_address=self.wallet.address,
                        slippage_percentage=self._swap_slippage_fraction(),
                    )
                    if not quote.success:
                        logger.error(f"Refreshed quote failed: {quote.error}")
                        return

        # Approval/refresh can replace both route calldata and gas estimate.
        # Re-run the economic guard against the final transaction immediately
        # before broadcast; only an explicit stop-loss may bypass profitability.
        # Fetch Normal gas at the final broadcast boundary. The same exact
        # price is used for both the economic guard and the signed transaction.
        gas_limit, gas_price = self._swap_gas_fields(quote, 300000)
        if not is_stoploss:
            final_return_wei = self._taxed_quote_return_wei(quote)
            final_minimum_wei = self._minimum_gas_aware_return_wei(
                int(round(sold_cost_eth * 10**18)), quote, min_profit,
                setup_gas_wei=sell_setup_gas_wei,
                projected_gas_cost_wei=gas_limit * gas_price,
            )
            if final_return_wei < final_minimum_wei:
                logger.warning(
                    "❌ Sell aborted after route refresh: return %.8f ETH < gas-aware minimum %.8f ETH",
                    final_return_wei / 10**18, final_minimum_wei / 10**18,
                )
                return
        
        # Execute swap with configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        if not self._gas_within_hard_cap(gas_limit, gas_price, "sell"):
            return

        from web3 import Web3
        trade_balance_before = self._raw_trade_balance()
        result = self.wallet._send_transaction({
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        })
        
        if result.success:
            received_wei = self._measured_trade_received_wei(
                trade_balance_before, result, self._taxed_quote_return_wei(quote)
            )
            logger.info("Measured trade-token receipt: %s wei", received_wei)
            eth_received = received_wei / 10**18
            sold_cost_wei = int(round(sold_cost_eth * 10**18))
            profit_wei = self._net_sale_profit_wei(
                received_wei, sold_cost_wei, result, setup_gas_wei=sell_setup_gas_wei
            )
            actual_profit = profit_wei / 10**18
            
            self.session_sells += 1
            self.session_profit_weth += actual_profit
            try:
                self.profit_tracker.record_sale(profit_wei, result.tx_hash)
            except (OSError, ValueError) as exc:
                logger.error(f"Could not persist realized profit: {exc}")
            
            # Remove position
            remove_position(pos_id)
            
            if moonbag_tokens > 0:
                logger.info(f"   Moonbag: {moonbag_tokens/self.token_unit:.4f} tokens to wallet")
            
            profit_pct = (actual_profit / sold_cost_eth * 100) if sold_cost_eth > 0 else 0
            total_sell_gas_wei = sell_setup_gas_wei + self._receipt_gas_cost_wei(result)
            self._record_dashboard_trade(
                "sell", eth_received, sell_tokens, price, result.tx_hash,
                actual_profit, gas_fee_eth=total_sell_gas_wei / 10**18,
            )
            logger.info(f"✅ Gridless sell successful! Profit: {actual_profit:.6f} {self.trade_token_name} ({profit_pct:+.2f}%)")

            self._charge_profit_fee(profit_wei, result.tx_hash)
            
            # Reset buy cooldown so we can buy again immediately after selling
            self.last_buy_time = 0
            logger.debug(f"🔄 Buy cooldown reset after sell")
            
            # Banking
            bank_pct = getattr(self.config, 'bank_percentage', 0)
            if bank_pct > 0 and actual_profit > 0:
                bank_amount = actual_profit * bank_pct / 100
                logger.info(f"🏦 Banking {bank_pct}% of profit = {bank_amount:.6f} {self.trade_token_name} → USDG")
                self.bank_profit(bank_amount, profit_budget_eth=actual_profit)
            
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Gridless sell failed: {result.error}")
    
    @_with_swap_provider_fallback
    def execute_buy(self, pos_id, price):
        """Execute a buy order."""
        pos = self.positions[pos_id]
        
        # Calculate buy amount (divide available ETH/WETH by available slots up to max_active_positions)
        if getattr(self.config, 'use_eth_trading', False):
            eth_balance = self.wallet.get_eth_balance()
            gas_reserve = getattr(self.config, 'eth_gas_reserve', 0.001)
            trade_balance = max(0, eth_balance - gas_reserve)
        else:
            trade_balance, _ = self.wallet.get_token_balance(self.config.weth_address)
        
        active_positions = sum(1 for p in self.positions.values() if p['balance'] > 0)
        available_slots = self.config.max_active_positions - active_positions
        
        if available_slots <= 0:
            logger.debug(f"Max active positions reached ({active_positions}/{self.config.max_active_positions})")
            return
        
        # Use configured % of available balance divided by available slots
        tradeable_pct = getattr(self.config, 'tradeable_balance_percent', 90.0) / 100.0
        buy_amount_eth = (trade_balance * tradeable_pct) / available_slots
        buy_amount_wei = int(buy_amount_eth * 10**18)
        
        logger.info(f"Buying position {pos_id}: {buy_amount_eth:.6f} {self.trade_token_name} ({trade_balance:.6f} {self.trade_token_name} × {tradeable_pct*100:.0f}% / {available_slots} slots)")
        
        # Get quote FIRST to know the approval spender
        logger.info("Getting quote...")
        quote = self.api_client.build_swap_transaction(
            sell_token=self.trade_token_address,
            buy_token=self.config.token_address,
            sell_amount=buy_amount_wei,
            taker_address=self.wallet.address,
            slippage_percentage=self._swap_slippage_fraction(),
        )
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            self._observe_token_tax_failure(quote, direction="buy")
            return

        initial_gas_limit, initial_gas_price = self._swap_gas_fields(quote, 350000)
        if not self._gas_within_hard_cap(initial_gas_limit, initial_gas_price, "buy"):
            return
        
        buy_setup_gas_wei = 0
        # Determine approval spender - use quote's allowance_target if available (LI.FI)
        spender = quote.allowance_target or self.config.zero_x_proxy
        
        # Check ERC20 approval (skip for native ETH - it doesn't need approval)
        if not getattr(self.config, 'use_eth_trading', False):
            allowance = self.wallet.check_allowance(
                self.config.weth_address,
                spender,
                use_permit2=False
            )
            logger.info(f"WETH allowance to {spender[:20]}...: {allowance}")
            if allowance < buy_amount_wei:
                logger.info(f"Approving WETH to {spender[:20]}...")
                result = self.wallet.approve_token(
                    self.config.weth_address,
                    spender,
                    2**256 - 1
                )
                if not result.success:
                    logger.error(f"Approval failed: {result.error}")
                    return
                buy_setup_gas_wei += self._receipt_gas_cost_wei(result)
                self._seal_provider_fallback()
                
                # Refresh provider routes after approval when required.
                if self.provider.capabilities.refresh_after_approval:
                    logger.info(f"Refreshing {self.provider.name} quote after approval...")
                    quote = self.api_client.refresh_quote(
                        sell_token=self.config.weth_address,
                        buy_token=self.config.token_address,
                        sell_amount=buy_amount_wei,
                        taker_address=self.wallet.address,
                        slippage_percentage=self._swap_slippage_fraction(),
                    )
                    if not quote.success:
                        logger.error(f"Refreshed quote failed: {quote.error}")
                        return
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            self._observe_token_tax_failure(quote, direction="buy")
            return
        
        # Prepare executable calldata when the provider separates quote and swap.
        if self.provider.capabilities.quote_requires_preparation:
            swap_result = self.provider.prepare_swap(quote)
            if not swap_result.success:
                logger.error(f"{self.provider.name} swap preparation failed: {swap_result.error}")
                return
            quote = swap_result

        # Execute swap with checksummed addresses and configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit, gas_price = self._swap_gas_fields(quote, 350000)
        if not self._gas_within_hard_cap(gas_limit, gas_price, "buy"):
            return
        
        from web3 import Web3
        tx_params = {
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        }
        
        logger.info(f"Sending tx to {quote.to} with gas {gas_limit}")
        token_balance_before = self._raw_token_balance(self.config.token_address)
        result = self.wallet._send_transaction(tx_params)
        
        if result.success:
            # Update position - store actual WETH cost (not price) in nano-WETH
            tokens_received = self._measured_token_received_raw(token_balance_before, result, quote.buy_amount)
            logger.info("Measured post-buy token receipt: %s raw units", tokens_received)
            if tokens_received <= 0:
                logger.error("Buy confirmed but no token balance increase could be reconciled")
                return
            tokens = tokens_received / self.token_unit
            self.positions[pos_id]['balance'] = tokens_received
            # Cost = actual WETH spent for profit calculation (in wei for precision)
            principal_cost_wei = quote.sell_amount if quote.sell_amount else buy_amount_wei
            buy_gas_wei = buy_setup_gas_wei + self._receipt_gas_cost_wei(result)
            cost_wei = principal_cost_wei + buy_gas_wei
            self.positions[pos_id]['cost_wei'] = cost_wei
            # Keep legacy 'cost' field for backward compatibility
            self.positions[pos_id]['cost'] = cost_wei // 10**9
            self.save_positions()
            
            # Calculate buy price for logging
            economic_cost_eth = cost_wei / 10**18
            buy_price = economic_cost_eth / tokens if tokens > 0 else 0
            
            # Track session stats
            self.session_buys += 1
            self._record_dashboard_trade(
                "buy", economic_cost_eth, tokens, buy_price, result.tx_hash,
                gas_fee_eth=buy_gas_wei / 10**18,
            )
            
            logger.info(f"✅ Buy successful!")
            logger.info(f"   Position: #{pos_id}")
            logger.info(f"   Tokens: {tokens:.6f} {self.config.token_symbol}")
            logger.info(
                "   Economic cost: %.6f %s (principal %.6f + gas %.6f)",
                economic_cost_eth, self.trade_token_name, principal_cost_wei / 10**18, buy_gas_wei / 10**18,
            )
            logger.info(f"   Buy price: {buy_price:.10f} {self.trade_token_name} per token")
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Buy failed: {result.error}")
    
    @_with_swap_provider_fallback
    def execute_sell(self, pos_id, price):
        """Execute a sell order with moonbag and banking."""
        pos = self.positions[pos_id]
        total_balance = pos['balance']
        total_tokens = total_balance / self.token_unit
        
        # Validate position has tokens and cost basis
        cost_wei = pos.get('cost_wei', pos.get('cost', 0) * 10**9)
        if total_balance <= 0 or cost_wei <= 0:
            logger.warning(f"Skipping sell for position {pos_id}: balance={total_balance}, cost_wei={cost_wei}")
            return
        
        # Cost is ETH/WETH spent (in wei)
        cost_eth = cost_wei / 10**18
        buy_price = cost_eth / total_tokens if total_tokens > 0 else 0
        
        # Calculate profit
        if buy_price > 0:
            profit_percent = ((price - buy_price) / buy_price) * 100
        else:
            profit_percent = 0
        
        # Moonbag: Keep X% of tokens, sell the rest
        moonbag_pct = getattr(self.config, 'moonbag_percentage', 0)
        if moonbag_pct > 0:
            moonbag_tokens = int(total_balance * moonbag_pct / 100)
            sell_amount = total_balance - moonbag_tokens
            sell_tokens = sell_amount / self.token_unit
            logger.info(f"🌙 Moonbag: Keeping {moonbag_tokens / self.token_unit:.4f} tokens ({moonbag_pct}%), selling {sell_tokens:.4f}")
        else:
            sell_amount = total_balance
            sell_tokens = total_tokens
            moonbag_tokens = 0

        if not self._wallet_can_cover_sell(sell_amount, pos_id):
            return
        
        # Calculate expected ETH/WETH return (proportional to sold amount)
        expected_eth = sell_tokens * price
        # Cost basis for sold portion only
        sold_cost_eth = cost_eth * (sell_tokens / total_tokens) if total_tokens > 0 else 0
        profit_eth = expected_eth - sold_cost_eth
        
        logger.info(f"💰 Selling position {pos_id}:")
        logger.info(f"   Total tokens: {total_tokens:.6f}")
        logger.info(f"   Selling: {sell_tokens:.6f}")
        logger.info(f"   Buy price: {buy_price:.10f} {self.trade_token_name}")
        logger.info(f"   Current: {price:.10f} {self.trade_token_name}")
        logger.info(f"   Cost basis (sold): {sold_cost_eth:.6f} {self.trade_token_name}")
        logger.info(f"   Expected return: {expected_eth:.6f} {self.trade_token_name}")
        logger.info(f"   Profit: {profit_eth:.6f} {self.trade_token_name} ({profit_percent:+.2f}%)")
        
        # Get quote
        quote = self.provider.build_swap_transaction(
            sell_token=self.config.token_address,
            buy_token=self.trade_token_address,
            sell_amount=sell_amount,
            taker_address=self.wallet.address,
            slippage_percentage=self._swap_slippage_fraction(),
        )
        
        if not quote.success:
            logger.error(f"Quote failed: {quote.error}")
            self._observe_token_tax_failure(quote, direction="sell", position_id=pos_id)
            return

        # No approval may be broadcast until the quoted sale can repay its
        # economic cost, projected swap gas, and configured minimum profit.
        # This first guard deliberately runs before allowance inspection.
        min_profit_percent = getattr(self.config, 'min_profit_percent', 2.0)
        preapproval_return_wei = self._taxed_quote_return_wei(quote)
        preapproval_minimum_wei = self._minimum_gas_aware_return_wei(
            int(round(sold_cost_eth * 10**18)), quote, min_profit_percent
        )
        if preapproval_return_wei < preapproval_minimum_wei:
            logger.warning(
                "❌ Sell ABORTED before approval: return %.8f ETH < gas-aware minimum %.8f ETH",
                preapproval_return_wei / 10**18, preapproval_minimum_wei / 10**18,
            )
            return

        initial_gas_limit, initial_gas_price = self._swap_gas_fields(quote, 300000)
        if not self._gas_within_hard_cap(initial_gas_limit, initial_gas_price, "sell"):
            return
        
        # Determine approval spender - use quote's allowance_target if available (LI.FI)
        # otherwise fall back to zero_x_proxy (0x Protocol)
        spender = quote.allowance_target or self.config.zero_x_proxy
        
        sell_setup_gas_wei = 0
        # Check/approve token for selling
        token_allowance = self.wallet.check_allowance(
            self.config.token_address,
            spender,
            use_permit2=False
        )
        if token_allowance < sell_amount:
            logger.info(f"Approving {self.config.token_symbol} to {spender[:20]}...")
            result = self.wallet.approve_token(
                self.config.token_address,
                spender,
                2**256 - 1
            )
            if not result.success:
                logger.error(f"Token approval failed: {result.error}")
                return
            sell_setup_gas_wei += self._receipt_gas_cost_wei(result)
            self._seal_provider_fallback()
            
            # Refresh provider routes after approval when required.
            # Gas prices, calldata, and routes may have changed
            if self.provider.capabilities.refresh_after_approval:
                logger.info(f"Refreshing {self.provider.name} quote after approval...")
                quote = self.api_client.refresh_quote(
                    sell_token=self.config.token_address,
                    buy_token=self.trade_token_address,
                    sell_amount=sell_amount,
                    taker_address=self.wallet.address,
                    slippage_percentage=self._swap_slippage_fraction(),
                )
                if not quote.success:
                    logger.error(f"Refreshed quote failed: {quote.error}")
                    return
        
        # Validate quote meets minimum profit requirement after projected gas.
        min_profit_eth = sold_cost_eth * (min_profit_percent / 100)
        min_return_eth = self._minimum_gas_aware_return_wei(
            int(round(sold_cost_eth * 10**18)), quote, min_profit_percent,
            setup_gas_wei=sell_setup_gas_wei,
        ) / 10**18
        
        # quote.buy_amount is in wei
        quote_return_eth = self._taxed_quote_return_wei(quote) / 10**18
        
        if quote_return_eth < min_return_eth:
            logger.warning(f"❌ Sell ABORTED: Quote return ({quote_return_eth:.6f} {self.trade_token_name}) < minimum ({min_return_eth:.6f} {self.trade_token_name})")
            logger.warning(f"   Cost: {sold_cost_eth:.6f}, Min profit: {min_profit_eth:.6f}")
            return  # Abort - never sell at loss
        
        logger.info(f"✅ Quote validated: {quote_return_eth:.6f} {self.trade_token_name} >= {min_return_eth:.6f} {self.trade_token_name} minimum")
        
        # Prepare executable calldata when the provider separates quote and swap.
        if self.provider.capabilities.quote_requires_preparation:
            swap_result = self.provider.prepare_swap(quote)
            if not swap_result.success:
                logger.error(f"{self.provider.name} swap preparation failed: {swap_result.error}")
                return
            quote = swap_result

        # Fetch Normal gas at the final broadcast boundary. The same exact
        # price is used for both the economic guard and the signed transaction.
        gas_limit, gas_price = self._swap_gas_fields(quote, 300000)
        final_return_wei = self._taxed_quote_return_wei(quote)
        final_minimum_wei = self._minimum_gas_aware_return_wei(
            int(round(sold_cost_eth * 10**18)), quote, min_profit_percent,
            setup_gas_wei=sell_setup_gas_wei,
            projected_gas_cost_wei=gas_limit * gas_price,
        )
        if final_return_wei < final_minimum_wei:
            logger.warning(
                "❌ Sell ABORTED after transaction preparation: return %.8f ETH < "
                "gas-aware minimum %.8f ETH",
                final_return_wei / 10**18, final_minimum_wei / 10**18,
            )
            return
        
        # Execute swap with checksummed addresses and configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        if not self._gas_within_hard_cap(gas_limit, gas_price, "sell"):
            return
        
        trade_balance_before = self._raw_trade_balance()
        result = self.wallet._send_transaction({
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        })
        
        if result.success:
            # Get actual ETH/WETH received from transaction
            received_wei = self._measured_trade_received_wei(
                trade_balance_before, result, self._taxed_quote_return_wei(quote)
            )
            logger.info("Measured trade-token receipt: %s wei", received_wei)
            eth_received = received_wei / 10**18
            sold_cost_wei = int(round(sold_cost_eth * 10**18))
            profit_wei = self._net_sale_profit_wei(
                received_wei, sold_cost_wei, result, setup_gas_wei=sell_setup_gas_wei
            )
            actual_profit_eth = profit_wei / 10**18
            
            # Track session stats
            self.session_sells += 1
            self.session_profit_weth += actual_profit_eth
            try:
                self.profit_tracker.record_sale(profit_wei, result.tx_hash)
            except (OSError, ValueError) as exc:
                logger.error(f"Could not persist realized profit: {exc}")
            total_sell_gas_wei = sell_setup_gas_wei + self._receipt_gas_cost_wei(result)
            self._record_dashboard_trade(
                "sell", eth_received, sell_tokens, price, result.tx_hash,
                actual_profit_eth, gas_fee_eth=total_sell_gas_wei / 10**18,
            )
            
            # Position is always cleared to 0 after sell
            # Moonbag tokens go to wallet balance (not tracked in position)
            self.positions[pos_id]['balance'] = 0
            self.positions[pos_id]['cost'] = 0
            self.save_positions()
            
            if moonbag_tokens > 0:
                logger.info(f"   Moonbag: {moonbag_tokens / self.token_unit:.4f} tokens added to wallet balance")
            
            logger.info(f"✅ Sell successful!")
            logger.info(f"   Actual return: {eth_received:.6f} {self.trade_token_name}")
            logger.info(f"   Profit: {actual_profit_eth:.6f} {self.trade_token_name} ({(actual_profit_eth/sold_cost_eth*100) if sold_cost_eth > 0 else 0:+.2f}%)")

            self._charge_profit_fee(profit_wei, result.tx_hash)
            
            # Banking: Swap % of profit to USDG
            bank_pct = getattr(self.config, 'bank_percentage', 0)
            if bank_pct > 0 and actual_profit_eth > 0:
                bank_amount = actual_profit_eth * bank_pct / 100
                logger.info(f"🏦 Banking: Swapping {bank_pct}% of profit = {bank_amount:.6f} {self.trade_token_name} → USDG")
                self.bank_profit(bank_amount, profit_budget_eth=actual_profit_eth)
            
            logger.info(f"   Tx: {result.tx_hash}")
        else:
            logger.error(f"❌ Sell failed: {result.error}")
    
    @_with_swap_provider_fallback
    def bank_profit(self, eth_amount, profit_budget_eth=None):
        """Swap ETH/WETH profit to USDG for banking."""
        if eth_amount <= 0:
            return
        
        # Convert to wei
        eth_wei = int(eth_amount * 10**18)
        
        logger.info(f"🏦 Getting quote for banking {eth_amount:.6f} {self.trade_token_name} → USDG...")
        
        # Get quote for ETH/WETH -> USDG
        quote = self.provider.build_swap_transaction(
            sell_token=self.trade_token_address,
            buy_token=self.config.usdg_address,
            sell_amount=eth_wei,
            taker_address=self.wallet.address,
            slippage_percentage=0.01,  # 1% slippage for stable swaps
        )
        
        if not quote.success:
            logger.error(f"Banking quote failed: {quote.error}")
            return
        
        # Check minimum bank amount (in USDG - 6 decimals)
        bank_min_usdg = getattr(self.config, 'bank_min_amount', 0.5)  # Default 0.5 USDG
        expected_usdg = quote.buy_amount / 10**6 if quote.buy_amount else 0
        
        if expected_usdg < bank_min_usdg:
            logger.info(f"🏦 Banking skipped: {expected_usdg:.2f} USDG below minimum {bank_min_usdg} USDG")
            return

        initial_gas_limit, initial_gas_price = self._swap_gas_fields(quote, 300000)
        if not self._gas_within_hard_cap(initial_gas_limit, initial_gas_price, "banking"):
            return
        
        logger.info(f"🏦 Banking {eth_amount:.6f} {self.trade_token_name} → ~{expected_usdg:.2f} USDG...")
        bank_setup_gas_wei = 0
        
        # Check/approve WETH for swapping (skip for native ETH)
        if not getattr(self.config, 'use_eth_trading', False):
            spender = quote.allowance_target or self.config.zero_x_proxy
            weth_allowance = self.wallet.check_allowance(
                self.config.weth_address,
                spender,
                use_permit2=False
            )
            if weth_allowance < eth_wei:
                logger.info(f"Approving WETH to {spender[:20]} for banking...")
                result = self.wallet.approve_token(
                    self.config.weth_address,
                    spender,
                    2**256 - 1
                )
                if not result.success:
                    logger.error(f"WETH approval for banking failed: {result.error}")
                    return
                bank_setup_gas_wei += self._receipt_gas_cost_wei(result)
                self._seal_provider_fallback()
                if self.provider.capabilities.refresh_after_approval:
                    quote = self.provider.refresh_quote(
                        sell_token=self.trade_token_address,
                        buy_token=self.config.usdg_address,
                        sell_amount=eth_wei,
                        taker_address=self.wallet.address,
                        slippage_percentage=0.01,
                    )
                    if not quote.success:
                        logger.error(f"Refreshed banking quote failed: {quote.error}")
                        return
        
        # Prepare executable calldata when the provider separates quote and swap.
        if self.provider.capabilities.quote_requires_preparation:
            swap_result = self.provider.prepare_swap(quote)
            if not swap_result.success:
                logger.error(f"{self.provider.name} swap preparation failed: {swap_result.error}")
                return
            quote = swap_result
        
        # Execute banking swap with configurable gas multipliers
        # Use API's gas price estimate if available (more accurate than network average)
        gas_limit, gas_price = self._swap_gas_fields(quote, 300000)
        if not self._gas_within_hard_cap(gas_limit, gas_price, "banking"):
            return

        # Banking is profit extraction, so its principal plus gas must fit
        # entirely inside the confirmed net-profit budget from the sale.
        if profit_budget_eth is not None:
            budget_wei = int(float(profit_budget_eth) * 10**18)
            economic_cost_wei = eth_wei + bank_setup_gas_wei + gas_limit * gas_price
            if economic_cost_wei > budget_wei:
                logger.warning(
                    "🏦 Banking skipped: amount plus projected gas (%.8f ETH) exceeds "
                    "confirmed net-profit budget (%.8f ETH)",
                    economic_cost_wei / 10**18, budget_wei / 10**18,
                )
                return

        # A native-ETH banking swap spends both the amount being banked and
        # gas from the same balance. Never let profit extraction consume the
        # reserve required for the bot's next trade.
        if getattr(self.config, 'use_eth_trading', False):
            balance_wei = self.wallet.get_eth_balance_wei()
            reserve_wei = int(Decimal(str(getattr(self.config, 'eth_gas_reserve', 0))) * Decimal(10**18))
            maximum_cost_wei = int(quote.value or 0) + gas_limit * gas_price
            if balance_wei - maximum_cost_wei < reserve_wei:
                logger.warning(
                    "🏦 Banking skipped: transaction could reduce native ETH below "
                    "ETH_GAS_RESERVE (balance %.8f, required %.8f including gas, reserve %.8f ETH)",
                    balance_wei / 10**18,
                    maximum_cost_wei / 10**18,
                    reserve_wei / 10**18,
                )
                return
        
        result = self.wallet._send_transaction({
            "from": Web3.to_checksum_address(self.wallet.address),
            "to": Web3.to_checksum_address(quote.to),
            "data": quote.data,
            "value": quote.value or 0,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": self.wallet.w3.eth.get_transaction_count(self.wallet.address),
            "chainId": self.config.chain_id,
        })
        
        if result.success:
            usdg_received = quote.buy_amount / 10**6 if quote.buy_amount else 0  # USDG is 6 decimals
            logger.info(f"✅ Banked! Received {usdg_received:.2f} USDG")
            self._record_dashboard_event(
                "success",
                "usdg_banked",
                f"Banked {eth_amount:.6f} {self.trade_token_name} into {usdg_received:.2f} USDG",
                tx_hash=str(result.tx_hash),
                source_amount=eth_amount,
                source_asset=self.trade_token_name,
                usdg_amount=usdg_received,
            )
        else:
            logger.error(f"❌ Banking failed: {result.error}")
    
    def run_cycle(self):
        """Run one trading cycle."""
        self.round_count += 1
        # Ephemeral by design: a sell attempt must be re-established by this
        # round's quote check or it disappears from the next dashboard report.
        self._sell_attempt = None
        elapsed = time.time() - self.start_time
        
        # Get balances
        if getattr(self.config, 'use_eth_trading', False):
            eth_bal = self.wallet.get_eth_balance()
            weth_bal = eth_bal  # Use ETH balance for display
        else:
            weth_bal, weth_raw = self.wallet.get_token_balance(self.config.weth_address)
        token_bal, token_raw = self.wallet.get_token_balance(self.config.token_address)
        usdg_bal = None
        if self.config.usdg_address and self.config.usdg_address != "0x...":
            try:
                usdg_bal, _ = self.wallet.get_token_balance(self.config.usdg_address)
            except Exception as exc:
                # Dashboard enrichment must never interrupt trading or create a
                # recurring warning event when a public RPC is briefly limited.
                logger.debug(f"USDG balance read failed: {exc}")
        
        # Check if gridless mode is enabled
        use_gridless = getattr(self.config, 'use_gridless', False)
        
        # Count positions - balance > 0 means active (even if cost is 0, could be moonbag)
        if use_gridless:
            # Load gridless positions for display
            from gridless import load_positions
            gridless_positions = load_positions()
            active = len(gridless_positions)
            empty = 0  # Gridless doesn't have empty slots
            position_balance_raw = sum(p.get('balance', 0) for p in gridless_positions.values())
            position_balance_total = position_balance_raw / self.token_unit
        else:
            active = sum(1 for p in self.positions.values() if p['balance'] > 0)
            empty = sum(1 for p in self.positions.values() if p['balance'] == 0)
            position_balance_raw = sum(p['balance'] for p in self.positions.values())
            position_balance_total = position_balance_raw / self.token_unit
        
        # Calculate moonbag (tokens in wallet not in positions)
        moonbag_balance = (int(token_raw) - int(position_balance_raw)) / self.token_unit
        
        # Get price
        price = self.get_token_price()
        if price is None:
            logger.warning("Could not get price")
            return
        
        # Check for compact mode (tmux-friendly output)
        compact_mode = getattr(self.config, 'compact_mode', False)
        
        if compact_mode:
            # Compact output for tmux multi-pane view
            from datetime import datetime
            time_str = datetime.now().strftime('%H:%M')
            
            # Positions are shown first so the bot identity and balances remain
            # visible at the bottom of narrow tmux panes.
            if use_gridless:
                # Sort by buy price ascending for consistent display
                from gridless import get_buy_price
                active_positions = sorted(
                    [(pid, p) for pid, p in gridless_positions.items()],
                    key=lambda x: get_buy_price(x[1], self.token_decimals)
                )
                for pos_id, pos in active_positions[:3]:
                    tokens = pos.get('balance', 0) / self.token_unit
                    # Support both cost_wei (new) and cost (legacy nano-ETH)
                    cost_wei = pos.get('cost_wei', 0)
                    if cost_wei <= 0 and 'cost' in pos:
                        old_cost = pos.get('cost', 0)
                        if old_cost > 0:
                            cost_wei = old_cost * 10**9
                    cost_eth = cost_wei / 10**18
                    if tokens > 0 and cost_eth > 0:
                        buy_price = cost_eth / tokens
                        pnl = ((price - buy_price) / buy_price * 100)
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | P&L: {pnl:>+5.1f}%")
                    else:
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | N/A")
            else:
                active_positions = [(pid, p) for pid, p in self.positions.items() if p['balance'] > 0]
                for pos_id, pos in active_positions[:3]:
                    tokens = pos['balance'] / self.token_unit
                    cost_weth = pos['cost'] / 10**9
                    if tokens > 0 and cost_weth > 0:
                        buy_price = cost_weth / tokens
                        pnl = ((price - buy_price) / buy_price * 100)
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | P&L: {pnl:>+5.1f}%")
                    else:
                        logger.info(f"#{pos_id:>3}: {tokens:>6.1f} | moonbag")
            if len(active_positions) > 3:
                logger.info(f"... and {len(active_positions) - 3} more")

            # Keep the divider narrow so it does not dominate tiny phone panes.
            logger.info("-" * 12)

            # Balance/session summary, followed by a labelled footer. Keeping the
            # token on the final line makes the pane identity survive scrolling
            # and makes it obvious which pane to restart from a phone.
            logger.info(f"{time_str} R#{self.round_count}")
            balance_letter = "E" if getattr(self.config, 'use_eth_trading', False) else "W"
            logger.info(f"{balance_letter}:{weth_bal:.3f} T:{token_bal:.0f} {active}/{self.config.max_active_positions}/{active+empty}")
            logger.info(f"B:{self.session_buys} S:{self.session_sells} P:{self.session_profit_weth:.6f}")

            logger.info(f"------ {self.config.token_symbol}")
        else:
            # Verbose round summary (original format)
            balance_label = "ETH" if getattr(self.config, 'use_eth_trading', False) else "WETH"
            logger.info("=" * 70)
            logger.info(f"ROUND #{self.round_count} | {self.config.token_symbol} | Elapsed: {elapsed:.0f}s")
            logger.info("=" * 70)
            logger.info(f"💰 {balance_label} Balance: {weth_bal:.6f}")
            logger.info(f"🪙 Token Balance: {token_bal:.6f} (in positions: {position_balance_total:.4f}, moonbag: {moonbag_balance:.4f})")
            logger.info(f"📊 Price: 1 {self.config.token_symbol} = {price:.10f} {balance_label}")
            logger.info(f"📈 Positions: {active} active / {empty} empty (max active: {self.config.max_active_positions})")
            logger.info(f"📊 Session: {self.session_buys} buys, {self.session_sells} sells, {self.session_profit_weth:.6f} {balance_label} profit")
            
            # Show active positions with P&L and sell targets
            if active > 0:
                logger.info("🎯 Active Positions:")
                if use_gridless:
                    # Display gridless positions sorted by buy price ascending
                    from gridless import get_buy_price
                    sell_threshold = getattr(self.config, 'gridless_sell_threshold', 5.0)
                    sorted_positions = sorted(
                        gridless_positions.items(),
                        key=lambda x: get_buy_price(x[1], self.token_decimals)
                    )
                    for pos_id, pos in sorted_positions:
                        balance_raw = pos.get('balance', 0)
                        # Support both cost_wei (new) and cost (legacy nano-ETH)
                        cost_wei = pos.get('cost_wei', 0)
                        if cost_wei <= 0 and 'cost' in pos:
                            old_cost = pos.get('cost', 0)
                            if old_cost > 0:
                                cost_wei = old_cost * 10**9
                        tokens = balance_raw / self.token_unit
                        cost_eth = cost_wei / 10**18
                        # Calculate buy_price from cost/balance
                        if tokens > 0 and cost_eth > 0:
                            buy_price = cost_eth / tokens
                            sell_target = buy_price * (1 + sell_threshold / 100)
                            pnl = ((price - buy_price) / buy_price * 100)
                            price_diff = sell_target - price
                            price_pct = (price_diff / price * 100) if price > 0 else 0
                            logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: {buy_price:>12.10f} | Sell@: {sell_target:>12.10f} | P&L: {pnl:>+6.2f}% (need +{price_pct:>5.1f}% more to sell)")
                        else:
                            logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: N/A | P&L: N/A")
                else:
                    # Display classic grid positions
                    for pos_id, pos in self.positions.items():
                        if pos['balance'] > 0:
                            balance_raw = pos['balance']
                            cost_raw = pos['cost']
                            tokens = balance_raw / self.token_unit
                            cost_weth = cost_raw / 10**9
                            sell_min = pos['sellMin'] / 10**9
                            # Buy price = WETH spent / tokens received
                            if tokens > 0 and cost_weth > 0:
                                buy_price = cost_weth / tokens
                                pnl = ((price - buy_price) / buy_price * 100)
                                # Show how much more price needs to rise to hit sell target
                                price_diff = sell_min - price
                                price_pct = (price_diff / price * 100) if price > 0 else 0
                                logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: {buy_price:>12.10f} | Sell@: {sell_min:>12.10f} | P&L: {pnl:>+6.2f}% (need +{price_pct:>5.1f}% more to sell)")
                            else:
                                # Moonbag or dust position with unknown cost
                                price_diff = sell_min - price
                                price_pct = (price_diff / price * 100) if price > 0 else 0
                                logger.info(f"   #{pos_id:>3}: {tokens:>8.4f} tokens | Buy: moonbag | Sell@: {sell_min:>12.10f} | P&L: N/A (need +{price_pct:>5.1f}% more to sell)")
            
            # Show next buy trigger (lowest empty position buy range)
            if empty > 0:
                next_buy = None
                for pos_id, pos in self.positions.items():
                    if pos['balance'] == 0:  # Empty position
                        buy_max = pos['buyMax'] / 10**9
                        buy_min = pos['buyMin'] / 10**9
                        # Find the highest buyMax below current price (closest buy trigger)
                        if buy_max <= price:
                            if next_buy is None or buy_max > next_buy['buy_max']:
                                next_buy = {
                                    'pos_id': pos_id,
                                    'buy_min': buy_min,
                                    'buy_max': buy_max
                                }
                
                if next_buy:
                    drop_pct = (price - next_buy['buy_max']) / price * 100
                    logger.info(f"🛒 Next Buy: Position #{next_buy['pos_id']} at {next_buy['buy_min']:.10f}-{next_buy['buy_max']:.10f} (need -{drop_pct:.1f}% drop)")
                else:
                    # All empty positions are above current price, find lowest
                    lowest_buy = None
                    for pos_id, pos in self.positions.items():
                        if pos['balance'] == 0:
                            buy_max = pos['buyMax'] / 10**9
                            if lowest_buy is None or buy_max < lowest_buy['buy_max']:
                                lowest_buy = {'pos_id': pos_id, 'buy_max': buy_max}
                    if lowest_buy:
                        rise_pct = (lowest_buy['buy_max'] - price) / price * 100
                        logger.info(f"🛒 Next Buy: Position #{lowest_buy['pos_id']} at {lowest_buy['buy_max']:.10f} (need +{rise_pct:.1f}% rise to enter range)")
            
            logger.info("-" * 70)
        
        # Check sells before reporting so this round's transient attempt state
        # appears immediately rather than one poll late.
        self.check_sells(price)

        # Report to dashboard if configured (runs regardless of compact mode)
        if self._reporter:
            try:
                positions_data = []
                capacity_warning = None
                if use_gridless:
                    from gridless import load_positions, get_capacity_warning
                    gpos = load_positions()
                    capacity_warning = get_capacity_warning(gpos, price, self.config)
                    for pos_id, pos in gpos.items():
                        bal = pos.get('balance', 0)
                        if bal > 0:
                            tokens = bal / self.token_unit
                            cost_wei = pos.get('cost_wei', pos.get('cost', 0) * 10**9)
                            cost_eth = cost_wei / 10**18
                            if tokens > 0 and cost_eth > 0:
                                buy_price = cost_eth / tokens
                                pnl = ((price - buy_price) / buy_price * 100)
                                positions_data.append({
                                    'id': pos_id,
                                    'buy_amount_token': tokens,
                                    'cost_basis': cost_eth,
                                    'pnl': round(pnl, 2),
                                    'timestamp': pos.get('timestamp'),
                                })
                else:
                    for pos_id, pos in self.positions.items():
                        if pos['balance'] > 0:
                            tokens = pos['balance'] / self.token_unit
                            cost_eth = pos.get('cost', 0) / 10**9
                            if tokens > 0 and cost_eth > 0:
                                buy_price = cost_eth / tokens
                                pnl = ((price - buy_price) / buy_price * 100)
                                positions_data.append({
                                    'id': pos_id,
                                    'buy_amount_token': tokens,
                                    'cost_basis': cost_eth,
                                    'pnl': round(pnl, 2),
                                    'timestamp': pos.get('timestamp'),
                                })

                total_cost = sum(p['cost_basis'] for p in positions_data)
                total_value = sum(p['buy_amount_token'] * price for p in positions_data)
                profit_percent = (
                    ((total_value - total_cost) / total_cost) * 100
                    if total_cost > 0 else 0.0
                )
                
                gas_reserve_eth = getattr(self.config, 'eth_gas_reserve', 0.001)
                needs_gas = None
                if eth_bal < gas_reserve_eth:
                    needs_gas = {
                        'balance_eth': eth_bal,
                        'reserve_eth': gas_reserve_eth,
                        'shortfall_eth': gas_reserve_eth - eth_bal,
                    }

                self._reporter.report(
                    price=price,
                    eth_balance=eth_bal,
                    gas_reserve_eth=gas_reserve_eth,
                    usdg_balance=usdg_bal,
                    treasury_sent_usdg=_total_successful_treasury_sent_usdg(self.config.usdg_address),
                    token_balance=token_bal,
                    moonbag_balance=moonbag_balance,
                    estimated_moonbag_value_eth=max(0.0, moonbag_balance) * price,
                    positions=positions_data,
                    profit_percent=round(profit_percent, 2),
                    session_profit_eth=self.session_profit_weth,
                    realized_profit_eth=self.profit_tracker.realized_profit_eth,
                    realized_profit_periods=self.profit_tracker.period_profits_eth(),
                    realized_sales=self.profit_tracker.realized_sales,
                    profit_tracking_started_at=self.profit_tracker.tracking_started_at,
                    buys=self.session_buys,
                    sells=self.session_sells,
                    filled_positions=active,
                    max_positions=self.config.max_active_positions,
                    capacity_warning=capacity_warning,
                    needs_gas=needs_gas,
                    funding_warning=self._funding_warning,
                    sell_attempt=self._sell_attempt,
                    chain_id=self.config.chain_id,
                    swap_provider=self.provider.name,
                    taxed_token=self._taxed_token_active(),
                    token_transfer_fee_percent=self._effective_token_transfer_fee_percent(),
                    token_tax_detection_source=(
                        "declared" if self.config.taxed_token
                        else "auto-detected" if self.tax_detector.confirmed
                        else "none"
                    ),
                    token_tax_detection_observations=self.tax_detector.observation_count,
                    swap_slippage_percent=self._swap_slippage_fraction() * 100,
                    token_symbol=self.config.token_symbol,
                    token_address=self.config.token_address,
                    wallet_address=self.wallet.address,
                    display_name=self.config.dashboard_name,
                    group=self.config.dashboard_group,
                    buy_point_percent=self.config.gridless_buy_threshold,
                    sell_point_percent=self.config.gridless_sell_threshold,
                    poll_interval_seconds=self.config.poll_interval_seconds,
                    trades_history=self.dashboard_trades,
                    events=self.dashboard_events,
                    rpc_status="ok",
                )
            except Exception as e:
                logger.warning(f"Dashboard report failed: {e}")
        
        # Funding blocks are discovered during the buy check, which runs after
        # this round's status report. Preserve the prior round's finding long
        # enough to report it above, then reset it immediately before the next
        # check. Clearing it at cycle start made the warning exist only between
        # reports, so DoomDash could never receive it.
        self._funding_warning = None

        # Then check buys
        self.check_buys(price)
    
    def run(self):
        """Main bot loop."""
        self.load_positions()

        invoke_mercury(
            getattr(self.config, "mercury_evocation", True),
            lambda text: logger.info("\n%s", text),
        )

        poll_interval = getattr(self.config, 'poll_interval_seconds', 30)
        startup_jitter = getattr(self.config, 'startup_jitter_seconds', 0)
        if startup_jitter > 0:
            delay = random.uniform(0, startup_jitter)
            logger.info("Startup jitter: waiting %.1fs before first provider request", delay)
            time.sleep(delay)
        logger.info(f"Starting main loop (polling every {poll_interval}s)...")
        while self.running:
            try:
                self.run_cycle()
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Stopping bot...")
                self.running = False
            except Exception as e:
                logger.error(f"Error in cycle: {e}")
                time.sleep(10)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Robinhood Chain Grid Trading Bot")
    parser.add_argument("--check-config", action="store_true", help="Validate config without trading")
    parser.add_argument("--sweep-usdg", metavar="RECIPIENT", help="Transfer USDG; shorthand for --transfer-token USDG")
    parser.add_argument("--transfer-token", metavar="USDG_OR_ADDRESS", help="ERC-20 token to transfer")
    parser.add_argument("--transfer-eth", action="store_true", help="Transfer an exact native ETH amount")
    parser.add_argument("--recipient", help="Recipient for --transfer-token or --transfer-eth")
    parser.add_argument("--amount", default="all", help="Token amount, 'all', or native-ETH 'available' (default: all)")
    parser.add_argument("--confirm-recipient", help="Required exact recipient for a non-allowlisted address")
    parser.add_argument("--confirm-liquidate", action="store_true", help="Required to send all native ETH minus its maximum fee")
    parser.add_argument("--liquidate-assets", action="store_true", help="Convert all configured bot-managed tokens to native ETH")
    parser.add_argument("--confirm-liquidate-assets", action="store_true", help="Required acknowledgement for --liquidate-assets")
    parser.add_argument("--keep-usdg", action="store_true", help="Exclude configured USDG from --liquidate-assets")
    parser.add_argument("--sell-moonbag", action="store_true", help="Sell only token balance not allocated to positions")
    parser.add_argument("--confirm-sell-moonbag", action="store_true", help="Required acknowledgement to execute --sell-moonbag")
    parser.add_argument("--send-to-treasury", action="store_true", help="Forward only actual net moonbag-sale proceeds to --recipient")
    parser.add_argument("--confirm-send-to-treasury", action="store_true", help="Required acknowledgement to execute moonbag treasury forwarding")
    parser.add_argument("--confirm-bot-stopped", action="store_true", help="Acknowledge the bot sharing this wallet is stopped")
    parser.add_argument("--execute", action="store_true", help="Broadcast the planned transfer")
    args = parser.parse_args()
    if args.check_config:
        if any([args.sweep_usdg, args.transfer_token, args.transfer_eth, args.recipient, args.confirm_liquidate,
                args.liquidate_assets, args.confirm_liquidate_assets, args.keep_usdg,
                args.sell_moonbag, args.confirm_sell_moonbag, args.send_to_treasury,
                args.confirm_send_to_treasury,
                args.confirm_bot_stopped, args.execute]):
            parser.error("--check-config cannot be combined with a maintenance command")
        raise SystemExit(check_config())
    if args.liquidate_assets:
        if any([args.sweep_usdg, args.transfer_token, args.transfer_eth, args.recipient, args.confirm_liquidate,
                args.confirm_recipient, args.amount != "all", args.sell_moonbag, args.confirm_sell_moonbag,
                args.send_to_treasury, args.confirm_send_to_treasury]):
            parser.error("--liquidate-assets cannot be combined with treasury transfer commands")
        from asset_liquidator import run_asset_liquidation
        raise SystemExit(run_asset_liquidation(args))
    if args.sell_moonbag:
        if any([args.sweep_usdg, args.transfer_token, args.transfer_eth, args.confirm_liquidate,
                args.amount != "all", args.confirm_liquidate_assets, args.keep_usdg]):
            parser.error("--sell-moonbag cannot be combined with another maintenance command")
        if args.send_to_treasury != bool(args.recipient):
            parser.error("--send-to-treasury and --recipient must be supplied together for --sell-moonbag")
        if args.confirm_send_to_treasury and not args.send_to_treasury:
            parser.error("--confirm-send-to-treasury requires --send-to-treasury")
        if args.confirm_recipient and not args.send_to_treasury:
            parser.error("--confirm-recipient requires --send-to-treasury for --sell-moonbag")
        from moonbag_seller import run_moonbag_sale
        raise SystemExit(run_moonbag_sale(args))
    if args.keep_usdg:
        parser.error("--keep-usdg requires --liquidate-assets")
    if args.sweep_usdg:
        if args.transfer_token or args.transfer_eth or args.recipient:
            parser.error("--sweep-usdg cannot be combined with --transfer-token, --transfer-eth, or --recipient")
        args.transfer_token = "USDG"
        args.recipient = args.sweep_usdg
    if args.transfer_eth:
        if args.transfer_token or not args.recipient:
            parser.error("--transfer-eth requires --recipient and cannot be combined with --transfer-token")
        raise SystemExit(run_native_treasury_transfer(args))
    if args.transfer_token or args.recipient:
        if not args.transfer_token or not args.recipient:
            parser.error("--transfer-token and --recipient must be supplied together")
        raise SystemExit(run_treasury_transfer(args))
    if any([args.amount != "all", args.confirm_recipient, args.confirm_liquidate, args.confirm_liquidate_assets,
            args.keep_usdg, args.confirm_sell_moonbag, args.send_to_treasury,
            args.confirm_send_to_treasury, args.confirm_bot_stopped, args.execute]):
        parser.error("transfer options require --sweep-usdg, --transfer-token, or --transfer-eth with --recipient")
    bot = GridBot()
    bot.run()
