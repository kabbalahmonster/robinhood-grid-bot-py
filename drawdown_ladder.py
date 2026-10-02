"""Adaptive persistent drawdown allocation for gridless trading.

The plan freezes only its reference and maximum trigger geometry.  Rungs are
funded as strategy capital becomes available, recycle after confirmed exits,
and grow in principal only after maximum trigger coverage is funded.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4


LADDER_FILE = "data/gridless_ladder.json"
LADDER_VERSION = 3
SUPPORTED_LADDER_VERSIONS = {2, 3}
RUNG_STATES = {"inactive", "ready", "open", "waiting_reset", "retired"}


class LadderStateError(RuntimeError):
    """Raised when persisted ladder state is corrupt or ambiguous."""


@dataclass
class DrawdownRung:
    index: int
    price: float
    principal_wei: int = 0
    state: str = "inactive"
    position_id: Optional[str] = None
    open_principal_wei: int = 0
    fill_count: int = 0
    exit_count: int = 0
    realized_profit_wei: int = 0
    last_buy_at: Optional[float] = None
    last_sell_at: Optional[float] = None
    adopted_legacy_position: bool = False

    @classmethod
    def from_dict(cls, value: dict) -> "DrawdownRung":
        try:
            return cls(
                index=int(value["index"]),
                price=float(value["price"]),
                principal_wei=int(value.get("principal_wei", 0)),
                state=str(value.get("state", "inactive")),
                position_id=(
                    str(value["position_id"])
                    if value.get("position_id") is not None
                    else None
                ),
                open_principal_wei=int(value.get("open_principal_wei", 0)),
                fill_count=int(value.get("fill_count", 0)),
                exit_count=int(value.get("exit_count", 0)),
                realized_profit_wei=int(value.get("realized_profit_wei", 0)),
                last_buy_at=(
                    float(value["last_buy_at"])
                    if value.get("last_buy_at") is not None
                    else None
                ),
                last_sell_at=(
                    float(value["last_sell_at"])
                    if value.get("last_sell_at") is not None
                    else None
                ),
                adopted_legacy_position=bool(
                    value.get("adopted_legacy_position", False)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LadderStateError(f"invalid ladder rung: {exc}") from exc


@dataclass
class DrawdownLadderPlan:
    """Stable maximum geometry with dynamically funded, reusable rungs."""

    id: str
    chain_id: int
    token_address: str
    reference_price: float
    terminal_drawdown_percent: float
    spacing: str
    created_at: float
    expires_at: Optional[float]
    minimum_position_wei: int
    max_levels: int
    include_reference_entry: bool = False
    status: str = "active"
    rungs: list[DrawdownRung] = field(default_factory=list)
    last_funding_at: Optional[float] = None
    mode: str = "drawdown_ladder"
    reanchor_count: int = 0
    last_reanchor_at: Optional[float] = None
    leading_edge_pending: bool = False
    leading_edge_level_index: Optional[int] = None
    version: int = LADDER_VERSION

    @property
    def level_prices(self) -> list[float]:
        return [rung.price for rung in self.rungs]

    @property
    def funded_count(self) -> int:
        return sum(rung.principal_wei > 0 for rung in self.rungs)

    @property
    def allocated_wei(self) -> int:
        return sum(rung.principal_wei for rung in self.rungs)

    @property
    def deployed_wei(self) -> int:
        return sum(rung.open_principal_wei for rung in self.rungs)

    @property
    def reserved_wei(self) -> int:
        return max(0, self.allocated_wei - self.deployed_wei)

    @property
    def next_level_price(self) -> Optional[float]:
        ready = [rung.price for rung in self.rungs if rung.state == "ready"]
        return max(ready) if self.status == "active" and ready else None

    def amount_for_level(self, index: int) -> int:
        try:
            amount = self.rungs[index].principal_wei
        except IndexError as exc:
            raise LadderStateError("ladder level index is outside the plan") from exc
        if amount <= 0:
            raise LadderStateError("ladder level is not funded")
        return amount

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "DrawdownLadderPlan":
        version = int(value.get("version", 0))
        if version not in SUPPORTED_LADDER_VERSIONS:
            raise LadderStateError(
                f"unsupported ladder version {version}; expected 2 or 3; "
                "archive the v1 one-shot plan before enabling adaptive mode"
            )
        try:
            plan = cls(
                id=str(value["id"]),
                chain_id=int(value["chain_id"]),
                token_address=str(value["token_address"]),
                reference_price=float(value["reference_price"]),
                terminal_drawdown_percent=float(value["terminal_drawdown_percent"]),
                spacing=str(value["spacing"]),
                created_at=float(value["created_at"]),
                expires_at=(
                    float(value["expires_at"])
                    if value.get("expires_at") is not None
                    else None
                ),
                minimum_position_wei=int(value["minimum_position_wei"]),
                max_levels=int(value["max_levels"]),
                include_reference_entry=bool(
                    value.get("include_reference_entry", False)
                ),
                status=str(value.get("status", "active")),
                rungs=[DrawdownRung.from_dict(rung) for rung in value["rungs"]],
                last_funding_at=(
                    float(value["last_funding_at"])
                    if value.get("last_funding_at") is not None
                    else None
                ),
                mode=str(value.get("mode", "drawdown_ladder")),
                reanchor_count=int(value.get("reanchor_count", 0)),
                last_reanchor_at=(
                    float(value["last_reanchor_at"])
                    if value.get("last_reanchor_at") is not None else None
                ),
                leading_edge_pending=bool(value.get("leading_edge_pending", False)),
                leading_edge_level_index=(
                    int(value["leading_edge_level_index"])
                    if value.get("leading_edge_level_index") is not None else None
                ),
                version=version,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LadderStateError(f"invalid ladder state: {exc}") from exc
        validate_plan(plan)
        return plan


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
            else terminal_multiplier**fraction
        )
        level = reference_price * multiplier
        if levels and not level < levels[-1]:
            raise ValueError("generated ladder levels are not strictly descending")
        levels.append(level)
    return levels


def _eth_to_wei(value: Any) -> int:
    return int(Decimal(str(value)) * (Decimal(10) ** 18))


def _target_budget_wei(
    plan: Optional[DrawdownLadderPlan], spendable_balance_wei: int, config: Any
) -> int:
    deployed = plan.deployed_wei if plan is not None else 0
    capital = max(0, int(spendable_balance_wei)) + deployed
    fraction = Decimal(str(config.tradeable_balance_percent)) / Decimal(100)
    target = int(Decimal(capital) * fraction)
    maximum = _eth_to_wei(config.gridless_ladder_max_budget_eth)
    return min(target, maximum) if maximum > 0 else target


def _initial_indices(
    max_levels: int, count: int, include_reference_entry: bool = False
) -> list[int]:
    """Select evenly distributed stable triggers, always including the floor."""
    if include_reference_entry and count > 1:
        return sorted(
            {round(step * (max_levels - 1) / (count - 1)) for step in range(count)}
        )
    return sorted(
        {math.ceil((step + 1) * max_levels / count) - 1 for step in range(count)}
    )


def _next_density_index(
    plan: DrawdownLadderPlan, current_price: Optional[float] = None
) -> Optional[int]:
    inactive = [r.index for r in plan.rungs if r.state == "inactive"]
    if not inactive:
        return None
    if current_price is not None and math.isfinite(current_price):
        future = [
            index for index in inactive if plan.rungs[index].price < current_price
        ]
        if future:
            inactive = future
    funded = [r.index for r in plan.rungs if r.principal_wei > 0]
    anchors = [-1, *funded]
    # Split the largest uncovered interval in normalized ladder-index space.
    return max(
        inactive,
        key=lambda index: (min(abs(index - anchor) for anchor in anchors), -index),
    )


def _position_cost_wei(position: Dict[str, Any]) -> int:
    cost = int(position.get("cost_wei", 0) or 0)
    if cost <= 0:
        cost = int(position.get("cost", 0) or 0) * 10**9
    return cost


def _legacy_position_price(position: Dict[str, Any], token_decimals: int) -> float:
    cost = _position_cost_wei(position)
    balance = int(position.get("balance", 0) or 0)
    if cost <= 0 or balance <= 0:
        raise LadderStateError("legacy position has invalid cost or token balance")
    price = (cost / 10**18) / (balance / (10**token_decimals))
    if not math.isfinite(price) or price <= 0:
        raise LadderStateError("legacy position has an invalid entry price")
    return price


def _map_prices_to_levels(
    entry_prices: list[float], level_prices: list[float], spacing: str
) -> list[int]:
    """Return the minimum-distance monotonic one-to-one rung assignment."""
    count = len(entry_prices)
    level_count = len(level_prices)
    if count > level_count:
        raise LadderStateError("open positions exceed maximum drawdown ladder levels")
    if not count:
        return []

    def coordinate(value: float) -> float:
        return math.log(value) if spacing == "log" else value / entry_prices[0]

    position_coordinates = [coordinate(value) for value in entry_prices]
    level_coordinates = [coordinate(value) for value in level_prices]
    infinity = float("inf")
    costs = [[infinity] * level_count for _ in range(count)]
    previous = [[-1] * level_count for _ in range(count)]
    for level in range(level_count - count + 1):
        costs[0][level] = (position_coordinates[0] - level_coordinates[level]) ** 2
    for position in range(1, count):
        best_cost = infinity
        best_level = -1
        last_level = level_count - (count - position)
        for level in range(position, last_level + 1):
            candidate = costs[position - 1][level - 1]
            if candidate < best_cost:
                best_cost = candidate
                best_level = level - 1
            costs[position][level] = (
                best_cost
                + (position_coordinates[position] - level_coordinates[level]) ** 2
            )
            previous[position][level] = best_level

    level = min(range(count - 1, level_count), key=lambda i: costs[-1][i])
    result = [level]
    for position in range(count - 1, 0, -1):
        level = previous[position][level]
        result.append(level)
    return list(reversed(result))


def adopt_legacy_positions(
    positions: Dict[str, Dict],
    spendable_balance_wei: int,
    config: Any,
    current_price: Optional[float] = None,
    now: Optional[float] = None,
) -> tuple[DrawdownLadderPlan, Dict[str, Dict]]:
    """Create a ladder around existing gridless positions and add provenance.

    The highest historical entry price is the stable reference. Existing entries
    are assigned to the nearest distinct ideal rungs without changing their cost
    basis or token balances. Remaining capital then funds uncovered rungs.
    """
    if not positions:
        raise LadderStateError("legacy adoption requires at least one open position")
    if len(positions) > int(config.max_active_positions):
        raise LadderStateError(
            "open positions exceed MAX_ACTIVE_POSITIONS; increase the cap before adoption"
        )
    token_decimals = int(getattr(config, "token_decimals", 18))
    if not 0 <= token_decimals <= 255:
        raise LadderStateError("TOKEN_DECIMALS is invalid for legacy adoption")

    entries = []
    for position_id, position in positions.items():
        provenance = (
            position.get("ladder_id"),
            position.get("ladder_level_index"),
            position.get("ladder_principal_wei"),
        )
        if any(value is not None for value in provenance):
            raise LadderStateError(
                "cannot adopt positions carrying existing or partial ladder provenance"
            )
        entries.append(
            (
                str(position_id),
                position,
                _legacy_position_price(position, token_decimals),
                _position_cost_wei(position),
            )
        )
    entries.sort(key=lambda item: (-item[2], item[0]))
    reference_price = entries[0][2]
    created_at = time.time() if now is None else now
    expiry_seconds = int(config.gridless_ladder_expiry_seconds)
    maximum = int(config.max_active_positions)
    minimum = _eth_to_wei(config.gridless_min_position_eth)
    prices = generate_levels(
        reference_price,
        maximum,
        config.gridless_ladder_terminal_drawdown_percent,
        config.gridless_ladder_spacing,
        config.gridless_ladder_include_reference_entry,
    )
    assignments = _map_prices_to_levels(
        [entry[2] for entry in entries], prices, config.gridless_ladder_spacing
    )
    plan = DrawdownLadderPlan(
        id=uuid4().hex[:12],
        chain_id=int(config.chain_id),
        token_address=str(config.token_address).lower(),
        reference_price=reference_price,
        terminal_drawdown_percent=config.gridless_ladder_terminal_drawdown_percent,
        spacing=config.gridless_ladder_spacing,
        created_at=created_at,
        expires_at=created_at + expiry_seconds if expiry_seconds > 0 else None,
        minimum_position_wei=minimum,
        max_levels=maximum,
        include_reference_entry=config.gridless_ladder_include_reference_entry,
        rungs=[
            DrawdownRung(index=index, price=price) for index, price in enumerate(prices)
        ],
        last_funding_at=created_at,
        mode=str(getattr(config, "gridless_allocation_mode", "drawdown_ladder")),
        version=(3 if getattr(config, "gridless_allocation_mode", "") == "survivor" else 2),
    )
    adopted = {str(key): dict(value) for key, value in positions.items()}
    for (position_id, _position, _price, cost), index in zip(entries, assignments):
        rung = plan.rungs[index]
        rung.principal_wei = max(minimum, cost)
        rung.state = "open"
        rung.position_id = position_id
        rung.open_principal_wei = cost
        rung.fill_count = 1
        rung.last_buy_at = created_at
        rung.adopted_legacy_position = True
        adopted[position_id].update(
            {
                "ladder_id": plan.id,
                "ladder_level_index": index,
                "ladder_principal_wei": cost,
            }
        )
    refresh_plan_funding(
        plan,
        spendable_balance_wei,
        config,
        created_at,
        current_price=current_price,
    )
    validate_plan(plan)
    return plan, adopted


def reconcile_adoption_provenance(
    plan: DrawdownLadderPlan, positions: Dict[str, Dict]
) -> bool:
    """Finish the recoverable ladder-first half of a legacy adoption commit."""
    changed = False
    for rung in plan.rungs:
        if not rung.adopted_legacy_position or rung.state != "open":
            continue
        position_id = str(rung.position_id)
        position = positions.get(position_id)
        if position is None:
            raise LadderStateError("adopted ladder rung has no matching open position")
        provenance = (
            position.get("ladder_id"),
            position.get("ladder_level_index"),
            position.get("ladder_principal_wei"),
        )
        if all(value is None for value in provenance):
            if _position_cost_wei(position) != rung.open_principal_wei:
                raise LadderStateError("adopted position cost changed during migration")
            position.update(
                {
                    "ladder_id": plan.id,
                    "ladder_level_index": rung.index,
                    "ladder_principal_wei": rung.open_principal_wei,
                }
            )
            changed = True
        elif any(value is None for value in provenance):
            raise LadderStateError("adopted position has partial ladder provenance")
    return changed


def build_plan(
    reference_price: float,
    spendable_balance_wei: int,
    config: Any,
    now: Optional[float] = None,
) -> Optional[DrawdownLadderPlan]:
    """Create stable maximum geometry and fund the coverage currently affordable."""
    minimum = _eth_to_wei(config.gridless_min_position_eth)
    target = _target_budget_wei(None, spendable_balance_wei, config)
    count = min(int(config.max_active_positions), target // minimum)
    if count < 1:
        return None
    created_at = time.time() if now is None else now
    expiry_seconds = int(config.gridless_ladder_expiry_seconds)
    max_levels = int(config.max_active_positions)
    prices = generate_levels(
        reference_price,
        max_levels,
        config.gridless_ladder_terminal_drawdown_percent,
        config.gridless_ladder_spacing,
        config.gridless_ladder_include_reference_entry,
    )
    selected = set(
        _initial_indices(
            max_levels, int(count), config.gridless_ladder_include_reference_entry
        )
    )
    plan = DrawdownLadderPlan(
        id=uuid4().hex[:12],
        chain_id=int(config.chain_id),
        token_address=str(config.token_address).lower(),
        reference_price=reference_price,
        terminal_drawdown_percent=config.gridless_ladder_terminal_drawdown_percent,
        spacing=config.gridless_ladder_spacing,
        created_at=created_at,
        expires_at=created_at + expiry_seconds if expiry_seconds > 0 else None,
        minimum_position_wei=minimum,
        max_levels=max_levels,
        include_reference_entry=config.gridless_ladder_include_reference_entry,
        rungs=[
            DrawdownRung(
                index=index,
                price=price,
                principal_wei=minimum if index in selected else 0,
                state="ready" if index in selected else "inactive",
            )
            for index, price in enumerate(prices)
        ],
        last_funding_at=created_at,
        mode=str(getattr(config, "gridless_allocation_mode", "drawdown_ladder")),
        version=(3 if getattr(config, "gridless_allocation_mode", "") == "survivor" else 2),
    )
    refresh_plan_funding(plan, spendable_balance_wei, config, created_at)
    validate_plan(plan)
    return plan


def refresh_plan_funding(
    plan: DrawdownLadderPlan,
    spendable_balance_wei: int,
    config: Any,
    now: Optional[float] = None,
    current_price: Optional[float] = None,
) -> bool:
    """Add stable coverage first, then water-fill rung targets at maximum density."""
    if plan.status != "active":
        return False
    target = _target_budget_wei(plan, spendable_balance_wei, config)
    available = target - plan.allocated_wei
    changed = False
    while (
        plan.funded_count < plan.max_levels and available >= plan.minimum_position_wei
    ):
        index = _next_density_index(plan, current_price)
        if index is None:
            break
        rung = plan.rungs[index]
        rung.principal_wei = plan.minimum_position_wei
        rung.state = (
            "waiting_reset"
            if current_price is not None and current_price <= rung.price
            else "ready"
        )
        if rung.state == "waiting_reset":
            rung.last_sell_at = time.time() if now is None else now
        available -= plan.minimum_position_wei
        changed = True
    if plan.funded_count == plan.max_levels and available > 0:
        quotient, remainder = divmod(available, plan.max_levels)
        if quotient or remainder:
            for offset, rung in enumerate(plan.rungs):
                rung.principal_wei += quotient + (1 if offset < remainder else 0)
            changed = True
    if changed:
        plan.last_funding_at = time.time() if now is None else now
        validate_plan(plan)
    return changed


def advance_rearms(
    plan: DrawdownLadderPlan,
    current_price: float,
    cooldown_seconds: int,
    now: Optional[float] = None,
) -> bool:
    """Re-enable sold rungs only after price is back above their trigger."""
    if plan.status != "active" or not math.isfinite(current_price):
        return False
    now = time.time() if now is None else now
    changed = False
    for rung in plan.rungs:
        elapsed = now - rung.last_sell_at if rung.last_sell_at is not None else 0
        if (
            rung.state == "waiting_reset"
            and elapsed >= cooldown_seconds
            and current_price > rung.price
        ):
            rung.state = "ready"
            changed = True
    if changed:
        validate_plan(plan)
    return changed


def eligible_level(plan: DrawdownLadderPlan, current_price: float) -> Optional[int]:
    if plan.status != "active" or not math.isfinite(current_price):
        return None
    crossed = [
        rung.index
        for rung in plan.rungs
        if rung.state == "ready" and current_price <= rung.price
    ]
    return min(crossed) if crossed else None


def reanchor_survivor(
    plan: DrawdownLadderPlan,
    current_price: float,
    config: Any,
    now: Optional[float] = None,
) -> bool:
    """Move survivor geometry upward after a configured new-high advance."""
    if plan.mode != "survivor" or plan.status != "active":
        return False
    if not math.isfinite(current_price) or current_price <= 0:
        return False
    threshold = float(getattr(config, "gridless_survivor_reanchor_percent", 0.25))
    trigger = plan.reference_price * (1 + threshold / 100)
    if current_price < trigger or math.isclose(current_price, plan.reference_price):
        return False
    prices = generate_levels(
        current_price,
        plan.max_levels,
        plan.terminal_drawdown_percent,
        plan.spacing,
        plan.include_reference_entry,
    )
    plan.reference_price = current_price
    for rung, price in zip(plan.rungs, prices):
        rung.price = price
    plan.reanchor_count += 1
    plan.last_reanchor_at = time.time() if now is None else now
    if plan.leading_edge_level_index is None:
        plan.leading_edge_pending = True
    validate_plan(plan)
    return True


def level_is_crossed(plan: DrawdownLadderPlan, current_price: float) -> bool:
    return eligible_level(plan, current_price) is not None


def record_fill(
    plan: DrawdownLadderPlan,
    level_index: int,
    principal_wei: int,
    position_id: Optional[str] = None,
    filled_at: Optional[float] = None,
    entry_kind: str = "ladder",
) -> None:
    if plan.status != "active":
        raise LadderStateError("ladder is not active")
    rung = plan.rungs[level_index]
    if rung.state != "ready":
        raise LadderStateError("ladder rung is not ready")
    if int(principal_wei) != rung.principal_wei:
        raise LadderStateError("ladder principal does not match the funded rung")
    rung.state = "open"
    rung.position_id = str(position_id) if position_id is not None else None
    rung.open_principal_wei = int(principal_wei)
    rung.fill_count += 1
    rung.last_buy_at = time.time() if filled_at is None else filled_at
    if entry_kind == "leading_edge":
        if plan.mode != "survivor" or plan.leading_edge_level_index is not None:
            raise LadderStateError("survivor leading-edge ownership is invalid")
        plan.leading_edge_level_index = level_index
        plan.leading_edge_pending = False
    validate_plan(plan)


def mark_exit(
    plan: DrawdownLadderPlan,
    level_index: int,
    position_id: Optional[str] = None,
    exited_at: Optional[float] = None,
    realized_profit_wei: int = 0,
    recycle: bool = True,
) -> None:
    rung = plan.rungs[level_index]
    if rung.state != "open":
        raise LadderStateError("sold ladder rung is not open")
    if position_id is not None and rung.position_id not in {None, str(position_id)}:
        raise LadderStateError("sold position does not match its ladder rung")
    rung.state = "waiting_reset" if recycle and plan.status == "active" else "retired"
    rung.position_id = None
    rung.open_principal_wei = 0
    rung.exit_count += 1
    rung.realized_profit_wei += int(realized_profit_wei)
    rung.last_sell_at = time.time() if exited_at is None else exited_at
    if plan.leading_edge_level_index == level_index:
        plan.leading_edge_level_index = None
    validate_plan(plan)


def reconcile_confirmed_positions(
    plan: DrawdownLadderPlan, positions: Dict[str, Dict]
) -> bool:
    """Recover a confirmed position written before its rung checkpoint."""
    changed = False
    for position_id, position in positions.items():
        if position.get("ladder_id") != plan.id:
            continue
        index = int(position.get("ladder_level_index", -1))
        if not 0 <= index < len(plan.rungs):
            raise LadderStateError("open position references an invalid ladder level")
        rung = plan.rungs[index]
        if rung.state == "ready":
            principal = int(position.get("ladder_principal_wei", 0) or 0)
            if principal != rung.principal_wei:
                raise LadderStateError(
                    "recovered position principal does not match rung"
                )
            record_fill(
                plan,
                index,
                principal,
                position_id,
                entry_kind=position.get("ladder_entry_kind", "ladder"),
            )
            changed = True
    return changed


def validate_plan(plan: DrawdownLadderPlan) -> None:
    if plan.version not in SUPPORTED_LADDER_VERSIONS:
        raise LadderStateError("unsupported ladder version")
    if plan.mode not in {"drawdown_ladder", "survivor"}:
        raise LadderStateError("ladder mode is invalid")
    if plan.version == 2 and plan.mode != "drawdown_ladder":
        raise LadderStateError("version 2 ladder cannot use survivor behavior")
    if plan.version == 3 and plan.mode != "survivor":
        raise LadderStateError("version 3 ladder must use survivor behavior")
    if plan.reanchor_count < 0:
        raise LadderStateError("ladder reanchor count cannot be negative")
    if (plan.leading_edge_level_index is not None
            and not 0 <= plan.leading_edge_level_index < plan.max_levels):
        raise LadderStateError("leading-edge level is invalid")
    if (plan.leading_edge_level_index is not None
            and plan.rungs[plan.leading_edge_level_index].state != "open"):
        raise LadderStateError("leading-edge level is not open")
    if plan.leading_edge_pending and plan.leading_edge_level_index is not None:
        raise LadderStateError("leading-edge entry cannot be pending and open")
    if not plan.id or plan.chain_id <= 0 or not plan.token_address:
        raise LadderStateError("ladder identity is incomplete")
    if not math.isfinite(plan.reference_price) or plan.reference_price <= 0:
        raise LadderStateError("ladder reference price must be positive and finite")
    if plan.spacing not in {"linear", "log"}:
        raise LadderStateError("ladder spacing must be linear or log")
    if not 0 < plan.terminal_drawdown_percent < 100:
        raise LadderStateError("ladder terminal drawdown must be between 0 and 100")
    if plan.minimum_position_wei <= 0 or plan.max_levels <= 0:
        raise LadderStateError("ladder sizing must be positive")
    if len(plan.rungs) != plan.max_levels:
        raise LadderStateError("ladder rung count does not match maximum coverage")
    if plan.status not in {"active", "expired", "cancelled"}:
        raise LadderStateError("ladder status is invalid")
    if plan.expires_at is not None and plan.expires_at <= plan.created_at:
        raise LadderStateError("ladder expiry must follow creation")
    prices = [rung.price for rung in plan.rungs]
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        raise LadderStateError("ladder levels must be positive and finite")
    if any(a <= b for a, b in zip(prices, prices[1:])):
        raise LadderStateError("ladder levels must be strictly descending")
    for index, rung in enumerate(plan.rungs):
        if rung.index != index or rung.state not in RUNG_STATES:
            raise LadderStateError("ladder rung identity or state is invalid")
        if (
            min(
                rung.principal_wei,
                rung.open_principal_wei,
                rung.fill_count,
                rung.exit_count,
            )
            < 0
        ):
            raise LadderStateError("ladder rung accounting cannot be negative")
        if rung.state == "inactive" and rung.principal_wei != 0:
            raise LadderStateError("inactive rung cannot reserve principal")
        if rung.state != "inactive" and rung.principal_wei < plan.minimum_position_wei:
            raise LadderStateError("funded rung is below the minimum principal")
        if rung.state == "open":
            if (
                rung.open_principal_wei <= 0
                or rung.open_principal_wei > rung.principal_wei
            ):
                raise LadderStateError("open rung principal is invalid")
        elif rung.open_principal_wei or rung.position_id is not None:
            raise LadderStateError("non-open rung cannot retain an open position")
        if rung.exit_count > rung.fill_count:
            raise LadderStateError("rung exits exceed fills")


def validate_context(
    plan: DrawdownLadderPlan, positions: Dict[str, Dict], config: Any
) -> None:
    validate_plan(plan)
    if plan.chain_id != int(config.chain_id):
        raise LadderStateError("ladder chain does not match this bot")
    if plan.token_address != str(config.token_address).lower():
        raise LadderStateError("ladder token does not match this bot")
    if plan.max_levels != int(config.max_active_positions):
        raise LadderStateError(
            "persisted ladder maximum does not match MAX_ACTIVE_POSITIONS"
        )
    if plan.minimum_position_wei != _eth_to_wei(config.gridless_min_position_eth):
        raise LadderStateError(
            "persisted ladder minimum does not match GRIDLESS_MIN_POSITION_ETH"
        )
    if plan.spacing != str(config.gridless_ladder_spacing):
        raise LadderStateError("persisted ladder spacing does not match configuration")
    if not math.isclose(
        plan.terminal_drawdown_percent,
        float(config.gridless_ladder_terminal_drawdown_percent),
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise LadderStateError(
            "persisted ladder terminal drawdown does not match configuration"
        )
    if plan.include_reference_entry != bool(
        config.gridless_ladder_include_reference_entry
    ):
        raise LadderStateError(
            "persisted reference-entry mode does not match configuration"
        )
    configured_mode = str(getattr(config, "gridless_allocation_mode", "drawdown_ladder"))
    if plan.mode != configured_mode:
        raise LadderStateError(
            "persisted ladder mode does not match GRIDLESS_ALLOCATION_MODE; "
            "archive ladder and positions together before changing modes"
        )
    seen = set()
    for position_id, position in positions.items():
        if position.get("ladder_id") != plan.id:
            raise LadderStateError("open position does not belong to persisted ladder")
        index = int(position.get("ladder_level_index", -1))
        if not 0 <= index < len(plan.rungs) or index in seen:
            raise LadderStateError(
                "open position has invalid or duplicate ladder level"
            )
        seen.add(index)
        rung = plan.rungs[index]
        principal = int(position.get("ladder_principal_wei", 0) or 0)
        if rung.state != "open" or principal != rung.open_principal_wei:
            raise LadderStateError("position and rung accounting disagree")
        if rung.position_id is not None and rung.position_id != str(position_id):
            raise LadderStateError("position id and rung accounting disagree")
        entry_kind = position.get("ladder_entry_kind", "ladder")
        if entry_kind not in {"ladder", "leading_edge"}:
            raise LadderStateError("position ladder entry kind is invalid")
        if (entry_kind == "leading_edge") != (plan.leading_edge_level_index == index):
            raise LadderStateError("position and leading-edge ownership disagree")
    open_indices = {rung.index for rung in plan.rungs if rung.state == "open"}
    if open_indices != seen:
        raise LadderStateError("persisted open rung has no matching position")


def expire_plan(plan: DrawdownLadderPlan) -> None:
    plan.status = "expired"
    for rung in plan.rungs:
        if rung.state in {"ready", "waiting_reset"}:
            rung.state = "retired"
    validate_plan(plan)


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
    return {
        "id": plan.id,
        "status": plan.status,
        "mode": plan.mode,
        "spacing": plan.spacing,
        "reference_price": plan.reference_price,
        "terminal_drawdown_percent": plan.terminal_drawdown_percent,
        "levels_total": plan.max_levels,
        "levels_funded": plan.funded_count,
        "levels_open": sum(r.state == "open" for r in plan.rungs),
        "levels_adopted": sum(r.adopted_legacy_position for r in plan.rungs),
        "levels_ready": sum(r.state == "ready" for r in plan.rungs),
        "completed_cycles": sum(r.exit_count for r in plan.rungs),
        "next_level_price": plan.next_level_price,
        "allocated_budget_eth": plan.allocated_wei / 10**18,
        "deployed_eth": plan.deployed_wei / 10**18,
        "reserved_eth": plan.reserved_wei / 10**18,
        "average_position_eth": (
            plan.allocated_wei / plan.funded_count / 10**18 if plan.funded_count else 0
        ),
        "realized_profit_eth": sum(r.realized_profit_wei for r in plan.rungs) / 10**18,
        "expires_at": plan.expires_at,
        "reanchor_count": plan.reanchor_count,
        "last_reanchor_at": plan.last_reanchor_at,
        "leading_edge_pending": plan.leading_edge_pending,
        "leading_edge_open": plan.leading_edge_level_index is not None,
    }
