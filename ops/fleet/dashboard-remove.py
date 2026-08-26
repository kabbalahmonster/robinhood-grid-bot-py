#!/usr/bin/env python3
"""Safely preview or remove retired bot states from DoomDash."""

import argparse
import json
import sys
from urllib import error, parse, request

from dotenv import dotenv_values


def removal_url(dashboard_url: str, bot_id: str) -> str:
    parsed = parse.urlsplit(dashboard_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("DASHBOARD_URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("DASHBOARD_URL must use HTTPS except for localhost")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/status"):
        raise ValueError("DASHBOARD_URL must end in /api/status")
    api_root = path[: -len("/status")]
    encoded_id = parse.quote(bot_id, safe="")
    return parse.urlunsplit((parsed.scheme, parsed.netloc, f"{api_root}/bots/{encoded_id}", "", ""))


def remove_bot(url: str, api_key: str, timeout: float = 10.0) -> tuple[bool, str]:
    req = request.Request(url, method="DELETE", headers={"X-API-Key": api_key, "Accept": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok")), str(payload.get("bot_id") or "")
    except error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("error") or f"HTTP {exc.code}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = f"HTTP {exc.code}"
        return False, str(detail)
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--bot-id", action="append", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    values = dotenv_values(args.env_file)
    dashboard_url = str(values.get("DASHBOARD_URL") or "").strip()
    api_key = str(values.get("DASHBOARD_API_KEY") or "").strip()
    if not dashboard_url:
        raise ValueError(f"{args.env_file}: DASHBOARD_URL is missing")
    if not api_key:
        raise ValueError(f"{args.env_file}: DASHBOARD_API_KEY is missing")

    bot_ids = []
    seen = set()
    for raw in args.bot_id:
        bot_id = raw.strip()
        if not bot_id or len(bot_id) > 128 or any(ord(char) < 32 for char in bot_id):
            raise ValueError(f"Invalid BOT_ID: {raw!r}")
        if bot_id not in seen:
            seen.add(bot_id)
            bot_ids.append(bot_id)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"DoomDash retired-bot removal: {mode}")
    print(f"Dashboard: {parse.urlsplit(dashboard_url).scheme}://{parse.urlsplit(dashboard_url).netloc}")
    print(f"Bot IDs: {' '.join(bot_ids)}")
    if not args.execute:
        print("PREVIEW ONLY: no requests sent. Repeat with --execute --confirm-retired.")
        return 0

    failures = 0
    for bot_id in bot_ids:
        ok, detail = remove_bot(removal_url(dashboard_url, bot_id), api_key)
        if ok:
            print(f"REMOVED: {bot_id}")
        else:
            failures += 1
            print(f"FAILED: {bot_id}: {detail}", file=sys.stderr)
    print(f"Removal complete: {len(bot_ids) - failures} removed, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
