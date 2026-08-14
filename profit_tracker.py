"""Durable, transaction-idempotent realized-profit accounting."""

import json
import os
from datetime import datetime, timezone


MAX_RECENT_TX_HASHES = 5000


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
        updated["last_updated_at"] = _now()
        self._save(updated)
        self.state = updated
        return True

    def reset_baseline(self):
        """Start a new displayed baseline without deleting the all-time ledger."""
        updated = dict(self.state)
        updated["baseline_profit_wei"] = int(self.state["realized_profit_wei"])
        updated["baseline_sales"] = int(self.state["realized_sales"])
        updated["baseline_at"] = _now()
        self._save(updated)
        self.state = updated
