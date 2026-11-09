"""Read/write round-trip test: write a batch of Fluid + K8s metrics, then read each one
back through PromQL and print it.

Usage:
  #  run the test (manual verification; does not affect the test suite)
  python -m tests.prometheus.test_prometheus_readback \
      --prometheus http://localhost:9090 \
      --pushgateway http://localhost:9091

The write channel only supports the pushgateway (Prometheus must scrape it per its
scrape config); the read channel goes through PromQL. After writing, it polls until the
metrics are scraped into Prometheus, up to --wait seconds.

Note: if push returns 400 after a label/grouping change (mentioning duplicate or
inconsistent), the pushgateway still holds the old grouping (a single job may not contain
identical series), so delete the old one first:
  curl -X DELETE http://localhost:9091/metrics/job/fluid_agent:fluid/<label>/<value>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

# * Lets tests/ import server (works no matter which directory the script starts from)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.prometheus import configure
from server.agent.schemas import CollectorSnapshot, Metric


# ══════════════════════════════════════════════════════════════
# 1) Build the data: name / labels identical to what the two collectors produce
# ══════════════════════════════════════════════════════════════
class FakeCollector:
    FLUID = [
        ("fluid_cache_capacity_bytes",           "Fluid cache capacity (bytes)", 100.0 * 1024**3),
        ("fluid_cache_capacity_per_worker_bytes", "capacity per worker (bytes)", 25.0 * 1024**3),
        ("fluid_workers",                        "ready worker replicas",       4.0),
        ("fluid_cache_used_bytes",               "cache in use (bytes)",       70.0 * 1024**3),
        ("fluid_cache_used_ratio",               "cache used ratio (0..1)",    0.70),
    ]
    K8S = [
        ("k8s_nodes",                            "cluster nodes",              3.0),
        ("k8s_node_allocatable_memory_bytes",    "allocatable memory (bytes)", 120.0 * 1024**3),
        ("k8s_ready_pods",                       "ready pods in the namespace", 18.0),
        ("k8s_free_memory_bytes",               "estimated free memory (bytes)", 40.0 * 1024**3),
    ]

    def __init__(self, *, ctx, namespace, fluid_runtime, fluid_plural):
        self.ctx, self.ns = ctx, namespace
        self.fr, self.fp = fluid_runtime, fluid_plural

    @staticmethod
    def _now_ms() -> int:                 # * timestamps come only from here
        return int(time.time() * 1000)

    def fluid_snapshot(self):
        return self._snap("fluid", self.FLUID,
                         {"dataset": "train", "runtime": self.fr,
                          "runtime_plural": self.fp})

    def k8s_snapshot(self):
        return self._snap("k8s", self.K8S, {"node_role": "worker"})

    def _snap(self, collector, spec, extra):
        labels = {"kube_context": self.ctx, "namespace": self.ns, **extra}
        return CollectorSnapshot(
            collector=collector,
            identity={"kube_context": self.ctx, "namespace": self.ns},
            metrics=[Metric(name=n, help=h, type="gauge",
                            value=float(v),        # * constant, independent of time
                            labels=labels,
                            timestamp_ms=self._now_ms())  # * timestamp handled separately
                     for n, h, v in spec],
        )


# ══════════════════════════════════════════════════════════════
# 2) Test class: write -> poll until Prometheus scrapes -> read -> print and assert
# ══════════════════════════════════════════════════════════════
class PrometheusReadbackTest:
    """Round-trip test: check that what was written matches what is read back."""

    def __init__(self, prom_url: str, pushgw_url: str,
                 *, ctx: str = "test-cluster",
                 namespace: str = "ml-data",
                 fluid_runtime: str = "my-alluxio",
                 fluid_plural: str = "alluxioruntimes",
                 wait_seconds: float = 30.0,
                 poll_seconds: float = 1.0) -> None:
        if not pushgw_url:
            raise ValueError("the readback test needs a pushgateway write channel")
        self.prom_url = prom_url.rstrip("/")
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds

        # configure: writes go to the pushgateway, reads go through PromQL
        self.client, self.store = configure(
            pushgateway_url=pushgw_url.rstrip("/"),
            prometheus_query_url=self.prom_url,
        )

        self.fake = FakeCollector(
            ctx=ctx, namespace=namespace,
            fluid_runtime=fluid_runtime, fluid_plural=fluid_plural,
        )
        self.results: List[Dict[str, Any]] = []

    # ---------- main flow ---------- #
    def run(self) -> bool:
        print("═" * 78)
        print(f"  Prometheus readback test   prom={self.prom_url}")
        print("═" * 78)

        # 1. write
        fluid_snap = self.fake.fluid_snapshot()
        k8s_snap = self.fake.k8s_snapshot()
        self._write("Fluid", fluid_snap)
        self._write("K8s",   k8s_snap)

        # 2. after the pushgateway receives the push, wait for Prometheus to scrape it
        print(f"\n⏳ polling until the metrics reach Prometheus (up to {self.wait_seconds:g}s)…")

        # 3. read: verify both metric families
        fluid_ok = self._readback(fluid_snap, "Fluid")
        k8s_ok = self._readback(k8s_snap, "K8s")

        # 4. summary
        print("\n" + "═" * 78)
        print("  summary")
        print("═" * 78)
        for r in self.results:
            flag = "✅" if r["match"] else "❌"
            print(f"  {flag} {r['group']:5} {r['name']:38} "
                  f"wrote={r['wrote']}  read={r.get('read')}  "
                  f"label={r.get('label_match', '-')}")

        ok = all(r["match"] for r in self.results)
        print("\n👉 verdict:", "all consistent ✅" if ok else "inconsistencies found ❌")
        return ok

    # ---------- write ---------- #
    def _write(self, group: str, snap: CollectorSnapshot) -> None:
        try:
            self.client.write(snap)
            print(f"\n📤 writing {group}: {len(snap.metrics)} metrics → "
                  f"job=fluid_agent:{snap.collector}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n❌ writing {group} failed: {exc}", file=sys.stderr)
            raise

    # ---------- read and compare ---------- #
    def _wait_for(self, name: str, labels: Mapping[str, str],
                  expected: float) -> List[Metric]:
        """Poll until the expected value is read (Prometheus scraped this push) or timeout.

        When the metric already exists, the newest sample may still be the previous
        round's value (Prometheus scrapes the pushgateway with a scrape_interval delay),
        so the value must match rather than the metric merely appearing.
        """
        deadline = time.monotonic() + self.wait_seconds
        got: List[Metric] = []
        while True:
            got = self.client.reader.current(name, labels)
            if got and any(abs(x.value - expected) < 1e-6 for x in got):
                return got
            if time.monotonic() >= deadline:
                return got
            time.sleep(self.poll_seconds)

    def _readback(self, snap: CollectorSnapshot,
                  group: str) -> bool:
        print(f"\n🔍 reading back {group} metrics ──────────────────────────────────────")
        all_match = True
        for m in snap.metrics:
            # Filter by the full label set used when writing, so same-named series of
            # another runtime cannot bleed in.
            got = self._wait_for(m.name, m.labels, m.value)

            if not got:
                newest, match, delta, label_ok = None, False, None, False
            else:
                # Take the newest sample (current already uses last_over_time)
                newest = max(got, key=lambda x: x.timestamp_ms or 0)
                delta = abs(newest.value - m.value)
                # Every written label must match (Prometheus adds job/instance, ignored here)
                label_ok = all(newest.labels.get(k) == v
                               for k, v in m.labels.items())
                # Floats allow a 1e-6 tolerance; labels must match for a pass
                match = delta < 1e-6 and label_ok

            if not match:
                all_match = False

            self.results.append({
                "group": group, "name": m.name, "wrote": m.value,
                "read": newest.value if newest else None,
                "delta": delta, "label_match": label_ok, "match": match,
            })

            status = "✅" if match else "❌"
            print(f"  {status} {m.name:38} wrote={m.value:<12} "
                  f"read={newest.value if newest else 'MISSING':<12}"
                  f"{('Δ=' + f'{delta:.6f}') if delta is not None else ''}")
            if newest:
                print(f"        labels={_trim_labels(newest.labels)}  "
                      f"time={_ts(newest.timestamp_ms)}")
        return all_match

    def close(self) -> None:
        self.client.close()


# ══════════════════════════════════════════════════════════════
# 3) entry point
# ══════════════════════════════════════════════════════════════
def _trim_labels(labels: Dict[str, str], maxlen: int = 70) -> str:
    s = ", ".join(f'{k}="{v}"' for k, v in labels.items())
    return s if len(s) <= maxlen else s[:maxlen] + "…"


def _ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")


def main() -> int:
    p = argparse.ArgumentParser(description="Prometheus read/write round-trip test")
    p.add_argument("--prometheus", required=True, help="http://localhost:9090")
    p.add_argument("--pushgateway", required=True,
                   help="http://localhost:9091 (the only write channel)")
    p.add_argument("--wait", type=float, default=30.0,
                   help="poll timeout in seconds after writing (default 30)")
    args = p.parse_args()

    t = PrometheusReadbackTest(
        args.prometheus, args.pushgateway, wait_seconds=args.wait)
    try:
        ok = t.run()
        return 0 if ok else 1
    finally:
        t.close()


if __name__ == "__main__":
    sys.exit(main())
