#!/usr/bin/env python3
"""Simulate a user system: push one batch of model-file access events for the real
big_model zip files, so the agent walks through every strategy on its own.

The files live in the nju-model-hdfs dataset UFS (Alluxio path /models/data/...);
the sizes below are the real HDFS sizes, so the agent's capacity math is exact:

  big_model_104.zip  3152012255  (2.94GiB)
  big_model_105.zip  2643485774  (2.46GiB)
  big_model_106.zip  2830948396  (2.64GiB)
  big_model_107.zip  2841337723  (2.65GiB)
  big_model_108.zip  1985894795  (1.85GiB)
  big_model_109.zip  1671222590  (1.56GiB)

One worker holds 5GiB. Each scenario pushes exactly once and exits; the per-scenario
access count (SCENARIO_ACCESSES) is a fixed number that is only there to mark a file as
hot, not to model a rate.

  preload  50 accesses each for 108 + 109. They are 3.41GiB and fit the single worker
           (68% used), so the expected decision is: preload those two, no expansion,
           and nothing is above the 70% evict line yet.
  expand   60 accesses each for 104 + 105. With 108/109 already cached that is
           8.80GiB (9.45GB) against a 5GiB worker, so the expected decision is: expand to
           2 workers (desired_replicas=2), and the 104/105 preload follows once the new
           capacity is Ready.

From there no further pushes are needed: with 8.80GiB of 10GiB used (88%) the next
rounds cross the 70% evict line and free cold files, and once usage falls back below
the shrink line the surplus worker is dropped. One preload push and one expand push
cover the whole preload -> expand -> evict -> shrink chain.

Run preload first and let the agent finish caching 108/109 before running expand; the
two pushes are what separate "the cache filled up" from "the working set grew".

Usage:
  export SINAPIS_API_KEY=<key>          # the key the backend was started with
  python examples/user_system_simulator.py --scenario preload
  python examples/user_system_simulator.py --scenario expand
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

# Real sizes from the dataset UFS (hdfs dfs -ls, 2026-10-31).
BIG_FILES = {
    "/models/data/big_model_104.zip": 3152012255,
    "/models/data/big_model_105.zip": 2643485774,
    "/models/data/big_model_106.zip": 2830948396,
    "/models/data/big_model_107.zip": 2841337723,
    "/models/data/big_model_108.zip": 1985894795,
    "/models/data/big_model_109.zip": 1671222590,
}

# The two files each scenario heats.
PRELOAD_FILES = ("/models/data/big_model_108.zip", "/models/data/big_model_109.zip")
EXPAND_FILES = ("/models/data/big_model_104.zip", "/models/data/big_model_105.zip")

# Fixed burst per file, per scenario: enough to read as hot, nothing more. The expand
# pair runs hotter (60 vs 50) than the preload pair.
SCENARIO_ACCESSES = {"preload": 50, "expand": 60}

# Download bandwidth band (bytes/s); both scenarios carry normal demand, so the plain
# random band applies.
BANDWIDTH_BAND = (20 * 1024**2, 120 * 1024**2)

WORKER_CAPACITY = 5 * 1024**3  # cache capacity of one worker, for the summary line


def human(size: int) -> str:
    return f"{size / 1024**3:.2f}GiB"


def bandwidth_sample() -> float:
    return round(random.uniform(*BANDWIDTH_BAND), 2)


def post_json(url: str, payload: dict, api_key: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def get_json(url: str, api_key: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def push_once(base: str, source: str, paths: tuple[str, ...], accesses: int,
              api_key: str) -> None:
    """Push one batch of access events plus one bandwidth sample; raise if rejected."""
    now_ms = int(time.time() * 1000)
    events = [
        {
            "name": "model_file_access",
            "labels": {"file": path},
            "fields": {"access_time": now_ms, "size_bytes": float(BIG_FILES[path])},
        }
        for path in paths
        for _ in range(accesses)
    ]
    event = post_json(f"{base}/api/user-system/events",
                      {"source": source, "events": events}, api_key)
    metric = post_json(f"{base}/api/user-system/metrics", {
        "source": source,
        "metrics": [{"name": "model_download_bandwidth", "value": bandwidth_sample()}],
    }, api_key)
    if event["rejected"] or metric["rejected"]:
        raise RuntimeError(
            f"push was partially rejected: events={event['results']} metrics={metric['results']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push one batch of big_model access metrics to Sinapis"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                        help="web_controller base URL (default http://127.0.0.1:8000)")
    parser.add_argument("--api-key", default=os.environ.get("SINAPIS_API_KEY", ""),
                        help="API key the backend requires (default: env SINAPIS_API_KEY)")
    parser.add_argument("--source", default="hdfs-simulator",
                        help="pusher identity, used as the Prometheus grouping key source")
    parser.add_argument("--scenario", required=True, choices=("preload", "expand"),
                        help="preload: heat 108/109 (3.41GiB, fits one worker); "
                             "expand: heat 104/105 on top of them (8.80GiB > 5GiB)")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    if not args.api_key:
        print("no API key: export SINAPIS_API_KEY=<key> (or pass --api-key) — the "
              "backend rejects unauthenticated requests")
        return 1
    paths = PRELOAD_FILES if args.scenario == "preload" else EXPAND_FILES
    accesses = SCENARIO_ACCESSES[args.scenario]

    schema = get_json(f"{base}/api/user-system/metrics/schema", args.api_key)
    cfg = schema["config"]

    def counted(number: int, noun: str) -> str:
        return f"{number} {noun}" + ("" if number == 1 else "s")

    print(f"config loaded: {counted(cfg['metric_count'], 'metric')}, "
          f"{counted(cfg['event_count'], 'event type')} "
          f"(reload_error={cfg['reload_error']})")
    total = sum(BIG_FILES[path] for path in paths)
    print(f"scenario={args.scenario}: {counted(len(paths), 'file')} x "
          f"{accesses} accesses = {len(paths) * accesses} events, "
          f"{human(total)} (one push)")

    try:
        push_once(base, args.source, paths, accesses, args.api_key)
    except (urllib.error.URLError, RuntimeError) as exc:
        # URLError covers HTTPError and connection-level failures (refused, timeout).
        print(f"push failed: {exc}")
        return 1

    for path in paths:
        print(f"  pushed {accesses}x {path.rsplit('/', 1)[-1]} "
              f"({human(BIG_FILES[path])})")
    if args.scenario == "preload":
        print(f"  expected: preload 108+109 = {human(total)} of "
              f"{human(WORKER_CAPACITY)} per worker, no expansion")
    else:
        cached = sum(BIG_FILES[path] for path in PRELOAD_FILES) + total
        print(f"  expected: expand to 2 workers, then preload 104+105; "
              f"{human(cached)} of {human(2 * WORKER_CAPACITY)} once cached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
