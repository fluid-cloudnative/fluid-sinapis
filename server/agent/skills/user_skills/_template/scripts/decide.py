#!/usr/bin/env python3
"""Example user strategy script: derive a cache operation from custom metrics.

stdin:  {"observations": [{"tool": "...", "result": {...}}], "context": {...}}
stdout: {"action": "expand"|"preload"|"shrink"|"evict"|null,
         "parameters": {...}, "reason": "<=300 chars"}

action=null means no opinion this round. The template has no opinion by default --
replace the "your decision rules" section with your own logic.
"""

from __future__ import annotations

import json
import sys

# Example: threshold for the custom metric model_download_bandwidth (adjust it to your
# metric's semantics).
BANDWIDTH_THRESHOLD_BYTES = 100 * 1024 * 1024


def flatten_metrics(observations: list[dict]) -> dict[tuple[str, str], float]:
    """Flatten the metric series of every observation source into {(metric name, labels): value}."""
    metrics: dict[tuple[str, str], float] = {}
    for observation in observations:
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        for metric in result.get("metrics", []):
            key = (
                str(metric.get("name")),
                json.dumps(metric.get("labels", {}), sort_keys=True),
            )
            if isinstance(metric.get("value"), (int, float)):
                metrics[key] = float(metric["value"])
    return metrics


def main() -> int:
    payload = json.load(sys.stdin)
    metrics = flatten_metrics(payload.get("observations", []))

    bandwidth = max(
        (value for (name, _labels), value in metrics.items()
         if name == "model_download_bandwidth"),
        default=0.0,
    )

    # -- your decision rules ---------------------------------- #
    # Example: recommend expansion while bandwidth stays above the threshold (note that
    # expand needs desired_replicas; when the framework does not provide the current
    # desired replica count, stay conservative or emit a parameter-less action only).
    if bandwidth > BANDWIDTH_THRESHOLD_BYTES:
        verdict = {
            "action": None,  # the demo template has no opinion; replace once your rules are ready
            "parameters": {},
            "reason": f"bandwidth {bandwidth:.0f}B/s is above the "
                      f"{BANDWIDTH_THRESHOLD_BYTES}B/s threshold; the template performs "
                      "no real action",
        }
    else:
        verdict = {"action": None, "parameters": {}, "reason": "custom condition not met"}
    # ───────────────────────────────────────────────────────── #

    print(json.dumps(verdict, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
