"""Durable, transaction-idempotent realized-profit accounting."""

import json
import os
from datetime import datetime, timedelta, timezone


MAX_RECENT_TX_HASHES = 5000
MAX_PROFIT_HISTORY = 10000
PROFIT_HISTORY_RETENTION_DAYS = 32
PERIOD_HOURS = {
    "month": 24 * 30,
    "week": 24 * 7,
    "3d": 24 * 3,
    "24h": 24,
    "12h": 12,
    "6h": 6,
    "4h": 4,
    "2h": 2,
    "1h": 1,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class ProfitTracker:
    def __init__(self, path="data/profit_totals.json"):
        self.path = path
        self.state = self._load()

    def _default_state(self):
        return {
            "schema_version": 1,
            "tracking_started_at": _now(),
            "last_updated_at": None,
            "realized_profit_wei": 0,
            "realized_sales": 0,
            "profitable_sales": 0,
            "losing_sales": 0,
            "baseline_profit_wei": 0,
            "baseline_sales": 0,
            "baseline_at": None,
            "recent_tx_hashes": [],
            "profit_history": [],
        }

    def _load(self):
        try:
            with open(self.path, "r") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ValueError("unsupported profit state")
            defaults = self._default_state()
            defaults.update(data)
            defaults["recent_tx_hashes"] = list(defaults.get("recent_tx_hashes", []))[-MAX_RECENT_TX_HASHES:]
            defaults["profit_history"] = list(defaults.get("profit_history", []))[-MAX_PROFIT_HISTORY:]
            return defaults
        except FileNotFoundError:
            state = self._default_state()
            self._save(state)
            return state

    def _save(self, state=None):
        state = state or self.state
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w") as handle:
            json.dump(state, handle, indent=2)
        os.replace(temp_path, self.path)

    @property
    def realized_profit_wei(self):
        return int(self.state["realized_profit_wei"]) - int(self.state["baseline_profit_wei"])

    @property
    def realized_profit_eth(self):
        return self.realized_profit_wei / 10**18

    @property
    def realized_sales(self):
        return int(self.state["realized_sales"]) - int(self.state["baseline_sales"])

    @property
    def tracking_started_at(self):
        return self.state["baseline_at"] or self.state["tracking_started_at"]

    def record_sale(self, profit_wei, tx_hash):
        """Record one confirmed sale. Returns False when already counted."""
        tx_hash = str(tx_hash or "").strip().lower()
        if not tx_hash:
            raise ValueError("tx_hash is required for durable profit accounting")
        if tx_hash in self.state["recent_tx_hashes"]:
            return False

        updated = dict(self.state)
        updated["recent_tx_hashes"] = (list(self.state["recent_tx_hashes"]) + [tx_hash])[-MAX_RECENT_TX_HASHES:]
        updated["realized_profit_wei"] = int(self.state["realized_profit_wei"]) + int(profit_wei)
        updated["realized_sales"] = int(self.state["realized_sales"]) + 1
        if int(profit_wei) >= 0:
            updated["profitable_sales"] = int(self.state["profitable_sales"]) + 1
        else:
            updated["losing_sales"] = int(self.state["losing_sales"]) + 1
        recorded_at = _now()
        cutoff = datetime.now(timezone.utc) - timedelta(days=PROFIT_HISTORY_RETENTION_DAYS)
        retained = []
        for entry in self.state.get("profit_history", []):
            try:
                if datetime.fromisoformat(entry["timestamp"]) >= cutoff:
                    retained.append(entry)
            except (KeyError, TypeError, ValueError):
                continue
        retained.append({"timestamp": recorded_at, "profit_wei": int(profit_wei), "tx_hash": tx_hash})
        updated["profit_history"] = retained[-MAX_PROFIT_HISTORY:]
        updated["last_updated_at"] = recorded_at
        self._save(updated)
        self.state = updated
        return True

    def seed_profit_history(self, trades):
        """Backfill an empty rolling ledger from retained dashboard sell trades."""
        if self.state.get("profit_history"):
            return False
        tracking_start = datetime.fromisoformat(self.tracking_started_at)
        entries = []
        for trade in trades or []:
            try:
                if trade.get("side") != "sell" or trade.get("profit_eth") is None:
                    continue
                timestamp = datetime.fromisoformat(trade["timestamp"])
                if timestamp < tracking_start:
                    continue
                entries.append({
                    "timestamp": timestamp.isoformat(),
                    "profit_wei": int(round(float(trade["profit_eth"]) * 10**18)),
                    "tx_hash": str(trade.get("tx_hash", "")).strip().lower(),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if not entries:
            return False
        updated = dict(self.state)
        updated["profit_history"] = entries[-MAX_PROFIT_HISTORY:]
        self._save(updated)
        self.state = updated
        return True

    def period_profits_eth(self, now=None):
        now = now or datetime.now(timezone.utc)
        tracking_start = datetime.fromisoformat(self.tracking_started_at)
        result = {}
        for name, hours in PERIOD_HOURS.items():
            cutoff = now - timedelta(hours=hours)
            if tracking_start >= cutoff:
                result[name] = self.realized_profit_eth
                continue
            profit_wei = 0
            for entry in self.state.get("profit_history", []):
                try:
                    if datetime.fromisoformat(entry["timestamp"]) >= cutoff:
                        profit_wei += int(entry["profit_wei"])
                except (KeyError, TypeError, ValueError):
                    continue
            result[name] = profit_wei / 10**18
        return result

    def reset_baseline(self):
        """Start a new displayed baseline without deleting the all-time ledger."""
        updated = dict(self.state)
        updated["baseline_profit_wei"] = int(self.state["realized_profit_wei"])
        updated["baseline_sales"] = int(self.state["realized_sales"])
        updated["baseline_at"] = _now()
        updated["profit_history"] = []
        self._save(updated)
        self.state = updated
