#!/usr/bin/env python3
"""Evict files from the Alluxio cache with an approximate LRU policy (free clears the
cache only and never touches the UFS).

Flow: locate the master Pod -> `alluxio fs ls -R` to find files that are 100% cached ->
`alluxio fs stat` to fill in timestamps (prefer lastAccessTimesMs, the last access
time; fall back to the modification time, and rank last when neither is available) ->
accumulate from oldest to newest until the target is met -> `alluxio fs free` one by one.

Cluster access goes through kubectl subprocesses (exec/get) rather than the Python
kubernetes package: on this repository's environment the python client's websocket exec
channel received no data, while kubectl was reliable. The ls/stat output is mixed with
Alluxio stderr noise, so parsing matches line by line with regexes and silently ignores
anything that does not match, keeping the parser noise-insensitive.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any

# ls line (modern Alluxio): size PERSISTED date time cache%|DIR path
LS_LINE_MODERN = re.compile(
    r"^\s*(\d+)\s+(?:NOT_)?PERSISTED\s+\d{1,2}-\d{1,2}-\d{4}\s+[\d:.]+\s+(\d+%|DIR)\s+(.+?)\s*$"
)
# ls line (legacy): permissions owner group size in-memory% persisted% path (the path may
# contain spaces, hence it comes last)
LS_LINE_LEGACY = re.compile(
    r"^([-dlbcps][rwxStT-]{9})\s+\S+\s+\S+\s+(\d+)\s+(\d+)%\s+\d+%\s+(.+?)\s*$"
)
# stat's URIStatus dump: lastAccessTimesMs is the real LRU signal, lastModificationTimesMs
# is the fallback
STAT_ACCESS = re.compile(r"lastAccessTimesMs=(\d{10,17})")
STAT_MODIFY = re.compile(r"lastModificationTimesMs=(\d{10,17})")


# -- Pure functions, unit-test friendly ---------------------- #
def _parse_ls_line(line: str) -> dict[str, Any] | None:
    """Parse one ls output line; return None for directories or unrecognised lines."""
    match = LS_LINE_MODERN.match(line.strip())
    if match:
        if match.group(2) == "DIR":
            return None
        return {"path": match.group(3), "size_bytes": int(match.group(1)),
                "cache_pct": int(match.group(2).rstrip("%"))}
    match = LS_LINE_LEGACY.match(line.strip())
    if match and not match.group(1).startswith("d"):
        return {"path": match.group(4), "size_bytes": int(match.group(2)),
                "cache_pct": int(match.group(3))}
    return None


def parse_ls(text: str, *, cached_only: bool = True) -> list[dict[str, Any]]:
    """Parse `alluxio fs ls -R` output; with cached_only keep only 100%-cached files."""
    files: list[dict[str, Any]] = []
    for line in text.splitlines():
        parsed = _parse_ls_line(line)
        if parsed is None or (cached_only and parsed["cache_pct"] < 100):
            continue
        files.append({"path": parsed["path"], "size_bytes": parsed["size_bytes"]})
    return files


def parse_stat(text: str) -> float | None:
    """Take a timestamp (seconds) from `alluxio fs stat` URIStatus output: prefer the last
    access time, fall back to the modification time; None when neither parses."""
    for pattern in (STAT_ACCESS, STAT_MODIFY):
        match = pattern.search(text)
        if match:
            value = float(match.group(1))
            return value / 1000 if value > 1e12 else value  # normalise milliseconds to seconds
    return None


def cache_pct_map(text: str) -> dict[str, int]:
    """Build a path -> cached percentage map from ls output (files only), for the
    post-free verification."""
    result: dict[str, int] = {}
    for line in text.splitlines():
        parsed = _parse_ls_line(line)
        if parsed is not None:
            result[parsed["path"]] = parsed["cache_pct"]
    return result


def build_results(submitted: list[dict[str, Any]], after: dict[str, int]) -> list[dict[str, Any]]:
    """Pair each free submission with the cache ratio seen afterwards.

    A file counts as freed once its cached percentage drops below 100; a path that no
    longer appears in the listing is treated as "still cached" rather than freed, so an
    incomplete listing can never be mistaken for a successful eviction. exit_ok is
    carried through because the caller needs it to tell a real failure from exit-code
    noise.
    """
    return [
        {"path": item["path"], "freed": after.get(item["path"], 100) < 100,
         "cached_after_pct": after.get(item["path"]), "exit_ok": bool(item["exit_ok"]),
         "message": item["message"]}
        for item in submitted
    ]


def failure_messages(results: list[dict[str, Any]]) -> list[str]:
    """Only definite failures: free reported an error AND the file is still cached. A
    non-zero exit with the file gone from the cache was just exit-code noise."""
    return [
        f"free {r['path']} failed: {r['message']}"
        for r in results if not r["freed"] and not r["exit_ok"]
    ]


def victim_order(file: dict[str, Any]) -> tuple[int, float, str]:
    """Files with older access times come first; ones without a timestamp rank last so
    that unknown files are never evicted blindly."""
    mtime = file.get("mtime")
    if mtime is None:
        return (1, 0.0, str(file["path"]))
    return (0, float(mtime), str(file["path"]))


def pick_victims(
    files: list[dict[str, Any]], *, target_bytes: int | None = None, count: int | None = None
) -> list[dict[str, Any]]:
    """Pick eviction candidates in LRU order: --count takes a fixed number,
    --target-bytes stops once the total is reached.

    The check runs before accumulation, so a single oversized file is still selected
    (files cannot be split) and progress is guaranteed.
    """
    ordered = sorted(files, key=victim_order)
    if count is not None:
        return ordered[:count]
    victims: list[dict[str, Any]] = []
    total = 0
    for file in ordered:
        if target_bytes is not None and total >= target_bytes:
            break
        victims.append(file)
        total += int(file["size_bytes"])
    return victims


def master_pod_selectors(namespace: str, dataset: str) -> list[str]:
    """Different Fluid versions label the master Pod differently, so try the common
    selectors in order of likelihood.

    Observed on this cluster: workers carry fluid.io/dataset while the master only has
    fluid.io/dataset-id.
    """
    uid = f"{namespace}-{dataset}"
    return [
        f"fluid.io/dataset={uid},role=alluxio-master",
        f"fluid.io/dataset-id={uid},role=alluxio-master",
        f"app.kubernetes.io/instance={dataset},role=alluxio-master",
    ]


def pick_container(pod: dict[str, Any], prefer: str = "master") -> str:
    """Choose the main container in a multi-container Pod: the one whose name contains
    prefer, otherwise the first."""
    names = [container["name"] for container in pod["spec"]["containers"]]
    for name in names:
        if prefer in name:
            return name
    return names[0]


# -- Cluster interaction (kubectl subprocess) ---------------- #
def run_kubectl(
    kubectl: str, context: str, namespace: str, args: list[str], timeout: float
) -> str:
    """Run a kubectl subcommand; on failure raise with a stderr summary."""
    cmd = [kubectl, "--context", context, "-n", namespace, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()[-500:] or f"kubectl exited with {result.returncode}"
        )
    return result.stdout


def exec_in_pod(
    kubectl: str, context: str, namespace: str, pod: str, container: str,
    command: str, timeout: float,
) -> str:
    """Run a command inside the master Pod container and return the merged output (noise
    is filtered by the regexes)."""
    return run_kubectl(
        kubectl, context, namespace,
        ["exec", pod, "-c", container, "--", "sh", "-c", command], timeout,
    )


def resolve_master_pod(
    kubectl: str, context: str, namespace: str, selectors: list[str],
    pod_name: str, timeout: float,
) -> str:
    """Prefer --pod-name; otherwise find the master Pod by label, taking the
    lexicographically smallest name across HA replicas."""
    if pod_name.strip():
        run_kubectl(kubectl, context, namespace,
                    ["get", "pod", pod_name, "-o", "json"], timeout)
        return pod_name
    tried: list[str] = []
    for selector in selectors:
        pods = json.loads(run_kubectl(
            kubectl, context, namespace,
            ["get", "pods", "-l", selector, "-o", "json"], timeout,
        ))
        names = sorted(item["metadata"]["name"] for item in pods.get("items") or [])
        if names:
            return names[0]
        tried.append(selector)
    raise RuntimeError(
        f"none of these selectors found a master pod: {tried}; "
        "check with kubectl get pods --show-labels and set --pod-label / --pod-name"
    )


def free_paths(
    kubectl: str, context: str, namespace: str, pod: str, container: str,
    alluxio_bin: str, paths: list[str], *, force: bool, timeout: float,
) -> list[dict[str, Any]]:
    """Run `alluxio fs free` per file; one failure does not stop the rest.

    free is asynchronous: Alluxio exits 255 on timeout while the eviction continues in
    the background, so only the submission result is recorded here (the exit code goes
    into message). The caller decides whether a file was really cleared by re-checking
    the cache ratio after free; only definite failures reach errors.
    """
    flag = " -f" if force else ""
    submitted: list[dict[str, Any]] = []
    for path in paths:
        try:
            output = exec_in_pod(
                kubectl, context, namespace, pod, container,
                # Paths come from ls output and reach the pod's shell, so quote them.
                f"{alluxio_bin} fs free{flag} {shlex.quote(path)}", timeout,
            )
            submitted.append({"path": path, "exit_ok": True, "message": output.strip()[-500:]})
        except Exception as exc:  # noqa: BLE001 - record the single-file failure and continue
            submitted.append({"path": path, "exit_ok": False,
                              "message": f"{exc.__class__.__name__}: {exc}"})
    return submitted


# -- Main flow ------------------------------------------------ #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evict Alluxio cache files using an approximate LRU policy"
    )
    parser.add_argument("--kube-context", default=os.environ.get("KUBE_CONTEXT", ""))
    parser.add_argument("--kubectl", default=os.environ.get("KUBECTL", "kubectl"),
                        help="path to the kubectl binary")
    parser.add_argument("--namespace", default=os.environ.get("KUBE_NS", ""))
    parser.add_argument("--dataset", default=os.environ.get("FLUID_DATASET", ""))
    parser.add_argument("--pod-label", default="")
    parser.add_argument("--pod-name", default="")
    parser.add_argument("--container", default="")
    parser.add_argument("--alluxio-bin", default="alluxio")
    parser.add_argument("--path", default="/", help="UFS subtree to scan")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--target-bytes", type=int, default=None)
    group.add_argument("--count", type=int, default=None)
    parser.add_argument("--stat-limit", type=int, default=500)
    parser.add_argument("--stat-seconds", type=float, default=60.0,
                        help="wall-clock budget for the stat phase; files past it keep "
                             "no timestamp and rank last in the eviction order")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--exec-timeout", type=float, default=120)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.namespace.strip() or not args.dataset.strip():
        parser.error("--namespace / --dataset (or KUBE_NS / FLUID_DATASET) must not be empty")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload: dict[str, Any] = {"action": "evict_lru", "dry_run": args.dry_run}
    try:
        selectors = ([args.pod_label] if args.pod_label.strip()
                     else master_pod_selectors(args.namespace, args.dataset))
        pod = resolve_master_pod(
            args.kubectl, args.kube_context, args.namespace, selectors,
            args.pod_name, args.exec_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - connection/lookup failures go back to the caller as-is
        payload.update({"status": "error", "reason_code": "master_pod_unavailable",
                        "error": str(exc)})
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    payload["master_pod"] = pod
    try:
        if not args.container.strip():
            # The master Pod usually carries sidecar containers (such as
            # alluxio-job-master), so exec must name the main container.
            pod_json = json.loads(run_kubectl(
                args.kubectl, args.kube_context, args.namespace,
                ["get", "pod", pod, "-o", "json"], args.exec_timeout,
            ))
            args.container = pick_container(pod_json)
        payload["container"] = args.container

        listing = exec_in_pod(
            args.kubectl, args.kube_context, args.namespace, pod, args.container,
            f"{args.alluxio_bin} fs ls -R {shlex.quote(args.path)}", args.exec_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        payload.update({"status": "error", "reason_code": "list_failed", "error": str(exc)})
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    all_files = parse_ls(listing, cached_only=False)
    candidates = parse_ls(listing, cached_only=True)
    payload["scanned_files"] = len(all_files)
    payload["cached_candidates"] = len(candidates)
    if not candidates:
        payload.update({"status": "ok", "victims": [], "estimated_freed_bytes": 0,
                        "note": "no fully cached file in the scanned range, nothing to evict"})
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    # stat costs a round trip, so cap it at stat-limit following ls order, plus a
    # wall-clock budget so the whole script stays inside the runner's timeout; files
    # without a timestamp rank later in the eviction order.
    stat_deadline = time.monotonic() + max(args.stat_seconds, 0)
    for file in candidates[: max(args.stat_limit, 0)]:
        if time.monotonic() >= stat_deadline:
            break
        try:
            stat_text = exec_in_pod(
                args.kubectl, args.kube_context, args.namespace, pod, args.container,
                f"{args.alluxio_bin} fs stat {shlex.quote(file['path'])}", args.exec_timeout,
            )
            file["mtime"] = parse_stat(stat_text)
        except Exception:  # noqa: BLE001 - a failed stat only affects this file's ranking accuracy
            file["mtime"] = None
    victims = pick_victims(candidates, target_bytes=args.target_bytes, count=args.count)
    payload["victims"] = victims
    payload["estimated_freed_bytes"] = sum(int(v["size_bytes"]) for v in victims)

    if args.dry_run:
        payload["status"] = "ok"
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    submitted = free_paths(
        args.kubectl, args.kube_context, args.namespace, pod, args.container,
        args.alluxio_bin, [str(v["path"]) for v in victims],
        force=args.force, timeout=args.exec_timeout,
    )

    # Re-list after free and verify each file's cached percentage; that is what decides
    # success or failure.
    verify_ok = True
    after: dict[str, int] = {}
    try:
        verify_text = exec_in_pod(
            args.kubectl, args.kube_context, args.namespace, pod, args.container,
            f"{args.alluxio_bin} fs ls -R {shlex.quote(args.path)}", args.exec_timeout,
        )
        after = cache_pct_map(verify_text)
    except Exception:  # noqa: BLE001 - a failed verification can only report "result unknown"
        verify_ok = False
    results = build_results(submitted, after)
    payload["results"] = results
    # Only definite failures go to errors: when free reported an error but verification
    # shows the file was evicted, it was just exit-code noise.
    payload["errors"] = failure_messages(results)
    freed_count = sum(1 for r in results if r["freed"])
    if not verify_ok:
        payload.update({"status": "unknown", "reason_code": "verify_failed",
                        "note": ("free was submitted but the follow-up ls failed; "
                                 "whether it was evicted is unknown")})
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    if victims and freed_count == 0:
        payload.update({"status": "error", "reason_code": "all_free_failed"})
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    payload["status"] = "ok" if freed_count == len(results) else "partial"
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
