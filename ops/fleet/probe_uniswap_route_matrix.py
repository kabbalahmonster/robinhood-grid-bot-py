#!/usr/bin/env python3
"""Read-only, single-variable Uniswap /quote diagnostic matrix.

This tool never creates approvals, requests swap calldata, signs, or broadcasts.
It calls only Uniswap's /v1/quote endpoint, serially and without retries.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from copy import deepcopy

import requests
from dotenv import dotenv_values

URL = "https://trade-api.gateway.uniswap.org/v1/quote"
DEFAULT_DELAY_SECONDS = 2.0
MAX_ROUNDS = 3
MAX_REQUESTS = 27


def _fingerprint(body):
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()[:12]}", encoded


def _clean(value, limit=240):
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(value.split())[:limit]


def _safe_detail(value):
    value = _clean(value)
    value = re.sub(r"0x[a-fA-F0-9]{40}\b", "[address]", value)
    return re.sub(r"\b\d{5,}\b", "[number]", value)


def build_variants(body, headers, include_slippage=False, slippage_tolerance=0.5):
    """Return baseline plus probes that alter exactly one request dimension."""
    baseline = {"name": "baseline", "body": deepcopy(body), "headers": deepcopy(headers)}
    variants = [baseline]

    amm_body = deepcopy(body)
    amm_body["protocols"] = ["V2", "V3", "V4"]
    variants.append({"name": "amm_protocols", "body": amm_body, "headers": deepcopy(headers)})
    for protocol in ("V2", "V3", "V4"):
        protocol_body = deepcopy(body)
        protocol_body["protocols"] = [protocol]
        variants.append({"name": f"{protocol.lower()}_only", "body": protocol_body,
                         "headers": deepcopy(headers)})

    false_headers = deepcopy(headers)
    false_headers["x-erc20eth-enabled"] = "false"
    variants.append({"name": "erc20eth_false", "body": deepcopy(body), "headers": false_headers})

    omitted_headers = deepcopy(headers)
    omitted_headers.pop("x-erc20eth-enabled", None)
    variants.append({"name": "erc20eth_omitted", "body": deepcopy(body), "headers": omitted_headers})

    connection_headers = deepcopy(headers)
    connection_headers.pop("Connection", None)
    variants.append({"name": "connection_omitted", "body": deepcopy(body), "headers": connection_headers})

    agent_headers = deepcopy(headers)
    agent_headers.pop("User-Agent", None)
    variants.append({"name": "user_agent_omitted", "body": deepcopy(body), "headers": agent_headers})

    if include_slippage:
        slippage_body = deepcopy(body)
        if "slippageTolerance" in slippage_body:
            slippage_body.pop("slippageTolerance")
            name = "slippage_omitted"
        else:
            slippage_body["slippageTolerance"] = round(float(slippage_tolerance), 2)
            name = "slippage_explicit"
        variants.append({"name": name, "body": slippage_body, "headers": deepcopy(headers)})
    return variants


def baseline_series(variants, rounds):
    """Repeat the exact current-production baseline for a bounded time series."""
    baseline = next(variant for variant in variants if variant["name"] == "baseline")
    return [
        {"name": f"baseline_round_{round_number}", "body": deepcopy(baseline["body"]),
         "headers": deepcopy(baseline["headers"])}
        for round_number in range(1, int(rounds) + 1)
    ]


def _record_response(variant, response, latency_ms):
    fingerprint, encoded = _fingerprint(variant["body"])
    try:
        parsed = response.json()
        parsed = parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        parsed = {}
    error = parsed if response.status_code != 200 else {}
    routing = _clean(parsed.get("routing"), 40).upper() or None
    if routing is not None and not re.fullmatch(r"[A-Z0-9_]{1,40}", routing):
        routing = None
    detail = error.get("detail") or error.get("error") or error.get("message") or ""
    return {
        "variant": variant["name"],
        "status": int(response.status_code),
        "error_code": _clean(error.get("errorCode") or error.get("code")),
        "detail": _safe_detail(detail),
        "request_id": _clean(response.headers.get("x-request-id") or response.headers.get("request-id"), 100) or None,
        "routing": routing,
        "latency_ms": round(latency_ms, 1),
        "payload_fingerprint": fingerprint,
        "body_bytes": len(encoded),
    }


def _record_transport_error(variant, exc, latency_ms):
    fingerprint, encoded = _fingerprint(variant["body"])
    if isinstance(exc, requests.Timeout):
        error_code, detail = type(exc).__name__, "request timed out"
    else:
        error_code, detail = type(exc).__name__, "transport request failed"
    return {
        "variant": variant["name"], "status": None, "error_code": error_code,
        "detail": detail, "request_id": None, "routing": None, "latency_ms": round(latency_ms, 1),
        "payload_fingerprint": fingerprint, "body_bytes": len(encoded),
    }


def run_matrix(variants, *, api_key, post=requests.post, sleep=time.sleep, output=sys.stdout,
               timeout_seconds=10, delay_seconds=DEFAULT_DELAY_SECONDS):
    """Send each probe exactly once; stop on quota/server/transport warning signs."""
    failures = 0
    completed = 0
    for index, variant in enumerate(variants):
        headers = {"x-api-key": api_key, "Content-Type": "application/json", **variant["headers"]}
        _, encoded = _fingerprint(variant["body"])
        started = time.monotonic()
        try:
            response = post(URL, headers=headers, data=encoded, timeout=timeout_seconds)
            record = _record_response(variant, response, (time.monotonic() - started) * 1000)
            stop = (record["status"] == 429 or bool(response.headers.get("Retry-After")) or
                    (500 <= record["status"] <= 599 and failures >= 1))
            failures = failures + 1 if 500 <= record["status"] <= 599 else 0
        except requests.RequestException as exc:
            record = _record_transport_error(variant, exc, (time.monotonic() - started) * 1000)
            failures += 1
            stop = failures >= 2
        print(json.dumps(record, sort_keys=True), file=output, flush=True)
        completed += 1
        if stop:
            break
        if index + 1 < len(variants):
            sleep(delay_seconds)
    return completed


def _load_values(env_path):
    return {**dotenv_values(env_path), **os.environ}


def _swapper(values, explicit):
    if explicit:
        return explicit
    private_key = values.get("PRIVATE_KEY", "")
    if not private_key:
        raise ValueError("--swapper is required when PRIVATE_KEY is unavailable")
    from eth_account import Account
    return Account.from_key(private_key).address


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--sell-token", required=True)
    parser.add_argument("--buy-token", required=True)
    amounts = parser.add_mutually_exclusive_group(required=True)
    amounts.add_argument("--sell-amount")
    amounts.add_argument("--buy-amount")
    parser.add_argument("--swapper")
    parser.add_argument("--slippage", type=float, help="Add one explicit slippageTolerance variant in percent")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--baseline-only", action="store_true",
                        help="Repeat only the exact production baseline (up to 12 rounds)")
    parser.add_argument("--delay-seconds", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--output", help="JSONL output path; defaults to stdout")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    max_rounds = 12 if args.baseline_only else MAX_ROUNDS
    if not 1 <= args.rounds <= max_rounds:
        raise SystemExit(f"--rounds must be between 1 and {max_rounds}")
    if args.delay_seconds < 2:
        raise SystemExit("--delay-seconds must be at least 2 seconds")
    if not 0.05 <= args.timeout_seconds <= 30:
        raise SystemExit("--timeout-seconds must be between 0.05 and 30")
    values = _load_values(args.env)
    api_key = values.get("UNISWAP_API_KEY", "")
    if not api_key:
        raise SystemExit("UNISWAP_API_KEY is required")
    try:
        chain_id = int(values.get("CHAIN_ID", "4663"))
        swapper = _swapper(values, args.swapper)
    except (TypeError, ValueError) as exc:
        raise SystemExit(_clean(exc, 120)) from None
    amount = args.sell_amount or args.buy_amount
    if not str(amount).isdigit() or int(amount) <= 0:
        raise SystemExit("quote amount must be a positive integer in base units")
    body = {
        "tokenInChainId": chain_id, "tokenOutChainId": chain_id,
        "tokenIn": args.sell_token, "tokenOut": args.buy_token, "swapper": swapper,
        "amount": str(amount), "type": "EXACT_INPUT" if args.sell_amount else "EXACT_OUTPUT",
    }
    if args.slippage is not None:
        body["slippageTolerance"] = round(float(args.slippage), 2)
    headers = {
        "x-universal-router-version": "2.1.1", "x-erc20eth-enabled": "true",
        "x-permit2-disabled": str(values.get("UNISWAP_PERMIT2_DISABLED", "true")).lower(),
        "User-Agent": "curl/8.0", "Connection": "close", "Accept": "application/json",
    }
    variants = build_variants(body, headers, args.slippage is not None,
                              0.5 if args.slippage is None else args.slippage)
    variants = (baseline_series(variants, args.rounds) if args.baseline_only
                else variants * args.rounds)
    if len(variants) > MAX_REQUESTS:
        raise SystemExit(f"matrix exceeds {MAX_REQUESTS} requests")
    handle = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    try:
        run_matrix(variants, api_key=api_key, output=handle, timeout_seconds=args.timeout_seconds,
                   delay_seconds=args.delay_seconds)
    finally:
        if args.output:
            handle.close()


if __name__ == "__main__":
    main()
