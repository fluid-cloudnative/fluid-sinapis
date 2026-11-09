#!/usr/bin/env python3
"""Create a Fluid DataLoad to preload UFS paths into the distributed cache.

Scope: this only "submits a load request". It returns as soon as the DataLoad is
created; whether loading actually completes is up to Fluid asynchronously, so add
--wait to poll status.phase when the result matters.

Naming follows the same rule as server/agent/adapters/fluid.py, so the same
--operation-id always yields the same DataLoad name (a re-run hits 409).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

GROUP, VERSION = "data.fluid.io", "v1alpha1"
OPERATION_LABEL = "sinapis.fluid.io/operation-id"
MANAGED_LABEL = "app.kubernetes.io/managed-by"

# DataLoad terminal phases; any other phase (Pending/Loading, etc.) counts as in progress.
PHASE_COMPLETE, PHASE_FAILED = "Complete", "Failed"
# Explicit-rejection error codes; 5xx and network errors mean "result unknown" and must
# never be retried automatically.
DEFINITE_FAILURES = (400, 401, 403, 404, 409, 422)


# -- Pure functions, unit-test friendly ---------------------- #
def load_name(operation_id: str) -> str:
    """Derive a stable DNS-compatible DataLoad name from the operation id."""
    return "sinapis-" + hashlib.sha256(operation_id.encode()).hexdigest()[:24]


def load_body(
    namespace: str, dataset: str, paths: list[str], replicas_per_path: int, operation_id: str
) -> dict[str, Any]:
    """Build the DataLoad; it points at the Dataset and Fluid resolves the bound Runtime."""
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "DataLoad",
        "metadata": {
            "name": load_name(operation_id),
            "namespace": namespace,
            "labels": {OPERATION_LABEL: operation_id, MANAGED_LABEL: "fluid-sinapis-agent"},
        },
        "spec": {
            "dataset": {"name": dataset, "namespace": namespace},
            "target": [{"path": path, "replicas": replicas_per_path} for path in paths],
        },
    }


def extract_phase(resource: dict[str, Any]) -> str:
    """Read the DataLoad's status.phase, returning an empty string when absent."""
    return str((resource.get("status") or {}).get("phase") or "")


# -- Cluster interaction -------------------------------------- #
def _bypass_no_proxy(configuration: client.Configuration) -> None:
    """kubernetes 34 and similar versions read https_proxy into Configuration but drop
    no_proxy, so internal cluster addresses get hijacked by the proxy (connection cut);
    hosts matched by no_proxy are forced to connect directly."""
    if not configuration.proxy:
        return
    from requests.utils import should_bypass_proxies
    if should_bypass_proxies(configuration.host,
                             no_proxy=os.environ.get("NO_PROXY")
                             or os.environ.get("no_proxy")):
        configuration.proxy = None


def build_api(kubeconfig: str | None, context: str) -> client.CustomObjectsApi:
    """Load the kubeconfig for a context and return a CRD API client."""
    if not context.strip():
        raise ValueError("--kube-context / KUBE_CONTEXT must not be empty")
    configuration = client.Configuration()
    config.load_kube_config(
        config_file=kubeconfig or None, context=context, client_configuration=configuration
    )
    _bypass_no_proxy(configuration)
    api_client = client.ApiClient(configuration)
    return client.CustomObjectsApi(api_client)


def wait_phase(
    api: client.CustomObjectsApi, namespace: str, name: str,
    timeout: float, interval: float,
) -> tuple[str, bool]:
    """Poll status.phase until a terminal phase; return (last phase seen, timed_out)."""
    deadline = time.monotonic() + timeout
    phase = ""
    while True:
        resource = api.get_namespaced_custom_object(
            GROUP, VERSION, namespace, "dataloads", name, _request_timeout=10
        )
        phase = extract_phase(resource)
        if phase in (PHASE_COMPLETE, PHASE_FAILED):
            return phase, False
        if time.monotonic() >= deadline:
            return phase, True
        time.sleep(interval)


def emit(payload: dict[str, Any]) -> None:
    """Write JSON to stdout only; logs go to stderr."""
    print(json.dumps(payload, ensure_ascii=False))


def fail(action: str, reason_code: str, message: str, **extra: Any) -> int:
    emit({"status": "error", "action": action, "reason_code": reason_code,
          "error": message, **extra})
    return 1


# -- Main flow ------------------------------------------------ #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a DataLoad to preload UFS files into the Fluid cache"
    )
    parser.add_argument("--kube-context", default=os.environ.get("KUBE_CONTEXT", ""))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", ""))
    parser.add_argument("--namespace", default=os.environ.get("KUBE_NS", ""))
    parser.add_argument("--dataset", default=os.environ.get("FLUID_DATASET", ""))
    parser.add_argument("--path", action="append", required=True,
                        help="UFS path, repeatable")
    parser.add_argument("--replicas-per-path", type=int, default=1)
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=600)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.namespace.strip() or not args.dataset.strip():
        parser.error("--namespace / --dataset (or KUBE_NS / FLUID_DATASET) must not be empty")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    operation_id = args.operation_id.strip() or f"preload-{int(time.time() * 1000)}"
    body = load_body(args.namespace, args.dataset, args.path,
                     args.replicas_per_path, operation_id)
    name = body["metadata"]["name"]

    if args.dry_run:
        emit({"status": "ok", "action": "preload", "dry_run": True,
              "operation_id": operation_id,
              "dataload": {"name": name, "namespace": args.namespace},
              "manifest": body})
        return 0

    try:
        api = build_api(args.kubeconfig, args.kube_context)
    except Exception as exc:  # noqa: BLE001 - kubeconfig problems go back to the caller as-is
        return fail("preload", "kubeconfig_unavailable", str(exc))

    try:
        api.create_namespaced_custom_object(
            GROUP, VERSION, args.namespace, "dataloads", body, _request_timeout=10
        )
    except ApiException as exc:
        hint = (
            "a DataLoad with this name already exists, use another --operation-id"
            if exc.status == 409
            else ""
        )
        return fail("preload", f"kubernetes_{exc.status}", exc.reason or str(exc),
                    hint=hint, retryable=exc.status not in DEFINITE_FAILURES)

    result: dict[str, Any] = {
        "status": "ok", "action": "preload", "dry_run": False,
        "operation_id": operation_id,
        "dataload": {"name": name, "namespace": args.namespace},
    }

    if not args.wait:
        # An API confirmation is not a completed load; see SKILL.md for the query command.
        result["phase"] = ""
        emit(result)
        return 0

    phase, timed_out = wait_phase(api, args.namespace, name, args.wait_timeout, args.interval)
    result["phase"] = phase
    if timed_out:
        result["status"] = "timeout"
        emit(result)
        return 1
    if phase == PHASE_FAILED:
        result["status"] = "error"
        result["reason_code"] = "dataload_failed"
        emit(result)
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
