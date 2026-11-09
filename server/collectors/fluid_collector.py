"""Fluid cache metrics: total capacity / per-worker capacity / worker count / used /
used ratio / desired worker replicas.

Source: the Fluid Runtime CRD (spec.replicas, plus status: cacheCapacity / cacheUsed /
workerNumberReady). Field names follow what `kubectl get <runtime> -o yaml` shows on
your cluster; if they differ, change them only here.
"""

from __future__ import annotations

from server.agent.adapters.kubernetes import KubernetesAdapter
from server.collectors.contracts import Collector, qty_to_bytes
from server.agent.schemas import CollectorSnapshot, Target

# Keep in sync with manifests.GROUP / VERSION; do not hardcode literals here.
from server.agent.adapters.fluid import GROUP, VERSION


class FluidCollector(Collector):
    name = "fluid"

    _CAP     = "fluid_cache_capacity_bytes"
    _PER     = "fluid_cache_capacity_per_worker_bytes"
    _WORK    = "fluid_workers"
    _DESIRED = "fluid_workers_desired"
    _USED    = "fluid_cache_used_bytes"
    _RATIO   = "fluid_cache_used_ratio"

    def __init__(self, adapter: KubernetesAdapter, target: Target) -> None:
        # The Fluid fields of Target are optional for pure Kubernetes collection, but
        # Fluid collection needs all of them.
        if not (target.dataset and target.runtime_name and target.runtime_plural):
            raise ValueError(
                "FluidCollector needs a complete Fluid target: "
                "dataset / runtime_name / runtime_plural must all be set")
        self._api = adapter.api
        self._timeout = adapter.timeout
        self._t = target

    def collect(self) -> CollectorSnapshot:
        runtime = self._fetch()
        status = runtime.get("status") or {}

        states = status.get("cacheStates") or {}
        total = qty_to_bytes(states.get("cacheCapacity") or status.get("cacheCapacity") or "0")
        used = qty_to_bytes(states.get("cached") or status.get("cacheUsed") or "0")
        workers = int(status.get("workerNumberReady") or _ready_workers(status))
        # spec.replicas is the configured target the shrink strategy compares against;
        # a missing value falls back to 1, but an explicit 0 stays 0 (scale-to-zero is
        # not mistaken for 1) — the same rule scale_up.py applies.
        raw_desired = (runtime.get("spec") or {}).get("replicas")
        desired = 1 if raw_desired is None else int(raw_desired)

        per_worker = total / workers if workers > 0 else 0.0
        ratio = used / total if total > 0 else 0.0   # capacity=0 yields zero, never NaN

        labels = self._labels()
        ts = self.now_ms()
        return CollectorSnapshot(
            collector=self.name,
            identity={"kube_context": self._t.kube_context,
                      "namespace": self._t.namespace,
                      "runtime": self._t.runtime_name},
            metrics=[
                self.gauge(self._CAP,     "Total Fluid cache capacity in bytes.", total, labels, ts),
                self.gauge(self._PER,     "Average Fluid cache capacity per ready worker, in bytes.",
                          per_worker, labels, ts),
                self.gauge(self._WORK,    "Number of ready Fluid worker replicas.",
                           workers, labels, ts),
                self.gauge(self._DESIRED, "Configured Fluid worker replica count (spec.replicas).",
                           desired, labels, ts),
                self.gauge(self._USED,    "Fluid cache currently in use, in bytes.",
                           used, labels, ts),
                self.gauge(self._RATIO,   "Fraction of the Fluid cache in use (0..1).",
                           ratio, labels, ts),
            ],
        )

    # ---------- private ---------- #
    def _fetch(self) -> dict:
        try:
            return self._api.get_namespaced_custom_object(
                GROUP, VERSION, self._t.namespace,
                self._t.runtime_plural, self._t.runtime_name,
                _request_timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to read the Fluid runtime: {exc}") from exc

    def _labels(self) -> dict[str, str]:
        t = self._t
        return {"kube_context": t.kube_context, "namespace": t.namespace,
                "dataset": t.dataset, "runtime": t.runtime_name,
                "runtime_plural": t.runtime_plural}


# -- Pure functions, unit-test friendly ---------------------- #
def _ready_workers(status: dict) -> int:
    for c in status.get("conditions") or []:
        if c.get("type") == "Ready":
            return int(c.get("workerNumberReady", 0))
    return 0