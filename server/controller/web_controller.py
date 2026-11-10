"""Web console backend: expose collector metrics as JSON and serve the built frontend.

Data comes from exactly the same place as ``server/collectors``:

- Kubernetes: ``K8sCollector`` (node count / allocatable memory / ready pods / free memory)
- Fluid     : ``FluidCollector`` (total capacity / per-worker capacity / workers / used /
              used ratio)

Three design constraints:

1. Collecting hits the Kubernetes API, which is slow and can fail, so results are TTL
   cached: at most one collection per refresh period;
2. A failing source only affects its own section; the other section still returns, so
   the page never goes blank as a whole;
3. Data comes only from real cluster collection; no demo or simulated values.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server.agent.adapters.kubernetes import KubernetesAdapter
from server.agent.schemas import CollectorSnapshot, Metric, Target
from server.collectors import FluidCollector, K8sCollector
from server.config import DEFAULT_CONFIG_PATH, DEFAULT_TARGET, AppConfig
from server.controller.cached_files import CachedFilesResponse, CachedFileTracker
from server.controller.user_system_controller import (
    DEFAULT_USER_CONFIG,
    ConfigStore,
    create_user_system_router,
)
from server.prometheus.prometheus_client import MetricsSink, PushgatewaySink

logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

Unit = Literal["count", "bytes", "ratio"]
SectionKey = Literal["k8s", "fluid"]


# ==============================================================
# Response models
# ==============================================================
class MetricView(BaseModel):
    """One metric as shown by the frontend: raw value, display label and unit;
    formatting is left to the frontend."""

    name: str
    label: str
    help: str
    unit: Unit
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    timestamp_ms: int | None = None
    # Derived by this module from collected values (such as the memory allocation
    # ratio); not produced directly by a collector.
    derived: bool = False


class SectionView(BaseModel):
    """One data-source section. With status=error, metrics is empty and error explains why."""

    key: SectionKey
    title: str
    status: Literal["ok", "error"]
    error: str | None = None
    collected_at_ms: int | None = None
    metrics: list[MetricView] = Field(default_factory=list)


class Overview(BaseModel):
    generated_at_ms: int
    target: Target
    sections: list[SectionView]


# ==============================================================
# Display dictionary: adds a display label and unit to each collector metric name
# ==============================================================
@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: Unit
    order: int


_METRIC_SPECS: Mapping[str, MetricSpec] = {
    # -- Kubernetes (k8s_collector.py) --
    "k8s_nodes": MetricSpec("集群节点数", "count", 10),
    "k8s_node_allocatable_memory_bytes": MetricSpec("可分配内存总量", "bytes", 20),
    "k8s_free_memory_bytes": MetricSpec("空闲内存", "bytes", 30),
    "k8s_ready_pods": MetricSpec("Ready Pod 数", "count", 40),
    # -- Fluid (fluid_collector.py) --
    "fluid_cache_capacity_bytes": MetricSpec("缓存总容量", "bytes", 10),
    "fluid_cache_used_bytes": MetricSpec("已用缓存", "bytes", 20),
    "fluid_cache_used_ratio": MetricSpec("缓存使用率", "ratio", 30),
    "fluid_cache_capacity_per_worker_bytes": MetricSpec("单 Worker 平均容量", "bytes", 40),
    "fluid_workers": MetricSpec("Ready Worker 数", "count", 50),
    # -- Derived metrics --
    "derived_memory_allocated_ratio": MetricSpec("内存分配率", "ratio", 60),
}

_SECTION_TITLES: Mapping[SectionKey, str] = {
    "k8s": "K8s 集群信息",
    "fluid": "Fluid 系统信息",
}


def _infer_unit(name: str) -> Unit:
    if name.endswith("_bytes"):
        return "bytes"
    if name.endswith("_ratio"):
        return "ratio"
    return "count"


def _spec(name: str) -> MetricSpec:
    spec = _METRIC_SPECS.get(name)
    return spec if spec is not None else MetricSpec(name, _infer_unit(name), 999)


def _metric_view(metric: Metric, *, derived: bool = False) -> MetricView:
    spec = _spec(metric.name)
    return MetricView(
        name=metric.name,
        label=spec.label,
        help=metric.help,
        unit=spec.unit,
        value=metric.value,
        labels=metric.labels,
        timestamp_ms=metric.timestamp_ms,
        derived=derived,
    )


def _sorted(views: list[MetricView]) -> list[MetricView]:
    return sorted(views, key=lambda view: (_spec(view.name).order, view.name))


def _derived_metrics(key: SectionKey, snapshot: CollectorSnapshot) -> list[MetricView]:
    """Compute the few derived values the dashboard needs, without extra API calls."""
    if key != "k8s":
        return []

    values = {m.name: m.value for m in snapshot.metrics}
    alloc = values.get("k8s_node_allocatable_memory_bytes", 0.0)
    free = values.get("k8s_free_memory_bytes", 0.0)
    if alloc <= 0:
        return []
    ratio = min(max((alloc - free) / alloc, 0.0), 1.0)
    return [_derived("derived_memory_allocated_ratio",
                     "Scheduled memory as a fraction of allocatable memory (0..1).", ratio)]


def _derived(name: str, help_: str, value: float) -> MetricView:
    metric = Metric(name=name, help=help_, type="gauge", value=value)
    return _metric_view(metric, derived=True)


def _build_section(key: SectionKey, snapshot: CollectorSnapshot) -> SectionView:
    views = [_metric_view(m) for m in snapshot.metrics]
    views.extend(_derived_metrics(key, snapshot))
    collected = max((m.timestamp_ms or 0 for m in snapshot.metrics), default=0) or None
    return SectionView(
        key=key,
        title=_SECTION_TITLES[key],
        status="ok",
        collected_at_ms=collected,
        metrics=_sorted(views),
    )


# ==============================================================
# Configuration
# ==============================================================
@dataclass(frozen=True)
class WebSettings:
    """Web console options plus the connection settings used to reach the cluster.

    The Fluid / Kubernetes / Prometheus settings come from the same place as
    run_collectors.py: config.yml as the base, with per-field environment overrides
    (resolution lives in server/config.py).
    """

    # All interfaces, matching bin/_lib.sh: a reverse proxy on another host must
    # be able to reach this API, and the key check (from_env) is the gate that
    # keeps an unauthenticated instance off the network.
    host: str = "0.0.0.0"
    port: int = 8000
    # Cluster targeting shares its source with run_collectors.py (the target section
    # of config.yml).
    kube_context: str = DEFAULT_TARGET.kube_context
    namespace: str = DEFAULT_TARGET.namespace
    dataset: str = DEFAULT_TARGET.dataset
    runtime_name: str = DEFAULT_TARGET.dataset
    runtime_plural: str = DEFAULT_TARGET.runtime_plural
    node_role: str = "worker"
    free_mode: Literal["request", "usage"] = "request"
    request_timeout_seconds: float = 10.0
    cache_ttl_seconds: float = 5.0
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    # Cached-file table: the Alluxio subtree scanned for 100%-cached files, how long a
    # scan stays cached, and which kubectl binary runs the exec (web-only, env-sourced
    # like the other web options).
    cached_files_scan_path: str = "/models"
    cached_files_ttl_seconds: float = 30.0
    cached_files_kubectl: str = "kubectl"
    # User-system external metrics: the push/storage channel and the metric definition file.
    user_pushgateway_url: str = "http://localhost:9091"
    user_metrics_config: str = ""
    # API key required on every endpoint ("Authorization: Bearer <key>"). Empty only
    # ever happens for programmatically built settings (tests); the real entry point
    # from_env() refuses to produce settings without a key unless auth is explicitly
    # disabled, so no production path can end up serving an unauthenticated API.
    api_key: str = ""

    def target(self) -> Target:
        return Target(
            kube_context=self.kube_context,
            namespace=self.namespace,
            dataset=self.dataset,
            runtime_name=self.runtime_name,
            runtime_plural=self.runtime_plural,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        e = os.environ if env is None else env
        # Connection settings come from config.yml (still overridable per field via the
        # environment); web-only options read the environment alone.
        cfg = AppConfig.load(env=e)
        target = cfg.resolved_target()
        # The user-system definitions default to the SAME file the rest of the
        # settings came from (SINAPIS_CONFIG-aware), so retargeting the config file
        # cannot leave user_system silently reading server/config.yml.
        config_path = e.get("SINAPIS_CONFIG", "").strip() or str(DEFAULT_CONFIG_PATH)
        # Fail closed: without a key the API would be unauthenticated. The key comes
        # from the deployment environment, so it stays stable across restarts; the
        # only way to run without one is the explicit escape hatch.
        api_key = e.get("SINAPIS_API_KEY", "").strip()
        auth_disabled = e.get("SINAPIS_AUTH_DISABLED", "").strip().lower() in (
            "1", "true", "yes",
        )
        if api_key and auth_disabled:
            raise ValueError(
                "SINAPIS_API_KEY and SINAPIS_AUTH_DISABLED are mutually exclusive: "
                "either set a key or disable authentication, not both"
            )
        if not api_key and not auth_disabled:
            raise ValueError(
                "SINAPIS_API_KEY is not set: refusing to expose an unauthenticated API. "
                "Generate a key once with `openssl rand -hex 32`, persist it in the "
                "deployment environment (it must survive restarts), and never commit "
                "it to version control. For a deliberately keyless local run set "
                "SINAPIS_AUTH_DISABLED=true."
            )
        return cls(
            host=e.get("WEB_HOST", cls.host),
            port=int(e.get("WEB_PORT", cls.port)),
            kube_context=target.kube_context,
            namespace=target.namespace,
            dataset=target.dataset,
            runtime_name=target.runtime_name,
            runtime_plural=target.runtime_plural,
            node_role=cfg.k8s.node_role,
            free_mode=cfg.k8s.free_mode,
            request_timeout_seconds=cfg.k8s.request_timeout_seconds,
            cache_ttl_seconds=float(e.get("WEB_CACHE_TTL_SECONDS", 5)),
            user_pushgateway_url=cfg.prometheus.pushgateway_url,
            user_metrics_config=e.get("SINAPIS_USER_METRICS_CONFIG", "") or config_path,
            cors_origins=tuple(
                origin.strip()
                for origin in e.get("WEB_CORS_ORIGINS", ",".join(cls.cors_origins)).split(",")
                if origin.strip()
            ),
            cached_files_scan_path=e.get(
                "WEB_CACHED_FILES_SCAN_PATH", cls.cached_files_scan_path),
            cached_files_ttl_seconds=float(e.get("WEB_CACHED_FILES_TTL_SECONDS", 30)),
            cached_files_kubectl=e.get("KUBECTL", cls.cached_files_kubectl),
            api_key=api_key,
        )


# ==============================================================
# Data source: real cluster collection
# ==============================================================
class LiveSource:
    """Reuse a single KubernetesAdapter so kubeconfig is not reloaded per request."""

    def __init__(self, settings: WebSettings) -> None:
        self._settings = settings
        self._adapter: KubernetesAdapter | None = None
        self._lock = threading.Lock()

    def _kubernetes(self) -> KubernetesAdapter:
        with self._lock:
            if self._adapter is None:
                if not self._settings.kube_context.strip():
                    raise RuntimeError("kube_context is not configured, cannot reach the cluster")
                self._adapter = KubernetesAdapter(
                    self._settings.kube_context,
                    request_timeout_seconds=self._settings.request_timeout_seconds,
                )
            return self._adapter

    def collect(self, key: SectionKey) -> CollectorSnapshot:
        adapter = self._kubernetes()
        target = self._settings.target()
        if key == "fluid":
            return FluidCollector(adapter, target).collect()
        return K8sCollector(
            adapter,
            target,
            node_role=self._settings.node_role,
            free_mode=self._settings.free_mode,
        ).collect()


# ==============================================================
# Service: TTL cache plus per-source failure isolation
# ==============================================================
class MetricsService:
    """Collect each source at most once per refresh period; failures are cached
    briefly so recovery shows up quickly."""

    def __init__(
        self,
        settings: WebSettings,
        *,
        source: LiveSource | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._source = source if source is not None else LiveSource(settings)
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[SectionKey, tuple[float, SectionView]] = {}

    def section(self, key: SectionKey) -> SectionView:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        # Collect outside the lock so one slow source never blocks reads of the other;
        # a concurrent miss just collects the same section twice.
        section = self._collect(key)
        # Cache failures for a shorter time so cluster recovery surfaces quickly.
        ttl = self._settings.cache_ttl_seconds
        if section.status == "error":
            ttl = min(ttl, 3.0)
        with self._lock:
            self._cache[key] = (self._clock() + ttl, section)
        return section

    def overview(self) -> Overview:
        return Overview(
            generated_at_ms=int(time.time() * 1000),
            target=self._settings.target(),
            # Fluid first: the console leads with the cache system this agent serves.
            sections=[self.section("fluid"), self.section("k8s")],
        )

    def _collect(self, key: SectionKey) -> SectionView:
        try:
            snapshot = self._source.collect(key)
        except Exception as exc:  # noqa: BLE001 - a collection failure must become a page-level error
            logger.warning("Collecting %s failed: %s", key, exc, exc_info=exc)
            return SectionView(
                key=key,
                title=_SECTION_TITLES[key],
                status="error",
                error=_friendly_error(exc),
            )
        return _build_section(key, snapshot)


def _friendly_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if "kubeconfig" in message.lower():
        return f"{message} (check KUBE_CONTEXT and ~/.kube/config)"
    return message


# ==============================================================
# FastAPI application
# ==============================================================
def _api_key_auth(api_key: str) -> Callable[[str | None], None]:
    """Require ``Authorization: Bearer <api_key>`` on every request.

    The comparison is constant-time so response latency cannot leak key bytes.
    Rejections are deliberately uniform (401, same message) whether the header is
    missing, malformed, or wrong, so a caller cannot probe which of the three it hit.
    """
    expected = api_key

    def check(authorization: str | None = Header(default=None)) -> None:
        scheme, _, credential = (authorization or "").partition(" ")
        if (
            scheme.strip().lower() != "bearer"
            or not credential
            or not secrets.compare_digest(credential.strip(), expected)
        ):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return check


def create_app(
    settings: WebSettings | None = None,
    *,
    service: MetricsService | None = None,
    user_sink: MetricsSink | None = None,
    user_config_path: str | Path | None = None,
    tracker: CachedFileTracker | None = None,
) -> FastAPI:
    settings = settings or WebSettings.from_env()
    service = service if service is not None else MetricsService(settings)
    tracker = tracker or CachedFileTracker(
        kube_context=settings.kube_context,
        namespace=settings.namespace,
        dataset=settings.dataset,
        scan_path=settings.cached_files_scan_path,
        kubectl=settings.cached_files_kubectl,
        ttl_seconds=settings.cached_files_ttl_seconds,
    )

    app = FastAPI(
        title="Fluid-Sinapis-Agent Web API",
        version="0.1.0",
        # Custom (gated) docs below instead of docs_url/openapi_url: FastAPI's
        # built-in docs routes live at the Starlette layer, where global
        # dependencies do NOT apply — they would bypass the API key.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Global auth: every route (GET and POST alike) requires the key.
        # Programmatic callers (tests) may build settings without a key; the real
        # entry point WebSettings.from_env() never produces one.
        dependencies=[Depends(_api_key_auth(settings.api_key))] if settings.api_key else [],
    )
    app.state.settings = settings
    app.state.service = service
    app.state.tracker = tracker

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            # POST too: browser-based pushers need a passing preflight for the
            # user-system POST endpoints (OPTIONS itself is handled by the middleware).
            allow_methods=["GET", "POST"],
            # "*" does NOT cover Authorization per the fetch spec, so it must be
            # listed explicitly for browser clients.
            allow_headers=["*", "Authorization"],
        )

    @app.get("/api/healthz", tags=["system"])
    def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/api/openapi.json", include_in_schema=False)
    def openapi_schema() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    def swagger_ui() -> Any:
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(
            openapi_url="/api/openapi.json", title=app.title
        )

    @app.get("/api/overview", response_model=Overview, tags=["metrics"])
    def overview() -> Overview:
        """Return both the Kubernetes and Fluid sections at once, so a page refresh
        costs a single request."""
        return service.overview()

    @app.get("/api/k8s", response_model=SectionView, tags=["metrics"])
    def k8s_section() -> SectionView:
        return service.section("k8s")

    @app.get("/api/fluid", response_model=SectionView, tags=["metrics"])
    def fluid_section() -> SectionView:
        return service.section("fluid")

    @app.get("/api/cached-files", response_model=CachedFilesResponse, tags=["metrics"])
    def cached_files() -> CachedFilesResponse:
        """Dataset files with any part cached in Alluxio (>0%), with entry times."""
        return tracker.snapshot()

    _mount_user_system(app, settings, user_sink=user_sink, user_config_path=user_config_path)
    _mount_frontend(app)
    return app


def _mount_user_system(
    app: FastAPI,
    settings: WebSettings,
    *,
    user_sink: MetricsSink | None,
    user_config_path: str | Path | None,
) -> None:
    """Mount the user-system ingestion endpoints; the write channel shares the
    collectors' Pushgateway."""
    path = Path(
        user_config_path or settings.user_metrics_config or DEFAULT_USER_CONFIG
    )
    store = ConfigStore(path)
    if user_sink is not None:
        sink: MetricsSink = user_sink
    else:
        # Only the write channel is needed; building the full configure() pipeline
        # would also create a reader and snapshot store that nothing uses here.
        sink = PushgatewaySink(settings.user_pushgateway_url)
    app.include_router(create_user_system_router(store=store, sink=sink))


def _mount_frontend(app: FastAPI) -> None:
    """Production mode: serve web/dist directly, and explain the fix instead of 404
    when it has not been built."""
    assets = FRONTEND_DIST / "assets"
    index = FRONTEND_DIST / "index.html"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> Any:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="unknown api route")
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "frontend build not found; run: cd web && npm ci && npm run build"
            },
        )


def main() -> None:
    import uvicorn

    settings = WebSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
else:
    def __getattr__(name: str) -> Any:
        # Lazy import-time app for `uvicorn server.controller.web_controller:app`:
        # settings must NOT be resolved at import time, because from_env() fails
        # closed without SINAPIS_API_KEY and a hard import would crash unrelated
        # importers (tests, tooling) before they can even run. uvicorn resolves the
        # "app" attribute after import, so PEP 562 defers the build to that point
        # (and once set, the module attribute short-circuits this hook).
        if name == "app":
            global app
            app = create_app()
            return app
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
