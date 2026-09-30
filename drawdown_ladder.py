"""Persistent drawdown-ladder planning for gridless entry allocation.

The ladder owns entry geometry and a frozen principal budget. Swap execution,
gas reserves, route selection, token-tax handling, receipt reconciliation, and
position exits remain the responsibility of ``GridTradingBot``.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4


LADDER_FILE = "data/gridless_ladder.json"
LADDER_VERSION = 1


class LadderStateError(RuntimeError):
    """Raised when persisted ladder state is corrupt or ambiguous."""


@dataclass
class DrawdownLadderPlan:
    """Frozen entry levels and capital allocation for one token/wallet cycle."""

    id: str
    chain_id: int
    token_address: str
    reference_price: float
    level_prices: list[float]
    budget_wei: int
    position_size_wei: int
    remainder_wei: int
    terminal_drawdown_percent: float
    spacing: str
    created_at: float
    expires_at: float
    include_reference_entry: bool = False
    next_level_index: int = 0
    spent_wei: int = 0
    status: str = "active"
    last_exit_at: Optional[float] = None
    version: int = LADDER_VERSION

    def amount_for_level(self, index: int) -> int:
        if not 0 <= index < len(self.level_prices):
            raise LadderStateError("ladder level index is outside the plan")
        return self.position_size_wei + (
            self.remainder_wei if index == len(self.level_prices) - 1 else 0
        )

    @property
    def next_level_price(self) -> Optional[float]:
        if self.status != "active" or self.next_level_index >= len(self.level_prices):
            return None
        return self.level_prices[self.next_level_index]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "DrawdownLadderPlan":
        try:
            plan = cls(
                id=str(value["id"]),
                chain_id=int(value["chain_id"]),
                token_address=str(value["token_address"]),
                reference_price=float(value["reference_price"]),
                level_prices=[float(level) for level in value["level_prices"]],
                budget_wei=int(value["budget_wei"]),
                position_size_wei=int(value["position_size_wei"]),
                remainder_wei=int(value.get("remainder_wei", 0)),
                terminal_drawdown_percent=float(value["terminal_drawdown_percent"]),
                spacing=str(value["spacing"]),
                created_at=float(value["created_at"]),
                expires_at=float(value["expires_at"]),
                include_reference_entry=bool(value.get("include_reference_entry", False)),
                next_level_index=int(value.get("next_level_index", 0)),
                spent_wei=int(value.get("spent_wei", 0)),
                status=str(value.get("status", "active")),
                last_exit_at=(
                    float(value["last_exit_at"])
                    if value.get("last_exit_at") is not None else None
                ),
                version=int(value.get("version", LADDER_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LadderStateError(f"invalid ladder state: {exc}") from exc
        validate_plan(plan)
        return plan


def validate_plan(plan: DrawdownLadderPlan) -> None:
    """Validate all invariants needed to avoid duplicate or oversized buys."""
    if plan.version != LADDER_VERSION:
        raise LadderStateError(
            f"unsupported ladder version {plan.version}; expected {LADDER_VERSION}"
        )
    if not plan.id or plan.chain_id <= 0 or not plan.token_address:
        raise LadderStateError("ladder identity is incomplete")
    if not math.isfinite(plan.reference_price) or plan.reference_price <= 0:
        raise LadderStateError("ladder reference price must be positive and finite")
    if not plan.level_prices or any(
        not math.isfinite(level) or level <= 0 for level in plan.level_prices
    ):
        raise LadderStateError("ladder levels must be positive and finite")
    if any(
        current <= following
        for current, following in zip(plan.level_prices, plan.level_prices[1:])
    ):
        raise LadderStateError("ladder levels must be strictly descending")
    if plan.level_prices[0] > plan.reference_price:
        raise LadderStateError("ladder level cannot exceed its reference price")
    if not 0 <= plan.next_level_index <= len(plan.level_prices):
        raise LadderStateError("ladder next level is outside the plan")
    if plan.budget_wei <= 0 or plan.position_size_wei <= 0:
        raise LadderStateError("ladder budget and position size must be positive")
    if plan.remainder_wei < 0 or plan.spent_wei < 0:
        raise LadderStateError("ladder remainder and spend must be non-negative")
    if plan.position_size_wei * len(plan.level_prices) + plan.remainder_wei != plan.budget_wei:
        raise LadderStateError("ladder position sizes do not equal its frozen budget")
    expected_spend = plan.position_size_wei * plan.next_level_index
    if plan.next_level_index == len(plan.level_prices):
        expected_spend += plan.remainder_wei
    if plan.spent_wei != expected_spend:
        raise LadderStateError("ladder spend does not match filled levels")
    if plan.status not in {"active", "terminal", "expired", "closed"}:
        raise LadderStateError("ladder status is invalid")
    if plan.status == "active" and plan.next_level_index == len(plan.level_prices):
        raise LadderStateError("completed ladder cannot remain active")
    if plan.status == "terminal" and plan.next_level_index != len(plan.level_prices):
        raise LadderStateError("terminal ladder must have every level filled")
    if plan.spacing not in {"linear", "log"}:
        raise LadderStateError("ladder spacing must be linear or log")
    if not 0 < plan.terminal_drawdown_percent < 100:
        raise LadderStateError("terminal drawdown must be between 0 and 100")
    if plan.expires_at <= plan.created_at:
        raise LadderStateError("ladder expiry must follow creation")


def generate_levels(
    reference_price: float,
    count: int,
    terminal_drawdown_percent: float,
    spacing: str,
    include_reference_entry: bool = False,
) -> list[float]:
    """Generate deterministic linear or log-spaced price thresholds."""
    if not math.isfinite(reference_price) or reference_price <= 0 or count <= 0:
        raise ValueError("reference price and position count must be positive")
    if not 0 < terminal_drawdown_percent < 100:
        raise ValueError("terminal drawdown must be between 0 and 100")
    if spacing not in {"linear", "log"}:
        raise ValueError("spacing must be linear or log")

    terminal_multiplier = 1 - terminal_drawdown_percent / 100
    if include_reference_entry and count > 1:
        fractions = [index / (count - 1) for index in range(count)]
    else:
        fractions = [index / count for index in range(1, count + 1)]

    levels = []
    for fraction in fractions:
        multiplier = (
            1 - (1 - terminal_multiplier) * fraction
            if spacing == "linear"
            else terminal_multiplier ** fraction
        )
        level = reference_price * multiplier
        if levels and not level < levels[-1]:
            raise ValueError("generated ladder levels are not strictly descending")
        levels.append(level)
    return levels


def _eth_to_wei(value: Any) -> int:
    return int(Decimal(str(value)) * (Decimal(10) ** 18))


def build_plan(
    reference_price: float,
    spendable_balance_wei: int,
    config: Any,
    now: Optional[float] = None,
) -> Optional[DrawdownLadderPlan]:
    """Freeze available liquid into a bounded set of minimum-safe positions."""
    tradeable_fraction = Decimal(str(config.tradeable_balance_percent)) / Decimal(100)
    budget_wei = int(Decimal(max(0, spendable_balance_wei)) * tradeable_fraction)
    maximum_wei = _eth_to_wei(config.gridless_ladder_max_budget_eth)
    if maximum_wei > 0:
        budget_wei = min(budget_wei, maximum_wei)
    minimum_wei = _eth_to_wei(config.gridless_min_position_eth)
    requested_count = min(
        int(config.max_active_positions),
        budget_wei // minimum_wei if minimum_wei > 0 else 0,
    )
    if requested_count < 1:
        return None

    levels = generate_levels(
        reference_price,
        requested_count,
        config.gridless_ladder_terminal_drawdown_percent,
        config.gridless_ladder_spacing,
        config.gridless_ladder_include_reference_entry,
    )
    position_size_wei, remainder_wei = divmod(budget_wei, len(levels))
    created_at = time.time() if now is None else now
    plan = DrawdownLadderPlan(
        id=uuid4().hex[:12],
        chain_id=int(config.chain_id),
        token_address=str(config.token_address).lower(),
        reference_price=reference_price,
        level_prices=levels,
        budget_wei=budget_wei,
        position_size_wei=position_size_wei,
        remainder_wei=remainder_wei,
        terminal_drawdown_percent=config.gridless_ladder_terminal_drawdown_percent,
        spacing=config.gridless_ladder_spacing,
        created_at=created_at,
        expires_at=created_at + config.gridless_ladder_expiry_seconds,
        include_reference_entry=config.gridless_ladder_include_reference_entry,
    )
    validate_plan(plan)
    return plan


def level_is_crossed(plan: DrawdownLadderPlan, current_price: float) -> bool:
    return (
        plan.next_level_price is not None
        and math.isfinite(current_price)
        and current_price <= plan.next_level_price
    )


def record_fill(
    plan: DrawdownLadderPlan,
    level_index: int,
    principal_wei: int,
) -> None:
    """Advance exactly one level after a confirmed, reconciled position write."""
    if plan.status != "active" or level_index != plan.next_level_index:
        raise LadderStateError("ladder fill does not match the next active level")
    expected = plan.amount_for_level(level_index)
    if int(principal_wei) != expected:
        raise LadderStateError(
            f"ladder principal {principal_wei} does not match planned amount {expected}"
        )
    plan.next_level_index += 1
    plan.spent_wei += expected
    if plan.next_level_index == len(plan.level_prices):
        plan.status = "terminal"
    validate_plan(plan)


def mark_exit(
    plan: DrawdownLadderPlan,
    exited_at: Optional[float] = None,
    cycle_closed: bool = False,
) -> None:
    plan.last_exit_at = time.time() if exited_at is None else exited_at
    if cycle_closed:
        plan.status = "closed"
    validate_plan(plan)


def validate_context(
    plan: DrawdownLadderPlan,
    positions: Dict[str, Dict],
    config: Any,
) -> None:
    """Tie all open positions to unique, already-filled levels in this plan."""
    validate_plan(plan)
    if plan.chain_id != int(config.chain_id):
        raise LadderStateError("ladder chain does not match this bot")
    if plan.token_address != str(config.token_address).lower():
        raise LadderStateError("ladder token does not match this bot")
    if len(plan.level_prices) > int(config.max_active_positions):
        raise LadderStateError("persisted ladder exceeds MAX_ACTIVE_POSITIONS")

    seen = set()
    for position_id, position in positions.items():
        if position.get("ladder_id") != plan.id:
            raise LadderStateError(
                f"open position {position_id} does not belong to the persisted ladder"
            )
        try:
            level_index = int(position["ladder_level_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LadderStateError(
                f"open position {position_id} has no valid ladder level"
            ) from exc
        if not 0 <= level_index <= plan.next_level_index:
            raise LadderStateError(
                f"open position {position_id} references an invalid ladder level"
            )
        if level_index in seen:
            raise LadderStateError("multiple open positions reference the same ladder level")
        seen.add(level_index)


def reconcile_confirmed_positions(
    plan: DrawdownLadderPlan,
    positions: Dict[str, Dict],
) -> bool:
    """Recover a crash between position persistence and ladder advancement."""
    changed = False
    by_level = {
        int(position["ladder_level_index"]): position
        for position in positions.values()
        if position.get("ladder_id") == plan.id
        and position.get("ladder_level_index") is not None
    }
    while plan.status == "active" and plan.next_level_index in by_level:
        position = by_level[plan.next_level_index]
        principal_wei = int(position.get("ladder_principal_wei", 0) or 0)
        record_fill(plan, plan.next_level_index, principal_wei)
        changed = True
    return changed


def load_plan(path: str = LADDER_FILE) -> Optional[DrawdownLadderPlan]:
    state_path = Path(path)
    if not state_path.exists():
        return None
    try:
        with state_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise LadderStateError(f"cannot read ladder state at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LadderStateError("ladder state root must be an object")
    return DrawdownLadderPlan.from_dict(value)


def save_plan(plan: DrawdownLadderPlan, path: str = LADDER_FILE) -> None:
    validate_plan(plan)
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.", dir=state_path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(plan.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def status_payload(plan: Optional[DrawdownLadderPlan]) -> Optional[dict]:
    if plan is None:
        return None
    next_price = plan.next_level_price
    return {
        "id": plan.id,
        "status": plan.status,
        "spacing": plan.spacing,
        "reference_price": plan.reference_price,
        "terminal_drawdown_percent": plan.terminal_drawdown_percent,
        "levels_total": len(plan.level_prices),
        "levels_filled": plan.next_level_index,
        "next_level_price": next_price,
        "budget_eth": plan.budget_wei / 10**18,
        "spent_eth": plan.spent_wei / 10**18,
        "reserved_eth": max(0, plan.budget_wei - plan.spent_wei) / 10**18,
        "expires_at": plan.expires_at,
    }
