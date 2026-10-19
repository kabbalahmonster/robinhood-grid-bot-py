"""
Gridless Trading Mode - Simple position-based trading without fixed grid levels.
Buy: No positions OR (count < max AND top_pnl <= buy_threshold)
Sell: pnl >= sell_threshold (check quote) OR stoploss triggered
"""

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

POSITIONS_FILE = "data/gridless_positions.json"
SURVIVOR_LEADING_EDGE_RUNWAY = 3


def _configured_token_decimals(config: Any) -> int:
    """Return validated runtime metadata while tolerating legacy test configs."""
    value = getattr(config, 'token_decimals', 18)
    return value if isinstance(value, int) and 0 <= value <= 255 else 18


def calculate_pnl(position: Dict[str, int], current_price: float,
                  token_decimals: int = 18) -> float:
    """Calculate P&L using wei units throughout for precision.
    
    Args:
        position: Dict with 'cost_wei' (wei spent) and 'balance' (wei tokens received)
        current_price: Current price in ETH per token (for display comparison only)
    
    Returns:
        P&L percentage
    """
    # Migrate from old cost (nano-ETH) if needed
    cost_wei = position.get('cost_wei', 0)
    if cost_wei <= 0 and 'cost' in position:
        old_cost = position.get('cost', 0)
        if old_cost > 0:
            cost_wei = old_cost * 10**9
    
    balance = position.get('balance', 0)
    if balance <= 0 or cost_wei <= 0:
        return 0.0
    
    # Calculate buy price in wei per wei-token for precise comparison
    buy_price_eth_per_token = (cost_wei / 1e18) / (balance / (10 ** token_decimals))
    
    if buy_price_eth_per_token <= 0:
        return 0.0
    
    # Debug
    import logging
    logger = logging.getLogger('gridless')
    logger.debug(f"P&L calc: cost_wei={cost_wei}, balance={balance}, buy_price={buy_price_eth_per_token}, current_price={current_price}")
    
    return ((current_price - buy_price_eth_per_token) / buy_price_eth_per_token) * 100


def get_buy_price(position: Dict[str, int], token_decimals: int = 18) -> float:
    """Calculate buy price in WETH per token."""
    # Migrate from old cost (nano-ETH) if needed
    cost_wei = position.get('cost_wei', 0)
    if cost_wei <= 0 and 'cost' in position:
        old_cost = position.get('cost', 0)
        if old_cost > 0:
            cost_wei = old_cost * 10**9
    
    balance = position.get('balance', 0)
    if balance <= 0 or cost_wei <= 0:
        return 0.0
    return (cost_wei / 1e18) / (balance / (10 ** token_decimals))


def get_threshold_entry_price(position: Dict[str, int],
                              token_decimals: int = 18) -> float:
    """Return the confirmed principal-only threshold entry reference.

    New threshold fills persist their measured execution price without buy gas.
    Legacy positions safely fall back to their economic cost-basis price.
    """
    try:
        fill_price = float(position.get('threshold_fill_price', 0) or 0)
    except (TypeError, ValueError):
        fill_price = 0.0
    if math.isfinite(fill_price) and fill_price > 0:
        return fill_price
    return get_buy_price(position, token_decimals)


def get_top_position(positions: Dict[str, Dict], token_decimals: int = 18) -> Optional[Tuple[str, Dict]]:
    """Get position with lowest buy price (best position)."""
    if not positions:
        return None
    top_id, top_pos, top_price = None, None, float('inf')
    for pos_id, pos in positions.items():
        buy_price = get_buy_price(pos, token_decimals)
        if buy_price > 0 and buy_price < top_price:
            top_price, top_id, top_pos = buy_price, pos_id, pos
    return (top_id, top_pos) if top_id else None


def get_highest_position(positions: Dict[str, Dict], token_decimals: int = 18) -> Optional[Tuple[str, Dict]]:
    """Get the highest valid purchase point for leading-edge continuation."""
    highest_id, highest_pos, highest_price = None, None, 0.0
    for pos_id, pos in positions.items():
        buy_price = get_buy_price(pos, token_decimals)
        if buy_price > highest_price:
            highest_price, highest_id, highest_pos = buy_price, pos_id, pos
    return (highest_id, highest_pos) if highest_id is not None else None


def survivor_leading_edge_trigger(positions: Dict[str, Dict], current_price: float,
                                  config: Any,
                                  position_pnls: Optional[Dict[str, Dict[str, float]]] = None):
    """Return a trigger only when the highest purchase point clears half-sell P&L."""
    if not getattr(config, "gridless_leading_edge", False):
        return False, "Leading edge disabled", None
    if not positions:
        return False, "No purchase point exists yet", None
    if len(positions) >= int(getattr(config, "max_active_positions", 10)):
        return False, "Max positions reached", None
    highest = get_highest_position(
        positions, _configured_token_decimals(config)
    )
    if highest is None:
        return False, "No valid purchase point exists", None
    position_id, position = highest
    observed = (position_pnls or {}).get(str(position_id), {})
    candidates = trigger_pnl_candidates(observed, "buy")
    if not candidates:
        if (position_pnls is not None
                and getattr(config, "bidirectional_pnl_enabled", False)):
            return False, "No fresh leading-edge P&L observation", position_id
        candidates = [("legacy", calculate_pnl(
            position, current_price, _configured_token_decimals(config)
        ))]
    source, pnl = max(candidates, key=lambda item: item[1])
    trigger = get_sell_trigger_percent(config) * 0.5
    crossed = pnl > trigger or math.isclose(
        pnl, trigger, rel_tol=1e-12, abs_tol=1e-9
    )
    reason = (
        f"Highest purchase point {source} P&L {pnl:.2f}% "
        f"{'>' if crossed else '<'}= 50% of sell ({trigger}%)"
    )
    return crossed, reason, position_id


def get_sell_trigger_percent(config: Any) -> float:
    """Return the configured net P&L threshold that wakes a normal sell."""
    if getattr(config, 'pnl_trigger_by_min_profit', False) is True:
        return float(getattr(config, 'min_profit_percent', 5.0))
    return float(getattr(config, 'gridless_sell_threshold', 5.0))


def threshold_gap_leading_edge_trigger(
        positions: Dict[str, Dict], current_price: float, config: Any,
        position_pnls: Optional[Dict[str, Dict[str, float]]] = None):
    """Return a leading-edge trigger that advances through an open price gap.

    The closest confirmed purchase below market is the moving lower anchor and
    the closest confirmed purchase above market is the upper boundary.  A buy
    is eligible only after price advances half a sell trigger from the lower
    anchor and there is another half-trigger of room before the upper boundary.
    This keeps fills evenly separated and, after a profitable lower position
    exits, leaves a newer anchor behind instead of immediately dip-rebuying
    against an old underwater position.
    """
    if not getattr(config, "gridless_leading_edge", False):
        return False, "Leading edge disabled", None
    if len(positions) < 2:
        return False, "No bounded position gap exists", None
    if len(positions) >= int(getattr(config, "max_active_positions", 10)):
        return False, "Max positions reached", None
    try:
        market_price = float(current_price)
    except (TypeError, ValueError):
        return False, "Current price is invalid", None
    if not math.isfinite(market_price) or market_price <= 0:
        return False, "Current price is invalid", None

    token_decimals = _configured_token_decimals(config)
    purchase_points = sorted(
        (price, str(position_id), position)
        for position_id, position in positions.items()
        for price in [get_threshold_entry_price(position, token_decimals)]
        if math.isfinite(price) and price > 0
    )
    lower = [point for point in purchase_points if point[0] <= market_price]
    upper = [point for point in purchase_points if point[0] > market_price]
    if not lower or not upper:
        return False, "Market is not inside a confirmed position gap", None

    lower_price, lower_id, _ = lower[-1]
    upper_price = upper[0][0]
    trigger = get_sell_trigger_percent(config) * 0.5
    room_above = ((upper_price - market_price) / market_price) * 100
    has_upper_room = room_above > trigger or math.isclose(
        room_above, trigger, rel_tol=1e-12, abs_tol=1e-9
    )
    if not has_upper_room:
        return (
            False,
            f"Gap has only {room_above:.2f}% room above market; "
            f"needs {trigger}%",
            lower_id,
        )

    pnl = ((market_price - lower_price) / lower_price) * 100
    crossed = pnl > trigger or math.isclose(
        pnl, trigger, rel_tol=1e-12, abs_tol=1e-9
    )
    reason = (
        f"Gap leading edge: nearest lower position {lower_id} price move "
        f"{pnl:.2f}% {'>' if crossed else '<'}= 50% of sell ({trigger}%); "
        f"{room_above:.2f}% room to next position"
    )
    return crossed, reason, lower_id


def threshold_gap_status(positions: Dict[str, Dict], current_price: float,
                         config: Any,
                         position_pnls: Optional[Dict[str, Dict[str, float]]] = None
                         ) -> Dict[str, Any]:
    """Describe threshold entry authorization and physical price spacing."""
    max_active = int(getattr(config, 'max_active_positions', 10))
    buy_threshold = float(getattr(config, 'gridless_buy_threshold', -10.0))
    spacing = get_sell_trigger_percent(config) * 0.5
    token_decimals = _configured_token_decimals(config)
    try:
        market_price = float(current_price)
    except (TypeError, ValueError):
        market_price = 0.0
    points = sorted(
        (price, str(position_id))
        for position_id, position in positions.items()
        for price in [get_threshold_entry_price(position, token_decimals)]
        if math.isfinite(price) and price > 0
    )
    status: Dict[str, Any] = {
        "enabled": bool(getattr(config, 'gridless_leading_edge', False)),
        "eligible": False,
        "decision": "invalid_market" if market_price <= 0 else "waiting",
        "reason": "Current price is invalid" if market_price <= 0 else "Waiting for entry spacing",
        "market_price": market_price,
        "filled_positions": len(positions),
        "max_positions": max_active,
        "remaining_slots": max(0, max_active - len(positions)),
        "buy_threshold_percent": buy_threshold,
        "leading_spacing_percent": spacing,
    }
    if not points or market_price <= 0:
        return status

    lowest_price, lowest_id = points[0]
    dip_move = ((market_price - lowest_price) / lowest_price) * 100
    status.update({
        "dip_anchor_position_id": lowest_id,
        "dip_anchor_price": lowest_price,
        "dip_price_move_percent": dip_move,
        "next_dip_price": lowest_price * (1 + buy_threshold / 100),
    })

    top = get_top_position(positions, token_decimals)
    candidates = []
    if top is not None:
        observed = (position_pnls or {}).get(str(top[0]), {})
        candidates = trigger_pnl_candidates(observed, "buy")
        if not candidates and not (
                position_pnls is not None
                and getattr(config, 'bidirectional_pnl_enabled', False)):
            candidates = [("legacy", calculate_pnl(
                top[1], market_price, token_decimals
            ))]
    if candidates:
        source, route_pnl = min(candidates, key=lambda item: item[1])
        status.update({
            "buy_trigger_source": source,
            "buy_trigger_pnl_percent": route_pnl,
            "buy_trigger_crossed": route_pnl <= buy_threshold,
        })
    else:
        route_pnl = None
        status["buy_trigger_crossed"] = False

    lower = [point for point in points if point[0] <= market_price]
    upper = [point for point in points if point[0] > market_price]
    if lower:
        lower_price, lower_id = lower[-1]
        lead_move = ((market_price - lower_price) / lower_price) * 100
        status.update({
            "lower_anchor_position_id": lower_id,
            "lower_anchor_price": lower_price,
            "lower_anchor_move_percent": lead_move,
            "next_leading_edge_price": lower_price * (1 + spacing / 100),
        })
    if upper:
        upper_price, upper_id = upper[0]
        room_above = ((upper_price - market_price) / market_price) * 100
        status.update({
            "upper_boundary_position_id": upper_id,
            "upper_boundary_price": upper_price,
            "room_above_percent": room_above,
            "max_gap_entry_price": upper_price / (1 + spacing / 100),
        })

    if len(positions) >= max_active:
        status.update(decision="at_capacity", reason="All position slots are filled")
        return status
    if (status["buy_trigger_crossed"]
            and (not status["enabled"] or len(positions) <= 1
                 or dip_move <= buy_threshold
                 or math.isclose(dip_move, buy_threshold,
                                 rel_tol=1e-12, abs_tol=1e-9))):
        status.update(
            eligible=True,
            decision="dip_buy",
            reason=(f"Net buy trigger crossed and price moved {dip_move:.2f}% "
                    f"from dip anchor {lowest_id}"),
        )
        return status
    if status["enabled"] and len(positions) == 1 and candidates:
        leading_source, leading_pnl = max(candidates, key=lambda item: item[1])
        lead_crossed = leading_pnl >= spacing or math.isclose(
            leading_pnl, spacing, rel_tol=1e-12, abs_tol=1e-9
        )
        if lead_crossed:
            status.update(
                eligible=True,
                decision="leading_edge",
                reason=(f"Single-position {leading_source} P&L "
                        f"{leading_pnl:.2f}% reached the {spacing:.2f}% "
                        "leading edge"),
                leading_trigger_source=leading_source,
                leading_trigger_pnl_percent=leading_pnl,
            )
            return status
    if (status["enabled"] and len(positions) > 1 and lower and upper):
        lead_move = float(status["lower_anchor_move_percent"])
        room_above = float(status["room_above_percent"])
        lead_crossed = lead_move >= spacing or math.isclose(
            lead_move, spacing, rel_tol=1e-12, abs_tol=1e-9
        )
        room_crossed = room_above >= spacing or math.isclose(
            room_above, spacing, rel_tol=1e-12, abs_tol=1e-9
        )
        if lead_crossed and room_crossed:
            status.update(
                eligible=True,
                decision="gap_leading_edge",
                reason=(f"Price advanced {lead_move:.2f}% from anchor {lower_id} "
                        f"with {room_above:.2f}% room below {upper_id}"),
            )
            return status
    if status["buy_trigger_crossed"] and status["enabled"] and len(positions) > 1:
        status.update(
            decision="dip_spacing_guard",
            reason=(f"Net buy trigger crossed, but price moved only {dip_move:.2f}% "
                    f"from anchor {lowest_id}; needs {buy_threshold:.2f}%"),
        )
    elif not lower or not upper:
        status.update(
            decision="outside_bounded_gap",
            reason="Market is not inside a bounded position gap",
        )
    else:
        status.update(
            decision="waiting_for_leading_edge",
            reason=(f"Waiting for {spacing:.2f}% rise from the lower anchor and "
                    f"{spacing:.2f}% room below the upper boundary"),
        )
    return status


def threshold_gap_fill_bounds(context: Dict[str, Any],
                              quoted_fill_price: float) -> Tuple[bool, float, float]:
    """Validate a prospective gap fill against its confirmed neighbours."""
    lower_price = float(context.get("lower_anchor_price") or 0)
    upper_price = float(context.get("upper_boundary_price") or 0)
    spacing = float(context.get("leading_spacing_percent") or 0)
    minimum_fill = lower_price * (1 + spacing / 100)
    maximum_fill = upper_price / (1 + spacing / 100)
    allowed = bool(
        quoted_fill_price > 0
        and lower_price > 0
        and upper_price > 0
        and quoted_fill_price >= minimum_fill
        and quoted_fill_price <= maximum_fill
    )
    return allowed, minimum_fill, maximum_fill


def survivor_next_leading_edge_price(
        positions: Dict[str, Dict], config: Any) -> Optional[float]:
    """Return the indicative raw-price target for the next Survivor lead buy.

    Execution remains governed by a fresh authorized buy-side P&L observation;
    this value is public dashboard telemetry, not an alternate trigger path.
    """
    prices = survivor_leading_edge_runway_prices(positions, config)
    return prices[0] if prices else None


def survivor_leading_edge_runway_prices(
        positions: Dict[str, Dict], config: Any) -> List[float]:
    """Return the next three sequential Survivor leading-edge targets.

    Only the first price is actionable. Later prices are forward projections
    that advance from the confirmed fill price after each guarded buy.
    """
    allocation_mode = str(
        getattr(config, 'gridless_allocation_mode', 'threshold')
    ).lower()
    if allocation_mode != 'survivor':
        return []
    if not getattr(config, 'gridless_leading_edge', False):
        return []
    available_slots = max(
        0,
        int(getattr(config, 'max_active_positions', 10)) - len(positions),
    )
    if not positions or available_slots == 0:
        return []
    highest = get_highest_position(
        positions, _configured_token_decimals(config)
    )
    if highest is None:
        return []
    purchase_price = get_buy_price(
        highest[1], _configured_token_decimals(config)
    )
    if not math.isfinite(purchase_price) or purchase_price <= 0:
        return []
    multiplier = 1 + get_sell_trigger_percent(config) * 0.5 / 100
    runway_length = min(SURVIVOR_LEADING_EDGE_RUNWAY, available_slots)
    return [purchase_price * multiplier**step
            for step in range(1, runway_length + 1)]


def trigger_pnl_candidates(observed: Dict[str, Any], direction: str):
    """Return named, finite P&L marks authorized for one strategy direction."""
    configured = observed.get(f"{direction}_trigger_pnls")
    if isinstance(configured, dict):
        candidates = []
        for source, value in configured.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                candidates.append((str(source), numeric))
        if candidates:
            return candidates
    fallback = observed.get(
        f"{direction}_trigger_pnl", observed.get(f"{direction}_pnl")
    )
    try:
        numeric = float(fallback)
    except (TypeError, ValueError):
        return []
    return [(direction, numeric)] if math.isfinite(numeric) else []


def trigger_focus_candidates(positions: Dict[str, Dict], config: Any,
                             position_pnls: Dict[str, Dict[str, Any]]):
    """Describe how close every authorized mark is to an actionable trigger."""
    result = {"buy": {}, "sell": {}}
    token_decimals = _configured_token_decimals(config)
    max_active = int(getattr(config, 'max_active_positions', 10))
    buy_threshold = float(getattr(config, 'gridless_buy_threshold', -10.0))
    sell_threshold = get_sell_trigger_percent(config)
    leading_edge = bool(getattr(config, 'gridless_leading_edge', False))
    stoploss_enabled = bool(getattr(config, 'gridless_stoploss_enabled', False))
    stoploss_threshold = float(getattr(
        config, 'gridless_stoploss_threshold', -25.0
    ))

    allocation_mode = str(getattr(
        config, 'gridless_allocation_mode', 'threshold'
    )).lower()
    if (allocation_mode != 'drawdown_ladder'
            and positions and len(positions) < max_active):
        top = (
            get_highest_position(positions, token_decimals)
            if allocation_mode == 'survivor'
            else get_top_position(positions, token_decimals)
        )
        if top is not None:
            observed = (position_pnls or {}).get(str(top[0]), {})
            for source, pnl in trigger_pnl_candidates(observed, "buy"):
                if allocation_mode == 'survivor':
                    leading_threshold = sell_threshold * 0.5
                    distances = [max(0.0, leading_threshold - pnl)]
                    triggered = leading_edge and pnl >= leading_threshold
                else:
                    distances = [max(0.0, pnl - buy_threshold)]
                    triggered = pnl <= buy_threshold
                if (allocation_mode != 'survivor'
                        and leading_edge and len(positions) == 1):
                    leading_threshold = sell_threshold * 0.5
                    distances.append(max(0.0, leading_threshold - pnl))
                    triggered = triggered or pnl >= leading_threshold
                result["buy"][source] = {
                    "triggered": triggered,
                    "distance": 0.0 if triggered else min(distances),
                    "pnl": pnl,
                    "position_id": str(top[0]),
                }

    for position_id in positions:
        observed = (position_pnls or {}).get(str(position_id), {})
        for source, pnl in trigger_pnl_candidates(observed, "sell"):
            distances = [max(0.0, sell_threshold - pnl)]
            triggered = pnl >= sell_threshold
            if stoploss_enabled:
                distances.append(max(0.0, pnl - stoploss_threshold))
                triggered = triggered or pnl <= stoploss_threshold
            candidate = {
                "triggered": triggered,
                "distance": 0.0 if triggered else min(distances),
                "pnl": pnl,
                "position_id": str(position_id),
            }
            previous = result["sell"].get(source)
            if (previous is None
                    or (candidate["triggered"] and not previous["triggered"])
                    or (candidate["triggered"] == previous["triggered"]
                        and candidate["distance"] < previous["distance"])):
                result["sell"][source] = candidate

    return result


def should_buy(positions: Dict[str, Dict], current_price: float, config: Any,
               position_pnls: Optional[Dict[str, Dict[str, float]]] = None) -> Tuple[bool, str]:
    """Check buy rules with leading edge support.
    
    Standard buy: no positions OR (under max AND top_pnl <= buy_threshold)
    Leading edge: one position, or a bounded multi-position gap with enough
    room on both sides, at 50% of sell_threshold
    """
    max_active = getattr(config, 'max_active_positions', 10)
    buy_threshold = getattr(config, 'gridless_buy_threshold', -10.0)
    sell_threshold = get_sell_trigger_percent(config)
    leading_edge_enabled = getattr(config, 'gridless_leading_edge', False)
    token_decimals = _configured_token_decimals(config)
    
    # No positions - initial buy
    if len(positions) == 0 and max_active > 0:
        return (True, "Initial buy (no positions)")
    
    # Max positions reached
    if len(positions) >= max_active:
        return (False, f"Max positions reached ({len(positions)}/{max_active})")
    
    # Standard dip-buying logic
    top = get_top_position(positions, token_decimals)
    if top is None:
        return (True, "No holding positions found")
    
    observed = (position_pnls or {}).get(str(top[0]), {})
    candidates = trigger_pnl_candidates(observed, "buy")
    if not candidates:
        if (position_pnls is not None
                and getattr(config, 'bidirectional_pnl_enabled', False)):
            candidates = []
        else:
            candidates = [("legacy", calculate_pnl(
                top[1], current_price, token_decimals
            ))]
    dip_source, dip_pnl = (
        min(candidates, key=lambda item: item[1])
        if candidates else (None, None)
    )
    if dip_pnl is not None and dip_pnl <= buy_threshold:
        if leading_edge_enabled and len(positions) > 1:
            gap_status = threshold_gap_status(
                positions, current_price, config, position_pnls
            )
            if gap_status["decision"] == "dip_buy":
                return (
                    True,
                    f"Top position {dip_source} P&L {dip_pnl:.2f}% <= "
                    f"threshold {buy_threshold}%; price spacing confirmed at "
                    f"{gap_status['dip_price_move_percent']:.2f}%",
                )
        else:
            return (True, f"Top position {dip_source} P&L {dip_pnl:.2f}% <= threshold {buy_threshold}%")
    
    # Leading edge: buy into strength when single position is climbing
    # Trigger at 50% of sell threshold (e.g., if sell=5%, buy at +2.5%)
    if leading_edge_enabled and len(positions) == 1 and candidates:
        leading_edge_trigger = sell_threshold * 0.5
        leading_source, leading_pnl = max(candidates, key=lambda item: item[1])
        if leading_pnl > leading_edge_trigger or math.isclose(
            leading_pnl, leading_edge_trigger, rel_tol=1e-12, abs_tol=1e-9
        ):
            return (True, f"Leading edge: {leading_source} P&L {leading_pnl:.2f}% >= 50% of sell ({leading_edge_trigger}%)")

    if leading_edge_enabled and len(positions) > 1:
        gap_triggered, gap_reason, _ = threshold_gap_leading_edge_trigger(
            positions, current_price, config, position_pnls
        )
        if gap_triggered:
            return True, gap_reason

        gap_status = threshold_gap_status(
            positions, current_price, config, position_pnls
        )
        if gap_status["decision"] == "dip_spacing_guard":
            return False, gap_status["reason"]

    if not candidates:
        return (False, "No fresh buy-trigger P&L observation")
    
    return (False, f"Top position P&L marks remain above threshold {buy_threshold}%")


def get_capacity_warning(positions: Dict[str, Dict], current_price: float, config: Any,
                         position_pnls: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[Dict]:
    """Describe a dip buy blocked only because all gridless slots are filled."""
    max_active = getattr(config, 'max_active_positions', 10)
    buy_threshold = getattr(config, 'gridless_buy_threshold', -10.0)
    token_decimals = _configured_token_decimals(config)
    if max_active <= 0 or len(positions) < max_active:
        return None

    top = get_top_position(positions, token_decimals)
    if top is None:
        return None

    observed = (position_pnls or {}).get(str(top[0]), {})
    candidates = trigger_pnl_candidates(observed, "buy")
    if not candidates:
        if (position_pnls is not None
                and getattr(config, 'bidirectional_pnl_enabled', False)):
            return None
        candidates = [("legacy", calculate_pnl(
            top[1], current_price, token_decimals
        ))]
    _, top_pnl = min(candidates, key=lambda item: item[1])
    if top_pnl > buy_threshold:
        return None

    return {
        'code': 'buy_blocked_at_capacity',
        'message': 'Buy point reached, but all position slots are filled',
        'highest_position_pnl': round(top_pnl, 2),
        'buy_threshold': round(buy_threshold, 2),
        'filled_positions': len(positions),
        'max_positions': max_active,
    }


def should_sell(position: Dict[str, int], current_price: float, config: Any,
                quote_profit_eth: float = 0.0) -> Tuple[bool, str]:
    """Check sell rules: profit target OR stoploss."""
    sell_threshold = get_sell_trigger_percent(config)
    stoploss_threshold = getattr(config, 'gridless_stoploss_threshold', -25.0)
    stoploss_enabled = getattr(config, 'gridless_stoploss_enabled', False)
    min_profit = getattr(config, 'min_profit_percent', 1.5)
    token_decimals = _configured_token_decimals(config)
    pnl = calculate_pnl(position, current_price, token_decimals)
    # Stoploss check (highest priority)
    if stoploss_enabled and pnl <= stoploss_threshold:
        if quote_profit_eth < 0:
            return (False, f"STOPLOSS ({pnl:.1f}%) but quote loss")
        return (True, f"STOPLOSS: {pnl:.1f}%")
    # Profit target check (with small tolerance for floating point precision)
    if pnl >= sell_threshold - 0.01:  # >= with 0.01% tolerance
        if quote_profit_eth <= 0:
            return (False, f"Target met ({pnl:.1f}%) but no quote profit")
        cost_wei = position.get('cost_wei', position.get('cost', 0) * 10**9)
        cost_eth = cost_wei / 1e18
        min_profit_eth = cost_eth * (min_profit / 100)
        if quote_profit_eth < min_profit_eth:
            return (False, f"Target met ({pnl:.1f}%) but quote < min")
        return (True, f"PROFIT: {pnl:.1f}%")
    return (False, f"No sell (P&L: {pnl:.1f}%)")


def find_sell_candidate(positions: Dict[str, Dict], current_price: float,
                        config: Any, quote_profit_eth: float = 0.0) -> Optional[Tuple[str, Dict, str]]:
    """Find best position to sell (stoploss first, then highest P&L)."""
    stoploss_enabled = getattr(config, 'gridless_stoploss_enabled', False)
    stoploss_threshold = getattr(config, 'gridless_stoploss_threshold', -25.0)
    token_decimals = _configured_token_decimals(config)
    candidates = []
    for pos_id, pos in positions.items():
        pnl = calculate_pnl(pos, current_price, token_decimals)
        should_sell_flag, reason = should_sell(pos, current_price, config, quote_profit_eth)
        if should_sell_flag:
            priority = 0 if (stoploss_enabled and pnl <= stoploss_threshold) else 1
            candidates.append((priority, -pnl, pos_id, pos, reason))
    if not candidates:
        return None
    candidates.sort()
    return (candidates[0][2], candidates[0][3], candidates[0][4])


def validate_position(position: Dict[str, int]) -> bool:
    """Validate: cost_wei or cost > 0 and balance > 0."""
    cost_wei = position.get('cost_wei', 0)
    cost = position.get('cost', 0)
    has_cost = cost_wei > 0 or cost > 0
    has_balance = position.get('balance', 0) > 0
    return has_cost and has_balance


def load_positions() -> Dict[str, Dict[str, int]]:
    """Load and validate positions from JSON."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, 'r') as f:
            data = json.load(f)
        positions = {}
        for k, v in data.items():
            if validate_position(v):
                # Migrate from cost (nano-ETH) to cost_wei if needed
                if 'cost' in v and 'cost_wei' not in v:
                    positions[k] = {
                        'cost_wei': int(v['cost']) * 10**9,
                        'balance': int(v['balance'])
                    }
                else:
                    positions[k] = {
                        'cost_wei': int(v.get('cost_wei', 0)),
                        'balance': int(v['balance'])
                    }
                # Optional immutable provenance used by the recovery tool.
                # Keeping it in the position itself makes replay protection
                # survive a missing/corrupt auxiliary journal.
                if v.get('reconciliation_tx_hash'):
                    positions[k]['reconciliation_tx_hash'] = str(
                        v['reconciliation_tx_hash']
                    ).lower()
                if int(v.get('deferred_sell_gas_wei', 0) or 0) > 0:
                    positions[k]['deferred_sell_gas_wei'] = int(
                        v['deferred_sell_gas_wei']
                    )
                if v.get('ladder_id'):
                    positions[k]['ladder_id'] = str(v['ladder_id'])
                if v.get('ladder_level_index') is not None:
                    positions[k]['ladder_level_index'] = int(
                        v['ladder_level_index']
                    )
                if int(v.get('ladder_principal_wei', 0) or 0) > 0:
                    positions[k]['ladder_principal_wei'] = int(
                        v['ladder_principal_wei']
                    )
                if v.get('ladder_entry_kind') in {'ladder', 'leading_edge'}:
                    positions[k]['ladder_entry_kind'] = v['ladder_entry_kind']
                if float(v.get('ladder_fill_price', 0) or 0) > 0:
                    positions[k]['ladder_fill_price'] = float(
                        v['ladder_fill_price']
                    )
                if v.get('ladder_bootstrap_reference') is True:
                    positions[k]['ladder_bootstrap_reference'] = True
                if float(v.get('threshold_fill_price', 0) or 0) > 0:
                    positions[k]['threshold_fill_price'] = float(
                        v['threshold_fill_price']
                    )
                if v.get('threshold_entry_kind') in {
                        'dip', 'leading_edge', 'gap_leading_edge', 'initial'}:
                    positions[k]['threshold_entry_kind'] = str(
                        v['threshold_entry_kind']
                    )
        return positions
    except (json.JSONDecodeError, IOError):
        return {}


def save_positions(positions: Dict[str, Dict[str, int]]) -> None:
    """Atomic save: write to temp, then rename."""
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    temp_file = POSITIONS_FILE + '.tmp'
    with open(temp_file, 'w') as f:
        json.dump(positions, f, indent=2)
    os.replace(temp_file, POSITIONS_FILE)


def add_position(cost_wei: int, balance: int,
                 ladder_id: Optional[str] = None,
                 ladder_level_index: Optional[int] = None,
                 ladder_principal_wei: Optional[int] = None,
                 ladder_entry_kind: Optional[str] = None,
                 ladder_fill_price: Optional[float] = None,
                 ladder_bootstrap_reference: bool = False,
                 threshold_fill_price: Optional[float] = None,
                 threshold_entry_kind: Optional[str] = None) -> str:
    """Add new position with lowest available ID (fills gaps).
    
    Args:
        cost_wei: Cost in wei (not nano-ETH)
        balance: Token balance in wei
    """
    positions = load_positions()
    
    # Find the lowest unused ID
    existing_ids = {int(k) for k in positions.keys()}
    next_id = 0
    while next_id in existing_ids:
        next_id += 1
    
    position = {'cost_wei': cost_wei, 'balance': balance}
    if threshold_fill_price is not None or threshold_entry_kind is not None:
        try:
            threshold_price = float(threshold_fill_price or 0)
        except (TypeError, ValueError):
            threshold_price = 0.0
        if (not math.isfinite(threshold_price) or threshold_price <= 0
                or threshold_entry_kind not in {
                    'dip', 'leading_edge', 'gap_leading_edge', 'initial'
                }):
            raise ValueError("threshold entry provenance must be complete")
        position.update({
            'threshold_fill_price': threshold_price,
            'threshold_entry_kind': threshold_entry_kind,
        })
    ladder_values = (ladder_id, ladder_level_index, ladder_principal_wei)
    if any(value is not None for value in ladder_values):
        if (not ladder_id or ladder_level_index is None
                or ladder_principal_wei is None or ladder_principal_wei <= 0):
            raise ValueError("ladder position provenance must be complete")
        position.update({
            'ladder_id': str(ladder_id),
            'ladder_level_index': int(ladder_level_index),
            'ladder_principal_wei': int(ladder_principal_wei),
            'ladder_entry_kind': (
                ladder_entry_kind if ladder_entry_kind in {'ladder', 'leading_edge'}
                else 'ladder'
            ),
        })
        if ladder_entry_kind == 'leading_edge':
            if ladder_fill_price is None or float(ladder_fill_price) <= 0:
                raise ValueError("leading-edge provenance requires fill price")
            position['ladder_fill_price'] = float(ladder_fill_price)
            if ladder_bootstrap_reference:
                position['ladder_bootstrap_reference'] = True
        elif ladder_bootstrap_reference:
            raise ValueError(
                "bootstrap-reference provenance requires a leading-edge entry"
            )
    positions[str(next_id)] = position
    save_positions(positions)
    return str(next_id)


def remove_position(position_id: str) -> bool:
    """Remove position by ID."""
    positions = load_positions()
    if position_id not in positions:
        return False
    del positions[position_id]
    save_positions(positions)
    return True


def migrate_from_grid(grid_positions: Dict[str, Dict]) -> Dict[str, Dict[str, int]]:
    """Migrate grid positions to gridless format."""
    gridless, next_id = {}, 0
    for pos in grid_positions.values():
        if pos.get('balance', 0) > 0:
            gridless[str(next_id)] = {'cost': int(pos.get('cost', 0)), 'balance': int(pos['balance'])}
            next_id += 1
    return gridless


def migrate_to_grid(gridless_positions: Dict[str, Dict[str, int]],
                    grid_spacing_percent: float = 6.0,
                    token_decimals: int = 18) -> Dict[str, Dict]:
    """Migrate gridless positions back to grid format.
    
    Args:
        gridless_positions: Gridless position dict {id: {cost, balance}}
        grid_spacing_percent: Grid spacing % for calculating ranges
        
    Returns:
        Grid format positions dict
    """
    import math
    grid_positions = {}
    
    for pos_id, pos in gridless_positions.items():
        balance = pos.get('balance', 0)
        cost = pos.get('cost', 0)
        if balance <= 0:
            continue
        
        # Calculate buy price
        buy_price = (cost / 1e9) / (balance / (10 ** token_decimals))
        
        # Calculate grid range based on buy price
        # Position covers range around its buy price
        range_pct = grid_spacing_percent / 100
        
        # Scale to nano-WETH (10^9)
        buy_price_nano = int(buy_price * 1e9)
        
        # Create grid position
        grid_pos = {
            'buyMin': int(buy_price_nano * (1 - range_pct)),
            'buyMax': int(buy_price_nano * (1 + range_pct)),
            'sellMin': int(buy_price_nano * (1 + range_pct * 2)),  # ~2x spacing for sell
            'cost': cost,
            'balance': balance
        }
        
        grid_positions[pos_id] = grid_pos
    
    return grid_positions


def save_grid_positions(grid_positions: Dict[str, Dict], filepath: str = "data/positions.json") -> None:
    """Save grid positions to JSON file (atomic write)."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    temp_file = filepath + '.tmp'
    with open(temp_file, 'w') as f:
        json.dump(grid_positions, f, indent=2)
    os.replace(temp_file, filepath)
