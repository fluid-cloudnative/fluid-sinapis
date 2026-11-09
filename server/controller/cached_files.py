"""Cached-file listing for the web console: which dataset files are in the Alluxio
cache right now, and when each one entered it.

Data source and semantics
-------------------------
- Which / how big: one ``alluxio fs ls -R <scan_path>`` inside the master Pod shows,
  per file, its size and cached percentage. "Cached" here means any partial caching:
  every file whose cached percentage is above 0 is listed (fully cached or not), so
  the table shows the whole cache, not just complete files.
- Cached-at time: Alluxio does not record when a file entered the cache, so this
  module tracks it itself. Every successful listing updates a persisted
  path -> entry-time map: a file newly seen above 0% gets the current time, a file
  that drops back to 0% (or disappears) is forgotten. The map survives backend
  restarts via a small JSON state file; its very first observation stamps
  already-cached files with "now" (they were cached before tracking began, and
  Alluxio offers no older timestamp to recover).

Cluster access goes through kubectl subprocesses (exec/get) rather than the Python
kubernetes package, for the same reason evict_lru.py does: the python client's
websocket exec channel received no data on this repository's environment while
kubectl was reliable. The ls output is mixed with Alluxio stderr noise, so parsing
matches line by line with regexes and ignores anything that does not match.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

RUN_DIR = Path(__file__).resolve().parents[2] / "run"

DEFAULT_SCAN_PATH = "/models"
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_EXEC_TIMEOUT = 120.0
DEFAULT_STATE_PATH = RUN_DIR / "cached-files-state.json"

# ls line (modern Alluxio): size PERSISTED date time cache%|DIR path
LS_LINE_MODERN = re.compile(
    r"^\s*(\d+)\s+(?:NOT_)?PERSISTED\s+\d{1,2}-\d{1,2}-\d{4}\s+[\d:.]+\s+(\d+%|DIR)\s+(.+?)\s*$"
)
# ls line (legacy): permissions owner group size in-memory% persisted% path (the path may
# contain spaces, hence it comes last)
LS_LINE_LEGACY = re.compile(
    r"^([-dlbcps][rwxStT-]{9})\s+\S+\s+\S+\s+(\d+)\s+(\d+)%\s+\d+%\s+(.+?)\s*$"
)


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


def parse_listing(text: str) -> dict[str, dict[str, int]]:
    """Parse a full ``ls -R`` dump into path -> {size_bytes, cache_pct}, files only.

    Everything is kept (including 0% files) so the tracker can also see a file leave
    the cache.
    """
    listing: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        parsed = _parse_ls_line(line)
        if parsed is None:
            continue
        listing[parsed["path"]] = {
            "size_bytes": parsed["size_bytes"], "cache_pct": parsed["cache_pct"],
        }
    return listing


# A file counts as cached once any block of it is in Alluxio.
CACHED_PCT_MIN = 1


# ==============================================================
# Response models
# ==============================================================
class CachedFileEntry(BaseModel):
    """One cached file; ``name`` is the path's last segment for direct display."""

    path: str
    name: str
    size_bytes: int
    cache_pct: int
    cached_at_ms: int


class CachedFilesResponse(BaseModel):
    """The endpoint payload; with status=error, files is empty and error explains why."""

    generated_at_ms: int
    scan_path: str
    status: Literal["ok", "error"]
    error: str | None = None
    files: list[CachedFileEntry] = Field(default_factory=list)


# ==============================================================
# Cluster interaction (kubectl subprocess) -- same shapes evict_lru.py uses
# ==============================================================
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
    """Run a command inside the master Pod container and return the merged output."""
    return run_kubectl(
        kubectl, context, namespace,
        ["exec", pod, "-c", container, "--", "sh", "-c", command], timeout,
    )


def master_pod_selectors(namespace: str, dataset: str) -> list[str]:
    """The common label selectors for the master Pod, in order of likelihood."""
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


def resolve_master_pod(
    kubectl: str, context: str, namespace: str, selectors: list[str], timeout: float
) -> str:
    """Find the master Pod by label, taking the lexicographically smallest name
    across HA replicas."""
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
    raise RuntimeError(f"none of these selectors found a master pod: {tried}")


# ==============================================================
# Tracker: listing + entry-time bookkeeping, TTL cached
# ==============================================================
class CachedFileTracker:
    """Answer "which files are cached" with a TTL cache, and record when each file
    entered the cache in a persisted state file.

    A scan takes seconds (one ``ls -R`` round trip), so requests within the TTL are
    served from the last response; errors are also cached briefly, like MetricsService.
    The injectable ``scanner`` keeps the bookkeeping logic testable without a cluster.
    """

    def __init__(
        self,
        *,
        kube_context: str,
        namespace: str,
        dataset: str,
        scan_path: str = DEFAULT_SCAN_PATH,
        kubectl: str = "kubectl",
        alluxio_bin: str = "alluxio",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        exec_timeout: float = DEFAULT_EXEC_TIMEOUT,
        state_path: str | Path = DEFAULT_STATE_PATH,
        clock: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        scanner: Callable[[], dict[str, dict[str, int]]] | None = None,
    ) -> None:
        self._kube_context = kube_context
        self._namespace = namespace
        self._dataset = dataset
        self._scan_path = scan_path
        self._kubectl = kubectl
        self._alluxio_bin = alluxio_bin
        self._ttl = max(float(ttl_seconds), 0.0)
        self._exec_timeout = exec_timeout
        self._state_path = Path(state_path)
        self._clock = clock
        self._now_ms = now_ms
        self._scanner = scanner or self._scan_cluster
        self._lock = threading.Lock()
        self._state: dict[str, int] = self._load_state()
        self._cached: CachedFilesResponse | None = None
        self._cache_until = 0.0

    # -- public ----------------------------------------------- #
    def snapshot(self) -> CachedFilesResponse:
        now = self._clock()
        with self._lock:
            hit = self._cached
            if hit is not None and self._cache_until > now:
                return hit
        # Scan outside the lock; a concurrent miss just scans twice. A failed scan is
        # cached briefly (like MetricsService caches section errors) so a recovering
        # cluster shows up quickly.
        try:
            listing = self._scanner()
            response, ttl = self._update_state(listing), self._ttl
        except Exception as exc:  # noqa: BLE001 - any scan failure becomes an error payload
            logger.warning("Scanning cached files failed: %s", exc, exc_info=exc)
            response, ttl = self.error_response(exc), min(self._ttl, 3.0)
        with self._lock:
            self._cached = response
            self._cache_until = self._clock() + ttl
        return response

    # -- scanning --------------------------------------------- #
    def _scan_cluster(self) -> dict[str, dict[str, int]]:
        """One real cluster round trip: resolve the master Pod, exec ``ls -R``, parse."""
        pod = resolve_master_pod(
            self._kubectl, self._kube_context, self._namespace,
            master_pod_selectors(self._namespace, self._dataset), self._exec_timeout,
        )
        pod_json = json.loads(run_kubectl(
            self._kubectl, self._kube_context, self._namespace,
            ["get", "pod", pod, "-o", "json"], self._exec_timeout,
        ))
        container = pick_container(pod_json)
        listing_text = exec_in_pod(
            self._kubectl, self._kube_context, self._namespace, pod, container,
            f"{self._alluxio_bin} fs ls -R {shlex.quote(self._scan_path)}",
            self._exec_timeout,
        )
        return parse_listing(listing_text)

    # -- state bookkeeping ------------------------------------ #
    def _update_state(self, listing: dict[str, dict[str, int]]) -> CachedFilesResponse:
        """Fold one successful listing into the entry-time map and build the rows."""
        now = self._now_ms()
        for path, info in listing.items():
            if info["cache_pct"] >= CACHED_PCT_MIN:
                self._state.setdefault(path, now)
            else:
                self._state.pop(path, None)
        for path in list(self._state):
            if path not in listing:
                # Gone from the namespace listing entirely: no longer cacheable state.
                del self._state[path]
        self._persist()
        rows = [
            CachedFileEntry(
                path=path,
                name=path.rsplit("/", 1)[-1] or path,
                size_bytes=listing[path]["size_bytes"],
                cache_pct=listing[path]["cache_pct"],
                cached_at_ms=cached_at,
            )
            for path, cached_at in sorted(self._state.items(), key=lambda kv: (kv[1], kv[0]))
        ]
        return CachedFilesResponse(
            generated_at_ms=now,
            scan_path=self._scan_path,
            status="ok",
            files=rows,
        )

    def error_response(self, exc: Exception) -> CachedFilesResponse:
        """Build the error payload; the state file is intentionally left untouched,
        because a failed listing says nothing about what is still cached."""
        message = str(exc).strip() or exc.__class__.__name__
        return CachedFilesResponse(
            generated_at_ms=self._now_ms(),
            scan_path=self._scan_path,
            status="error",
            error=message,
            files=[],
        )

    # -- persistence ------------------------------------------ #
    def _load_state(self) -> dict[str, int]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            state = raw.get("cached_since_ms", {})
            return {str(path): int(value) for path, value in state.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # A corrupt state file only loses the entry-time history, never correctness.
            logger.warning("Ignoring unreadable cached-files state %s: %s",
                           self._state_path, exc)
            return {}

    def _persist(self) -> None:
        """Atomic write; a failed write degrades to losing entry times on restart."""
        payload = json.dumps({"cached_since_ms": self._state}, ensure_ascii=False)
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.warning("Persisting cached-files state to %s failed: %s",
                           self._state_path, exc)
