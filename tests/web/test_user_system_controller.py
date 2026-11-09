"""External metric push endpoints: live config reload, validation rules and
partial-success semantics."""

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.controller.user_system_controller import (
    ConfigStore,
    create_user_system_router,
)

CONFIG_V1 = """
user_system:
  metrics:
    - name: model_file_access_frequency
      help: model file access frequency
      labels: [model, file]
    - name: model_file_last_access_time
      help: model file last access time (Unix ms)
    - name: model_file_size_bytes
      help: model file size (bytes)
      labels: [model, file]
"""

EVENT_CONFIG = """
user_system:
  events:
    - name: model_file_access
      help: model file access events
      labels: [file]
      counter: model_file_access_count
      gauges:
        - field: access_time
          metric: model_file_last_access_time
          help: model file last access time (Unix ms)
        - field: size_bytes
          metric: model_file_size_bytes
          help: model file size (bytes)
"""

EVENT_CONFIG_V2 = EVENT_CONFIG + """
    - name: model_download
      help: model download events
      labels: [file]
      counter: model_download_count
"""

ACCESS_TIME = 1792982400000


def file_event(file="a.safetensors", size=1073741824, name="model_file_access"):
    return {
        "name": name,
        "labels": {"file": file},
        "fields": {"access_time": ACCESS_TIME, "size_bytes": size},
    }


def push_events(client, events, source="model-service-01"):
    return client.post("/api/user-system/events", json={"source": source, "events": events})

CONFIG_V2 = CONFIG_V1 + """
    - name: model_download_bandwidth
      help: model download bandwidth (bytes/s)
    - name: model_cache_hit_ratio
      help: model cache hit ratio
      labels: [model]
"""

THREE_METRICS = [
    {"name": "model_file_access_frequency", "value": 12.5,
     "labels": {"model": "qwen", "file": "a.safetensors"}},
    {"name": "model_file_last_access_time", "value": 1792982400000,
     "labels": {"model": "qwen", "file": "a.safetensors"}},
    {"name": "model_file_size_bytes", "value": 1073741824,
     "labels": {"model": "qwen", "file": "a.safetensors"}},
]


class FakeSink:
    def __init__(self, fail: bool = False) -> None:
        self.snapshots = []
        self.fail = fail

    def write(self, snapshot) -> None:
        if self.fail:
            raise RuntimeError("gateway down")
        self.snapshots.append(snapshot)


def make_client(tmp_path, config_text: str, sink: FakeSink | None = None):
    path = tmp_path / "user_metrics.yml"
    if config_text is not None:
        path.write_text(config_text, encoding="utf-8")
    store = ConfigStore(path, min_reload_interval=0)
    sink = sink if sink is not None else FakeSink()
    app = FastAPI()
    app.include_router(create_user_system_router(store=store, sink=sink))
    return TestClient(app), path, sink


def bump_mtime(path) -> None:
    stat = path.stat()
    os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))


def push(client, metrics, source="model-service-01"):
    return client.post("/api/user-system/metrics", json={"source": source, "metrics": metrics})


def test_push_accepts_configured_metrics(tmp_path):
    client, _path, sink = make_client(tmp_path, CONFIG_V1)

    response = push(client, THREE_METRICS)

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 3
    assert body["rejected"] == 0
    assert body["config"]["metric_count"] == 3
    assert body["config"]["reload_error"] is None
    assert len(sink.snapshots) == 1

    snapshot = sink.snapshots[0]
    assert snapshot.collector == "user_system"
    assert snapshot.identity == {"source": "model-service-01"}
    assert {m.name for m in snapshot.metrics} == {
        "model_file_access_frequency",
        "model_file_last_access_time",
        "model_file_size_bytes",
    }
    assert all(m.type == "gauge" for m in snapshot.metrics)


def test_unknown_metric_is_rejected_without_blocking_others(tmp_path):
    client, _path, sink = make_client(tmp_path, CONFIG_V1)

    response = push(client, [
        THREE_METRICS[0],
        {"name": "not_in_config", "value": 1.0},
    ])

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 1
    rejected = next(r for r in body["results"] if r["status"] == "rejected")
    assert rejected["reason_code"] == "unknown_metric"
    assert len(sink.snapshots) == 1
    assert [m.name for m in sink.snapshots[0].metrics] == ["model_file_access_frequency"]


def test_missing_required_label_is_rejected(tmp_path):
    client, _path, _sink = make_client(tmp_path, CONFIG_V1)

    response = push(client, [
        {"name": "model_file_size_bytes", "value": 1.0, "labels": {"model": "qwen"}},
    ])

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["reason_code"] == "missing_labels"
    assert "file" in result["detail"]


def test_non_finite_value_is_rejected(tmp_path):
    client, _path, sink = make_client(tmp_path, CONFIG_V1)

    # requests refuses to serialise inf, so raw JSON text stands in for a bad pusher
    raw = '{"source": "s", "metrics": [{"name": "model_file_size_bytes", ' \
          '"value": Infinity, "labels": {"model": "qwen", "file": "a"}}]}'
    response = client.post(
        "/api/user-system/metrics", content=raw,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["reason_code"] == "invalid_value"
    assert sink.snapshots == []


def test_config_change_takes_effect_without_restart(tmp_path):
    client, path, sink = make_client(tmp_path, CONFIG_V1)

    first = push(client, THREE_METRICS)
    assert first.json()["accepted"] == 3

    # The configuration is unchanged so far: the new metric is rejected
    early = push(client, [{"name": "model_cache_hit_ratio", "value": 0.9,
                           "labels": {"model": "qwen"}}])
    assert early.json()["results"][0]["reason_code"] == "unknown_metric"

    # Edit the configuration file; the service is not restarted
    path.write_text(CONFIG_V2, encoding="utf-8")
    bump_mtime(path)

    second = push(client, THREE_METRICS + [
        {"name": "model_download_bandwidth", "value": 1048576.0},
        {"name": "model_cache_hit_ratio", "value": 0.9, "labels": {"model": "qwen"}},
    ])

    assert second.status_code == 200
    body = second.json()
    assert body["accepted"] == 5
    assert body["rejected"] == 0
    assert body["config"]["metric_count"] == 5
    names = {m.name for m in sink.snapshots[-1].metrics}
    assert {"model_download_bandwidth", "model_cache_hit_ratio"} <= names


def test_broken_config_keeps_last_good_and_warns(tmp_path):
    client, path, sink = make_client(tmp_path, CONFIG_V1)

    # Load a good configuration once first
    assert push(client, THREE_METRICS[:1]).json()["accepted"] == 1

    path.write_text("user_system: [不是映射", encoding="utf-8")
    bump_mtime(path)

    response = push(client, THREE_METRICS[:1])
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1  # the old configuration keeps serving
    assert body["config"]["reload_error"]  # with a warning

    path.write_text(CONFIG_V1, encoding="utf-8")
    bump_mtime(path)
    recovered = push(client, THREE_METRICS[:1])
    assert recovered.json()["config"]["reload_error"] is None


def test_missing_config_rejects_push_with_503(tmp_path):
    client, _path, _sink = make_client(tmp_path, None)

    response = push(client, THREE_METRICS[:1])

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_sink_failure_returns_502(tmp_path):
    client, _path, _sink = make_client(tmp_path, CONFIG_V1, sink=FakeSink(fail=True))

    response = push(client, THREE_METRICS[:1])

    assert response.status_code == 502
    assert "gateway down" in response.json()["detail"]


def test_schema_endpoint_reflects_config(tmp_path):
    client, path, _sink = make_client(tmp_path, CONFIG_V1)

    first = client.get("/api/user-system/metrics/schema").json()
    assert [m["name"] for m in first["metrics"]] == [
        "model_file_access_frequency",
        "model_file_last_access_time",
        "model_file_size_bytes",
    ]
    assert first["metrics"][0]["labels"] == ["model", "file"]

    path.write_text(CONFIG_V2, encoding="utf-8")
    bump_mtime(path)

    second = client.get("/api/user-system/metrics/schema").json()
    assert len(second["metrics"]) == 5


# ══════════════════════════════════════════════════════════════
# Event push
# ══════════════════════════════════════════════════════════════
def test_events_derive_counter_and_gauges(tmp_path):
    client, _path, sink = make_client(tmp_path, EVENT_CONFIG)

    # Two accesses to the same file plus one to another file
    response = push_events(client, [
        file_event("a.safetensors"),
        file_event("a.safetensors"),
        file_event("b.safetensors", size=2048),
    ])

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 3
    assert body["config"]["event_count"] == 1

    snapshot = sink.snapshots[0]
    assert snapshot.collector == "user_system_events"

    by_name = {}
    for m in snapshot.metrics:
        by_name.setdefault(m.name, []).append(m)

    # The counter accumulates per file dimension
    counts = {(m.labels["file"]): m.value for m in by_name["model_file_access_count"]}
    assert counts == {"a.safetensors": 2.0, "b.safetensors": 1.0}
    # The gauge records the field values of the latest event
    sizes = {m.labels["file"]: m.value for m in by_name["model_file_size_bytes"]}
    assert sizes == {"a.safetensors": 1073741824.0, "b.safetensors": 2048.0}
    times = {m.labels["file"]: m.value for m in by_name["model_file_last_access_time"]}
    assert times == {"a.safetensors": float(ACCESS_TIME), "b.safetensors": float(ACCESS_TIME)}


def test_unknown_event_is_rejected_without_blocking_others(tmp_path):
    client, _path, sink = make_client(tmp_path, EVENT_CONFIG)

    response = push_events(client, [
        file_event("a.safetensors"),
        {"name": "not_in_config", "labels": {}, "fields": {}},
    ])

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    rejected = next(r for r in body["results"] if r["status"] == "rejected")
    assert rejected["reason_code"] == "unknown_event"
    assert len(sink.snapshots) == 1


def test_event_missing_field_or_label_is_rejected(tmp_path):
    client, _path, sink = make_client(tmp_path, EVENT_CONFIG)

    no_label = file_event()
    no_label["labels"] = {}
    no_field = file_event()
    del no_field["fields"]["size_bytes"]

    response = push_events(client, [no_label, no_field])

    body = response.json()
    assert body["accepted"] == 0
    assert body["results"][0]["reason_code"] == "missing_labels"
    assert body["results"][1]["reason_code"] == "missing_fields"
    assert sink.snapshots == []


def test_event_counter_accumulates_across_requests(tmp_path):
    client, _path, sink = make_client(tmp_path, EVENT_CONFIG)

    push_events(client, [file_event("a.safetensors")])
    second = push_events(client, [file_event("a.safetensors")])

    assert second.json()["accepted"] == 1
    counts = [m.value for m in sink.snapshots[-1].metrics
              if m.name == "model_file_access_count"]
    assert counts == [2.0]  # the same dimension keeps accumulating across requests


def test_sink_failure_does_not_double_count_on_retry(tmp_path):
    """A 502 leaves the counters untouched, so a client retry counts each event once."""
    client, _path, sink = make_client(tmp_path, EVENT_CONFIG, sink=FakeSink(fail=True))

    first = push_events(client, [file_event("a.safetensors")])
    assert first.status_code == 502
    assert sink.snapshots == []

    sink.fail = False
    retry = push_events(client, [file_event("a.safetensors")])
    assert retry.status_code == 200
    counts = [m.value for m in sink.snapshots[-1].metrics
              if m.name == "model_file_access_count"]
    assert counts == [1.0]  # the failed push was not counted


def test_event_state_is_not_grouped_by_pusher_source(tmp_path):
    """The accumulated state is global, so two sources must not duplicate it into
    two pushgateway groups (that would double-count in sum(rate(...)))."""
    client, _path, sink = make_client(tmp_path, EVENT_CONFIG)

    push_events(client, [file_event("a.safetensors")], source="service-a")
    push_events(client, [file_event("a.safetensors")], source="service-b")

    identities = {tuple(sorted(s.identity.items())) for s in sink.snapshots}
    assert identities == {(("source", "user_system"),)}
    counts = [m.value for m in sink.snapshots[-1].metrics
              if m.name == "model_file_access_count"]
    assert counts == [2.0]  # merged into one global counter, not one per source


def test_accumulator_evicts_lru_beyond_max_dimensions():
    from server.controller.user_system_controller import EventAccumulator

    acc = EventAccumulator(max_dimensions=2)
    acc.commit([("e", {"file": "a"}, {"size": 1})])
    acc.commit([("e", {"file": "b"}, {"size": 1})])
    acc.commit([("e", {"file": "a"}, {"size": 2})])  # touch "a": "b" becomes LRU
    acc.commit([("e", {"file": "c"}, {"size": 1})])  # evicts "b"

    files = {key[1][0][1] for key in acc.snapshot()}
    assert files == {"a", "c"}


def test_event_config_hot_reload(tmp_path):
    client, path, sink = make_client(tmp_path, EVENT_CONFIG)

    early = push_events(client, [{
        "name": "model_download", "labels": {"file": "a"},
        "fields": {},
    }])
    assert early.json()["results"][0]["reason_code"] == "unknown_event"

    path.write_text(EVENT_CONFIG_V2, encoding="utf-8")
    bump_mtime(path)

    response = push_events(client, [{
        "name": "model_download", "labels": {"file": "a"}, "fields": {},
    }])
    assert response.json()["accepted"] == 1
    names = {m.name for m in sink.snapshots[-1].metrics}
    assert names == {"model_download_count"}


def test_duplicate_derived_metric_name_rejects_config(tmp_path):
    client, _path, _sink = make_client(tmp_path, EVENT_CONFIG + """
  metrics:
    - name: model_file_size_bytes
      help: collides with an event-derived metric
""")

    response = push_events(client, [file_event()])

    assert response.status_code == 503  # the first load already fails, so the error is explicit
    assert "duplicate metric name" in response.json()["detail"]
