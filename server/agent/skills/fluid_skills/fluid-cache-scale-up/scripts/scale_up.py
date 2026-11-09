#!/usr/bin/env python3
"""Fluid cache expansion: set the Runtime's worker replica count to a target (default +1).

The patch carries a resourceVersion optimistic lock: concurrent modification yields 409
and the caller is expected to re-read and retry.
This skill is an expansion tool: a target below the current count is refused by default
and requires the explicit --allow-shrink flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

DEFINITE_FAILURES = (400, 401, 403, 404, 409, 422)


# -- Pure functions, unit-test friendly ---------------------- #
def resolve_desired(current: int, add: int | None, replicas: int | None) -> int:
    """--replicas sets the total, --add sets the increment; neither means +1."""
    if replicas is not None:
        return replicas
    return current + (add if add is not None else 1)


def check_scale_direction(current: int, desired: int, *, allow_shrink: bool) -> None:
    """This skill refuses to shrink by default; shrinking is destructive and must be
    allowed explicitly."""
    if desired < current and not allow_shrink:
        raise ValueError(
            f"desired replicas {desired} is below the current {current}, which looks like a "
            "shrink; pass --allow-shrink to confirm"
        )


def ready_workers(status: dict[str, Any]) -> int:
    """Read the ready worker count: status.workerNumberReady first, conditions as fallback."""
    direct = status.get("workerNumberReady")
    if direct is not None:
        return int(direct)
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready" and condition.get("workerNumberReady") is not None:
            return int(condition["workerNumberReady"])
    return 0


def patch_body(desired: int, resource_version: str) -> dict[str, Any]:
    """Optimistic-lock patch: on a version mismatch the API returns 409, so nobody
    else's change is overwritten."""
    return {
        "metadata": {"resourceVersion": resource_version},
        "spec": {"replicas": desired},
    }


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
    return client.CustomObjectsApi(client.ApiClient(configuration))


def wait_ready(
    api: client.CustomObjectsApi, plural: str, namespace: str, name: str,
    current: int, desired: int, timeout: float, interval: float,
) -> tuple[int, bool]:
    """Poll until the ready worker count reaches the target; return (last known count,
    timed_out).

    The comparison follows the direction of the change: scaling up waits for the new
    replicas to become ready (ready >= desired), scaling down waits for the removed
    ones to actually go away (ready <= desired). A single "reached desired" test would
    return immediately on a scale-down, since the old replicas are still ready.

    A transient API error does not abort the wait; polling continues until the deadline
    and the last known count is reported.
    """
    shrinking = desired < current
    deadline = time.monotonic() + timeout
    ready = current  # last known replica count, until a poll succeeds
    while True:
        try:
            resource = api.get_namespaced_custom_object(
                "data.fluid.io", "v1alpha1", namespace, plural, name, _request_timeout=10
            )
        except ApiException as exc:  # noqa: PERF203 - retrying is the point of the loop
            print(f"poll failed, retrying: {exc.status} {exc.reason or ''}".strip(),
                  file=sys.stderr)
            resource = None
        if resource is not None:
            ready = ready_workers(resource.get("status") or {})
            if (ready <= desired) if shrinking else (ready >= desired):
                return ready, False
        if time.monotonic() >= deadline:
            return ready, True
        time.sleep(interval)


def emit(payload: dict[str, Any]) -> None:
    """Write JSON to stdout only; logs go to stderr."""
    print(json.dumps(payload, ensure_ascii=False))


def fail(reason_code: str, message: str, **extra: Any) -> int:
    emit({"status": "error", "action": "scale_up", "reason_code": reason_code,
          "error": message, **extra})
    return 1


# -- Main flow ------------------------------------------------ #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scale the Fluid Runtime worker replicas")
    parser.add_argument("--kube-context", default=os.environ.get("KUBE_CONTEXT", ""))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG", ""))
    parser.add_argument("--namespace", default=os.environ.get("KUBE_NS", ""))
    parser.add_argument("--runtime-name", default=os.environ.get("FLUID_RUNTIME", ""))
    parser.add_argument(
        "--runtime-plural",
        default=os.environ.get("FLUID_RUNTIME_PLURAL", "alluxioruntimes"),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--add", type=int, default=None, help="how many replicas to add, default 1")
    group.add_argument("--replicas", type=int, default=None, help="target replica count")
    parser.add_argument("--allow-shrink", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=600)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.namespace.strip() or not args.runtime_name.strip():
        parser.error("--namespace / --runtime-name must not be empty")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        api = build_api(args.kubeconfig, args.kube_context)
    except Exception as exc:  # noqa: BLE001 - kubeconfig problems go back to the caller as-is
        return fail("kubeconfig_unavailable", str(exc))

    try:
        runtime = api.get_namespaced_custom_object(
            "data.fluid.io", "v1alpha1", args.namespace,
            args.runtime_plural, args.runtime_name, _request_timeout=10,
        )
    except ApiException as exc:
        return fail(f"kubernetes_{exc.status}", exc.reason or str(exc))

    spec = runtime.get("spec") or {}
    raw_replicas = spec.get("replicas")
    # A missing spec.replicas falls back to 1, but an explicit 0 stays 0 (scale-to-zero
    # must not be mistaken for 1).
    current = 1 if raw_replicas is None else int(raw_replicas)
    version = (runtime.get("metadata") or {}).get("resourceVersion") or ""
    desired = resolve_desired(current, args.add, args.replicas)

    try:
        check_scale_direction(current, desired, allow_shrink=args.allow_shrink)
    except ValueError as exc:
        return fail("shrink_refused", str(exc), replicas={"before": current, "after": desired})
    if not version:
        return fail("runtime_version_unavailable",
                    "the Runtime has no resourceVersion, refusing to write blindly")
    if desired == current:
        emit({"status": "ok", "action": "scale_up", "dry_run": args.dry_run,
              "runtime": {"name": args.runtime_name, "namespace": args.namespace,
                          "plural": args.runtime_plural},
              "replicas": {"before": current, "after": current},
              "note": "already at the target replica count, nothing to change"})
        return 0

    if args.dry_run:
        emit({"status": "ok", "action": "scale_up", "dry_run": True,
              "runtime": {"name": args.runtime_name, "namespace": args.namespace,
                          "plural": args.runtime_plural},
              "replicas": {"before": current, "after": desired},
              "resource_version": version,
              "patch": patch_body(desired, version)})
        return 0

    try:
        patched = api.patch_namespaced_custom_object(
            "data.fluid.io", "v1alpha1", args.namespace, args.runtime_plural,
            args.runtime_name, patch_body(desired, version), _request_timeout=10,
        )
    except ApiException as exc:
        hint = ("resourceVersion is stale or was changed concurrently; reread the current "
                "replica count and recompute the target" if exc.status == 409 else "")
        return fail(f"kubernetes_{exc.status}", exc.reason or str(exc),
                    hint=hint, retryable=exc.status not in DEFINITE_FAILURES)

    result: dict[str, Any] = {
        "status": "ok", "action": "scale_up", "dry_run": False,
        "runtime": {"name": args.runtime_name, "namespace": args.namespace,
                    "plural": args.runtime_plural},
        "replicas": {"before": current, "after": desired},
        "resource_version": (patched.get("metadata") or {}).get("resourceVersion", version),
    }

    if not args.wait:
        result["ready_workers"] = ready_workers(runtime.get("status") or {})
        emit(result)
        return 0

    ready, timed_out = wait_ready(api, args.runtime_plural, args.namespace, args.runtime_name,
                                  current, desired, args.wait_timeout, args.interval)
    result["ready_workers"] = ready
    if timed_out:
        result["status"] = "timeout"
        emit(result)
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
