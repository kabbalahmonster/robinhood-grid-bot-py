#!/usr/bin/env python3
"""Plan and execute guarded many-to-many position-capacity transfers."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

WEI = Decimal(10**18)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WARNED_ENV_PERMISSIONS = set()
TREASURY_SOURCE = "Treasury"


def decimal_eth(value, label, *, allow_zero=False):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be decimal ETH") from exc
    if not result.is_finite() or result < 0 or (result == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {qualifier} decimal ETH")
    wei = int(result * WEI)
    if result and wei == 0:
        raise ValueError(f"{label} is below one wei")
    return result, wei


def dotenv(path, *, require_unique=()):
    values = {}
    duplicates = set()
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        key, value = match.groups()
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key in values:
            duplicates.add(key)
        values[key] = value
    unsafe_duplicates = duplicates & set(require_unique)
    if unsafe_duplicates:
        raise ValueError(
            f"{path}: variable(s) must be unique for safe capacity updates: "
            f"{', '.join(sorted(unsafe_duplicates))}"
        )
    return values


def filled_positions(bot_dir):
    counts = []
    root = Path(bot_dir)
    status = root / "data/fleet_status.json"
    if status.exists():
        payload = json.loads(status.read_text(encoding="utf-8"))
        counts.append(int(payload.get("filled_positions") or 0))
    for filename in ("positions.json", "gridless_positions.json"):
        path = root / "data" / filename
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path}: expected a JSON object")
        counts.append(sum(
            1 for item in payload.values()
            if isinstance(item, dict) and int(item.get("balance") or 0) > 0
        ))
    return max(counts, default=0)


def bot_metadata(name, directory, *, require_key=False, warn_permissions=True):
    root = Path(directory).resolve()
    env_path = root / ".env"
    if not env_path.is_file():
        raise ValueError(f"{name}: missing {env_path}")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if warn_permissions and mode & 0o077:
        warning_key = str(env_path)
        if warning_key not in WARNED_ENV_PERMISSIONS:
            print(
                f"POSITION PASS WARNING: {name}: .env permissions are broader than "
                f"recommended ({mode:o}); continuing because this does not affect "
                f"plan validity. Repair with: chmod 600 {env_path}",
                file=sys.stderr,
                flush=True,
            )
            WARNED_ENV_PERMISSIONS.add(warning_key)
    # python-dotenv, used by the bot itself, resolves repeated assignments with
    # the final value winning. Match that behavior instead of refusing an
    # unrelated legacy duplicate. MAX_ACTIVE_POSITIONS is the one exception:
    # pass-positions edits it and must not leave another assignment overriding
    # the committed capacity later in the file.
    values = dotenv(env_path, require_unique={"MAX_ACTIVE_POSITIONS"})
    raw_capacity = values.get("MAX_ACTIVE_POSITIONS", values.get("MAX_POSITIONS"))
    if raw_capacity is None:
        raise ValueError(f"{name}: MAX_ACTIVE_POSITIONS or MAX_POSITIONS is required")
    try:
        capacity = int(raw_capacity)
    except ValueError as exc:
        raise ValueError(f"{name}: position capacity must be an integer") from exc
    filled = filled_positions(root)
    if capacity < 0 or filled > capacity:
        raise ValueError(f"{name}: invalid capacity {capacity} with {filled} filled positions")
    reserve, reserve_wei = decimal_eth(
        values.get("TREASURY_POSITION_RESERVE_ETH", "0"),
        f"{name} TREASURY_POSITION_RESERVE_ETH", allow_zero=True,
    )
    gas_reserve, gas_reserve_wei = decimal_eth(
        values.get("ETH_GAS_RESERVE", "0"), f"{name} ETH_GAS_RESERVE", allow_zero=True,
    )
    raw_transfer_gas_cap = values.get("MAX_FEE_TRANSFER_GAS_ETH") or "0.0001"
    transfer_gas_cap, transfer_gas_cap_wei = decimal_eth(
        raw_transfer_gas_cap, f"{name} MAX_FEE_TRANSFER_GAS_ETH",
    )
    private_key = values.get("PRIVATE_KEY", "")
    if require_key and not private_key:
        raise ValueError(f"{name}: PRIVATE_KEY is required for a donor")
    return {
        "name": name, "dir": str(root), "env": str(env_path), "values": values,
        "capacity": capacity, "filled": filled, "available": capacity - filled,
        "reserve_eth": str(reserve), "reserve_wei": reserve_wei,
        "gas_reserve_eth": str(gas_reserve), "gas_reserve_wei": gas_reserve_wei,
        "transfer_gas_cap_eth": str(transfer_gas_cap),
        "transfer_gas_cap_wei": transfer_gas_cap_wei,
        "private_key": private_key,
    }


def treasury_metadata(path, *, warn_permissions=True):
    env_path = Path(path).expanduser().resolve()
    if not env_path.is_file():
        raise ValueError(f"Treasury env not found: {env_path}")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if warn_permissions and mode & 0o077:
        warning_key = str(env_path)
        if warning_key not in WARNED_ENV_PERMISSIONS:
            print(
                f"POSITION PASS WARNING: Treasury: .env permissions are broader than "
                f"recommended ({mode:o}); continuing because this does not affect plan "
                f"validity. Repair with: chmod 600 {env_path}",
                file=sys.stderr,
                flush=True,
            )
            WARNED_ENV_PERMISSIONS.add(warning_key)
    values = dotenv(env_path)
    if not values.get("PRIVATE_KEY") or not values.get("RPC_URL"):
        raise ValueError("Treasury env requires PRIVATE_KEY and RPC_URL")
    reserve, reserve_wei = decimal_eth(
        values.get("ETH_GAS_RESERVE", ""), "Treasury ETH_GAS_RESERVE"
    )
    raw_position_amount = values.get("TREASURY_POSITION_RESERVE_ETH", "")
    position_amount = position_amount_wei = None
    if raw_position_amount:
        position_amount, position_amount_wei = decimal_eth(
            raw_position_amount, "Treasury TREASURY_POSITION_RESERVE_ETH"
        )
    raw_transfer_gas_cap = values.get("MAX_FEE_TRANSFER_GAS_ETH") or "0.0001"
    transfer_gas_cap, transfer_gas_cap_wei = decimal_eth(
        raw_transfer_gas_cap, "Treasury MAX_FEE_TRANSFER_GAS_ETH"
    )
    return {
        "name": TREASURY_SOURCE,
        "kind": "treasury",
        "env": str(env_path),
        "values": values,
        "private_key": values["PRIVATE_KEY"],
        "reserve_eth": str(position_amount) if position_amount is not None else None,
        "reserve_wei": position_amount_wei,
        "gas_reserve_eth": str(reserve),
        "gas_reserve_wei": reserve_wei,
        "transfer_gas_cap_eth": str(transfer_gas_cap),
        "transfer_gas_cap_wei": transfer_gas_cap_wei,
    }


def parse_specs(raw_values, label):
    items = []
    seen = set()
    for raw in raw_values or []:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                raise ValueError(f"{label} contains an empty bot name")
            name, separator, count_text = token.partition("=")
            if not NAME_RE.fullmatch(name):
                raise ValueError(f"Invalid bot name in {label}: {name}")
            normalized = name.lower()
            if normalized in seen:
                raise ValueError(f"Duplicate bot in {label}: {name}")
            seen.add(normalized)
            count = None
            if separator:
                if not count_text.isdigit() or int(count_text) < 1:
                    raise ValueError(f"{label} count for {name} must be a positive integer")
                count = int(count_text)
            items.append((name, count))
    if not items:
        raise ValueError(f"At least one {label} bot is required")
    return items


def resolve_names(specs, bots, label):
    known = {name.lower(): name for name in bots}
    resolved = []
    for supplied, count in specs:
        if supplied.lower() not in known:
            raise ValueError(f"Unknown fleet bot in {label}: {supplied}")
        resolved.append((known[supplied.lower()], count))
    return resolved


def resolve_source_specs(raw_values, bots, destination_specs):
    if not raw_values:
        return []
    specs = parse_specs(raw_values, "--from")
    all_specs = [item for item in specs if item[0].lower() == "all"]
    if all_specs:
        if len(specs) != 1 or all_specs[0][1] is not None:
            raise ValueError("--from all must be used alone without an exact count")
        excluded = {name.lower() for name, _count in destination_specs}
        expanded = [
            (name, None) for name in bots if name.lower() not in excluded
        ]
        if not expanded:
            raise ValueError("--from all has no fleet bots left after excluding recipients")
        return expanded
    return resolve_names(specs, bots, "--from")


def parse_positions(value):
    if value is None:
        return None
    if str(value).lower() in {"all", "available"}:
        return "available"
    if not str(value).isdigit() or int(value) < 1:
        raise ValueError("--positions must be a positive integer, all, or available")
    return int(value)


def nonnegative_integer(value, label):
    if not str(value).isdigit():
        raise ValueError(f"{label} must be a non-negative integer")
    return int(value)


def parse_reserve_overrides(values, bots):
    overrides = {}
    known = {name.lower(): name for name in bots}
    for raw in values or []:
        name, separator, count_text = raw.partition("=")
        if not separator or name.lower() not in known:
            raise ValueError(f"--reserve-from must be a fleet BOT=N assignment: {raw}")
        canonical = known[name.lower()]
        if canonical in overrides:
            raise ValueError(f"Duplicate --reserve-from override: {canonical}")
        overrides[canonical] = nonnegative_integer(
            count_text, f"{canonical} reserve"
        )
    return overrides


def maximum_source_total(specs, capacities):
    total = 0
    for name, count in specs:
        available = capacities[name]
        if count is not None:
            if count > available:
                raise ValueError(
                    f"{name} can give at most {available} available position(s), not {count}"
                )
            total += count
        else:
            total += available
    return total


def allocate_destinations(specs, total, metadata):
    counts = fair_allocate(
        specs,
        total,
        initial_availability={name: metadata[name]["available"] for name, _ in specs},
        label="destination",
    )
    return {name: count for name, count in counts.items() if count}


def infer_total(requested, sources, destinations):
    if requested is not None:
        if requested < 1:
            raise ValueError("--positions must be a positive integer")
        return requested
    totals = []
    for specs in (sources, destinations):
        if all(count is not None for _, count in specs):
            totals.append(sum(count for _, count in specs))
    if not totals:
        raise ValueError("--positions is required unless one side has counts for every bot")
    if len(totals) == 2 and totals[0] != totals[1]:
        raise ValueError(f"Manual source and destination totals differ: {totals[0]} != {totals[1]}")
    return totals[0]


def fair_allocate(specs, total, capacities=None, label="allocation",
                  initial_availability=None, source_availability=None):
    result = {name: (count or 0) for name, count in specs}
    fixed = sum(result.values())
    if fixed > total:
        raise ValueError(f"Manual {label} counts total {fixed}, exceeding {total}")
    if capacities:
        for name, count in result.items():
            if count > capacities[name]:
                raise ValueError(f"{name} can give at most {capacities[name]} available position(s), not {count}")
    flexible = [name for name, count in specs if count is None]
    remaining = total - fixed
    if remaining and not flexible:
        raise ValueError(f"Manual {label} counts total {fixed}, but {total} positions were requested")
    while remaining:
        candidates = [
            name for name in flexible
            if not capacities or result[name] < capacities[name]
        ]
        if not candidates:
            available = sum(capacities[n] - result[n] for n in flexible)
            raise ValueError(
                f"Not enough available donor positions; {remaining} still needed "
                f"({available} allocatable)"
            )
        if capacities:
            # Give from the donor that will have the most open capacity left.
            # max() deliberately keeps the first supplied name on ties.
            balancing = source_availability or capacities
            name = max(candidates, key=lambda item: balancing[item] - result[item])
        elif initial_availability is not None:
            # Add to the recipient with the least open capacity. min() keeps
            # supplied order on ties, making the plan stable and reproducible.
            name = min(
                candidates,
                key=lambda item: initial_availability[item] + result[item],
            )
        else:
            name = min(candidates, key=lambda item: result[item])
        result[name] += 1
        remaining -= 1
    return result


def build_routes(source_counts, destination_counts):
    destinations = [[name, count] for name, count in destination_counts.items() if count]
    routes = []
    cursor = 0
    for source, amount in source_counts.items():
        left = amount
        while left:
            while cursor < len(destinations) and destinations[cursor][1] == 0:
                cursor += 1
            if cursor >= len(destinations):
                raise ValueError("Source and destination allocation totals differ")
            destination, needed = destinations[cursor]
            units = min(left, needed)
            routes.append({"source": source, "destination": destination, "positions": units})
            left -= units
            destinations[cursor][1] -= units
    return routes


def parse_amount_overrides(values, bots, *, option="--amount-from", label="amount per position",
                           allow_treasury=False):
    overrides = {}
    known = {name.lower(): name for name in bots}
    if allow_treasury:
        known[TREASURY_SOURCE.lower()] = TREASURY_SOURCE
    for raw in values or []:
        name, separator, amount = raw.partition("=")
        if not separator or name.lower() not in known:
            raise ValueError(f"{option} must be a fleet BOT=ETH assignment: {raw}")
        canonical = known[name.lower()]
        if canonical in overrides:
            raise ValueError(f"Duplicate {option} override: {canonical}")
        overrides[canonical] = decimal_eth(amount, f"{canonical} {label}")[0]
    return overrides


def treasury_affordable_positions(total, amount_wei, balance_wei, reserve_wei,
                                  gas_cap_wei, destination_counts):
    """Return the largest slot count safe under the treasury's configured caps."""
    for count in range(total, -1, -1):
        route_count = len(build_routes(
            {TREASURY_SOURCE: count}, destination_counts
        )) if count else 0
        required = count * amount_wei + route_count * gas_cap_wei + reserve_wei
        if required <= balance_wei:
            return count
    return 0


def canonical_id(plan):
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".position-pass.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replace_capacity_text(text, updated):
    pattern = re.compile(r"^(\s*(?:export\s+)?MAX_ACTIVE_POSITIONS\s*=)(.*?)(\r?\n)?$", re.M)
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise ValueError("MAX_ACTIVE_POSITIONS is defined more than once")
    if matches:
        match = matches[0]
        comment = ""
        old = match.group(2)
        if " #" in old:
            comment = " #" + old.split(" #", 1)[1]
        return text[:match.start()] + f"{match.group(1)}{updated}{comment}{match.group(3) or os.linesep}" + text[match.end():]
    suffix = "" if not text or text.endswith(("\n", "\r")) else os.linesep
    return text + suffix + f"MAX_ACTIVE_POSITIONS={updated}{os.linesep}"


def capacity_value(path):
    values = dotenv(path, require_unique={"MAX_ACTIVE_POSITIONS"})
    raw = values.get("MAX_ACTIVE_POSITIONS", values.get("MAX_POSITIONS"))
    return int(raw) if raw is not None else None


def apply_capacities(journal, journal_path):
    changes = journal["capacity_changes"]
    journal["status"] = "committing"
    atomic_json(journal_path, journal)
    originals = {}
    try:
        for name, change in changes.items():
            path = Path(change["env"])
            current = capacity_value(path)
            if current not in (change["before"], change["after"]):
                raise ValueError(f"{name}: capacity drifted to {current}; expected {change['before']} or {change['after']}")
            originals[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for name, change in changes.items():
            path = Path(change["env"])
            if capacity_value(path) == change["after"]:
                continue
            backup = path.with_name(path.name + f".bak.position-pass.{journal['plan_id']}")
            if not backup.exists():
                backup.write_bytes(originals[path][0]); os.chmod(backup, originals[path][1])
            updated = replace_capacity_text(originals[path][0].decode(), change["after"])
            fd, temporary = tempfile.mkstemp(prefix=".env.position-pass.", dir=path.parent, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(updated); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, originals[path][1]); os.replace(temporary, path)
    except Exception:
        for path, (content, mode) in originals.items():
            fd, temporary = tempfile.mkstemp(prefix=".env.position-pass.rollback.", dir=path.parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, mode); os.replace(temporary, path)
        journal["status"] = "transfers_confirmed"
        atomic_json(journal_path, journal)
        raise
    journal["status"] = "complete"
    journal["completed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(journal_path, journal)


def chain_imports():
    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError("pass-positions requires the bot virtualenv dependencies") from exc
    return Account, Web3


def validate_local_plan(plan, metadata):
    """Validate deterministic execution inputs without contacting an RPC."""
    Account, _Web3 = chain_imports()
    addresses = {}
    for name in set(plan["sources"]) | set(plan["destinations"]):
        key = metadata[name]["private_key"]
        if not key:
            raise ValueError(f"{name}: PRIVATE_KEY is required to resolve its fleet wallet")
        addresses[name] = Account.from_key(key).address
        planned = plan.get("wallet_addresses", {}).get(name)
        if planned and planned.lower() != addresses[name].lower():
            raise ValueError(f"{name}: wallet address changed since this plan was created")
    if len({address.lower() for address in addresses.values()}) != len(addresses):
        raise ValueError("Selected bots do not have unique wallet addresses")

    configured_chain_ids = set()
    for name in plan["sources"]:
        values = metadata[name]["values"]
        if not values.get("RPC_URL"):
            raise ValueError(f"{name}: RPC_URL is required")
        chain_text = values.get("CHAIN_ID")
        if chain_text:
            configured_chain_ids.add(int(chain_text))
        try:
            multiplier = Decimal(values.get("GAS_LIMIT_MULTIPLIER", "1.05"))
        except InvalidOperation as exc:
            raise ValueError(f"{name}: GAS_LIMIT_MULTIPLIER must be at least 1") from exc
        if not multiplier.is_finite() or multiplier < 1:
            raise ValueError(f"{name}: GAS_LIMIT_MULTIPLIER must be at least 1")
    if len(configured_chain_ids) > 1:
        raise ValueError("All donor bots must use the same chain")


def treasury_live_balance(data):
    Account, Web3 = chain_imports()
    values = data["values"]
    w3 = Web3(Web3.HTTPProvider(values["RPC_URL"], request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError("Treasury: could not connect to RPC_URL")
    expected = int(values.get("CHAIN_ID", str(w3.eth.chain_id)))
    if int(w3.eth.chain_id) != expected:
        raise ValueError(f"Treasury: RPC chain {w3.eth.chain_id} != CHAIN_ID={expected}")
    address = Account.from_key(values["PRIVATE_KEY"]).address
    return address, int(w3.eth.get_balance(address))


def gas_price(w3, floor=0):
    latest = w3.eth.get_block("latest")
    pending = w3.eth.get_block("pending")
    base = max(int(latest.get("baseFeePerGas") or 0), int(pending.get("baseFeePerGas") or 0))
    return int(Decimal(max(int(w3.eth.gas_price), base, floor)) * Decimal("1.01"))


def prepare_chain(plan, metadata, execute=False):
    Account, Web3 = chain_imports()
    print("LIVE PREFLIGHT", flush=True)
    print("- Resolving and cross-checking selected fleet wallet addresses...", flush=True)
    addresses = {}
    for name in set(plan["sources"]) | set(plan["destinations"]):
        key = metadata[name]["private_key"]
        if not key:
            raise ValueError(f"{name}: PRIVATE_KEY is required to resolve its fleet wallet")
        addresses[name] = Account.from_key(key).address
        planned_address = plan.get("wallet_addresses", {}).get(name)
        if planned_address and planned_address.lower() != addresses[name].lower():
            raise ValueError(f"{name}: wallet address changed since this plan was created")
    if len({address.lower() for address in addresses.values()}) != len(addresses):
        raise ValueError("Selected bots do not have unique wallet addresses")
    contexts = {}
    chain_ids = set()
    for source in plan["sources"]:
        values = metadata[source]["values"]
        rpc = values.get("RPC_URL", "")
        if not rpc:
            raise ValueError(f"{source}: RPC_URL is required")
        print(f"- {source}: connecting to RPC and checking chain identity...", flush=True)
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
        if not w3.is_connected():
            raise ConnectionError(f"{source}: could not connect to RPC_URL")
        expected = int(values.get("CHAIN_ID", str(w3.eth.chain_id)))
        if int(w3.eth.chain_id) != expected:
            raise ValueError(f"{source}: RPC chain {w3.eth.chain_id} != CHAIN_ID={expected}")
        chain_ids.add(expected)
        contexts[source] = {"w3": w3, "account": Account.from_key(values["PRIVATE_KEY"])}
    if len(chain_ids) != 1:
        raise ValueError("All donor bots must use the same chain")
    for route in plan["routes"]:
        source, destination = route["source"], route["destination"]
        w3 = contexts[source]["w3"]
        recipient = Web3.to_checksum_address(addresses[destination])
        print(
            f"- {source} -> {destination}: checking recipient code and estimating transfer gas...",
            flush=True,
        )
        if w3.eth.get_code(recipient):
            raise ValueError(f"{destination}: recipient wallet address has contract code")
        route["source_address"] = addresses[source]
        route["recipient"] = recipient
        route["amount_wei"] = int(plan["amounts_wei"][source]) * route["positions"]
        if route.get("tx_hash"):
            continue
        if route.get("broadcast_tx_hash"):
            broadcast_hash = route["broadcast_tx_hash"]
            try:
                receipt = w3.eth.get_transaction_receipt(broadcast_hash)
            except Exception as exc:
                raise RuntimeError(
                    f"{source} -> {destination}: broadcast {broadcast_hash} is still "
                    "unresolved; refusing to resend. Wait and resume again after checking the hash."
                ) from exc
            if int(receipt["status"]) == 1:
                route["tx_hash"] = broadcast_hash
                route.pop("broadcast_tx_hash", None)
                continue
            route.pop("broadcast_tx_hash", None)
            route.pop("actual_max_fee_wei", None)
        estimate = int(w3.eth.estimate_gas({"from": addresses[source], "to": recipient, "value": route["amount_wei"]}))
        multiplier = Decimal(metadata[source]["values"].get("GAS_LIMIT_MULTIPLIER", "1.05"))
        if not multiplier.is_finite() or multiplier < 1:
            raise ValueError(f"{source}: GAS_LIMIT_MULTIPLIER must be at least 1")
        route["gas"] = max(estimate, int(Decimal(estimate) * multiplier))
        route["gas_price"] = gas_price(w3)
        route["max_fee_wei"] = route["gas"] * route["gas_price"]
        route["gas_cap_wei"] = int(plan["gas_caps_wei"][source])
        if route["max_fee_wei"] > route["gas_cap_wei"]:
            raise ValueError(
                f"{source} -> {destination}: estimated transfer gas "
                f"{Decimal(route['max_fee_wei'])/WEI} ETH exceeds gas cap "
                f"{Decimal(route['gas_cap_wei'])/WEI} ETH"
            )
    plan["feasibility"] = {}
    for source, count in plan["sources"].items():
        routes = [r for r in plan["routes"] if r["source"] == source and not r.get("tx_hash")]
        principal = sum(r["amount_wei"] for r in routes)
        fees = sum(r["max_fee_wei"] for r in routes)
        final_slots = (
            None if source == TREASURY_SOURCE
            else metadata[source]["available"] - count
        )
        balance = int(contexts[source]["w3"].eth.get_balance(addresses[source]))
        confirmed_fees = sum(
            int(r.get("actual_max_fee_wei", 0)) for r in plan["routes"]
            if r["source"] == source and r.get("tx_hash")
        )
        effective_gas_reserve = (
            metadata[source]["gas_reserve_wei"]
            if source == TREASURY_SOURCE
            else max(0, metadata[source]["gas_reserve_wei"] - confirmed_fees)
        )
        required = (
            principal + fees + effective_gas_reserve
            if source == TREASURY_SOURCE
            else principal + max(effective_gas_reserve, fees)
        )
        projected_remaining = balance - principal - fees
        post_fee_gas_reserve = max(0, effective_gas_reserve - fees)
        plan["feasibility"][source] = {
            "balance_wei": balance,
            "principal_wei": principal,
            "maximum_fees_wei": fees,
            "final_available_slots": final_slots,
            "transfer_gas_cap_wei": int(plan["gas_caps_wei"][source]),
            "effective_gas_reserve_wei": effective_gas_reserve,
            "post_fee_gas_reserve_floor_wei": post_fee_gas_reserve,
            "required_wei": required,
            "projected_remaining_wei": projected_remaining,
        }
        print(
            f"- {source}: checking balance against principal and its own gas safety...",
            flush=True,
        )
        if balance < required:
            raise ValueError(
                f"{source}: balance {Decimal(balance)/WEI} ETH cannot cover remaining transfers, "
                f"and gas safety; needs {Decimal(required)/WEI} ETH"
            )
        plan.setdefault("balances_wei", {})[source] = balance
    return contexts


def send_route(context, route, chain_id, on_broadcast=None):
    w3, account = context["w3"], context["account"]
    floor = 0
    for attempt in range(2):
        price = gas_price(w3, floor)
        tx = {
            "from": account.address, "to": route["recipient"], "value": route["amount_wei"],
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": chain_id, "gas": route["gas"], "gasPrice": price,
        }
        maximum_fee = tx["gas"] * tx["gasPrice"]
        if maximum_fee > int(route["gas_cap_wei"]):
            raise ValueError(
                f"transfer gas {Decimal(maximum_fee)/WEI} ETH exceeds gas cap "
                f"{Decimal(route['gas_cap_wei'])/WEI} ETH"
            )
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", getattr(signed, "rawTransaction", None))
        try:
            tx_hash = w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            match = re.findall(r"basefee\s*:\s*(\d+)", str(exc), re.I)
            stale = "base fee" in str(exc).lower() or "fee cap" in str(exc).lower()
            if attempt == 0 and stale:
                floor = ((int(match[-1]) if match else price) * 102 + 99) // 100
                continue
            raise
        tx_hash_hex = tx_hash.hex()
        if on_broadcast:
            on_broadcast(tx_hash_hex, tx)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if int(receipt["status"]) != 1:
            raise RuntimeError(f"transaction {tx_hash_hex} mined with status=0")
        return tx_hash_hex, tx
    raise AssertionError("unreachable")


def print_allocation_preview(plan, metadata):
    print("POSITION PASS ALLOCATION PREVIEW", flush=True)
    print(f"Plan ID: {plan['plan_id']}")
    print(f"Positions: {plan['positions']}")
    if "treasury_priority" in plan:
        funded = plan["treasury_priority"]["positions"]
        print(f"Treasury priority: {funded} funded; {plan['positions'] - funded} from bot donors")
    print("Donors:")
    for name, count in plan["sources"].items():
        data = metadata[name]
        if name == TREASURY_SOURCE:
            print(f"- Treasury: fund {count}; no bot capacity removed; "
                  f"principal={plan['amounts_eth'][name]} ETH/position "
                  f"({Decimal(plan['amounts_eth'][name]) * count} ETH total); "
                  f"gas cap={plan['gas_caps_eth'][name]} ETH/transfer; "
                  f"preserved reserve={data['gas_reserve_eth']} ETH")
            continue
        original = plan["capacity_snapshot"][name]["capacity"]
        available_before = original - data["filled"]
        reserve_floor = plan.get("availability_reserves", {}).get(name, 0)
        print(f"- {name}: give {count}; capacity {original} -> {original-count}; "
              f"filled={data['filled']} availability {available_before} -> {available_before-count}; "
              f"reserve floor={reserve_floor}; "
              f"principal={plan['amounts_eth'][name]} ETH/position "
              f"({Decimal(plan['amounts_eth'][name]) * count} ETH total); "
              f"gas cap={plan['gas_caps_eth'][name]} ETH/transfer")
    print("Recipients:")
    for name, count in plan["destinations"].items():
        original = plan["capacity_snapshot"][name]["capacity"]
        available_before = original - metadata[name]["filled"]
        print(f"- {name}: receive {count}; capacity {original} -> {original+count}; "
              f"availability {available_before} -> {available_before+count}")
    print("Allocation is locally valid. Running live wallet, balance, and gas preflight...", flush=True)


def print_plan(plan, metadata):
    print("POSITION PASS APPROVAL PLAN")
    print(f"Plan ID: {plan['plan_id']}")
    print(f"Positions: {plan['positions']}")
    print(f"Transactions: {len(plan['routes'])}")
    print("Donors and feasibility:")
    for name, count in plan["sources"].items():
        data = metadata[name]
        if name == TREASURY_SOURCE:
            feasibility = plan.get("feasibility", {}).get(name, {})
            print(f"- Treasury ({plan.get('wallet_addresses', {}).get(name, 'address unavailable')}):")
            print(f"    positions: fund {count}; no bot capacity removed")
            print(f"    principal/position: {plan['amounts_eth'][name]} ETH; "
                  f"total principal: {Decimal(plan['amounts_eth'][name]) * count} ETH")
            print(f"    gas cap/transfer: {plan['gas_caps_eth'][name]} ETH")
            if feasibility:
                print(f"    wallet balance: {Decimal(feasibility['balance_wei'])/WEI} ETH")
                print(f"    principal sent: {Decimal(feasibility['principal_wei'])/WEI} ETH")
                print(f"    maximum planned gas: {Decimal(feasibility['maximum_fees_wei'])/WEI} ETH")
                print(f"    preserved treasury reserve: "
                      f"{Decimal(feasibility['effective_gas_reserve_wei'])/WEI} ETH")
                print(f"    minimum required now: {Decimal(feasibility['required_wei'])/WEI} ETH")
                print(f"    projected remaining after maximum gas: "
                      f"{Decimal(feasibility['projected_remaining_wei'])/WEI} ETH")
            continue
        original = plan["capacity_snapshot"][name]["capacity"]
        feasibility = plan.get("feasibility", {}).get(name, {})
        reserve_floor = plan.get("availability_reserves", {}).get(name, 0)
        print(f"- {name} ({plan.get('wallet_addresses', {}).get(name, 'address unavailable')}):")
        print(f"    positions: give {count}; capacity {original} -> {original-count}; "
              f"filled={data['filled']} availability {original-data['filled']} -> "
              f"{original-data['filled']-count}; reserve floor={reserve_floor}")
        print(f"    principal/position: {plan['amounts_eth'][name]} ETH; "
              f"total principal: {Decimal(plan['amounts_eth'][name]) * count} ETH")
        print(f"    gas cap/transfer: {plan['gas_caps_eth'][name]} ETH")
        if feasibility:
            print(f"    wallet balance: {Decimal(feasibility['balance_wei'])/WEI} ETH")
            print(f"    principal sent: {Decimal(feasibility['principal_wei'])/WEI} ETH")
            print(f"    maximum planned gas: {Decimal(feasibility['maximum_fees_wei'])/WEI} ETH")
            print(f"    open slots remaining: {feasibility['final_available_slots']} "
                  "(no additional balance reserve required)")
            print(f"    gas reserve before remaining routes: "
                  f"{Decimal(feasibility['effective_gas_reserve_wei'])/WEI} ETH")
            print(f"    minimum required now: {Decimal(feasibility['required_wei'])/WEI} ETH")
            print(f"    projected remaining after maximum gas: "
                  f"{Decimal(feasibility['projected_remaining_wei'])/WEI} ETH")
            print(f"    projected gas-reserve floor after fees: "
                  f"{Decimal(feasibility['post_fee_gas_reserve_floor_wei'])/WEI} ETH")
    print("Recipients:")
    for name, count in plan["destinations"].items():
        original = plan["capacity_snapshot"][name]["capacity"]
        available_before = original - metadata[name]["filled"]
        print(f"- {name} ({plan.get('wallet_addresses', {}).get(name, 'address unavailable')}): "
              f"receive {count}; capacity {original} -> {original+count}; "
              f"availability {available_before} -> {available_before+count}")
    print("Transfers:")
    total_principal = 0
    total_fees = 0
    for route in plan["routes"]:
        total_principal += route["amount_wei"]
        total_fees += route["max_fee_wei"]
        print(f"- {route['source']} -> {route['destination']}: {route['positions']} position(s), "
              f"{Decimal(route['amount_wei'])/WEI} ETH, max gas {Decimal(route['max_fee_wei'])/WEI} ETH, "
              f"recipient={route.get('recipient', 'unavailable')}")
    print(f"Total principal: {Decimal(total_principal)/WEI} ETH")
    print(f"Total maximum gas: {Decimal(total_fees)/WEI} ETH")
    print("Approval status: FEASIBLE — all current preflight checks passed.")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="sources", action="append")
    parser.add_argument("--from-treasury", "--from-Treasury", action="store_true")
    parser.add_argument("--treasury-env")
    parser.add_argument("--to", dest="destinations", action="append")
    parser.add_argument("--positions")
    parser.add_argument("--reserve", default="0")
    parser.add_argument(
        "--reserve-from", "--reserve-bot", action="append", default=[]
    )
    parser.add_argument("--amount-per-position")
    parser.add_argument("--amount-from", action="append", default=[])
    parser.add_argument(
        "--max-gas", "--max-gas-eth", "--max-gas-per-transfer",
        "--max-fee-transfer-gas", dest="max_gas",
    )
    parser.add_argument(
        "--max-gas-from", "--max-fee-transfer-gas-from",
        dest="max_gas_from", action="append", default=[],
    )
    parser.add_argument("--bot", action="append", default=[])
    parser.add_argument("--journal-dir", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-plan")
    parser.add_argument("--resume")
    parser.add_argument("--list-involved", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--local-preflight", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.positions = parse_positions(args.positions)
    args.reserve = nonnegative_integer(args.reserve, "--reserve")

    bot_dirs = {}
    for item in args.bot:
        name, separator, directory = item.partition("=")
        if not separator or name.lower() in {n.lower() for n in bot_dirs}:
            raise ValueError(f"Invalid or duplicate --bot mapping: {item}")
        bot_dirs[name] = directory
    if not bot_dirs:
        raise ValueError("At least one fleet --bot mapping is required")
    journal_path = Path(args.journal_dir) / f"{args.resume}.json" if args.resume else None
    if args.list_involved:
        if args.resume:
            if not journal_path.is_file():
                raise ValueError(f"Unknown position-pass journal: {args.resume}")
            listed_plan = json.loads(journal_path.read_text())["plan"]
            names = [
                name for name in listed_plan["sources"]
                if name != TREASURY_SOURCE
            ] + list(listed_plan["destinations"])
        else:
            destination_specs = resolve_names(parse_specs(args.destinations, "--to"), bot_dirs, "--to")
            source_specs = resolve_source_specs(args.sources, bot_dirs, destination_specs)
            if not source_specs and not args.from_treasury:
                raise ValueError("At least one --from bot or --from-treasury is required")
            names = [name for name, _count in source_specs + destination_specs]
        if len({name.lower() for name in names}) != len(names):
            raise ValueError("Bots cannot be both donors and recipients")
        print("\n".join(names))
        return 0
    if args.resume:
        if (args.sources or args.destinations or args.positions
                or args.amount_per_position or args.amount_from
                or args.max_gas or args.max_gas_from or args.from_treasury
                or args.treasury_env or args.reserve or args.reserve_from):
            raise ValueError("--resume cannot be combined with new allocation arguments")
        if not journal_path.is_file():
            raise ValueError(f"Unknown position-pass journal: {args.resume}")
        journal = json.loads(journal_path.read_text())
        if (journal.get("plan_id") != args.resume
                or journal.get("plan", {}).get("plan_id") != args.resume):
            raise ValueError(f"Position-pass journal identity does not match --resume {args.resume}")
        if journal["status"] == "complete":
            if not args.local_preflight:
                print(f"Position pass {args.resume} is already complete; nothing changed.")
            return 0
        plan = journal["plan"]
    else:
        destination_specs = resolve_names(parse_specs(args.destinations, "--to"), bot_dirs, "--to")
        source_specs = resolve_source_specs(args.sources, bot_dirs, destination_specs)
        if not source_specs and not args.from_treasury:
            raise ValueError("At least one --from bot or --from-treasury is required")
        overlap = {name.lower() for name, _ in source_specs} & {name.lower() for name, _ in destination_specs}
        if overlap:
            raise ValueError(f"Bots cannot be both donors and recipients: {', '.join(sorted(overlap))}")
        selected_names = {name for name, _ in source_specs} | {name for name, _ in destination_specs}
        metadata = {
            name: bot_metadata(
                name, bot_dirs[name], require_key=False,
                warn_permissions=not args.local_preflight,
            )
            for name in selected_names
        }
        reserve_overrides = parse_reserve_overrides(args.reserve_from, bot_dirs)
        selected_source_names = {name for name, _count in source_specs}
        unknown_reserve_overrides = set(reserve_overrides) - selected_source_names
        if unknown_reserve_overrides:
            raise ValueError(
                "Reserve override supplied for non-source: "
                f"{', '.join(sorted(unknown_reserve_overrides))}"
            )
        reserve_floors = {
            name: reserve_overrides.get(name, args.reserve)
            for name, _count in source_specs
        }
        maximum_requested = args.positions == "available"
        total = None if maximum_requested else infer_total(
            args.positions, source_specs, destination_specs
        )
        global_amount = decimal_eth(args.amount_per_position, "amount per position")[0] if args.amount_per_position else None
        per_source = parse_amount_overrides(
            args.amount_from, bot_dirs, allow_treasury=args.from_treasury,
        )
        global_gas_cap = (
            decimal_eth(args.max_gas, "maximum gas per transfer")[0]
            if args.max_gas else None
        )
        per_source_gas_cap = parse_amount_overrides(
            args.max_gas_from, bot_dirs,
            option="--max-gas-from", label="maximum gas per transfer",
            allow_treasury=args.from_treasury,
        )

        treasury_data = None
        treasury_count = 0
        fixed_bot_count = sum(count or 0 for _name, count in source_specs)
        if total is not None and fixed_bot_count > total:
            raise ValueError(
                f"Manual source counts total {fixed_bot_count}, exceeding {total}"
            )
        if args.from_treasury:
            treasury_path = args.treasury_env or os.environ.get(
                "FLEET_TREASURY_ENV", str(Path.home() / "bot-farm" / "treasury.env")
            )
            treasury_data = treasury_metadata(
                treasury_path, warn_permissions=not args.local_preflight,
            )
            metadata[TREASURY_SOURCE] = treasury_data
            treasury_amount = per_source.get(
                TREASURY_SOURCE,
                global_amount if global_amount is not None
                else (Decimal(treasury_data["reserve_eth"])
                      if treasury_data["reserve_eth"] is not None else None),
            )
            if treasury_amount is None:
                raise ValueError(
                    "Treasury needs --amount-per-position, --amount-from Treasury=ETH, "
                    "or TREASURY_POSITION_RESERVE_ETH in its env"
                )
            treasury_gas_cap = per_source_gas_cap.get(
                TREASURY_SOURCE,
                global_gas_cap if global_gas_cap is not None
                else Decimal(treasury_data["transfer_gas_cap_eth"]),
            )
            _treasury_address, treasury_balance = treasury_live_balance(treasury_data)

        bot_capacities = {
            name: max(0, metadata[name]["available"] - reserve_floors[name])
            for name, _count in source_specs
        }
        for name, count in source_specs:
            if count is not None and count > bot_capacities[name]:
                raise ValueError(
                    f"{name} can give at most {bot_capacities[name]} position(s) "
                    f"while reserving {reserve_floors[name]} open slot(s), not {count}"
                )
        if maximum_requested:
            bot_total = maximum_source_total(source_specs, bot_capacities)
            if treasury_data:
                spendable = max(0, treasury_balance - treasury_data["gas_reserve_wei"])
                upper = spendable // int(treasury_amount * WEI)
                allocation_error = None
                funding_shortfall = False
                exact_destination_total = (
                    sum(count for _name, count in destination_specs)
                    if all(count is not None for _name, count in destination_specs)
                    else None
                )
                if exact_destination_total is not None:
                    exact_treasury_count = exact_destination_total - bot_total
                    if exact_treasury_count < 0:
                        raise ValueError(
                            "Exact destination counts are smaller than the maximum "
                            "available bot-source total"
                        )
                    candidates = (exact_treasury_count,)
                else:
                    candidates = range(upper, -1, -1)
                for candidate in candidates:
                    candidate_total = bot_total + candidate
                    if candidate_total < 1:
                        continue
                    try:
                        candidate_destinations = allocate_destinations(
                            destination_specs, candidate_total, metadata
                        )
                    except ValueError as exc:
                        allocation_error = exc
                        continue
                    route_count = len(build_routes(
                        {TREASURY_SOURCE: candidate}, candidate_destinations
                    )) if candidate else 0
                    required = (
                        candidate * int(treasury_amount * WEI)
                        + route_count * int(treasury_gas_cap * WEI)
                        + treasury_data["gas_reserve_wei"]
                    )
                    if candidate == 0 or required <= treasury_balance:
                        treasury_count = candidate
                        total = candidate_total
                        destination_counts = candidate_destinations
                        break
                    funding_shortfall = True
                else:
                    if funding_shortfall:
                        detail = (
                            "the exact destination total"
                            if exact_destination_total is not None else "any position"
                        )
                        raise ValueError(
                            f"Treasury cannot fund {detail} while preserving its "
                            "reserve and maximum route gas"
                        )
                    if allocation_error:
                        raise allocation_error
                    raise ValueError("No positions are available from the selected sources")
            else:
                total = bot_total
                if total < 1:
                    raise ValueError("No positions are available from the selected sources")
                destination_counts = allocate_destinations(
                    destination_specs, total, metadata
                )
        else:
            destination_counts = allocate_destinations(
                destination_specs, total, metadata
            )
            if treasury_data:
                treasury_count = treasury_affordable_positions(
                    total - fixed_bot_count,
                    int(treasury_amount * WEI),
                    treasury_balance,
                    treasury_data["gas_reserve_wei"],
                    int(treasury_gas_cap * WEI),
                    destination_counts,
                )

        if not maximum_requested:
            bot_total = total - treasury_count
        if bot_total and not source_specs:
            raise ValueError(
                f"Treasury can safely fund {treasury_count} of {total} position(s); "
                "add --from bots for the remainder"
            )
        bot_source_counts = fair_allocate(
            source_specs, bot_total,
            capacities=bot_capacities,
            label="source",
            source_availability={
                name: metadata[name]["available"] for name, _count in source_specs
            },
        ) if source_specs else {}
        source_counts = {}
        if treasury_count:
            source_counts[TREASURY_SOURCE] = treasury_count
        source_counts.update({
            name: count for name, count in bot_source_counts.items() if count
        })
        # Bots assigned no slots are not participants in the transfer plan.
        # In particular, a full donor may be listed but must not face wallet,
        # gas, or capacity mutations when it has nothing available to give.
        selected_names = set(source_counts) | set(destination_counts)
        unknown_overrides = set(per_source) - set(source_counts)
        if unknown_overrides:
            raise ValueError(f"Amount override supplied for non-donor: {', '.join(sorted(unknown_overrides))}")
        amounts = {}
        for name in source_counts:
            if name == TREASURY_SOURCE:
                amount = treasury_amount
            else:
                amount = per_source.get(
                    name,
                    global_amount if global_amount is not None
                    else Decimal(metadata[name]["reserve_eth"]),
                )
            if amount <= 0:
                raise ValueError(f"{name}: default TREASURY_POSITION_RESERVE_ETH is zero; pass --amount-per-position")
            amounts[name] = amount
        unknown_gas_overrides = set(per_source_gas_cap) - set(source_counts)
        if unknown_gas_overrides:
            raise ValueError(
                "Gas override supplied for non-donor: "
                f"{', '.join(sorted(unknown_gas_overrides))}"
            )
        gas_caps = {
            name: per_source_gas_cap.get(
                name,
                global_gas_cap if global_gas_cap is not None
                else Decimal(metadata[name]["transfer_gas_cap_eth"]),
            )
            for name in source_counts
        }
        routes = build_routes(source_counts, destination_counts)
        for name in selected_names:
            if name != TREASURY_SOURCE:
                metadata[name] = bot_metadata(
                    name, bot_dirs[name], require_key=True,
                    warn_permissions=not args.local_preflight,
                )
        Account, _Web3 = chain_imports()
        wallet_addresses = {
            name: Account.from_key(metadata[name]["private_key"]).address
            for name in selected_names
        }
        if len({address.lower() for address in wallet_addresses.values()}) != len(wallet_addresses):
            raise ValueError("Selected bots do not have unique wallet addresses")
        static = {
            "positions": total, "sources": source_counts, "destinations": destination_counts,
            "amounts_eth": {n: str(v) for n, v in amounts.items()},
            "amounts_wei": {n: int(v * WEI) for n, v in amounts.items()},
            "gas_caps_eth": {n: str(v) for n, v in gas_caps.items()},
            "gas_caps_wei": {n: int(v * WEI) for n, v in gas_caps.items()},
            "routes": routes,
            "capacity_snapshot": {n: {"capacity": metadata[n]["capacity"], "filled": metadata[n]["filled"]}
                                  for n in (set(source_counts) | set(destination_counts))
                                  if n != TREASURY_SOURCE},
            "wallet_addresses": wallet_addresses,
            "availability_reserves": {
                name: reserve_floors[name]
                for name in source_counts if name != TREASURY_SOURCE
            },
            "donor_safety": {n: {
                "position_reserve_wei": metadata[n]["reserve_wei"],
                "gas_reserve_wei": metadata[n]["gas_reserve_wei"],
                "configured_transfer_gas_cap_wei": metadata[n]["transfer_gas_cap_wei"],
            } for n in source_counts},
        }
        if treasury_data:
            static["treasury_env"] = treasury_data["env"]
            static["treasury_priority"] = {"positions": treasury_count}
        static["plan_id"] = canonical_id(static)
        plan = static
    selected_names = set(plan["sources"]) | set(plan["destinations"])
    metadata = {
        name: bot_metadata(
            name, bot_dirs[name], require_key=True,
            warn_permissions=not args.local_preflight,
        )
        for name in selected_names if name in bot_dirs
    }
    if TREASURY_SOURCE in selected_names:
        treasury_env = plan.get("treasury_env")
        if not treasury_env:
            raise ValueError("Treasury-backed plan is missing its treasury env path")
        metadata[TREASURY_SOURCE] = treasury_metadata(
            treasury_env, warn_permissions=not args.local_preflight,
        )
    # Journals made by the original implementation predate explicit transfer
    # gas caps. Derive those caps from the current donor environment so an
    # interrupted, possibly partially paid plan remains safely resumable.
    if "gas_caps_wei" not in plan or "gas_caps_eth" not in plan:
        plan["gas_caps_eth"] = {
            name: metadata[name]["transfer_gas_cap_eth"] for name in plan["sources"]
        }
        plan["gas_caps_wei"] = {
            name: metadata[name]["transfer_gas_cap_wei"] for name in plan["sources"]
        }
    commit_resume = bool(
        args.resume and args.execute
        and journal["status"] in ("transfers_confirmed", "committing")
    )
    for name, snapshot in plan["capacity_snapshot"].items():
        allowed_capacities = {snapshot["capacity"]}
        if commit_resume and name in journal["capacity_changes"]:
            allowed_capacities.add(journal["capacity_changes"][name]["after"])
        if (name not in metadata or metadata[name]["capacity"] not in allowed_capacities
                or metadata[name]["filled"] != snapshot["filled"]):
            raise ValueError(f"{name}: capacity or filled positions changed since this plan was created")
    for name, safety in plan.get("donor_safety", {}).items():
        if (metadata[name]["reserve_wei"] != safety["position_reserve_wei"]
                or metadata[name]["gas_reserve_wei"] != safety["gas_reserve_wei"]
                or ("configured_transfer_gas_cap_wei" in safety
                    and metadata[name]["transfer_gas_cap_wei"]
                    != safety["configured_transfer_gas_cap_wei"])):
            raise ValueError(f"{name}: position or gas reserve changed since this plan was created")
    if not commit_resume:
        validate_local_plan(plan, metadata)
    if args.execute and not args.resume:
        pending_journal = Path(args.journal_dir) / f"{plan['plan_id']}.json"
        if pending_journal.exists():
            raise ValueError(f"Journal already exists; resume with --resume {plan['plan_id']}")
    if args.execute and args.confirm_plan != plan["plan_id"]:
        raise ValueError(f"Execution requires --confirm-plan {plan['plan_id']}")
    if args.local_preflight:
        # The managed shell wrapper calls this before touching tmux or durable
        # desired-state markers. Treasury-backed allocations may make one
        # read-only balance query here so plan confirmation is still checked
        # before any bot lifecycle state changes.
        involved = [
            name for name in plan["sources"] if name != TREASURY_SOURCE
        ] + list(plan["destinations"])
        print("\n".join(involved))
        return 0
    if commit_resume:
        print_plan(plan, metadata)
        apply_capacities(journal, journal_path)
        print(f"POSITION PASS COMPLETE: {plan['plan_id']}")
        return 0
    print_allocation_preview(plan, metadata)
    contexts = prepare_chain(plan, metadata, execute=args.execute)
    print_plan(plan, metadata)
    if not args.execute:
        print("DRY RUN COMPLETE: no funds sent and no files changed.")
        print(
            f"APPROVE: repeat the same allocation with --execute --manage-bots "
            f"--confirm-plan {plan['plan_id']}"
        )
        print("Alternative: use --confirm-fleet-stopped instead of --manage-bots for a stopped whole fleet.")
        return 0
    if not args.resume:
        changes = {}
        for name, count in plan["sources"].items():
            if name == TREASURY_SOURCE:
                continue
            changes[name] = {"env": metadata[name]["env"], "before": metadata[name]["capacity"], "after": metadata[name]["capacity"] - count}
        for name, count in plan["destinations"].items():
            changes[name] = {"env": metadata[name]["env"], "before": metadata[name]["capacity"], "after": metadata[name]["capacity"] + count}
        journal_path = Path(args.journal_dir) / f"{plan['plan_id']}.json"
        if journal_path.exists():
            raise ValueError(f"Journal already exists; resume with --resume {plan['plan_id']}")
        journal = {"plan_id": plan["plan_id"], "created_at": datetime.now(timezone.utc).isoformat(),
                   "status": "transferring", "plan": plan, "capacity_changes": changes}
        atomic_json(journal_path, journal)
    first_source = next(iter(plan["sources"]))
    chain_text = metadata[first_source]["values"].get("CHAIN_ID")
    chain_id = int(chain_text) if chain_text else int(contexts[first_source]["w3"].eth.chain_id)
    for index, route in enumerate(plan["routes"]):
        if route.get("tx_hash"):
            continue
        def record_broadcast(tx_hash, tx):
            route["broadcast_tx_hash"] = tx_hash
            route["actual_max_fee_wei"] = tx["gas"] * tx["gasPrice"]
            atomic_json(journal_path, journal)
        try:
            tx_hash, tx = send_route(
                contexts[route["source"]], route, chain_id,
                on_broadcast=record_broadcast,
            )
        except Exception as exc:
            journal["status"] = "interrupted"
            journal["error"] = str(exc)
            atomic_json(journal_path, journal)
            raise RuntimeError(f"Transfers paused at route {index + 1}; resume safely with --resume {plan['plan_id']}") from exc
        route["tx_hash"] = tx_hash
        route.pop("broadcast_tx_hash", None)
        route["actual_max_fee_wei"] = tx["gas"] * tx["gasPrice"]
        journal["status"] = "transferring"; journal.pop("error", None)
        atomic_json(journal_path, journal)
        print(f"CONFIRMED {route['source']} -> {route['destination']}: {tx_hash}")
    journal["status"] = "transfers_confirmed"
    atomic_json(journal_path, journal)
    apply_capacities(journal, journal_path)
    print(f"POSITION PASS COMPLETE: {plan['plan_id']} ({plan['positions']} positions)")
    print("Capacity files changed; restart the affected bots when ready.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"POSITION PASS REFUSED: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
