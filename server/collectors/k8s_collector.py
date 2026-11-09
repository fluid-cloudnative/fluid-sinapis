"""Kubernetes cluster metrics: node count / allocatable memory / ready pods / free memory.

Free memory defaults to allocatable_memory minus the memory requests of the pods in
the target namespace (an estimate; other namespaces are not deducted, so it can
overstate the true free memory). Pass free_mode="usage" for the real remaining
physical memory (requires metrics-server's node.metrics.k8s.io).
"""

from __future__ import annotations

from typing import Literal

from kubernetes import client

from server.agent.adapters.kubernetes import KubernetesAdapter
from server.agent.schemas import CollectorSnapshot, Target
from server.collectors.contracts import Collector, qty_to_bytes


class K8sCollector(Collector):
    name = "k8s"

    _NODES     = "k8s_nodes"
    _ALLOC_MEM = "k8s_node_allocatable_memory_bytes"
    _READY_PODS = "k8s_ready_pods"
    _FREE_MEM  = "k8s_free_memory_bytes"

    def __init__(self, adapter: KubernetesAdapter, target: Target,
                 *, node_role: str = "",
                 free_mode: Literal["request", "usage"] = "request") -> None:
        self._core = client.CoreV1Api(
            client.ApiClient(adapter.api.api_client.configuration))
        self._timeout = adapter.timeout
        self._t = target
        self._role = node_role
        self._free_mode = free_mode

    def collect(self) -> CollectorSnapshot:
        nodes = self._list_nodes()
        pods = self._list_pods()

        alloc_mem = sum(_mem(n.status.allocatable) for n in nodes)
        free_mem = self._free_memory(alloc_mem, pods)

        labels = self._labels()
        ts = self.now_ms()
        return CollectorSnapshot(
            collector=self.name,
            identity={"kube_context": self._t.kube_context,
                      "namespace": self._t.namespace},
            metrics=[
                self.gauge(self._NODES,
                           "Number of nodes in the cluster (all roles are counted; the "
                           "node_role label is informational only).",
                           len(nodes), labels, ts),
                self.gauge(self._ALLOC_MEM,
                           "Sum of allocatable memory across all cluster nodes, in bytes "
                           "(not filtered by role).",
                           alloc_mem, labels, ts),
                self.gauge(self._READY_PODS,"Number of Ready pods in the target namespace.",
                           sum(1 for p in pods if _is_ready(p)), labels, ts),
                self.gauge(self._FREE_MEM,
                           "Estimated free memory: allocatable minus the memory requests of "
                           "the target namespace's pods (other namespaces are not deducted, "
                           "so the value overstates true free memory; in usage mode, "
                           "allocatable minus actual working set).",
                           free_mem, labels, ts),
            ],
        )

    # ---------- two free-memory modes ---------- #
    def _free_memory(self, alloc: int, pods) -> int:
        if self._free_mode == "usage":
            return max(alloc - self._actual_usage(), 0)
        requested = 0
        for p in pods:
            for c in p.spec.containers:
                requested += qty_to_bytes(
                    (c.resources.requests or {}).get("memory"))
        return max(alloc - requested, 0)

    def _actual_usage(self) -> int:
        """Via metrics-server: sum(node.working_set_bytes)."""
        try:
            mcli = client.CustomObjectsApi(
                client.ApiClient(self._core.api_client.configuration))
            items = mcli.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes").get("items") or []
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"free_mode='usage' failed to read metrics-server node metrics "
                f"(is metrics-server installed and readable?): {exc}"
            ) from exc
        used = 0
        for it in items:
            usage = (it.get("usage") or {}).get("memory")
            if usage is not None:
                used += qty_to_bytes(usage)
        return used

    def _list_nodes(self):
        try:
            # The kubernetes client validates timeout_seconds as a pydantic int, while
            # the configured value is a float.
            return self._core.list_node(timeout_seconds=int(self._timeout)).items
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to list Kubernetes nodes: {exc}") from exc

    def _list_pods(self):
        try:
            return self._core.list_namespaced_pod(
                self._t.namespace, timeout_seconds=int(self._timeout)).items
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to list Kubernetes pods: {exc}") from exc

    def _labels(self) -> dict[str, str]:
        return {"kube_context": self._t.kube_context,
                "namespace": self._t.namespace,
                "node_role": self._role}


# -- Pure functions ------------------------------------------ #
def _mem(node_mem) -> int:
    v = node_mem.get("memory") if isinstance(node_mem, dict) else None
    return qty_to_bytes(v)


def _is_ready(pod) -> bool:
    if getattr(pod.status, "phase", None) != "Running":
        return False
    for c in pod.status.conditions or []:
        if getattr(c, "type", None) == "Ready" and getattr(c, "status", None) == "True":
            return True
    return False
