"""Web API contract: both data sources must be present, and one failing must not sink the other."""

import pytest
from fastapi.testclient import TestClient

from server.agent.schemas import CollectorSnapshot, Metric
from server.controller.web_controller import (
    MetricsService,
    WebSettings,
    create_app,
)

K8S_METRICS = {
    "k8s_nodes",
    "k8s_node_allocatable_memory_bytes",
    "k8s_free_memory_bytes",
    "k8s_ready_pods",
}
FLUID_METRICS = {
    "fluid_cache_capacity_bytes",
    "fluid_cache_capacity_per_worker_bytes",
    "fluid_workers",
    "fluid_cache_used_bytes",
    "fluid_cache_used_ratio",
}


def _fake_snapshot(key: str) -> CollectorSnapshot:
    """Build a metric set with the same names as the collector, so tests need no real cluster."""
    now = 1700000000000
    labels = {"kube_context": "test-context", "namespace": "ns", "dataset": "ds"}
    if key == "fluid":
        metrics = [
            Metric(name="fluid_cache_capacity_bytes", help="capacity", type="gauge",
                   value=10 * 1024**3, labels=labels, timestamp_ms=now),
            Metric(name="fluid_cache_capacity_per_worker_bytes", help="per-worker capacity",
                   type="gauge", value=5 * 1024**3, labels=labels, timestamp_ms=now),
            Metric(name="fluid_workers", help="worker count", type="gauge",
                   value=2, labels=labels, timestamp_ms=now),
            Metric(name="fluid_cache_used_bytes", help="used", type="gauge",
                   value=2 * 1024**3, labels=labels, timestamp_ms=now),
            Metric(name="fluid_cache_used_ratio", help="used ratio", type="gauge",
                   value=0.2, labels=labels, timestamp_ms=now),
        ]
    else:
        metrics = [
            Metric(name="k8s_nodes", help="node count", type="gauge",
                   value=3, labels={**labels, "node_role": "worker"}, timestamp_ms=now),
            Metric(name="k8s_node_allocatable_memory_bytes", help="allocatable memory",
                   type="gauge", value=300 * 1024**3,
                   labels={**labels, "node_role": "worker"}, timestamp_ms=now),
            Metric(name="k8s_free_memory_bytes", help="free memory", type="gauge",
                   value=100 * 1024**3,
                   labels={**labels, "node_role": "worker"}, timestamp_ms=now),
            Metric(name="k8s_ready_pods", help="ready pods", type="gauge",
                   value=17, labels={**labels, "node_role": "worker"}, timestamp_ms=now),
        ]
    return CollectorSnapshot(collector=key, identity={"namespace": "ns"}, metrics=metrics)


class _FakeSource:
    """Return a canned snapshot per key and count the calls."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail_on = fail_on

    def collect(self, key):
        self.calls.append(key)
        if key == self._fail_on:
            raise RuntimeError(f"{key} read failed: boom")
        return _fake_snapshot(key)


def test_overview_contains_both_sections():
    source = _FakeSource()
    service = MetricsService(WebSettings(), source=source)
    client = TestClient(create_app(WebSettings(), service=service))
    response = client.get("/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert [section["key"] for section in body["sections"]] == ["fluid", "k8s"]

    k8s, fluid = body["sections"][1], body["sections"][0]
    assert k8s["status"] == fluid["status"] == "ok"
    assert {m["name"] for m in k8s["metrics"]} >= K8S_METRICS
    assert {m["name"] for m in fluid["metrics"]} >= FLUID_METRICS
    assert all(m["label"] for section in body["sections"] for m in section["metrics"])


def test_k8s_memory_ratio_is_derived_from_collected_values():
    source = _FakeSource()
    service = MetricsService(WebSettings(), source=source)
    client = TestClient(create_app(WebSettings(), service=service))
    k8s = client.get("/api/k8s").json()

    ratio = next(m for m in k8s["metrics"] if m["name"] == "derived_memory_allocated_ratio")
    assert ratio["derived"] is True
    assert ratio["unit"] == "ratio"
    assert 0.0 <= ratio["value"] <= 1.0


def test_sections_are_cached_within_ttl():
    now = [0.0]
    source = _FakeSource()
    service = MetricsService(WebSettings(), source=source, clock=lambda: now[0])

    service.section("k8s")
    service.section("k8s")
    assert source.calls == ["k8s"]

    now[0] = 10.0
    service.section("k8s")
    assert source.calls == ["k8s", "k8s"]


def test_one_failing_source_does_not_hide_the_other():
    source = _FakeSource(fail_on="k8s")
    service = MetricsService(WebSettings(), source=source)
    client = TestClient(create_app(WebSettings(), service=service))

    body = client.get("/api/overview").json()
    fluid, k8s = body["sections"]

    assert k8s["status"] == "error"
    assert "boom" in k8s["error"]
    assert k8s["metrics"] == []
    assert fluid["status"] == "ok"


def test_missing_frontend_build_is_reported_explicitly(monkeypatch, tmp_path):
    monkeypatch.setattr("server.controller.web_controller.FRONTEND_DIST", tmp_path / "dist")
    client = TestClient(create_app(WebSettings()))

    response = client.get("/")

    assert response.status_code == 503
    assert "npm run build" in response.json()["detail"]


def test_unknown_api_route_is_404_json():
    client = TestClient(create_app(WebSettings()))
    response = client.get("/api/unknown")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "env,expected_port",
    [
        ({"WEB_PORT": "9000"}, 9000),
        ({}, 8000),
    ],
)
def test_settings_read_from_environment(env, expected_port):
    # from_env() fails closed without an API key, so every env fixture here
    # carries one (or the explicit disable escape hatch).
    settings = WebSettings.from_env({**env, "SINAPIS_API_KEY": "k"})
    assert settings.port == expected_port
    assert settings.api_key == "k"


def test_healthz_reports_ok():
    client = TestClient(create_app(WebSettings()))
    body = client.get("/api/healthz").json()
    assert body == {"status": "ok"}


def test_cors_preflight_allows_get_and_post():
    """Browser pushers need a passing OPTIONS preflight for the POST endpoints too."""
    client = TestClient(create_app(WebSettings()))
    response = client.options(
        "/api/user-system/events",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]
