#!/usr/bin/env python3
"""Validate a position-capacity change for one bot checkout."""

import argparse
import json
import re
from pathlib import Path


def dotenv_integer(path, name):
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*([^#\s]+)")
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            values.append(match.group(1).strip("'\""))
    if len(values) > 1:
        raise ValueError(f"{path}: {name} is defined more than once")
    if not values:
        return None
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{path}: {name} must be an integer") from exc
    return value


def filled_positions(bot_dir):
    counts = []
    status_path = bot_dir / "data" / "fleet_status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        counts.append(int(status.get("filled_positions") or 0))
    for filename in ("positions.json", "gridless_positions.json"):
        path = bot_dir / "data" / filename
        if not path.exists():
            continue
        positions = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(positions, dict):
            raise ValueError(f"{path}: expected a JSON object")
        counts.append(sum(
            1 for position in positions.values()
            if isinstance(position, dict) and int(position.get("balance") or 0) > 0
        ))
    return max(counts, default=0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-dir", required=True, type=Path)
    change = parser.add_mutually_exclusive_group(required=True)
    change.add_argument("--delta", type=int)
    change.add_argument("--set-to-filled", action="store_true")
    args = parser.parse_args()
    env_path = args.bot_dir / ".env"
    if not env_path.is_file():
        raise ValueError(f"Missing .env: {env_path}")
    if args.delta == 0:
        raise ValueError("Position delta must not be zero")

    variable = "MAX_ACTIVE_POSITIONS"
    current = dotenv_integer(env_path, variable)
    if current is None:
        variable = "MAX_POSITIONS"
        current = dotenv_integer(env_path, variable)
    if current is None:
        raise ValueError(f"{env_path}: MAX_ACTIVE_POSITIONS or MAX_POSITIONS is required")
    if current < 1:
        raise ValueError(f"{env_path}: current position capacity must be at least 1")

    filled = filled_positions(args.bot_dir)
    updated = filled if args.set_to_filled else current + args.delta
    if updated < 0 or (updated < 1 and not args.set_to_filled):
        raise ValueError(f"refusing capacity {updated}; at least 1 position is required")
    if updated < filled:
        raise ValueError(
            f"refusing capacity {updated}; bot has {filled} filled positions"
        )
    print(f"{variable}\t{current}\t{filled}\t{updated}")


if __name__ == "__main__":
    main()
