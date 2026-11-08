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


SCHEMA_VERSION = 4
MODES = {"shadow", "live"}
ENTRY_STRATEGIES = {"dip", "route_arbitrage"}


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
    entry_strategy: str = "dip"
    lookback_samples: int = 12
    min_confidence_samples: int = 2
    max_open_positions: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "chain_id": int(self.chain_id),
            "token_address": self.token_address.lower(),
            "wallet_address": self.wallet_address.lower(),
            "principal_wei": int(self.principal_wei),
            "entry_drop_percent": float(self.entry_drop_percent),
            "min_profit_percent": float(self.min_profit_percent),
            "entry_strategy": self.entry_strategy,
            "lookback_samples": int(self.lookback_samples),
            "min_confidence_samples": int(self.min_confidence_samples),
            "max_open_positions": int(self.max_open_positions),
        }


class RapidCycleJournal:
    """Atomic, fail-closed journal for one shadow or live cycle engine."""

    def __init__(self, path: str | Path, identity: RapidCycleIdentity):
        if identity.mode not in MODES:
            raise ValueError("rapid-cycle journal mode must be shadow or live")
        if identity.entry_strategy not in ENTRY_STRATEGIES:
            raise ValueError("rapid-cycle entry_strategy must be dip or route_arbitrage")
        if int(identity.lookback_samples) < 2:
            raise ValueError("rapid-cycle lookback_samples must be at least 2")
        if int(identity.min_confidence_samples) < 1:
            raise ValueError("rapid-cycle min_confidence_samples must be at least 1")
        if not 1 <= int(identity.max_open_positions) <= 100:
            raise ValueError("rapid-cycle max_open_positions must be between 1 and 100")
        if identity.mode == "live" and int(identity.max_open_positions) != 1:
            raise ValueError("rapid-cycle live mode currently requires max_open_positions=1")
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
            "round_trip_confidence_streak": 0,
            "position": None,
            "positions": [],
            "next_shadow_position_id": 1,
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
            if not (self.identity.mode == "shadow" and isinstance(raw.get("positions"), list)):
                raise RuntimeError("rapid-cycle open state is missing its position")
        if raw.get("status") == "action_pending" and not isinstance(
            raw.get("pending_action"), dict
        ):
            raise RuntimeError("rapid-cycle pending state is missing its action")
        positions = raw.get("positions")
        if not isinstance(positions, list) or any(not isinstance(row, dict) for row in positions):
            raise RuntimeError("rapid-cycle journal has invalid shadow positions")
        if len(positions) > self.identity.max_open_positions:
            raise RuntimeError("rapid-cycle journal exceeds its open-position cap")
        position_ids = [row.get("position_id") for row in positions]
        if (any(not isinstance(value, str) or not value for value in position_ids)
                or len(set(position_ids)) != len(position_ids)):
            raise RuntimeError("rapid-cycle shadow position identities are invalid")
        for row in positions:
            try:
                valid_amounts = (
                    int(row.get("token_amount_raw", 0)) > 0
                    and int(row.get("cost_wei", 0)) > 0
                )
            except (TypeError, ValueError):
                valid_amounts = False
            if row.get("kind") != "paper" or not valid_amounts:
                raise RuntimeError("rapid-cycle shadow position accounting is invalid")
        next_position_id = raw.get("next_shadow_position_id")
        if (not isinstance(next_position_id, int) or isinstance(next_position_id, bool)
                or next_position_id < 1):
            raise RuntimeError("rapid-cycle next shadow position identity is invalid")
        if self.identity.mode == "shadow":
            if raw.get("position") is not None or raw.get("pending_action") is not None:
                raise RuntimeError("rapid-cycle shadow journal contains live-only state")
            expected_status = "open" if positions else "flat"
            if raw.get("status") != expected_status:
                raise RuntimeError("rapid-cycle shadow status does not match its positions")
        elif positions:
            raise RuntimeError("rapid-cycle live journal contains shadow positions")
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
        if (self.journal.identity.mode == "shadow"
                and self.journal.identity.entry_strategy == "dip"):
            return "portfolio"
        if self.state["status"] == "action_pending":
            return None
        return "buy" if self.state["status"] == "flat" else "sell"

    def shadow_positions(self) -> list[dict[str, Any]]:
        if self.journal.identity.mode != "shadow":
            return []
        return deepcopy(self.state.get("positions") or [])

    def has_buy_capacity(self) -> bool:
        if (self.journal.identity.mode == "shadow"
                and self.journal.identity.entry_strategy == "dip"):
            return len(self.state.get("positions") or []) < self.journal.identity.max_open_positions
        return self.state["status"] == "flat"

    def shadow_exit_candidate(self, cursor: int = 0) -> Optional[dict[str, Any]]:
        positions = self.shadow_positions()
        if not positions:
            return None
        return positions[int(cursor) % len(positions)]

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
                    exit_provider: Optional[str] = None,
                    exit_settlement: Optional[str] = None,
                    exit_output_floor_wei: Optional[int] = None,
                    exit_projected_gas_wei: Optional[int] = None,
                    exit_block_number: Optional[int] = None,
                    observed_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        if not self.has_buy_capacity() or self.state["status"] == "action_pending":
            return None
        principal = self.journal.identity.principal_wei
        token_floor_raw = int(token_floor_raw)
        projected_gas_wei = int(projected_gas_wei)
        if principal <= 0 or token_floor_raw <= 0 or projected_gas_wei < 0:
            raise ValueError("invalid rapid-cycle buy observation")
        economic_cost = principal + projected_gas_wei
        unit_cost = economic_cost * self.UNIT_SCALE // token_floor_raw
        # This is readiness progress, not a lifetime attempt/failure counter.
        # Keep it bounded so telemetry can never report nonsense such as 5 / 3.
        samples = min(
            int(self.state.get("buy_sample_count", 0)) + 1,
            self.min_buy_samples,
        )
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
        exit_ready = (
            exit_provider is not None
            and exit_settlement is not None
            and exit_output_floor_wei is not None
            and exit_projected_gas_wei is not None
        )
        projected_net_return = None
        projected_profit = None
        projected_profit_percent = None
        sell_observation = None
        if exit_ready:
            exit_output_floor_wei = int(exit_output_floor_wei)
            exit_projected_gas_wei = int(exit_projected_gas_wei)
            if exit_output_floor_wei <= 0 or exit_projected_gas_wei < 0:
                raise ValueError("invalid rapid-cycle exit-confidence observation")
            projected_net_return = exit_output_floor_wei - exit_projected_gas_wei
            projected_profit = projected_net_return - economic_cost
            projected_profit_percent = (
                projected_profit * 100 / economic_cost if economic_cost > 0 else -100.0
            )
            sell_observation = self._observation(
                direction="sell",
                block_number=exit_block_number or block_number,
                provider=exit_provider,
                settlement=exit_settlement,
                amount_in_raw=token_floor_raw,
                output_floor_raw=exit_output_floor_wei,
                projected_gas_wei=exit_projected_gas_wei,
                score_wei=projected_net_return,
                observed_at=observed_at,
            )
            sell_observation.update({
                "cost_wei": str(economic_cost),
                "net_return_wei": str(projected_net_return),
                "projected_profit_wei": str(projected_profit),
                "projected_profit_percent": round(projected_profit_percent, 8),
            })
            observation.update({
                "exit_provider": exit_provider,
                "exit_settlement": exit_settlement,
                "exit_output_floor_wei": str(exit_output_floor_wei),
                "exit_projected_gas_wei": str(exit_projected_gas_wei),
                "projected_net_return_wei": str(projected_net_return),
                "projected_profit_wei": str(projected_profit),
                "projected_profit_percent": round(projected_profit_percent, 8),
            })
        qualified = bool(
            exit_ready
            and projected_profit_percent is not None
            and projected_profit_percent >= self.journal.identity.min_profit_percent
        )
        confidence_streak = (
            int(self.state.get("round_trip_confidence_streak", 0)) + 1
            if qualified else 0
        )
        observation.update({
            "round_trip_qualified": qualified,
            "confidence_streak": confidence_streak,
            "required_confidence_samples": self.journal.identity.min_confidence_samples,
        })
        self.state.update({
            "buy_sample_count": samples,
            "peak_unit_cost_scaled": str(peak),
            "buy_unit_cost_window": [str(value) for value in window],
            "round_trip_confidence_streak": confidence_streak,
            "last_observation": observation,
        })
        route_arbitrage = self.journal.identity.entry_strategy == "route_arbitrage"
        if (
            confidence_streak < self.journal.identity.min_confidence_samples
            or (
                not route_arbitrage
                and (
                    samples < self.min_buy_samples
                    or drop < self.journal.identity.entry_drop_percent
                )
            )
        ):
            self.journal.save()
            return None
        action = {
            "type": "buy",
            "requested_at": observed_at,
            "trigger": observation,
        }
        if self.journal.identity.mode == "shadow":
            if route_arbitrage:
                position = {
                    "kind": "paper",
                    "position_id": f"paper-{int(self.state.get('next_shadow_position_id', 1))}",
                    "opened_at": observed_at,
                    "opened_block": int(block_number),
                    "token_amount_raw": str(token_floor_raw),
                    "cost_wei": str(economic_cost),
                    "buy_provider": provider,
                    "buy_settlement": settlement,
                    "buy_observation": observation,
                }
                self.state["next_shadow_position_id"] = (
                    int(self.state.get("next_shadow_position_id", 1)) + 1
                )
                self._complete_cycle(
                    sell_observation=sell_observation,
                    received_wei=int(projected_net_return),
                    profit_wei=int(projected_profit),
                    tx_hash=None,
                    closed_at=observed_at,
                    position=position,
                )
                return {
                    **action,
                    "paper_filled": True,
                    "position_id": position["position_id"],
                    "round_trip_completed": True,
                }
            sequence = int(self.state.get("next_shadow_position_id", 1))
            position = {
                "kind": "paper",
                "position_id": f"paper-{sequence}",
                "opened_at": observed_at,
                "opened_block": int(block_number),
                "token_amount_raw": str(token_floor_raw),
                "cost_wei": str(economic_cost),
                "buy_provider": provider,
                "buy_settlement": settlement,
                "buy_observation": observation,
            }
            positions = list(self.state.get("positions") or [])
            positions.append(position)
            self.state.update({
                "status": "open",
                "positions": positions,
                "next_shadow_position_id": sequence + 1,
                "pending_action": None,
            })
            self.journal.save()
            return {**action, "paper_filled": True, "position_id": position["position_id"]}
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
                     position_id: Optional[str] = None,
                     observed_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        if self.state["status"] != "open":
            return None
        if self.journal.identity.mode == "shadow":
            positions = self.state.get("positions") or []
            position = next((row for row in positions
                             if row.get("position_id") == position_id), None)
            if position is None:
                raise RuntimeError("rapid-cycle shadow sell position is missing")
        else:
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
                position=position,
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
                        profit_wei: int, tx_hash: Optional[str], closed_at: str,
                        position: Optional[dict[str, Any]] = None) -> None:
        position = deepcopy(position or self.state["position"])
        record = {
            "mode": self.journal.identity.mode,
            "opened_at": position.get("opened_at"),
            "closed_at": closed_at,
            "cost_wei": position.get("cost_wei"),
            "token_amount_raw": position.get("token_amount_raw"),
            "position_id": position.get("position_id"),
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
        if self.journal.identity.mode == "shadow":
            remaining = [
                row for row in self.state.get("positions") or []
                if row.get("position_id") != position.get("position_id")
            ]
            self.state.update({
                "status": "open" if remaining else "flat",
                "positions": remaining,
                "completed_cycles": history[-500:],
                "last_completed_cycle": record,
            })
            if self.journal.identity.entry_strategy == "route_arbitrage":
                # Every shot must earn a fresh run of full-route confirmations.
                self.state["round_trip_confidence_streak"] = 0
        else:
            self.state.update({
                "status": "flat",
                "position": None,
                "pending_action": None,
                "buy_sample_count": 0,
                "peak_unit_cost_scaled": None,
                "buy_unit_cost_window": [],
                "round_trip_confidence_streak": 0,
                "completed_cycles": history[-500:],
                "last_completed_cycle": record,
            })
        self.journal.save()
