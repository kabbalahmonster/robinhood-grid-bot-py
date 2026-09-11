#!/usr/bin/env python3
"""Model the deterministic geometry of gridless buy/sell trigger combinations."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
from pathlib import Path


def positive_list(value: str, label: str) -> list[float]:
    try:
        values = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be comma-separated numbers") from exc
    if not values or any(not math.isfinite(item) or item <= 0 or item >= 100 for item in values):
        raise argparse.ArgumentTypeError(f"{label} values must be greater than 0 and less than 100")
    return sorted(set(values))


def env_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def metrics(buy: float, sell: float, positions: int, min_profit: float) -> dict[str, float | int]:
    step = 1.0 - buy / 100.0
    effective_sell = max(sell, min_profit)
    last_entry = step ** (positions - 1)
    capacity_boundary = step ** positions
    first_exit = last_entry * (1.0 + effective_sell / 100.0)
    average_entry = positions / sum(1.0 / (step ** index) for index in range(positions))
    return {
        "buy_trigger_percent": buy,
        "sell_trigger_percent": sell,
        "effective_sell_percent": effective_sell,
        "positions": positions,
        "last_funded_entry_drawdown_percent": (1.0 - last_entry) * 100.0,
        "capacity_boundary_drawdown_percent": (1.0 - capacity_boundary) * 100.0,
        "rebound_to_newest_exit_percent": (first_exit / capacity_boundary - 1.0) * 100.0,
        "newest_exit_vs_initial_percent": (first_exit - 1.0) * 100.0,
        "equal_eth_average_entry_price": average_entry,
        "portfolio_break_even_rebound_percent": (average_entry / capacity_boundary - 1.0) * 100.0,
    }


def fmt(value: float) -> str:
    return f"{value:.2f}"


def line_chart(buys: list[float], positions: int) -> str:
    width, height, left, top, right, bottom = 760, 330, 58, 24, 20, 48
    plot_w, plot_h = width - left - right, height - top - bottom
    maximum = max((1 - (1 - buy / 100) ** positions) * 100 for buy in buys)
    maximum = max(10.0, math.ceil(maximum / 10) * 10)
    colors = ["#67e8f9", "#a78bfa", "#f472b6", "#fbbf24", "#4ade80", "#fb7185"]
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Drawdown coverage by position slots">']
    for tick in range(0, 6):
        value = maximum * tick / 5
        y = top + plot_h * (1 - tick / 5)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{value:.0f}%</text>')
    for slot in range(1, positions + 1):
        x = left + plot_w * (slot - 1) / max(1, positions - 1)
        parts.append(f'<text x="{x:.1f}" y="{height - 18}" text-anchor="middle">{slot}</text>')
    for index, buy in enumerate(buys):
        points = []
        for slot in range(1, positions + 1):
            drawdown = (1 - (1 - buy / 100) ** slot) * 100
            x = left + plot_w * (slot - 1) / max(1, positions - 1)
            y = top + plot_h * (1 - drawdown / maximum)
            points.append(f"{x:.1f},{y:.1f}")
        color = colors[index % len(colors)]
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>')
        parts.append(f'<text x="{left + 12 + (index % 3) * 135}" y="{top + 16 + (index // 3) * 20}" fill="{color}">buy {buy:g}%</text>')
    parts.append(f'<text x="{width / 2}" y="{height - 2}" text-anchor="middle">filled position slots</text></svg>')
    return "".join(parts)


def render_report(rows: list[dict], buys: list[float], sells: list[float], positions: int,
                  min_profit: float, fleet_rows: list[dict]) -> str:
    max_bounce = max(float(row["rebound_to_newest_exit_percent"]) for row in rows)
    cells = []
    for sell in sells:
        cells.append(f"<tr><th>{sell:g}%</th>")
        for buy in buys:
            row = next(item for item in rows if item["buy_trigger_percent"] == buy and item["sell_trigger_percent"] == sell)
            bounce = float(row["rebound_to_newest_exit_percent"])
            intensity = min(1.0, bounce / max(max_bounce, 0.01))
            hue = 145 - intensity * 115
            cells.append(
                f'<td style="background:hsl({hue:.0f} 48% 22%)"><strong>cover {fmt(float(row["capacity_boundary_drawdown_percent"]))}%</strong>'
                f'<span>bounce {fmt(bounce)}%</span><span>exit vs start {float(row["newest_exit_vs_initial_percent"]):+.2f}%</span></td>'
            )
        cells.append("</tr>")
    fleet_html = ""
    if fleet_rows:
        body = "".join(
            f"<tr><td>{html.escape(str(row['name']))}</td><td>{row['buy']:g}%</td><td>{row['sell']:g}%</td>"
            f"<td>{row['positions']}</td><td>{fmt(row['coverage'])}%</td><td>{fmt(row['bounce'])}%</td></tr>"
            for row in fleet_rows
        )
        fleet_html = f"""
        <section><h2>Selected fleet configuration</h2><table><thead><tr><th>Bot</th><th>Buy</th><th>Sell</th><th>Slots</th><th>Coverage</th><th>Bounce</th></tr></thead><tbody>{body}</tbody></table></section>
        """
    headers = "".join(f"<th>buy {buy:g}%</th>" for buy in buys)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Gridless strategy model</title>
<style>
:root{{color-scheme:dark;font-family:Inter,system-ui,sans-serif;background:#090b13;color:#e8edf7}}body{{max-width:1180px;margin:auto;padding:32px}}h1{{margin-bottom:6px}}.lede{{color:#aeb8ca;max-width:850px}}section{{background:#111522;border:1px solid #273048;border-radius:14px;padding:20px;margin:22px 0;overflow:auto}}table{{border-collapse:separate;border-spacing:5px;width:100%}}th,td{{padding:10px;text-align:left}}td{{border-radius:8px}}td span{{display:block;color:#cad3e3;font-size:.86rem;margin-top:4px}}svg{{width:100%;min-width:650px;background:#0b0f19;border-radius:10px}}svg text{{fill:#aeb8ca;font-size:12px}}.grid{{stroke:#273048;stroke-width:1}}code{{background:#20283a;padding:2px 5px;border-radius:4px}}.note{{color:#fbbf24}}
</style></head><body><h1>Gridless strategy model</h1>
<p class="lede">Normalized starting price = 1.00, {positions} equal-ETH position slots. Buy triggers compound from the bot's current lowest-cost position. Effective sell target is max(configured sell, minimum profit) = per-row value with a {min_profit:g}% floor.</p>
<section><h2>Coverage curve</h2><p>Each point is the drawdown where the next buy would trigger after that many slots have filled.</p>{line_chart(buys, positions)}</section>
<section><h2>Buy / sell comparison</h2><p>Coverage is the drawdown at which another buy is wanted but all {positions} slots are full. Bounce is the recovery from that boundary needed for the newest funded position to reach its effective sell target. Greener cells need less recovery.</p>
<table><thead><tr><th>sell ↓ / buy →</th>{headers}</tr></thead><tbody>{''.join(cells)}</tbody></table></section>
{fleet_html}
<section><h2>Interpretation and limits</h2><p><strong>Last funded entry</strong> occurs one buy interval before the capacity boundary. <strong>Portfolio break-even</strong> uses equal ETH per slot and excludes fees. Real fills vary because the bot divides available capital by remaining slots, routes move, token taxes/slippage/gas exist, and price paths can gap. This is deterministic trigger geometry—not a backtest or profit forecast.</p><p class="note">A wider buy trigger increases drawdown reach but also demands a larger rebound before the newest position exits. A higher sell trigger compounds that recovery burden.</p></section>
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buy-triggers", default="5,10,15,20")
    parser.add_argument("--sell-triggers", default="3,5,10,15")
    parser.add_argument("--positions", type=int, default=8)
    parser.add_argument("--min-profit", type=float, default=0.0)
    parser.add_argument("--output", default="strategy-model.html")
    parser.add_argument("--bot", action="append", default=[], metavar="NAME=DIR", help=argparse.SUPPRESS)
    args = parser.parse_args()
    buys = positive_list(args.buy_triggers, "buy triggers")
    sells = positive_list(args.sell_triggers, "sell triggers")
    if args.positions < 1 or args.positions > 100:
        parser.error("--positions must be between 1 and 100")
    if not math.isfinite(args.min_profit) or args.min_profit < 0 or args.min_profit >= 100:
        parser.error("--min-profit must be at least 0 and less than 100")
    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() != ".html":
        parser.error("--output must end in .html")
    output.parent.mkdir(parents=True, exist_ok=True)

    rows = [metrics(buy, sell, args.positions, args.min_profit) for sell in sells for buy in buys]
    fleet_rows = []
    for assignment in args.bot:
        if "=" not in assignment:
            parser.error("invalid internal bot assignment")
        name, directory = assignment.split("=", 1)
        env = env_values(Path(directory) / ".env")
        try:
            buy = abs(float(env.get("GRIDLESS_BUY_THRESHOLD", "-10")))
            sell = float(env.get("GRIDLESS_SELL_THRESHOLD", "5"))
            floor = float(env.get("MIN_PROFIT_PERCENT", "5"))
            count = int(env.get("MAX_ACTIVE_POSITIONS", env.get("MAX_POSITIONS", "10")))
            row = metrics(buy, sell, count, floor)
        except (ValueError, ZeroDivisionError):
            continue
        fleet_rows.append({"name": name, "buy": buy, "sell": sell, "positions": count,
                           "coverage": row["capacity_boundary_drawdown_percent"],
                           "bounce": row["rebound_to_newest_exit_percent"]})

    output.write_text(render_report(rows, buys, sells, args.positions, args.min_profit, fleet_rows), encoding="utf-8")
    csv_path, json_path = output.with_suffix(".csv"), output.with_suffix(".json")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    json_path.write_text(json.dumps({"assumptions": {"positions": args.positions, "min_profit_percent": args.min_profit,
                                                      "equal_eth_slots": True, "starting_price": 1.0},
                                          "strategies": rows, "fleet": fleet_rows}, indent=2) + "\n", encoding="utf-8")

    print(f"Modeled {len(rows)} buy/sell combinations across {args.positions} slots.")
    for buy in buys:
        representative = metrics(buy, sells[0], args.positions, args.min_profit)
        print(f"  buy {buy:g}%: last entry {representative['last_funded_entry_drawdown_percent']:.2f}% down; "
              f"capacity boundary {representative['capacity_boundary_drawdown_percent']:.2f}% down")
    print(f"HTML: {output}\nCSV:  {csv_path}\nJSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
