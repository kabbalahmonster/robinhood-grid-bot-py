"""
Dashboard Reporter module for the Grid Trading Bot.

Sends bot status updates to a remote dashboard via HTTP POST.
Fire-and-forget design: threaded, non-blocking, silent failure.
The bot must never crash because of dashboard connectivity issues.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from sigil import create_sigil

logger = logging.getLogger("grid_bot.dashboard")

# How long (seconds) to wait for the dashboard HTTP round-trip before giving up
_REQUEST_TIMEOUT = 5

# Maximum number of queued payloads waiting to be sent.
# If the queue is full the oldest payload is dropped (fire-and-forget).
_MAX_QUEUE_SIZE = 10


class DashboardReporter:
    """
    Fire-and-forget HTTP reporter for bot status updates.

    Uses a single background daemon thread with a bounded queue so the
    bot's main loop is never blocked and memory stays bounded even if
    the dashboard is unreachable for extended periods.

    Usage:
        reporter = DashboardReporter(
            dashboard_url="https://dash.example.com/api/status",
            api_key="secret",
            bot_id="grid-bot-1",
        )
        reporter.report(price=1.0, eth_balance=0.5, ...)
        reporter.shutdown()
    """

    def __init__(
        self,
        dashboard_url: str,
        api_key: str = "",
        bot_id: str = "grid-bot-1",
        local_status_path: str = "",
    ):
        self._url = dashboard_url.rstrip("/")
        self._api_key = api_key
        self._bot_id = bot_id
        self._local_status_path = local_status_path
        self._start_time = time.monotonic()
        self._sigil = create_sigil(bot_id)

        # Internal queue + worker thread
        self._queue: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._shutdown = False

        self._thread = threading.Thread(
            target=self._worker,
            name="dashboard-reporter",
            daemon=True,          # die with the main process
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def bot_id(self) -> str:
        return self._bot_id

    @property
    def enabled(self) -> bool:
        """True when a dashboard URL is configured and reporting is active."""
        return bool(self._url)

    @property
    def uptime_seconds(self) -> float:
        """Seconds since this reporter was created (≈ bot uptime)."""
        return time.monotonic() - self._start_time

    def report(
        self,
        *,
        price: Optional[float] = None,
        eth_balance: float = 0.0,
        gas_reserve_eth: Optional[float] = None,
        weth_balance: Optional[float] = None,
        usdg_balance: Optional[float] = None,
        treasury_sent_usdg: float = 0.0,
        token_balance: float = 0.0,
        moonbag_balance: float = 0.0,
        estimated_moonbag_value_eth: float = 0.0,
        positions: Optional[list] = None,
        profit_percent: float = 0.0,
        session_profit_eth: float = 0.0,
        realized_profit_eth: float = 0.0,
        realized_profit_periods: Optional[dict] = None,
        realized_sales: int = 0,
        profit_tracking_started_at: Optional[str] = None,
        profit: Optional[float] = None,
        buys: int = 0,
        sells: int = 0,
        filled_positions: int = 0,
        max_positions: int = 0,
        capacity_warning: Optional[dict] = None,
        needs_gas: Optional[dict] = None,
        funding_warning: Optional[dict] = None,
        sell_attempt: Optional[dict] = None,
        chain_id: Optional[int] = None,
        swap_provider: str = "",
        taxed_token: bool = False,
        token_transfer_fee_percent: float = 0.0,
        token_tax_detection_source: str = "none",
        token_tax_detection_observations: int = 0,
        swap_slippage_percent: Optional[float] = None,
        token_symbol: str = "",
        token_address: str = "",
        wallet_address: str = "",
        display_name: str = "",
        group: str = "",
        buy_point_percent: Optional[float] = None,
        sell_point_percent: Optional[float] = None,
        poll_interval_seconds: Optional[int] = None,
        trades_history: Optional[list] = None,
        events: Optional[list] = None,
        trades: Optional[int] = None,
        rpc_status: str = "unknown",
    ) -> None:
        """
        Enqueue a status payload for delivery.  Returns immediately.

        All parameters are optional so callers can send partial updates
        without breaking the schema.
        """
        payload: Dict[str, Any] = {
            "dashboard_schema_version": 1,
            "bot_id": self._bot_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(self.uptime_seconds, 1),
            "price": price,
            "eth_balance": eth_balance,
            "gas_reserve_eth": gas_reserve_eth,
            "usdg_balance": usdg_balance,
            "treasury_sent_usdg": treasury_sent_usdg,
            "token_balance": token_balance,
            "moonbag_balance": moonbag_balance,
            "estimated_moonbag_value_eth": estimated_moonbag_value_eth,
            "positions": positions if positions is not None else [],
            "profit_percent": profit_percent,
            "session_profit_eth": session_profit_eth,
            "realized_profit_eth": realized_profit_eth,
            "realized_profit_periods": realized_profit_periods if realized_profit_periods is not None else {},
            "realized_sales": realized_sales,
            "profit_tracking_started_at": profit_tracking_started_at,
            "buys": buys,
            "sells": sells,
            "filled_positions": filled_positions,
            "max_positions": max_positions,
            "capacity_warning": capacity_warning,
            "needs_gas": needs_gas,
            "funding_warning": funding_warning,
            "sell_attempt": sell_attempt,
            "chain_id": chain_id,
            "swap_provider": swap_provider,
            "taxed_token": taxed_token,
            "token_transfer_fee_percent": token_transfer_fee_percent,
            "token_tax_detection_source": token_tax_detection_source,
            "token_tax_detection_observations": token_tax_detection_observations,
            "swap_slippage_percent": swap_slippage_percent,
            "token_symbol": token_symbol,
            "token_address": token_address,
            "wallet_address": wallet_address,
            "display_name": display_name,
            "group": group,
            "buy_point_percent": buy_point_percent,
            "sell_point_percent": sell_point_percent,
            "poll_interval_seconds": poll_interval_seconds,
            "trades_history": trades_history if trades_history is not None else [],
            "events": events if events is not None else [],
            "rpc_status": rpc_status,
            "sigil": self._sigil,
        }

        # Keep a tiny local mirror for terminal-only fleet monitoring.  This is
        # deliberately written before the network queue: a slow/unreachable
        # dashboard must not make the local operator view stale.  The payload
        # contains public operational state only (never credentials or keys).
        if self._local_status_path:
            try:
                directory = os.path.dirname(self._local_status_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                temporary = self._local_status_path + ".tmp"
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, separators=(",", ":"))
                os.replace(temporary, self._local_status_path)
            except OSError as exc:
                logger.debug("Local fleet status snapshot failed: %s", exc)

        with self._lock:
            if len(self._queue) >= _MAX_QUEUE_SIZE:
                # Drop oldest to make room — fire-and-forget semantics
                self._queue.pop(0)
            self._queue.append(payload)

        # Wake the worker thread
        self._event.set()

    def report_status(
        self,
        *,
        price: Optional[float] = None,
        eth_balance: float = 0.0,
        gas_reserve_eth: Optional[float] = None,
        usdg_balance: Optional[float] = None,
        treasury_sent_usdg: float = 0.0,
        token_balance: float = 0.0,
        moonbag_balance: float = 0.0,
        estimated_moonbag_value_eth: float = 0.0,
        positions: Optional[list] = None,
        profit_percent: float = 0.0,
        session_profit_eth: float = 0.0,
        realized_profit_eth: float = 0.0,
        realized_profit_periods: Optional[dict] = None,
        realized_sales: int = 0,
        profit_tracking_started_at: Optional[str] = None,
        buys: int = 0,
        sells: int = 0,
        filled_positions: int = 0,
        max_positions: int = 0,
        capacity_warning: Optional[dict] = None,
        funding_warning: Optional[dict] = None,
        sell_attempt: Optional[dict] = None,
        chain_id: Optional[int] = None,
        swap_provider: str = "",
        taxed_token: bool = False,
        token_transfer_fee_percent: float = 0.0,
        token_tax_detection_source: str = "none",
        token_tax_detection_observations: int = 0,
        swap_slippage_percent: Optional[float] = None,
        token_symbol: str = "",
        token_address: str = "",
        wallet_address: str = "",
        rpc_status: str = "unknown",
    ) -> None:
        """
        Alias for report() — provided for semantic clarity in bot.py.
        Enqueue a status payload for delivery.  Returns immediately.
        """
        self.report(
            price=price,
            eth_balance=eth_balance,
            gas_reserve_eth=gas_reserve_eth,
            usdg_balance=usdg_balance,
            treasury_sent_usdg=treasury_sent_usdg,
            token_balance=token_balance,
            moonbag_balance=moonbag_balance,
            estimated_moonbag_value_eth=estimated_moonbag_value_eth,
            positions=positions,
            profit_percent=profit_percent,
            session_profit_eth=session_profit_eth,
            realized_profit_eth=realized_profit_eth,
            realized_profit_periods=realized_profit_periods,
            realized_sales=realized_sales,
            profit_tracking_started_at=profit_tracking_started_at,
            buys=buys,
            sells=sells,
            filled_positions=filled_positions,
            max_positions=max_positions,
            capacity_warning=capacity_warning,
            funding_warning=funding_warning,
            sell_attempt=sell_attempt,
            chain_id=chain_id,
            swap_provider=swap_provider,
            taxed_token=taxed_token,
            token_transfer_fee_percent=token_transfer_fee_percent,
            token_tax_detection_source=token_tax_detection_source,
            token_tax_detection_observations=token_tax_detection_observations,
            swap_slippage_percent=swap_slippage_percent,
            token_symbol=token_symbol,
            token_address=token_address,
            wallet_address=wallet_address,
            rpc_status=rpc_status,
        )

    def shutdown(self, timeout: float = 3.0) -> None:
        """
        Signal the worker to drain remaining items and exit.

        Args:
            timeout: Max seconds to wait for the thread to finish.
        """
        self._shutdown = True
        self._event.set()
        self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Background loop: wait for payloads, POST them, repeat."""
        while not self._shutdown:
            # Block until there's work (or shutdown is signalled)
            self._event.wait(timeout=1.0)
            self._event.clear()

            while True:
                with self._lock:
                    if not self._queue:
                        break
                    payload = self._queue.pop(0)

                try:
                    self._post(payload)
                except Exception:
                    # Silent fail — never let dashboard issues propagate
                    pass

    def _post(self, payload: Dict[str, Any]) -> None:
        """
        Send a single payload to the dashboard.

        Raises on failure so the worker's except clause can swallow it.
        """
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"grid-bot/{self._bot_id}",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        logger.debug(f"Dashboard POST to {self._url} with bot_id={self._bot_id}")
        response = requests.post(
            self._url,
            data=json.dumps(payload),
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        logger.debug(f"Dashboard response: {response.status_code}")
        # Log non-2xx at debug level only — we don't want log spam
        if response.status_code >= 400:
            logger.warning(
                "Dashboard returned %s for bot %s: %s",
                response.status_code,
                self._bot_id,
                response.text[:200],
            )


# ------------------------------------------------------------------
# Convenience factory
# ------------------------------------------------------------------

def create_reporter_from_config(config) -> Optional[DashboardReporter]:
    """
    Build a DashboardReporter from a BotConfig instance.

    Returns None when DASHBOARD_URL is not configured so the bot
    can simply skip reporting without extra checks everywhere.
    """
    url = getattr(config, "dashboard_url", "") or ""
    if not url:
        return None

    return DashboardReporter(
        dashboard_url=url,
        api_key=getattr(config, "dashboard_api_key", "") or "",
        bot_id=getattr(config, "bot_id", "grid-bot-1") or "grid-bot-1",
        local_status_path="data/fleet_status.json",
    )
