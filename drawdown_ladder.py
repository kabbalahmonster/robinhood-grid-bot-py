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
LADDER_VERSION = 5
SUPPORTED_LADDER_VERSIONS = {2, 3, 4, 5}
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
    open_entry_kind: Optional[str] = None
    rearm_required: bool = False

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
                open_entry_kind=(
                    str(value["open_entry_kind"])
                    if value.get("open_entry_kind") is not None
                    else ("ladder" if value.get("state") == "open" else None)
                ),
                rearm_required=bool(
                    value.get(
                        "rearm_required",
                        value.get("state") == "waiting_reset",
                    )
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
    def reserved_count(self) -> int:
        return sum(
            rung.state in {"ready", "waiting_reset"} for rung in self.rungs
        )

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
                f"unsupported ladder version {version}; expected 2, 3, 4, or 5; "
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
    plan: Optional[DrawdownLadderPlan], spendable_balance_wei: int, config: Any,
    *, include_deployed: bool = True,
) -> int:
    deployed = plan.deployed_wei if plan is not None and include_deployed else 0
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


def _survivor_reservation_indices(
    plan: DrawdownLadderPlan,
    count: int,
    upper_bound_price: Optional[float],
) -> list[int]:
    """Spread the live future grid from the lowest open entry to the floor."""
    candidates = [
        rung.index
        for rung in plan.rungs
        if rung.state not in {"open", "retired"}
        and (
            upper_bound_price is None
            or not math.isfinite(upper_bound_price)
            or rung.price < upper_bound_price
        )
    ]
    count = min(max(0, int(count)), len(candidates))
    if count == 0:
        return []
    # The lowest open entry is the *upper boundary*, not itself a funded
    # endpoint. Put the first future buy one complete density interval below
    # it, and always retain terminal coverage. Including offset zero here can
    # create another immediately crossed rung at effectively the same fill
    # price after every buy, causing clustered catch-up purchases.
    offsets = _initial_indices(len(candidates), count, include_reference_entry=False)
    return [candidates[offset] for offset in offsets]


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
        version=(5 if getattr(config, "gridless_allocation_mode", "") == "survivor" else 2),
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
        rung.open_entry_kind = "ladder"
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
        upper_bound_price=min(entry[2] for entry in entries),
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
        version=(5 if getattr(config, "gridless_allocation_mode", "") == "survivor" else 2),
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
    upper_bound_price: Optional[float] = None,
) -> bool:
    """Add stable coverage first, then water-fill rung targets at maximum density."""
    if plan.status != "active":
        return False
    if plan.mode == "survivor" and plan.version >= 4:
        return rebalance_survivor_funding(
            plan,
            spendable_balance_wei,
            config,
            now,
            current_price,
            upper_bound_price,
        )
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
            rung.rearm_required = True
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


def resize_plan_capacity(
    plan: DrawdownLadderPlan,
    positions: Dict[str, Dict],
    config: Any,
) -> bool:
    """Migrate persisted trigger geometry to ``MAX_ACTIVE_POSITIONS`` safely.

    Existing rung indexes are stable so position provenance never needs a
    paired-file rewrite. Expansion appends fresh rungs. Contraction is allowed
    only when every truncated rung is disposable allocation state; durable
    position, cooldown, and historical accounting remains fail-closed.
    """
    # Capacity migration must never make an already split-brain ladder look
    # repaired.  In particular, the legacy reset command could empty the
    # positions file while leaving an ``open`` persisted rung behind.  Check
    # the paired state before changing geometry so callers cannot log or save
    # a successful migration over that corruption.
    validate_position_pairing(plan, positions)

    configured = int(config.max_active_positions)
    if configured < 1:
        raise LadderStateError(
            "MAX_ACTIVE_POSITIONS must be at least 1 while adaptive ladder "
            "state exists"
        )
    if configured == plan.max_levels:
        return False

    if configured < plan.max_levels:
        for position_id, position in positions.items():
            index = int(position.get("ladder_level_index", -1))
            if index >= configured:
                raise LadderStateError(
                    f"cannot reduce MAX_ACTIVE_POSITIONS to {configured}; "
                    f"open position {position_id} occupies ladder rung {index}"
                )
        for rung in plan.rungs[configured:]:
            durable = (
                rung.state in {"open", "waiting_reset", "retired"}
                or rung.position_id is not None
                or rung.open_principal_wei > 0
                or rung.fill_count > 0
                or rung.exit_count > 0
                or rung.realized_profit_wei != 0
                or rung.last_buy_at is not None
                or rung.last_sell_at is not None
                or rung.adopted_legacy_position
                or rung.open_entry_kind is not None
                or rung.rearm_required
            )
            if durable:
                raise LadderStateError(
                    f"cannot reduce MAX_ACTIVE_POSITIONS to {configured}; "
                    f"ladder rung {rung.index} contains durable state"
                )
        del plan.rungs[configured:]
    else:
        plan.rungs.extend(
            DrawdownRung(index=index, price=plan.reference_price)
            for index in range(plan.max_levels, configured)
        )

    plan.max_levels = configured
    prices = generate_levels(
        plan.reference_price,
        configured,
        plan.terminal_drawdown_percent,
        plan.spacing,
        plan.include_reference_entry,
    )
    for index, (rung, price) in enumerate(zip(plan.rungs, prices)):
        rung.index = index
        rung.price = price
    validate_plan(plan)
    return True


def rebalance_survivor_funding(
    plan: DrawdownLadderPlan,
    spendable_balance_wei: int,
    config: Any,
    now: Optional[float] = None,
    current_price: Optional[float] = None,
    upper_bound_price: Optional[float] = None,
) -> bool:
    """Derive every unfilled Survivor reservation from live wallet capital.

    Confirmed positions remain immutable accounting facts. Ready and reset
    rungs are a live allocation view: deposits may add coverage or enlarge
    future entries, while withdrawals defund or shrink them immediately.
    """
    if plan.mode != "survivor" or plan.version < 4 or plan.status != "active":
        return False
    before = [
        (rung.principal_wei, rung.state, rung.last_sell_at, rung.rearm_required)
        for rung in plan.rungs
    ]
    # Survivor's future grid is a view of liquid alone. Open positions are
    # immutable accounting facts, but their deployed principal does not buy
    # extra future reservations or dilute their size.
    reserve = _target_budget_wei(
        plan,
        spendable_balance_wei,
        config,
        include_deployed=False,
    )

    for rung in plan.rungs:
        if rung.state == "open":
            # An open position's measured principal cannot be resized by a
            # wallet balance change. Its next-cycle target is derived after exit.
            rung.principal_wei = rung.open_principal_wei
        elif rung.state == "retired":
            rung.principal_wei = 0
        else:
            rung.principal_wei = 0
            rung.state = "inactive"

    available_slots = sum(
        rung.state not in {"open", "retired"} for rung in plan.rungs
    )
    funded_slots = min(available_slots, reserve // plan.minimum_position_wei)
    selected = [
        plan.rungs[index]
        for index in _survivor_reservation_indices(
            plan, int(funded_slots), upper_bound_price
        )
    ]

    # Every usable wei of Survivor liquid belongs to the future grid. More
    # liquid first increases rung count/density, then increases every future
    # buy equally once the available geometry is saturated.
    if selected:
        quotient, remainder = divmod(reserve, len(selected))
        for offset, rung in enumerate(selected):
            rung.principal_wei = quotient + (1 if offset < remainder else 0)

    timestamp = time.time() if now is None else now
    for rung in selected:
        # A newly rendered Survivor trigger is live immediately, even when the
        # market is already below it. Execution remains one guarded buy per
        # poll. Only a real prior exit can impose the persistent reset guard.
        rung.state = "waiting_reset" if rung.rearm_required else "ready"

    after = [
        (rung.principal_wei, rung.state, rung.last_sell_at, rung.rearm_required)
        for rung in plan.rungs
    ]
    changed = before != after
    if changed:
        plan.last_funding_at = timestamp
        validate_plan(plan)
    return changed


def sync_survivor_state(
    plan: DrawdownLadderPlan,
    positions: Dict[str, Dict],
    spendable_balance_wei: int,
    config: Any,
    current_price: Optional[float] = None,
    now: Optional[float] = None,
) -> bool:
    """Migrate and derive Survivor geometry/allocation from live durable facts."""
    if plan.mode != "survivor" or plan.status != "active":
        return False
    changed = False
    if plan.version in {3, 4}:
        plan.version = 5
        changed = True
    if plan.version != 5:
        raise LadderStateError("unsupported Survivor state version")

    configured_spacing = str(config.gridless_ladder_spacing)
    configured_terminal = float(config.gridless_ladder_terminal_drawdown_percent)
    configured_reference_entry = bool(config.gridless_ladder_include_reference_entry)
    geometry_changed = (
        plan.spacing != configured_spacing
        or not math.isclose(
            plan.terminal_drawdown_percent,
            configured_terminal,
            rel_tol=0,
            abs_tol=1e-12,
        )
        or plan.include_reference_entry != configured_reference_entry
    )
    reference = plan.reference_price
    lowest_open_price = None
    if positions:
        token_decimals = int(getattr(config, "token_decimals", 18))
        entry_prices = [
            _legacy_position_price(position, token_decimals)
            for position in positions.values()
        ]
        reference = max(entry_prices)
        lowest_open_price = min(entry_prices)
    reference_changed = not math.isclose(
        reference, plan.reference_price, rel_tol=0, abs_tol=1e-18
    )
    if geometry_changed or reference_changed:
        plan.spacing = configured_spacing
        plan.terminal_drawdown_percent = configured_terminal
        plan.include_reference_entry = configured_reference_entry
        prices = generate_levels(
            reference,
            plan.max_levels,
            plan.terminal_drawdown_percent,
            plan.spacing,
            plan.include_reference_entry,
        )
        plan.reference_price = reference
        for rung, price in zip(plan.rungs, prices):
            rung.price = price
        if reference_changed:
            plan.reanchor_count += 1
            plan.last_reanchor_at = time.time() if now is None else now
        changed = True

    if rebalance_survivor_funding(
        plan,
        spendable_balance_wei,
        config,
        now,
        current_price,
        upper_bound_price=lowest_open_price,
    ):
        changed = True
    if changed:
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
            rung.rearm_required = False
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
    confirmed_buy_price: float,
    now: Optional[float] = None,
) -> bool:
    """Move Survivor geometry after a confirmed leading-edge fill."""
    if plan.mode != "survivor" or plan.status != "active":
        return False
    if not math.isfinite(confirmed_buy_price) or confirmed_buy_price <= 0:
        return False
    if math.isclose(confirmed_buy_price, plan.reference_price, rel_tol=0, abs_tol=1e-18):
        return False
    if plan.version == 3 and confirmed_buy_price < plan.reference_price:
        # Version 3 was an upward-only ratchet. It is explicitly migrated by
        # sync_survivor_state before version 4+ may move either direction.
        return False
    prices = generate_levels(
        confirmed_buy_price,
        plan.max_levels,
        plan.terminal_drawdown_percent,
        plan.spacing,
        plan.include_reference_entry,
    )
    plan.reference_price = confirmed_buy_price
    for rung, price in zip(plan.rungs, prices):
        rung.price = price
    plan.reanchor_count += 1
    plan.last_reanchor_at = time.time() if now is None else now
    validate_plan(plan)
    return True


def anchor_survivor_bootstrap(
    plan: DrawdownLadderPlan,
    confirmed_buy_price: float,
) -> bool:
    """Form fresh Survivor geometry from its first confirmed purchase point."""
    if plan.mode != "survivor" or plan.status != "active":
        return False
    if not math.isfinite(confirmed_buy_price) or confirmed_buy_price <= 0:
        return False
    open_rungs = [rung for rung in plan.rungs if rung.state == "open"]
    if (plan.reanchor_count != 0 or len(open_rungs) != 1
            or open_rungs[0].open_entry_kind != "leading_edge"):
        raise LadderStateError(
            "Survivor bootstrap anchor requires exactly one first leading fill"
        )
    prices = generate_levels(
        confirmed_buy_price,
        plan.max_levels,
        plan.terminal_drawdown_percent,
        plan.spacing,
        plan.include_reference_entry,
    )
    plan.reference_price = confirmed_buy_price
    for rung, price in zip(plan.rungs, prices):
        rung.price = price
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
    rung.open_entry_kind = entry_kind
    rung.rearm_required = False
    if entry_kind == "leading_edge":
        if plan.mode != "survivor":
            raise LadderStateError("leading-edge entries require survivor mode")
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
    rung.open_entry_kind = None
    rung.rearm_required = bool(recycle and plan.status == "active")
    rung.exit_count += 1
    rung.realized_profit_wei += int(realized_profit_wei)
    rung.last_sell_at = time.time() if exited_at is None else exited_at
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
            if position.get("ladder_entry_kind") == "leading_edge":
                fill_price = float(position.get("ladder_fill_price", 0) or 0)
                if fill_price <= 0:
                    raise LadderStateError(
                        "recovered leading-edge position is missing fill price"
                    )
                if position.get("ladder_bootstrap_reference") is True:
                    anchor_survivor_bootstrap(plan, fill_price)
                else:
                    reanchor_survivor(plan, fill_price)
            changed = True
    return changed


def validate_plan(plan: DrawdownLadderPlan) -> None:
    if plan.version not in SUPPORTED_LADDER_VERSIONS:
        raise LadderStateError("unsupported ladder version")
    if plan.mode not in {"drawdown_ladder", "survivor"}:
        raise LadderStateError("ladder mode is invalid")
    if plan.version == 2 and plan.mode != "drawdown_ladder":
        raise LadderStateError("version 2 ladder cannot use survivor behavior")
    if plan.version in {3, 4, 5} and plan.mode != "survivor":
        raise LadderStateError("version 3/4/5 ladder must use survivor behavior")
    if plan.reanchor_count < 0:
        raise LadderStateError("ladder reanchor count cannot be negative")
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
        if (rung.state in {"ready", "open", "waiting_reset"}
                and rung.principal_wei < plan.minimum_position_wei):
            raise LadderStateError("funded rung is below the minimum principal")
        if rung.state == "open":
            if (
                rung.open_principal_wei <= 0
                or rung.open_principal_wei > rung.principal_wei
            ):
                raise LadderStateError("open rung principal is invalid")
            if rung.open_entry_kind not in {"ladder", "leading_edge"}:
                raise LadderStateError("open rung entry kind is invalid")
            if rung.open_entry_kind == "leading_edge" and plan.mode != "survivor":
                raise LadderStateError("leading-edge rung requires survivor mode")
        elif (rung.open_principal_wei or rung.position_id is not None
              or rung.open_entry_kind is not None):
            raise LadderStateError("non-open rung cannot retain an open position")
        if rung.state == "waiting_reset" and not rung.rearm_required:
            raise LadderStateError("waiting reset rung is missing its reset guard")
        if rung.state in {"ready", "open"} and rung.rearm_required:
            raise LadderStateError("tradable rung cannot retain a reset guard")
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
    if (
        plan.mode != "survivor"
        and plan.spacing != str(config.gridless_ladder_spacing)
    ):
        raise LadderStateError("persisted ladder spacing does not match configuration")
    if plan.mode != "survivor" and not math.isclose(
        plan.terminal_drawdown_percent,
        float(config.gridless_ladder_terminal_drawdown_percent),
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise LadderStateError(
            "persisted ladder terminal drawdown does not match configuration"
        )
    if (
        plan.mode != "survivor"
        and plan.include_reference_entry
        != bool(config.gridless_ladder_include_reference_entry)
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
    validate_position_pairing(plan, positions)


def validate_position_pairing(
    plan: DrawdownLadderPlan, positions: Dict[str, Dict]
) -> None:
    """Validate the two-file position/rung invariant without config checks."""
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
        if entry_kind != rung.open_entry_kind:
            raise LadderStateError("position and rung entry kind disagree")
        if (position.get("ladder_bootstrap_reference") is True
                and entry_kind != "leading_edge"):
            raise LadderStateError(
                "Survivor bootstrap provenance requires a leading-edge entry"
            )
    open_indices = {rung.index for rung in plan.rungs if rung.state == "open"}
    missing = sorted(open_indices - seen)
    if missing:
        rendered = ", ".join(str(index) for index in missing)
        raise LadderStateError(
            "persisted open rung(s) " + rendered
            + " have no matching position; ladder and positions must be reset "
              "or restored together"
        )


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
    next_ready = max(
        (rung for rung in plan.rungs if rung.state == "ready"),
        key=lambda rung: rung.price,
        default=None,
    )
    return {
        "id": plan.id,
        "state_version": plan.version,
        "status": plan.status,
        "mode": plan.mode,
        "spacing": plan.spacing,
        "reference_price": plan.reference_price,
        "terminal_drawdown_percent": plan.terminal_drawdown_percent,
        "levels_total": plan.max_levels,
        "levels_funded": (
            plan.reserved_count if plan.mode == "survivor" else plan.funded_count
        ),
        "levels_open": sum(r.state == "open" for r in plan.rungs),
        "levels_adopted": sum(r.adopted_legacy_position for r in plan.rungs),
        "levels_ready": sum(r.state == "ready" for r in plan.rungs),
        "levels_reserved": plan.reserved_count,
        "completed_cycles": sum(r.exit_count for r in plan.rungs),
        "next_level_price": plan.next_level_price,
        "next_level_amount_eth": (
            next_ready.principal_wei / 10**18 if next_ready is not None else None
        ),
        "allocated_budget_eth": plan.allocated_wei / 10**18,
        "deployed_eth": plan.deployed_wei / 10**18,
        "reserved_eth": plan.reserved_wei / 10**18,
        "average_position_eth": (
            plan.reserved_wei / plan.reserved_count / 10**18
            if plan.mode == "survivor" and plan.reserved_count
            else plan.allocated_wei / plan.funded_count / 10**18
            if plan.funded_count
            else 0
        ),
        "realized_profit_eth": sum(r.realized_profit_wei for r in plan.rungs) / 10**18,
        "expires_at": plan.expires_at,
        "reanchor_count": plan.reanchor_count,
        "last_reanchor_at": plan.last_reanchor_at,
        "leading_edge_pending": False,
        "leading_edge_open": any(
            r.state == "open" and r.open_entry_kind == "leading_edge"
            for r in plan.rungs
        ),
        "leading_edge_open_count": sum(
            r.state == "open" and r.open_entry_kind == "leading_edge"
            for r in plan.rungs
        ),
    }
