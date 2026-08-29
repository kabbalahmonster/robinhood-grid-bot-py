#!/usr/bin/env python3
"""Render the whole fleet as a phone-friendly, one-row-per-bot terminal view."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def load_status(bot_dir: Path) -> dict:
    path = bot_dir / "data" / "fleet_status.json"
    try:
        with path.open(encoding="utf-8") as handle:
            status = json.load(handle)
        status["_snapshot_mtime"] = path.stat().st_mtime
        return status
    except (OSError, ValueError, TypeError):
        return {}


def best_pnl(status: dict) -> float | None:
    values = []
    for position in status.get("positions") or []:
        for key in ("pnl_percent", "profit_percent", "pnl"):
            try:
                if position.get(key) is not None:
                    values.append(float(position[key]))
                    break
            except (TypeError, ValueError):
                pass
    return max(values) if values else None


def state_for(status: dict, now: float) -> tuple[str, int]:
    if not status:
        return "NO DATA", 3
    age = max(0, int(now - float(status.get("_snapshot_mtime", 0))))
    poll = max(1, int(status.get("poll_interval_seconds") or 8))
    if age > max(45, poll * 4):
        return f"STALE {age}s", 3
    attempt = status.get("sell_attempt") or {}
    if attempt.get("status") == "position_balance_mismatch":
        return "BAL MISMATCH", 3
    if attempt.get("status") == "quote_below_minimum":
        return "SELL WAIT", 2
    if status.get("capacity_warning"):
        return "FULL", 2
    events = status.get("events") or []
    if events:
        event = events[-1]
        try:
            stamp = datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00"))
            recent = (datetime.now(timezone.utc) - stamp).total_seconds() < 600
        except (TypeError, ValueError):
            recent = False
        if recent and event.get("level") == "error":
            return "ERROR", 3
        if recent and event.get("level") == "warning":
            return "WARN", 2
    return "OK", 1


def color(text: str, level: int, enabled: bool) -> str:
    if not enabled:
        return text
    code = {1: "32", 2: "33", 3: "31"}.get(level, "0")
    return f"\033[{code}m{text}\033[0m"


def render(bot_dirs: list[Path], color_enabled: bool) -> str:
    width = max(32, shutil.get_terminal_size((48, 30)).columns)
    now = time.time()
    rows = []
    counts = {1: 0, 2: 0, 3: 0}
    for bot_dir in bot_dirs:
        status = load_status(bot_dir)
        name = str(status.get("token_symbol") or status.get("bot_id") or bot_dir.parent.name).upper()
        state, level = state_for(status, now)
        counts[level] += 1
        filled = int(status.get("filled_positions") or 0)
        maximum = int(status.get("max_positions") or 0)
        pnl = best_pnl(status)
        pnl_text = "   — " if pnl is None else f"{pnl:+5.1f}%"
        profit = float(status.get("session_profit_eth") or 0)
        balance = float(status.get("eth_balance") or 0)
        if width < 54:
            raw = f"{name[:8]:<8} {filled:>2}/{maximum:<2} {pnl_text:>6} {balance:>.3f} {state}"
        else:
            raw = f"{name[:12]:<12} {filled:>2}/{maximum:<2} {pnl_text:>6} E {balance:>.3f} P {profit:+.5f} {state}"
        rows.append((level, name, color(raw[:width], level, color_enabled)))

    # Problems first, then names, so the useful rows stay at the top on a phone.
    rows.sort(key=lambda item: (-item[0], item[1]))
    header = f"FLEET {len(rows)}  OK {counts[1]}  WARN {counts[2]}  BAD {counts[3]}  {time.strftime('%H:%M:%S')}"
    columns = "BOT      POS  BEST    ETH  STATE" if width < 54 else "BOT          POS   BEST     ETH        PROFIT  STATE"
    return "\n".join([header[:width], columns[:width], "-" * min(width, len(columns))] + [row[2] for row in rows])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("bot_dirs", nargs="+", type=Path)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    interactive = sys.stdout.isatty() and not args.once
    try:
        while True:
            if interactive:
                sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(render(args.bot_dirs, not args.no_color and sys.stdout.isatty()) + "\n")
            sys.stdout.flush()
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
