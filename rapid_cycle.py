"""Versioned decision state for rapid executable round-trip experiments.

This module is deliberately provider- and wallet-agnostic.  Shadow and live
callers feed it conservative route-tournament observations; only the caller may
prepare or broadcast a transaction.  Persisted shadow state is never accepted
by live mode (or vice versa).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional


SCHEMA_VERSION = 1
MODES = {"shadow", "live"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RapidCycleIdentity:
    mode: str
    chain_id: int
    token_address: str
    wallet_address: str
    principal_wei: int
    entry_drop_percent: float
    min_profit_percent: float
    lookback_samples: int = 12

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "chain_id": int(self.chain_id),
            "token_address": self.token_address.lower(),
            "wallet_address": self.wallet_address.lower(),
            "principal_wei": int(self.principal_wei),
            "entry_drop_percent": float(self.entry_drop_percent),
            "min_profit_percent": float(self.min_profit_percent),
            "lookback_samples": int(self.lookback_samples),
        }


class RapidCycleJournal:
    """Atomic, fail-closed journal for one shadow or live cycle engine."""

    def __init__(self, path: str | Path, identity: RapidCycleIdentity):
        if identity.mode not in MODES:
            raise ValueError("rapid-cycle journal mode must be shadow or live")
        if int(identity.lookback_samples) < 2:
            raise ValueError("rapid-cycle lookback_samples must be at least 2")
        self.path = Path(path)
        self.identity = identity
        self.state = self._load()

    def _empty(self) -> dict[str, Any]:
        now = _utc_now()
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self.identity.as_dict(),
            "status": "flat",
            "created_at": now,
            "updated_at": now,
            "buy_sample_count": 0,
            "peak_unit_cost_scaled": None,
            "buy_unit_cost_window": [],
            "position": None,
            "pending_action": None,
            "completed_cycles": [],
            "last_observation": None,
        }

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"rapid-cycle journal is unreadable: {self.path}") from exc
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(
                "rapid-cycle journal schema is unsupported; archive it before changing modes"
            )
        if raw.get("identity") != self.identity.as_dict():
            raise RuntimeError(
                "rapid-cycle journal identity/config changed; archive the journal and start clean"
            )
        if raw.get("status") not in {"flat", "open", "action_pending"}:
            raise RuntimeError("rapid-cycle journal has an invalid status")
        if raw.get("status") == "open" and not isinstance(raw.get("position"), dict):
            raise RuntimeError("rapid-cycle open state is missing its position")
        if raw.get("status") == "action_pending" and not isinstance(
            raw.get("pending_action"), dict
        ):
            raise RuntimeError("rapid-cycle pending state is missing its action")
        return raw

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = _utc_now()
        payload = json.dumps(self.state, indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=str(self.path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.state)


class RapidCycleEngine:
    """Observe conservative buy/sell economics and request bounded actions."""

    UNIT_SCALE = 10**30

    def __init__(self, journal: RapidCycleJournal, *, min_buy_samples: int = 3):
        if min_buy_samples < 2:
            raise ValueError("rapid-cycle min_buy_samples must be at least 2")
        if min_buy_samples > journal.identity.lookback_samples:
            raise ValueError("rapid-cycle min_buy_samples cannot exceed lookback_samples")
        self.journal = journal
        self.min_buy_samples = int(min_buy_samples)

    @property
    def state(self) -> dict[str, Any]:
        return self.journal.state

    def desired_direction(self) -> Optional[str]:
        if self.state["status"] == "action_pending":
            return None
        return "buy" if self.state["status"] == "flat" else "sell"

    @staticmethod
    def _observation(*, direction: str, block_number: int, provider: str,
                     settlement: str, amount_in_raw: int, output_floor_raw: int,
                     projected_gas_wei: int, score_wei: int, observed_at: str) -> dict[str, Any]:
        return {
            "direction": direction,
            "block_number": int(block_number),
            "provider": provider,
            "settlement": settlement,
            "amount_in_raw": str(int(amount_in_raw)),
            "output_floor_raw": str(int(output_floor_raw)),
            "projected_gas_wei": str(int(projected_gas_wei)),
            "score_wei": str(int(score_wei)),
            "observed_at": observed_at,
        }

    def observe_buy(self, *, block_number: int, provider: str, settlement: str,
                    token_floor_raw: int, projected_gas_wei: int,
                    observed_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        if self.state["status"] != "flat":
            return None
        principal = self.journal.identity.principal_wei
        token_floor_raw = int(token_floor_raw)
        projected_gas_wei = int(projected_gas_wei)
        if principal <= 0 or token_floor_raw <= 0 or projected_gas_wei < 0:
            raise ValueError("invalid rapid-cycle buy observation")
        economic_cost = principal + projected_gas_wei
        unit_cost = economic_cost * self.UNIT_SCALE // token_floor_raw
        samples = int(self.state.get("buy_sample_count", 0)) + 1
        window = [int(value) for value in self.state.get("buy_unit_cost_window", [])]
        window.append(unit_cost)
        window = window[-self.journal.identity.lookback_samples:]
        peak = max(window)
        drop = (peak - unit_cost) * 100 / peak if peak > 0 else 0.0
        observed_at = observed_at or _utc_now()
        observation = self._observation(
            direction="buy", block_number=block_number, provider=provider,
            settlement=settlement, amount_in_raw=principal,
            output_floor_raw=token_floor_raw, projected_gas_wei=projected_gas_wei,
            score_wei=-economic_cost, observed_at=observed_at,
        )
        observation.update({
            "economic_cost_wei": str(economic_cost),
            "unit_cost_scaled": str(unit_cost),
            "drop_from_peak_percent": round(drop, 8),
        })
        self.state.update({
            "buy_sample_count": samples,
            "peak_unit_cost_scaled": str(peak),
            "buy_unit_cost_window": [str(value) for value in window],
            "last_observation": observation,
        })
        if samples < self.min_buy_samples or drop < self.journal.identity.entry_drop_percent:
            self.journal.save()
            return None
        action = {
            "type": "buy",
            "requested_at": observed_at,
            "trigger": observation,
        }
        if self.journal.identity.mode == "shadow":
            self.state.update({
                "status": "open",
                "position": {
                    "kind": "paper",
                    "opened_at": observed_at,
                    "opened_block": int(block_number),
                    "token_amount_raw": str(token_floor_raw),
                    "cost_wei": str(economic_cost),
                    "buy_provider": provider,
                    "buy_settlement": settlement,
                    "buy_observation": observation,
                },
                "pending_action": None,
            })
            self.journal.save()
            return {**action, "paper_filled": True}
        self.state.update({"status": "action_pending", "pending_action": action})
        self.journal.save()
        return action

    def confirm_live_buy(self, *, position_id: str, token_amount_raw: int,
                         cost_wei: int, tx_hash: Optional[str] = None,
                         confirmed_at: Optional[str] = None) -> None:
        pending = self.state.get("pending_action") or {}
        if (self.journal.identity.mode != "live" or self.state["status"] != "action_pending"
                or pending.get("type") != "buy"):
            raise RuntimeError("no rapid-cycle live buy is pending")
        if int(token_amount_raw) <= 0 or int(cost_wei) <= 0:
            raise ValueError("confirmed rapid-cycle buy must have positive settlement")
        confirmed_at = confirmed_at or _utc_now()
        self.state.update({
            "status": "open",
            "position": {
                "kind": "live",
                "position_id": str(position_id),
                "opened_at": confirmed_at,
                "token_amount_raw": str(int(token_amount_raw)),
                "cost_wei": str(int(cost_wei)),
                "buy_tx_hash": str(tx_hash) if tx_hash else None,
                "buy_observation": pending.get("trigger"),
            },
            "pending_action": None,
        })
        self.journal.save()

    def observe_sell(self, *, block_number: int, provider: str, settlement: str,
                     output_floor_wei: int, projected_gas_wei: int,
                     observed_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        if self.state["status"] != "open":
            return None
        position = self.state["position"]
        token_amount = int(position["token_amount_raw"])
        cost = int(position["cost_wei"])
        output_floor_wei = int(output_floor_wei)
        projected_gas_wei = int(projected_gas_wei)
        net = output_floor_wei - projected_gas_wei
        profit = net - cost
        profit_percent = profit * 100 / cost if cost > 0 else -100.0
        observed_at = observed_at or _utc_now()
        observation = self._observation(
            direction="sell", block_number=block_number, provider=provider,
            settlement=settlement, amount_in_raw=token_amount,
            output_floor_raw=output_floor_wei, projected_gas_wei=projected_gas_wei,
            score_wei=net, observed_at=observed_at,
        )
        observation.update({
            "cost_wei": str(cost),
            "net_return_wei": str(net),
            "projected_profit_wei": str(profit),
            "projected_profit_percent": round(profit_percent, 8),
        })
        self.state["last_observation"] = observation
        if profit_percent < self.journal.identity.min_profit_percent:
            self.journal.save()
            return None
        action = {"type": "sell", "requested_at": observed_at, "trigger": observation}
        if self.journal.identity.mode == "shadow":
            self._complete_cycle(
                sell_observation=observation, received_wei=net,
                profit_wei=profit, tx_hash=None, closed_at=observed_at,
            )
            return {**action, "paper_filled": True}
        self.state.update({"status": "action_pending", "pending_action": action})
        self.journal.save()
        return action

    def confirm_live_sell(self, *, received_wei: int, profit_wei: int,
                          tx_hash: str, confirmed_at: Optional[str] = None) -> None:
        pending = self.state.get("pending_action") or {}
        if (self.journal.identity.mode != "live" or self.state["status"] != "action_pending"
                or pending.get("type") != "sell"):
            raise RuntimeError("no rapid-cycle live sell is pending")
        self._complete_cycle(
            sell_observation=pending.get("trigger"), received_wei=int(received_wei),
            profit_wei=int(profit_wei), tx_hash=tx_hash,
            closed_at=confirmed_at or _utc_now(),
        )

    def cancel_pending(self, reason: str) -> None:
        pending = self.state.get("pending_action") or {}
        if self.state["status"] != "action_pending":
            return
        action_type = pending.get("type")
        self.state["last_cancelled_action"] = {
            **pending, "cancelled_at": _utc_now(), "reason": str(reason)[:160]
        }
        self.state["pending_action"] = None
        self.state["status"] = "flat" if action_type == "buy" else "open"
        self.journal.save()

    def _complete_cycle(self, *, sell_observation: dict[str, Any], received_wei: int,
                        profit_wei: int, tx_hash: Optional[str], closed_at: str) -> None:
        position = deepcopy(self.state["position"])
        record = {
            "mode": self.journal.identity.mode,
            "opened_at": position.get("opened_at"),
            "closed_at": closed_at,
            "cost_wei": position.get("cost_wei"),
            "token_amount_raw": position.get("token_amount_raw"),
            "received_wei": str(int(received_wei)),
            "profit_wei": str(int(profit_wei)),
            "profit_percent": round(
                int(profit_wei) * 100 / int(position["cost_wei"]), 8
            ),
            "buy_observation": position.get("buy_observation"),
            "sell_observation": sell_observation,
            "sell_tx_hash": str(tx_hash) if tx_hash else None,
        }
        history = list(self.state.get("completed_cycles") or [])
        history.append(record)
        self.state.update({
            "status": "flat",
            "position": None,
            "pending_action": None,
            "buy_sample_count": 0,
            "peak_unit_cost_scaled": None,
            "buy_unit_cost_window": [],
            "completed_cycles": history[-500:],
            "last_completed_cycle": record,
        })
        self.journal.save()
