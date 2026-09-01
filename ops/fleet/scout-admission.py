#!/usr/bin/env python3
"""Fail closed unless DoomScout has a fresh PASS for a token."""

import argparse
import json
from datetime import datetime, timezone
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    args = parser.parse_args()
    endpoint = args.url.rstrip("/")
    if not endpoint.endswith("/api/scout"):
        endpoint += "/api/scout"
    with urlopen(Request(endpoint, headers={"Accept": "application/json"}), timeout=15) as response:
        payload = json.load(response)
    report = next((item for item in payload.get("reports", [])
                   if str(item.get("address", "")).lower() == args.address.lower()), None)
    if not report:
        raise SystemExit("DoomScout has no retained assessment for this token")
    try:
        assessed = datetime.fromisoformat(str(report["assessed_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit("DoomScout assessment has no valid timestamp") from exc
    age = (datetime.now(timezone.utc) - assessed).total_seconds()
    if age > max(60, args.max_age_seconds):
        raise SystemExit(f"DoomScout assessment is stale ({int(age)} seconds old)")
    if report.get("verdict") != "pass":
        reasons = ", ".join(report.get("reasons") or []) or "score below pass threshold"
        raise SystemExit(f"DoomScout verdict is {report.get('verdict', 'unknown').upper()}: {reasons}")
    print(f"DoomScout PASS: {report.get('score', 0)}/100, assessed {int(age)} seconds ago")


if __name__ == "__main__":
    main()
