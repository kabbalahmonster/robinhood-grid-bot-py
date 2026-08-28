"""Conservative runtime detection for fee-on-transfer tokens."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
import tempfile


_MIN_OUTPUT_RE = re.compile(
    r"minimum output violation during simulation\s*:?[\s-]*"
    r"(?P<value>\d+(?:\.\d+)?)(?P<percent>\s*%)?",
    re.IGNORECASE,
)


class TokenTaxDetector:
    """Persist observations and activate only after consistent simulations."""

    def __init__(
        self,
        *,
        path: str,
        chain_id: int,
        token_address: str,
        enabled: bool,
        max_fee_percent: float = 15.0,
        confirmations_required: int = 2,
        consistency_tolerance_percent: float = 0.15,
    ):
        self.path = path
        self.chain_id = int(chain_id)
        self.token_address = str(token_address).lower()
        self.enabled = bool(enabled)
        self.max_fee_percent = float(max_fee_percent)
        self.confirmations_required = max(2, int(confirmations_required))
        self.consistency_tolerance_percent = float(consistency_tolerance_percent)
        self.state = self._load()

    @property
    def detected_fee_percent(self) -> float:
        return float(self.state.get("detected_fee_percent") or 0.0)

    @property
    def confirmed(self) -> bool:
        return self.detected_fee_percent > 0

    @property
    def observation_count(self) -> int:
        return len(self.state.get("observations") or [])

    def observe(self, error: object, *, direction: str) -> dict | None:
        """Record an exact simulation violation and return detection metadata."""
        if not self.enabled:
            return None
        fee = self.parse_fee_percent(error)
        if fee is None or fee < 0.5 or fee > self.max_fee_percent:
            return None

        observations = list(self.state.get("observations") or [])
        observation = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": str(direction),
            "fee_percent": fee,
        }
        observations.append(observation)
        observations = observations[-20:]
        self.state["observations"] = observations

        matching = [
            item for item in observations
            if item.get("direction") == direction
            and abs(float(item.get("fee_percent", -999)) - fee)
            <= self.consistency_tolerance_percent
        ]
        newly_confirmed = False
        if len(matching) >= self.confirmations_required and not self.confirmed:
            # Round upward to a tenth so 2.999895% becomes a bounded 3.0%.
            detected = math.ceil((max(float(i["fee_percent"]) for i in matching) - 1e-9) * 10) / 10
            self.state["detected_fee_percent"] = min(detected, self.max_fee_percent)
            self.state["confirmed_at"] = observation["timestamp"]
            self.state["source"] = "simulation_minimum_output_violation"
            newly_confirmed = True

        self._save()
        return {
            "observed_fee_percent": fee,
            "matching_observations": len(matching),
            "confirmations_required": self.confirmations_required,
            "confirmed": self.confirmed,
            "detected_fee_percent": self.detected_fee_percent,
            "newly_confirmed": newly_confirmed,
        }

    @staticmethod
    def parse_fee_percent(error: object) -> float | None:
        match = _MIN_OUTPUT_RE.search(str(error or ""))
        if not match:
            return None
        value = float(match.group("value"))
        if not match.group("percent") and value <= 1:
            value *= 100
        return round(value, 8)

    def _empty_state(self) -> dict:
        return {
            "version": 1,
            "chain_id": self.chain_id,
            "token_address": self.token_address,
            "observations": [],
            "detected_fee_percent": 0.0,
        }

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if (
                not isinstance(state, dict)
                or int(state.get("chain_id", -1)) != self.chain_id
                or str(state.get("token_address", "")).lower() != self.token_address
            ):
                return self._empty_state()
            return state
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._empty_state()

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".token-tax-detection.", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
