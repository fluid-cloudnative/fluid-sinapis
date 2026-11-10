"""Prometheus read/write facade.

Collectors only use `PrometheusClient.write(snapshot)`;
readers (metric tools, strategies) use `PrometheusClient.reader.current(...)`.

The write and read protocols are entirely different, so they are split into the
Sink and Reader abstractions and unified behind this facade.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from server.agent.schemas import CollectorSnapshot, Metric

logger = logging.getLogger(__name__)


# ==============================================================
# Write side
# ==============================================================
class MetricsSink(ABC):
    @abstractmethod
    def write(self, snap: CollectorSnapshot) -> None:
        """Write idempotently. Failures must raise RuntimeError, never be swallowed."""

    def close(self) -> None:  
        return


class _StaticCollector:
    """Expose one pre-built prometheus_client Metric from a CollectorRegistry."""

    def __init__(self, metric: Any) -> None:
        self._metric = metric

    def collect(self) -> tuple[Any, ...]:
        return (self._metric,)


class PushgatewaySink(MetricsSink):
    """Push to the Pushgateway. job = prefix:collector, identity forms the grouping key.

    Metrics are pushed as low-level samples so every series keeps its exact name.
    Counters are exposed as untyped: declaring them "counter" would make the client
    library rename the family to ``<name>_total`` and break ``rate(<name>[5m])`` queries;
    an untyped series keeps the name and rate()/increase() still work on it.
    """

    def __init__(self, gateway_url: str, *,
                 job_prefix: str = "fluid_agent",
                 timeout: float = 10) -> None:
        if not gateway_url:
            raise ValueError("gateway_url must not be empty")
        self._gw = gateway_url.rstrip("/")
        self._job = job_prefix
        self._timeout = timeout

    def write(self, snap: CollectorSnapshot) -> None:
        from prometheus_client import CollectorRegistry, Metric, push_to_gateway

        if not snap.metrics:
            return

        reg = CollectorRegistry(auto_describe=True)
        built: dict[str, Metric] = {}
        for m in snap.metrics:
            metric = built.get(m.name)
            if metric is None:
                metric = built[m.name] = Metric(
                    m.name, m.help, "gauge" if m.type == "gauge" else "untyped"
                )
                reg.register(_StaticCollector(metric))
            # Push the value only; sampling time comes from the pushgateway push time
            # (samples carrying an old timestamp are dropped by Prometheus as stale).
            metric.add_sample(m.name, {k: (v or "") for k, v in m.labels.items()},
                              m.value)

        job = f"{self._job}:{snap.collector}"
        grouping = {k: (v or "") for k, v in snap.identity.items()}
        try:
            push_to_gateway(self._gw, job=job, registry=reg,
                            grouping_key=grouping, timeout=self._timeout)
        except Exception as exc:
            raise RuntimeError(f"pushgateway write failed for job={job}: {exc}") from exc
        logger.info("Pushed %s grouping=%s (%d metrics)",
                    job, grouping, len(snap.metrics))
        for _m in snap.metrics:
            logger.info("    %s = %s %s", _m.name, _m.value, _m.labels)


# ==============================================================
# Read side
# ==============================================================
class MetricsReader(ABC):
    """Future cache strategies depend on this interface; the backend can change
    without touching strategy code."""

    @abstractmethod
    def current(self, name: str,
                labels: Mapping[str, str] | None = None) -> list[Metric]:
        """Latest value. Empty labels means no filtering."""


class PromQLReader(MetricsReader):
    """Queries the real Prometheus HTTP API for the latest values. Preferred for
    cache strategies."""

    def __init__(self, query_url: str, *, timeout: float = 10) -> None:
        if not query_url:
            raise ValueError("query_url must not be empty")
        self._q = query_url.rstrip("/")
        self._timeout = timeout

    def current(self, name: str,
                labels: Mapping[str, str] | None = None) -> list[Metric]:
        # last_over_time takes the last point within the past 5m, which is stable.
        sel = self._selector(name, labels)
        data = self._get("/api/v1/query",
                         {"query": f"last_over_time({sel}[5m])"})
        return self._decode(data)

    # ---------- private ---------- #
    def _get(self, path: str, params: dict) -> dict:
        import requests
        r = requests.get(f"{self._q}{path}", params=params,
                         timeout=self._timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"promql query failed: {body}")
        return body["data"]

    @staticmethod
    def _selector(name: str,
                  labels: Mapping[str, str] | None) -> str:
        if not labels:
            return name
        # Escape quotes, backslashes and newlines in exact-match label values.
        pairs = ",".join(f"{k}={json.dumps(v)}" for k, v in labels.items())
        return f"{name}{{{pairs}}}"

    @staticmethod
    def _decode(data: dict) -> list[Metric]:
        out: list[Metric] = []
        for s in data.get("result", []):
            meta = {str(k): str(v) for k, v in (s.get("metric") or {}).items()}
            name = meta.pop("__name__", "")
            if not name:
                continue

            def _sample(pair):
                """Prometheus value elements are [unix_ts, value_str], timestamp first."""
                if not pair or len(pair) < 2:
                    return None, None
                ts_s, raw = pair[0], pair[1]
                if raw is None:
                    return ts_s, None
                try:
                    return ts_s, float(raw)
                except (TypeError, ValueError):
                    return ts_s, None

            ts_s, v = _sample(s.get("value"))
            if v is None:
                continue
            out.append(Metric(
                name=name, help="prometheus:query", type="gauge",
                value=v, labels=dict(meta),
                timestamp_ms=int(float(ts_s) * 1000) if ts_s is not None else None,
            ))
        return out


class SnapshotReader(MetricsReader):
    """Read the latest local snapshot (fallback when Prometheus is not wired up, or
    for fast in-process lookups from strategies)."""

    def __init__(self, store: SnapshotStore) -> None:
        self._store = store

    def current(self, name: str,
                labels: Mapping[str, str] | None = None) -> list[Metric]:
        want = dict(labels) if labels else None
        out: list[Metric] = []
        # store is shaped {collector: {metric_name: [Metric]}}, so both levels are searched.
        for by_name in self._store.latest().values():
            for m in by_name.get(name, []):
                if want and not _labels_match(m.labels, want):
                    continue
                out.append(m)
        return out



class PrometheusClient:
    """One instance per process. Writes go through the sink, reads through the reader."""

    def __init__(self, sink: MetricsSink, reader: MetricsReader) -> None:
        self._sink = sink
        self.reader: MetricsReader = reader   # strategies use this one

    def write(self, snap: CollectorSnapshot) -> None:
        """Called by collectors. Raises RuntimeError on failure."""
        self._sink.write(snap)

    def close(self) -> None:
        self._sink.close()


class SnapshotStore:
    """Thread-safely keeps the latest snapshot of each collector (used by SnapshotReader)."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, list[Metric]]] = {}
        self._lock = threading.Lock()

    def update(self, snap: CollectorSnapshot) -> None:
        by_name: dict[str, list[Metric]] = {}
        for m in snap.metrics:
            by_name.setdefault(m.name, []).append(m)
        with self._lock:
            self._data[snap.collector] = by_name

    def latest(self) -> dict[str, dict[str, list[Metric]]]:
        with self._lock:
            return {k: {n: list(v) for n, v in inner.items()}
                    for k, inner in self._data.items()}


# ==============================================================
# Wiring entry point: called once at process start
# ==============================================================
def configure(*,
             pushgateway_url: str | None = None,
             prometheus_query_url: str | None = None,
             job_prefix: str = "fluid_agent",
             ) -> tuple[PrometheusClient, SnapshotStore]:
    """
    Write: pushgateway (the only write channel today; direct remote-write is not
    implemented yet).
    Read: PromQL when available (real history), otherwise the local snapshot.
    """
    if not pushgateway_url:
        raise ValueError("pushgateway_url must not be empty")

    sink = PushgatewaySink(pushgateway_url, job_prefix=job_prefix)
    store = SnapshotStore()

    if prometheus_query_url:
        reader: MetricsReader = PromQLReader(prometheus_query_url)
    else:
        reader = SnapshotReader(store)

    return PrometheusClient(sink=sink, reader=reader), store


def _labels_match(have: dict[str, str], want: dict[str, str]) -> bool:
    return all(have.get(k) == v for k, v in want.items())
