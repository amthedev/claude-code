#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from claude_gateway.benchmark import (
    BENCHMARK_CASES,
    benchmark_failures,
    benchmark_payload,
)


DEFAULT_BASE_URL = "http://127.0.0.1:8787"


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def env_value(name: str, dotenv: dict[str, str]) -> str:
    return os.getenv(name) or dotenv.get(name, "")


def first_gateway_token(dotenv: dict[str, str]) -> str:
    explicit = env_value("GATEWAY_BENCH_TOKEN", dotenv)
    if explicit:
        return explicit
    raw = env_value("GATEWAY_API_KEYS", dotenv)
    return raw.split(",", 1)[0].strip() if raw else ""


def request_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> tuple[dict[str, Any], float]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} returned HTTP {exc.code}: {text[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
    elapsed_ms = (time.perf_counter() - started) * 1000
    return data, elapsed_ms


def text_from_message(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "\n".join(chunks).strip()


def run_route_suite(
    args: argparse.Namespace,
    cases: list[Any],
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    failures = 0
    for case in cases:
        data, elapsed_ms = request_json(
            "POST",
            f"{args.base_url.rstrip('/')}/v1/router/debug",
            token=args.token,
            payload=benchmark_payload(case),
            timeout=args.timeout,
        )
        case_failures = benchmark_failures(case, data)
        failures += len(case_failures)
        rows.append(
            {
                "case": case.id,
                "kind": "route",
                "latency_ms": round(elapsed_ms, 1),
                "mode": data.get("mode"),
                "task_type": data.get("task_type"),
                "complexity": data.get("complexity"),
                "selected_model": data.get("selected_openrouter_model"),
                "orchestration": data.get("use_orchestration"),
                "web_search": data.get("web_search_should_search"),
                "cost_ratio": (
                    (data.get("cost_estimate") or {})
                    .get("effective_path", {})
                    .get("cost_ratio_vs_claude")
                ),
                "status": "FAIL" if case_failures else "OK",
                "notes": "; ".join(case_failures),
            }
        )
    return rows, failures


def run_live_suite(
    args: argparse.Namespace,
    cases: list[Any],
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    failures = 0
    selected_cases = [case for case in cases if args.live_deep or case.live_default]
    for case in selected_cases:
        data, elapsed_ms = request_json(
            "POST",
            f"{args.base_url.rstrip('/')}/v1/messages",
            token=args.token,
            payload=benchmark_payload(case),
            timeout=args.timeout,
        )
        text = text_from_message(data)
        ok = bool(text)
        if not ok:
            failures += 1
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        rows.append(
            {
                "case": case.id,
                "kind": "live",
                "latency_ms": round(elapsed_ms, 1),
                "model": data.get("model"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "status": "OK" if ok else "FAIL",
                "notes": text[:160].replace("\n", " "),
            }
        )
    return rows, failures


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ["kind", "case", "latency_ms", "mode", "task_type", "selected_model", "orch", "web", "cost", "status"]
    print(" | ".join(headers))
    print(" | ".join("-" * len(header) for header in headers))
    for row in rows:
        print(
            " | ".join(
                [
                    str(row.get("kind", "")),
                    str(row.get("case", "")),
                    str(row.get("latency_ms", "")),
                    str(row.get("mode", "")),
                    str(row.get("task_type", "")),
                    str(row.get("selected_model", row.get("model", ""))),
                    str(row.get("orchestration", "")),
                    str(row.get("web_search", "")),
                    str(row.get("cost_ratio", "")),
                    str(row.get("status", "")),
                ]
            )
        )
    route_latencies = [row["latency_ms"] for row in rows if row.get("kind") == "route"]
    live_latencies = [row["latency_ms"] for row in rows if row.get("kind") == "live"]
    if route_latencies:
        print(f"\nroute median: {statistics.median(route_latencies):.1f} ms")
    if live_latencies:
        print(f"live median: {statistics.median(live_latencies):.1f} ms")


def parse_args(argv: list[str]) -> argparse.Namespace:
    dotenv = load_dotenv(Path(".env"))
    parser = argparse.ArgumentParser(
        description="Economical benchmark for the Claude Code gateway router and low-token live smoke."
    )
    parser.add_argument("--base-url", default=env_value("GATEWAY_BENCH_BASE_URL", dotenv) or DEFAULT_BASE_URL)
    parser.add_argument("--token", default=first_gateway_token(dotenv))
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--live", action="store_true", help="Call /v1/messages for small live smoke cases.")
    parser.add_argument(
        "--live-deep",
        action="store_true",
        help="Include deep/orchestrated cases in live mode. This spends more credits.",
    )
    parser.add_argument("--json-output", help="Optional path to save raw benchmark rows as JSON.")
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("missing token. Set GATEWAY_BENCH_TOKEN, GATEWAY_API_KEYS, or pass --token.")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    cases = list(BENCHMARK_CASES)
    rows, failures = run_route_suite(args, cases)
    if args.live:
        live_rows, live_failures = run_live_suite(args, cases)
        rows.extend(live_rows)
        failures += live_failures
    print_table(rows)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if failures:
        print(f"\nFAIL: {failures} benchmark expectation(s) failed.", file=sys.stderr)
        return 1
    print("\nOK: benchmark expectations passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
