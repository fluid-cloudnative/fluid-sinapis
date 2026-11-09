"""Read collector-produced and user-system metrics through the Prometheus reader."""

import logging
import math
from collections.abc import Sequence

from langchain_core.tools import BaseTool, tool

from server.agent.config import UserMetricConfig
from server.agent.schemas import Target
from server.prometheus.prometheus_client import MetricsReader

logger = logging.getLogger(__name__)

FLUID_METRICS = (
    "fluid_cache_capacity_bytes",
    "fluid_cache_capacity_per_worker_bytes",
    "fluid_workers",
    "fluid_workers_desired",
    "fluid_cache_used_bytes",
    "fluid_cache_used_ratio",
)
K8S_METRICS = (
    "k8s_nodes",
    "k8s_node_allocatable_memory_bytes",
    "k8s_ready_pods",
    "k8s_free_memory_bytes",
)


def create_metric_tools(
    reader: MetricsReader,
    target: Target,
    user_system_metrics: Sequence[UserMetricConfig] = (),
) -> list[BaseTool]:
    """Bind observations to host-selected resources; model arguments cannot retarget them."""
    cluster_labels = {"kube_context": target.kube_context, "namespace": target.namespace}
    fluid_labels = {
        **cluster_labels,
        "dataset": target.dataset,
        "runtime": target.runtime_name,
        "runtime_plural": target.runtime_plural,
    }
    user_metrics = tuple(user_system_metrics)

    def read(source: str, names: Sequence[str], labels: dict[str, str]) -> dict:
        logger.info("Reading %s metrics through Prometheus", source)
        metrics = []
        missing = []
        for name in names:
            samples = reader.current(name, labels)
            if not samples:
                missing.append(name)
            for sample in samples:
                if sample.name != name or any(sample.labels.get(k) != v for k, v in labels.items()):
                    raise RuntimeError(
                        "metric reader returned a sample outside the requested target"
                    )
                if not math.isfinite(sample.value):
                    raise RuntimeError(f"metric reader returned a non-finite value: {name}")
                metrics.append(sample.model_dump(mode="json"))
        warnings = [
            "Values use the latest sample within 5 minutes. Prometheus timestamps do not "
            "prove collector freshness when using Pushgateway."
        ]
        if missing:
            warnings.append("Missing metric series: " + ", ".join(missing))
        logger.info(
            "Metrics %s read finished: %d series, missing=%d", source, len(metrics), len(missing)
        )
        return {
            "collector": source,
            "identity": dict(labels),
            "metrics": metrics,
            "missing_metrics": missing,
            "warnings": warnings,
        }

    @tool
    def read_fluid_metrics() -> dict:
        """Read Fluid capacity, usage, and ready workers for the configured target.

        Returns observed metrics, missing_metrics, and warnings. Missing values
        and read failures must not be interpreted as zero. Does not provide file access.
        """
        return read("fluid", FLUID_METRICS, fluid_labels)

    @tool
    def read_k8s_metrics() -> dict:
        """Read node count, allocatable/estimated free memory, and ready pods.

        Values are collector estimates, not a scheduling guarantee. Returns metrics,
        missing_metrics, and warnings; cluster and namespace come from host settings.
        """
        return read("k8s", K8S_METRICS, cluster_labels)

    @tool
    def read_user_system_metrics() -> dict:
        """Read user-system access metrics: model file access counts, last-access
        timestamps, file sizes, and download bandwidth.

        Series carry source/file labels instead of the cluster target. Counter
        values grow monotonically since the receiving service started (a restart
        resets them); compare counts across files for hotness and use timestamps
        for recency. Returns metrics, missing_metrics, and warnings; missing
        values must not be interpreted as zero.
        """
        # User-system metrics are unrelated to the cluster: they carry no target labels
        # and are read in full by metric name.
        return read("user_system", [d.name for d in user_metrics], {})

    tools: list[BaseTool] = [read_fluid_metrics, read_k8s_metrics]
    if user_metrics:
        tools.append(read_user_system_metrics)
    return tools
