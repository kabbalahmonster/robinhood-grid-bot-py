#!/usr/bin/env python3
"""Read-only Uniswap gateway variant probe. Never signs or broadcasts."""

import argparse
import json
import os
import time

import requests
from dotenv import dotenv_values
from eth_account import Account


URL = "https://trade-api.gateway.uniswap.org/v1/quote"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    parser.add_argument("--boundary", action="store_true")
    parser.add_argument("--structure", action="store_true")
    parser.add_argument("--auth", action="store_true")
    parser.add_argument("--isolate", action="store_true")
    parser.add_argument("--transport", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    values = {**dotenv_values(args.env), **os.environ}
    api_key = values.get("UNISWAP_API_KEY", "")
    private_key = values.get("PRIVATE_KEY", "")
    token = values.get("TOKEN_ADDRESS", "")
    weth = values.get("WETH_ADDRESS", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")
    chain_id = int(values.get("CHAIN_ID", "4663"))
    if not api_key or not private_key or not token:
        raise SystemExit("UNISWAP_API_KEY, PRIVATE_KEY, and TOKEN_ADDRESS are required")

    body = {
        "tokenInChainId": chain_id,
        "tokenOutChainId": chain_id,
        "tokenIn": weth,
        "tokenOut": token,
        "swapper": Account.from_key(private_key).address,
        "amount": "1000000000000000",
        "type": "EXACT_INPUT",
    }
    variants = [
        ("bare", {}),
        ("official-defaults", {
            "x-universal-router-version": "2.0",
            "x-erc20eth-enabled": "false",
            "x-permit2-disabled": "false",
        }),
        ("router-2.1.1-only", {"x-universal-router-version": "2.1.1"}),
        ("erc20eth-only", {"x-erc20eth-enabled": "true"}),
        ("permit2-disabled-only", {"x-permit2-disabled": "true"}),
        ("current", {
            "x-universal-router-version": "2.1.1",
            "x-erc20eth-enabled": "true",
            "x-permit2-disabled": "true",
        }),
    ]
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")

    if args.production:
        production_body = {
            **body,
            "tokenIn": "0x0000000000000000000000000000000000000000",
        }
        production_headers = {
            "x-universal-router-version": "2.1.1",
            "x-erc20eth-enabled": "true",
            "x-permit2-disabled": "true",
            "User-Agent": "curl/8.0",
            "Accept": "application/json",
            "Connection": "close",
        }
        variants = [
            (
                f"production-fixed-user-agent-round-{round_number}",
                production_headers,
                production_body["amount"],
                production_body,
            )
            for round_number in range(1, args.rounds + 1)
        ]
    elif args.transport:
        integration_headers = {
            "x-universal-router-version": "2.1.1",
            "x-erc20eth-enabled": "true",
            "x-permit2-disabled": "true",
        }
        safe_transport = {
            "User-Agent": "curl/8.0",
            "Accept-Encoding": "identity",
            "Accept": "*/*",
            "Connection": "close",
        }
        transport_variants = [
            ("transport-safe", safe_transport),
            ("transport-python-user-agent", {
                **safe_transport,
                "User-Agent": "python-requests/2.31.0",
            }),
            ("transport-zstd-encoding", {
                **safe_transport,
                "Accept-Encoding": "gzip, deflate, zstd",
            }),
            ("transport-keep-alive", {
                **safe_transport,
                "Connection": "keep-alive",
            }),
            ("transport-requests-wire", {
                **safe_transport,
                "User-Agent": "python-requests/2.31.0",
                "Accept-Encoding": "gzip, deflate, zstd",
                "Connection": "keep-alive",
            }),
        ]
        variants = [
            (
                f"{name}-round-{round_number}",
                {**integration_headers, **transport_headers},
                body["amount"],
                None,
            )
            for round_number in range(1, args.rounds + 1)
            for name, transport_headers in transport_variants
        ]
    elif args.isolate:
        control_token = "0x18E674231A58c239Dc7DaeDcffE15Ec3A24cff5c"
        control_swapper = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        bot_swapper = body["swapper"]
        common_headers = {
            "x-universal-router-version": "2.1.1",
            "x-erc20eth-enabled": "true",
            "x-permit2-disabled": "true",
        }
        variants = []
        for token_name, token_address in (("control-token", control_token), ("bot-token", token)):
            for swapper_name, swapper_address in (("control-swapper", control_swapper), ("bot-swapper", bot_swapper)):
                isolate_body = {
                    **body,
                    "amount": "1000000000",
                    "tokenOut": token_address,
                    "swapper": swapper_address,
                    "slippageTolerance": 0.5,
                }
                variants.append((
                    f"isolate-{token_name}-{swapper_name}",
                    common_headers,
                    "",
                    isolate_body,
                ))
    elif args.auth:
        mutated_key = api_key[:-1] + ("0" if api_key[-1] != "0" else "1")
        variants = [
            ("auth-real", {"x-api-key": api_key}, "", {}),
            ("auth-mutated", {"x-api-key": mutated_key}, "", {}),
            ("auth-missing", {"x-api-key": None}, "", {}),
        ]
    elif args.structure:
        ordered = [
            ("type", body["type"]),
            ("amount", body["amount"]),
            ("tokenInChainId", body["tokenInChainId"]),
            ("tokenOutChainId", body["tokenOutChainId"]),
            ("tokenIn", body["tokenIn"]),
            ("tokenOut", body["tokenOut"]),
            ("swapper", body["swapper"]),
        ]
        partial = {}
        variants = [("structure-empty", {}, "", {})]
        for key, value in ordered:
            partial[key] = value
            variants.append((f"structure-through-{key}", {}, "", dict(partial)))
    elif args.boundary:
        variants = [
            (f"body-boundary-{digits}", {}, "1" + "0" * (digits - 1), None)
            for digits in (8, 9, 10, 11, 16)
        ]
    else:
        variants = [(name, headers, body["amount"], None) for name, headers in variants]
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    print(f"Read-only /quote probe: variants={len(variants)}")
    statuses = []
    for name, optional, amount, explicit_body in variants:
        request_body = explicit_body if explicit_body is not None else {**body, "amount": amount}
        encoded = json.dumps(request_body, separators=(",", ":"), sort_keys=True).encode()
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
            **optional,
        }
        headers = {key: value for key, value in headers.items() if value is not None}
        started = time.monotonic()
        try:
            response = requests.post(URL, headers=headers, data=encoded, timeout=20)
            statuses.append(response.status_code)
            elapsed = round((time.monotonic() - started) * 1000, 1)
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id") or ""
            detail = ""
            if response.status_code != 200:
                try:
                    error = response.json()
                    detail = error.get("error") or error.get("detail") or error.get("errorCode") or ""
                except ValueError:
                    detail = response.text[:160]
            print(json.dumps({
                "variant": name,
                "body_bytes": len(encoded),
                "status": response.status_code,
                "elapsed_ms": elapsed,
                "request_id": request_id,
                "detail": detail,
            }))
        except requests.RequestException as exc:
            print(json.dumps({"variant": name, "transport_error": str(exc)}))
        time.sleep(1.1)
    if args.production:
        passed = sum(status == 200 for status in statuses)
        print(f"Production fixed-header result: {passed}/{len(variants)} returned 200")
        if passed != len(variants):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
