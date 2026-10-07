"""
Lightweight load smoke for ShareSquad scale hardening.

Usage (server already running on BASE_URL):
  python scripts/load_smoke.py --base-url http://127.0.0.1:8001 --users 20

This is not a full k6 suite — it verifies health/ready and auth-less endpoints
stay responsive under concurrent GETs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import statistics
import time
import urllib.error
import urllib.request


def timed_get(url: str) -> float:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=10) as response:
        response.read()
        if response.status >= 400:
            raise RuntimeError(f"{url} -> {response.status}")
    return (time.perf_counter() - started) * 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    paths = ["/healthz", "/readyz", "/api/health"]

    for path in paths:
        timed_get(base + path)

    samples: list[float] = []
    errors = 0

    def one(round_i: int, path: str) -> None:
        nonlocal errors
        try:
            samples.append(timed_get(base + path))
        except Exception as err:
            errors += 1
            print(f"error round={round_i} path={path}: {err}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.users) as pool:
        futures = []
        for round_i in range(args.rounds):
            for path in paths:
                futures.append(pool.submit(one, round_i, path))
        concurrent.futures.wait(futures)

    if not samples:
        raise SystemExit("No successful samples")
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    print(
        f"ok samples={len(samples)} errors={errors} "
        f"p50={statistics.median(samples):.1f}ms p95={p95:.1f}ms max={max(samples):.1f}ms"
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
