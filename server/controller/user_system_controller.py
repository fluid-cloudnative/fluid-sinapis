"""Ingest external metrics from the user's system: accept pushes, validate, write
to Prometheus.

Unlike the active polling done by ``server/collectors``, these metrics cannot be
scraped; the user's system POSTs them and they are stored in Prometheus.

Three design constraints:

1. Which metrics are accepted is decided by the ``user_system.metrics`` section of
   the configuration file; the endpoint hardcodes no metric of its own;
2. Editing the configuration needs no restart: every request checks the mtime
   (1 second debounce) and reloads when it changed;
3. When the new configuration fails to parse, the last working configuration keeps
   serving and the response carries a warning, so one typo cannot break ingestion.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from server.agent.schemas import CollectorSnapshot, Metric
from server.prometheus.prometheus_client import MetricsSink

logger = logging.getLogger(__name__)

DEFAULT_USER_CONFIG = Path(__file__).resolve().parents[1] / "config.yml"

METRIC_NAME_PATTERN = r"^[a-zA-Z_:][a-zA-Z0-9_:]*$"
LABEL_KEY_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]*$"

SNAPSHOT_COLLECTOR = "user_system"
# Event-derived metrics use their own job so they cannot overwrite directly pushed gauges.
SNAPSHOT_COLLECTOR_EVENTS = "user_system_events"


# ==============================================================
# Configuration: user_system.metrics defines which metrics may be pushed
# ==============================================================
class MetricSpec(BaseModel):
    """Definition of one external metric, loaded from the configuration file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    help: str = Field(min_length=1)
    type: Literal["gauge", "counter"] = "gauge"
    # Label keys every push must carry; other keys are kept but not required.
    labels: tuple[str, ...] = ()


class GaugeSpec(BaseModel):
    """One numeric field of an event, stored as a gauge metric."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    metric: str = Field(min_length=1)
    help: str = Field(min_length=1)


class EventSpec(BaseModel):
    """Definition of one event stream: the user's system POSTs one entry per occurrence.

    The server maintains two kinds of metrics for it:
      counter -- occurrence count accumulated per (event, labels) pair; the rate is
                 left to Prometheus rate();
      gauges  -- numeric fields carried by the event, stored as gauges (last value).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    help: str = Field(min_length=1)
    # Dimensions every event must carry, e.g. [file]
    labels: tuple[str, ...] = ()
    # Metric name of the accumulated counter (maintained by the server, not the pusher)
    counter: str = Field(min_length=1)
    # Mapping of event numeric fields to stored gauges
    gauges: tuple[GaugeSpec, ...] = ()


class ConfigState(BaseModel):
    """Snapshot of the currently effective configuration."""

    specs: dict[str, MetricSpec] = Field(default_factory=dict)
    events: dict[str, EventSpec] = Field(default_factory=dict)
    loaded_at_ms: int | None = None
    # Reason the last reload failed; while specs is non-empty the old config is still serving.
    error: str | None = None
    path: str = ""


class ConfigStore:
    """Live configuration reload driven by mtime; current() stats the file at most once."""

    def __init__(
        self,
        path: str | Path,
        *,
        min_reload_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        # Validate the raw value first: Path("") normalizes to Path("."), so checking
        # after construction would never fire.
        if not str(path).strip():
            raise ValueError("config file path must not be empty")
        self.path = Path(path)
        self._min_interval = min_reload_interval
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._state: ConfigState | None = None
        self._mtime: float | None = None
        self._last_check = 0.0

    def current(self) -> ConfigState:
        now = self._clock()
        with self._lock:
            if self._state is not None and now - self._last_check < self._min_interval:
                return self._state
            self._last_check = now

            mtime = self._stat_mtime()
            if self._state is not None and mtime == self._mtime:
                return self._state

            state = self._reload(mtime)
            self._state = state
            self._mtime = mtime
            if state.error:
                logger.warning(
                    "Reloading the external metric config failed, keeping the previous one: %s",
                    state.error,
                )
            else:
                logger.info(
                    "External metric config loaded: %d metrics (%s)", len(state.specs), self.path
                )
            return state

    # ---------- private ---------- #
    def _stat_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def _reload(self, mtime: float | None) -> ConfigState:
        previous = self._state
        try:
            raw = self.path.read_text(encoding="utf-8")
            specs, events = _parse_config(yaml.safe_load(raw) or {})
        except Exception as exc:  # noqa: BLE001 - a broken config must not interrupt the service
            message = f"{exc.__class__.__name__}: {exc}"
            if previous is None:
                return ConfigState(error=message, path=str(self.path))
            return previous.model_copy(update={"error": message})

        return ConfigState(
            specs=specs,
            events=events,
            loaded_at_ms=self._wall_clock(),
            error=None,
            path=str(self.path),
        )


def _parse_config(raw: object) -> tuple[dict[str, MetricSpec], dict[str, EventSpec]]:
    if not isinstance(raw, dict):
        raise ValueError("the config must be a YAML mapping")
    section = raw.get("user_system") or {}
    if not isinstance(section, dict):
        raise ValueError("the user_system section must be a mapping")

    specs = _parse_metrics(section.get("metrics"))
    events = _parse_events(section.get("events"))
    _check_name_collisions(specs, events)
    return specs, events


def _parse_metrics(items: object) -> dict[str, MetricSpec]:
    if items is None:
        return {}
    if not isinstance(items, list):
        raise ValueError("user_system.metrics must be a list")

    specs: dict[str, MetricSpec] = {}
    for index, item in enumerate(items):
        try:
            spec = MetricSpec.model_validate(item)
        except Exception as exc:
            raise ValueError(f"invalid user_system.metrics[{index}]: {exc}") from exc
        if not re.fullmatch(METRIC_NAME_PATTERN, spec.name):
            raise ValueError(f"metrics[{index}].name is not a valid Prometheus name: {spec.name!r}")
        for key in spec.labels:
            if not re.fullmatch(LABEL_KEY_PATTERN, key):
                raise ValueError(
                    f"metrics[{index}].labels has {key!r}, which is not a valid Prometheus label"
                )
        if spec.name in specs:
            raise ValueError(f"duplicate metric name: {spec.name}")
        specs[spec.name] = spec
    return specs


def _parse_events(items: object) -> dict[str, EventSpec]:
    if items is None:
        return {}
    if not isinstance(items, list):
        raise ValueError("user_system.events must be a list")

    events: dict[str, EventSpec] = {}
    for index, item in enumerate(items):
        try:
            event = EventSpec.model_validate(item)
        except Exception as exc:
            raise ValueError(f"invalid user_system.events[{index}]: {exc}") from exc
        if not re.fullmatch(METRIC_NAME_PATTERN, event.name):
            raise ValueError(f"events[{index}].name is not a valid Prometheus name: {event.name!r}")
        if not re.fullmatch(METRIC_NAME_PATTERN, event.counter):
            raise ValueError(
                f"events[{index}].counter is not a valid Prometheus name: {event.counter!r}"
            )
        for key in event.labels:
            if not re.fullmatch(LABEL_KEY_PATTERN, key):
                raise ValueError(
                    f"events[{index}].labels has {key!r}, which is not a valid Prometheus label"
                )
        if event.name in events:
            raise ValueError(f"duplicate event name: {event.name}")
        events[event.name] = event
    return events


def _check_name_collisions(
    specs: dict[str, MetricSpec], events: dict[str, EventSpec]
) -> None:
    """The metrics and events are ultimately written to the same Prometheus, so
    names must not collide."""
    names = (
        list(specs)
        + [event.counter for event in events.values()]
        + [gauge.metric for event in events.values() for gauge in event.gauges]
    )
    seen: set[str] = set()
    duplicated: list[str] = []
    for name in names:
        if name in seen:
            duplicated.append(name)
        seen.add(name)
    if duplicated:
        raise ValueError(f"duplicate metric name: {', '.join(sorted(set(duplicated)))}")


# ==============================================================
# Push protocol
# ==============================================================
class PushedMetric(BaseModel):
    """A single metric pushed by the user's system. type/help are filled in by the
    server from the configuration, so the pusher need not send them.

    inf/nan are allowed through here and rejected per item by the business
    validation, so one bad value cannot fail the whole batch with a 422.
    """

    name: str = Field(min_length=1)
    value: float
    labels: dict[str, str] = Field(default_factory=dict)


class MetricsPush(BaseModel):
    source: str = Field(
        default="", description="pusher identity, used as the Prometheus grouping key"
    )
    metrics: list[PushedMetric] = Field(min_length=1, max_length=500)


class MetricResult(BaseModel):
    name: str
    status: Literal["accepted", "rejected"]
    reason_code: str | None = None
    detail: str | None = None


class ConfigInfo(BaseModel):
    metric_count: int
    event_count: int = 0
    loaded_at_ms: int | None
    reload_error: str | None = None


class PushedEvent(BaseModel):
    """A single event pushed by the user's system.

    fields are the numeric fields the event carries (such as access_time,
    size_bytes); the counter and derived gauges are produced by the server from the
    configuration, so the pusher does not deal with them.
    """

    name: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, float] = Field(default_factory=dict)


class EventsPush(BaseModel):
    source: str = Field(
        default="",
        description="informational pusher identity only: event counters are accumulated "
        "globally and stored under the fixed grouping key source=user_system",
    )
    events: list[PushedEvent] = Field(min_length=1, max_length=500)


class EventResult(BaseModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    status: Literal["accepted", "rejected"]
    reason_code: str | None = None
    detail: str | None = None


class EventsResponse(BaseModel):
    accepted: int
    rejected: int
    results: list[EventResult]
    config: ConfigInfo


class PushResponse(BaseModel):
    accepted: int
    rejected: int
    results: list[MetricResult]
    config: ConfigInfo


# ==============================================================
# Validation and storage
# ==============================================================
class EventAccumulator:
    """In-memory accumulator turning events into counters, keyed by event name + labels.

    Counters start at process start and reset to zero on restart: Prometheus
    rate()/increase() handle counter resets, so no persistence is needed. With
    multiple replicas use sum(rate(...)). It also keeps the latest numeric fields per
    dimension: the pushgateway overwrites earlier pushes for the same job and grouping
    key, so pushing only this event's dimension would evict the other files' series
    from their current values. Every write therefore carries the full state.

    The state is global (not partitioned by pushing source), so the events snapshot
    must use a constant pushgateway grouping key; grouping it by the pusher's source
    would emit the full counter state once per source and double-count in PromQL.

    Dimensions are evicted least-recently-used beyond ``max_dimensions`` so a
    high-cardinality label (e.g. per-file) cannot grow the process without bound; an
    evicted counter restarts from zero, which rate() tolerates.
    """

    def __init__(self, *, max_dimensions: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._latest: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, float]] = {}
        # Insertion order doubles as the LRU order: touched keys move to the end.
        self._max_dimensions = max(1, max_dimensions)

    @staticmethod
    def _key(event_name: str, labels: dict[str, str]) -> tuple:
        return (event_name, tuple(sorted(labels.items())))

    def pending(
        self, events: Sequence[tuple[str, dict[str, str], dict[str, float]]]
    ) -> dict[tuple, tuple[int, dict[str, float]]]:
        """The snapshot the accumulator WOULD have after committing ``events``,
        without mutating any state (the caller writes it to Prometheus first)."""
        with self._lock:
            merged = {key: (count, dict(self._latest.get(key, {})))
                      for key, count in self._counts.items()}
        for event_name, labels, fields in events:
            key = self._key(event_name, labels)
            count, latest = merged.get(key, (0, {}))
            merged[key] = (count + 1, {**latest, **fields})
        return merged

    def commit(
        self, events: Sequence[tuple[str, dict[str, str], dict[str, float]]]
    ) -> None:
        """Make ``events`` permanent; call only after their snapshot was written."""
        with self._lock:
            for event_name, labels, fields in events:
                key = self._key(event_name, labels)
                # pop + re-insert moves the key to the end (LRU touch).
                self._counts[key] = self._counts.pop(key, 0) + 1
                self._latest[key] = dict(fields)
            while len(self._counts) > self._max_dimensions:
                evicted = next(iter(self._counts))  # oldest = least recently used
                self._counts.pop(evicted)
                self._latest.pop(evicted, None)

    def snapshot(
        self,
    ) -> dict[tuple[str, tuple[tuple[str, str], ...]], tuple[int, dict[str, float]]]:
        """Current state of every dimension: {(event, labels): (count, latest fields)}."""
        with self._lock:
            return {key: (count, dict(self._latest.get(key, {})))
                    for key, count in self._counts.items()}


def _validate(item: PushedMetric, spec: MetricSpec) -> tuple[Metric | None, str | None, str | None]:
    """Return (metric, reason_code, detail); metric is None when rejected."""
    if not math.isfinite(item.value):
        return None, "invalid_value", f"value={item.value!r} is not a finite number"
    missing = [key for key in spec.labels if key not in item.labels]
    if missing:
        return None, "missing_labels", f"missing required label: {', '.join(missing)}"
    metric = Metric(
        name=spec.name,
        help=spec.help,
        type=spec.type,
        value=float(item.value),
        labels=dict(item.labels),
    )
    return metric, None, None


def _build_snapshot(
    source: str,
    metrics: list[Metric],
    *,
    collector: str = SNAPSHOT_COLLECTOR,
) -> CollectorSnapshot:
    """Group by metric name and merge the label key set per name (missing keys become
    empty strings), so the pushgateway does not reject inconsistent labels."""
    by_name: dict[str, list[Metric]] = {}
    for metric in metrics:
        by_name.setdefault(metric.name, []).append(metric)

    out: list[Metric] = []
    for items in by_name.values():
        keys: list[str] = []
        seen: set[str] = set()
        for metric in items:
            for key in metric.labels:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        for metric in items:
            out.append(
                Metric(
                    name=metric.name,
                    help=metric.help,
                    type=metric.type,
                    value=metric.value,
                    labels={key: metric.labels.get(key, "") for key in keys},
                )
            )
    return CollectorSnapshot(
        collector=collector,
        identity={"source": source or "unknown"},
        metrics=out,
    )


def _accumulated_metrics(
    events: dict[str, EventSpec],
    state: dict[tuple, tuple[int, dict[str, float]]],
) -> list[Metric]:
    """Expand an accumulator state snapshot into the series to push.

    The pushgateway overwrites earlier pushes for the same job and grouping key, so
    pushing only this event's dimension would evict the other files' series from
    their current values. Both counters and gauges are therefore emitted in full.
    """
    out: list[Metric] = []
    for (event_name, label_items), (count, fields) in state.items():
        event = events.get(event_name)
        if event is None:
            # A dimension whose event was removed by a live reload: stop emitting it and
            # let the old series expire naturally.
            continue
        labels = dict(label_items)
        out.append(Metric(
            name=event.counter,
            help=f"{event.help} (cumulative count; use rate() for the frequency)",
            type="counter",
            value=float(count),
            labels=labels,
        ))
        for gauge in event.gauges:
            value = fields.get(gauge.field)
            if value is None:
                # A field added by a live reload has no value yet in old dimensions;
                # omit it rather than invent one.
                continue
            out.append(Metric(
                name=gauge.metric,
                help=gauge.help,
                type="gauge",
                value=float(value),
                labels=labels,
            ))
    return out


# ==============================================================
# Routes
# ==============================================================
def create_user_system_router(
    *,
    store: ConfigStore,
    sink: MetricsSink,
    accumulator: EventAccumulator | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/user-system", tags=["user-system"])
    accumulator = accumulator or EventAccumulator()
    # Serializes "snapshot → write → commit" across concurrent event pushes: pending()
    # and commit() lock separately by design (the write happens outside the lock), so
    # two overlapping pushes would each snapshot the same pre-existing state and the
    # second write would overwrite the first commit's counts, silently dropping them.
    write_lock = threading.Lock()

    def _config_info(state: ConfigState) -> ConfigInfo:
        return ConfigInfo(
            metric_count=len(state.specs),
            event_count=len(state.events),
            loaded_at_ms=state.loaded_at_ms,
            reload_error=state.error,
        )

    def _unavailable(state: ConfigState) -> bool:
        return bool(state.error) and not state.specs and not state.events

    @router.get("/metrics/schema")
    def schema() -> dict:
        """Metrics and events the current configuration allows, so integrators can
        generate their push code from it."""
        state = store.current()
        return {
            "metrics": [
                {
                    "name": spec.name,
                    "help": spec.help,
                    "type": spec.type,
                    "labels": list(spec.labels),
                }
                for spec in state.specs.values()
            ],
            "events": [
                {
                    "name": event.name,
                    "help": event.help,
                    "labels": list(event.labels),
                    "fields": [gauge.field for gauge in event.gauges],
                    "derived_metrics": {
                        "counter": event.counter,
                        "gauges": [gauge.metric for gauge in event.gauges],
                    },
                }
                for event in state.events.values()
            ],
            "config": _config_info(state).model_dump(),
        }

    @router.post("/metrics", response_model=PushResponse)
    def receive(push: MetricsPush) -> PushResponse:
        """Accept a batch of external metrics; one failure does not affect the rest of
        the batch (partial-success semantics)."""
        state = store.current()
        if _unavailable(state):
            raise HTTPException(
                status_code=503, detail=f"external metric config unavailable: {state.error}"
            )

        results: list[MetricResult] = []
        groups: dict[str, list[Metric]] = {}
        for item in push.metrics:
            spec = state.specs.get(item.name)
            if spec is None:
                results.append(MetricResult(
                    name=item.name, status="rejected", reason_code="unknown_metric",
                    detail="not in the configured metric list",
                ))
                continue
            metric, reason_code, detail = _validate(item, spec)
            if metric is None:
                results.append(MetricResult(
                    name=item.name, status="rejected", reason_code=reason_code, detail=detail,
                ))
                continue
            groups.setdefault(spec.name, []).append(metric)
            results.append(MetricResult(name=item.name, status="accepted"))

        if groups:
            snapshot = _build_snapshot(
                push.source, [m for items in groups.values() for m in items],
            )
            try:
                sink.write(snapshot)
            except Exception as exc:  # noqa: BLE001 - a write failure must surface to the pusher
                logger.error("Writing external metrics failed: %s", exc, exc_info=exc)
                raise HTTPException(
                    status_code=502, detail=f"failed to write to Prometheus: {exc}"
                ) from exc

        accepted = sum(1 for r in results if r.status == "accepted")
        return PushResponse(
            accepted=accepted,
            rejected=len(results) - accepted,
            results=results,
            config=_config_info(state),
        )

    @router.post("/events", response_model=EventsResponse)
    def receive_events(push: EventsPush) -> EventsResponse:
        """Accept a batch of events (such as file accesses).

        Each event accumulates a counter and stores its numeric fields as gauges per
        the configuration. One failure does not affect the rest of the batch
        (partial-success semantics). Rates are not computed here: query
        rate(counter[5m]) in Prometheus, which also tolerates counters resetting to
        zero on restart.
        """
        state = store.current()
        if _unavailable(state):
            raise HTTPException(
                status_code=503, detail=f"external metric config unavailable: {state.error}"
            )

        results: list[EventResult] = []
        accepted_events: list[tuple[str, dict[str, str], dict[str, float]]] = []
        for item in push.events:
            event = state.events.get(item.name)
            if event is None:
                results.append(EventResult(
                    name=item.name, labels=dict(item.labels), status="rejected",
                    reason_code="unknown_event", detail="not in the configured event list",
                ))
                continue
            missing = [key for key in event.labels if key not in item.labels]
            if missing:
                results.append(EventResult(
                    name=item.name, labels=dict(item.labels), status="rejected",
                    reason_code="missing_labels",
                    detail=f"missing required label: {', '.join(missing)}",
                ))
                continue
            missing_fields = [g.field for g in event.gauges if g.field not in item.fields]
            if missing_fields:
                results.append(EventResult(
                    name=item.name, labels=dict(item.labels), status="rejected",
                    reason_code="missing_fields",
                    detail=f"missing required field: {', '.join(missing_fields)}",
                ))
                continue
            bad_fields = [g.field for g in event.gauges
                          if not math.isfinite(item.fields[g.field])]
            if bad_fields:
                results.append(EventResult(
                    name=item.name, labels=dict(item.labels), status="rejected",
                    reason_code="invalid_value",
                    detail=f"field is not a finite number: {', '.join(bad_fields)}",
                ))
                continue

            accepted_events.append((item.name, dict(item.labels), dict(item.fields)))
            results.append(EventResult(
                name=item.name, labels=dict(item.labels), status="accepted",
            ))

        accepted = sum(1 for r in results if r.status == "accepted")
        if accepted:
            # The pusher identity is informational (see EventsPush.source); log it so
            # the field is not silently ignored.
            logger.info("Events from source=%s: %d accepted", push.source or "unknown", accepted)
        if accepted_events:
            # The accumulated state is global, not partitioned by pushing source, so the
            # grouping key must be constant: grouping by the pusher's source would emit
            # the full counter state once per source and double-count it in PromQL.
            # Store every dimension: pushing only this dimension would evict the other
            # files' series from their current values.
            #
            # The whole snapshot → write → commit sequence holds write_lock so a
            # concurrent push cannot overwrite this push's counters with a stale
            # snapshot (commit only happens after the write succeeded; on a 502 the
            # counters are unchanged, so a client retry cannot double-count).
            with write_lock:
                snapshot = _build_snapshot(
                    "user_system",
                    _accumulated_metrics(state.events, accumulator.pending(accepted_events)),
                    collector=SNAPSHOT_COLLECTOR_EVENTS,
                )
                try:
                    sink.write(snapshot)
                except Exception as exc:  # noqa: BLE001 - a write failure must surface to the pusher
                    logger.error("Writing external events failed: %s", exc, exc_info=exc)
                    raise HTTPException(
                        status_code=502, detail=f"failed to write to Prometheus: {exc}"
                    ) from exc
                accumulator.commit(accepted_events)
        return EventsResponse(
            accepted=accepted,
            rejected=len(results) - accepted,
            results=results,
            config=_config_info(state),
        )

    return router
