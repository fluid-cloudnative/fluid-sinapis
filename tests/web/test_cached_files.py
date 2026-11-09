"""Cached-file listing: ls parsing, entry-time bookkeeping, persistence and the API."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from server.controller.cached_files import CachedFileTracker, parse_listing
from server.controller.web_controller import WebSettings, create_app

# One modern ls line per shape: fully cached, partial, directory, unparseable noise.
LS_SAMPLE = "\n".join([
    "     1671222590       PERSISTED 11-02-2026 08:13:47:285 100% /models/data/big.zip",
    "     3152012255       PERSISTED 11-02-2026 08:21:03:811  48% /models/data/part.zip",
    "      134217728       PERSISTED 10-13-2026 10:16:12:965   0% /models/data/file.bin",
    "         512       PERSISTED 10-31-2026 05:52:30:112  DIR /models/data",
    "stress test line that matches nothing",
])


def test_parse_listing_keeps_files_and_skips_dirs_and_noise():
    listing = parse_listing(LS_SAMPLE)
    assert set(listing) == {"/models/data/big.zip", "/models/data/part.zip",
                            "/models/data/file.bin"}
    assert listing["/models/data/big.zip"] == {"size_bytes": 1671222590, "cache_pct": 100}
    assert listing["/models/data/part.zip"]["cache_pct"] == 48


def _stepping_clock(step: float = 60.0):
    """Monotonic clock advancing one step per call, so successive snapshots pass the TTL."""
    state = {"t": 0.0}

    def tick():
        state["t"] += step
        return state["t"]

    return tick


def _tracker(tmp_path, listings, *, now=None, clock=None, ttl=30.0):
    """Build a tracker over a scripted sequence of listings; the n-th snapshot() call
    (outside the TTL) sees the n-th listing."""
    calls = {"n": 0}

    def scanner():
        index = min(calls["n"], len(listings) - 1)
        calls["n"] += 1
        item = listings[index]
        if isinstance(item, Exception):
            raise item
        return item

    return CachedFileTracker(
        kube_context="test", namespace="ns", dataset="ds",
        state_path=tmp_path / "state.json", ttl_seconds=ttl, scanner=scanner,
        now_ms=(lambda: now[0]) if now else (lambda: 1_700_000_000_000),
        clock=clock or _stepping_clock(),
    )


def test_cached_file_gets_entry_time_and_row_fields(tmp_path):
    listing = {"/models/data/big.zip": {"size_bytes": 1671222590, "cache_pct": 100}}
    tracker = _tracker(tmp_path, [listing])

    body = tracker.snapshot()

    assert body.status == "ok"
    assert len(body.files) == 1
    row = body.files[0]
    assert row.path == "/models/data/big.zip"
    assert row.name == "big.zip"
    assert row.size_bytes == 1671222590
    assert row.cache_pct == 100
    assert row.cached_at_ms == 1_700_000_000_000


def test_any_nonzero_cache_counts_as_cached_and_pct_is_carried(tmp_path):
    listings = [
        {"/models/data/part.zip": {"size_bytes": 1000, "cache_pct": 0}},
        {"/models/data/part.zip": {"size_bytes": 1000, "cache_pct": 48}},
    ]
    now = [1_700_000_000_000]
    tracker = _tracker(tmp_path, listings, now=now, clock=lambda: now[0])

    assert tracker.snapshot().files == []
    now[0] += 10_000
    body = tracker.snapshot()
    assert len(body.files) == 1
    assert body.files[0].cache_pct == 48
    assert body.files[0].cached_at_ms == now[0]


def test_file_leaving_cache_is_forgotten(tmp_path):
    listings = [
        {"/models/data/big.zip": {"size_bytes": 10, "cache_pct": 100}},
        {"/models/data/big.zip": {"size_bytes": 10, "cache_pct": 0}},
    ]
    tracker = _tracker(tmp_path, listings)

    assert len(tracker.snapshot().files) == 1
    assert tracker.snapshot().files == []


def test_rows_are_sorted_by_entry_time_oldest_first(tmp_path):
    # Two listings: a enters the cache first, b joins later and must sort second.
    listings = [
        {"/models/a.zip": {"size_bytes": 1, "cache_pct": 100}},
        {"/models/a.zip": {"size_bytes": 1, "cache_pct": 100},
         "/models/b.zip": {"size_bytes": 2, "cache_pct": 100}},
    ]
    now = [1_700_000_000_000]
    tracker = _tracker(tmp_path, listings, now=now, clock=lambda: now[0])
    tracker.snapshot()

    now[0] += 5_000
    body = tracker.snapshot()

    assert [row.path for row in body.files] == ["/models/a.zip", "/models/b.zip"]
    assert [row.cached_at_ms for row in body.files] == [1_700_000_000_000, 1_700_000_005_000]


def test_state_survives_tracker_restart(tmp_path):
    listing = {"/models/data/big.zip": {"size_bytes": 10, "cache_pct": 100}}
    state_path = tmp_path / "state.json"
    first = CachedFileTracker(
        kube_context="t", namespace="ns", dataset="ds", state_path=state_path,
        ttl_seconds=0.0, scanner=lambda: listing,
    )
    body = first.snapshot()
    assert json.loads(state_path.read_text())["cached_since_ms"] == {
        "/models/data/big.zip": body.files[0].cached_at_ms,
    }

    # A fresh tracker (as after a backend restart) keeps the recorded entry time.
    def fail():
        raise RuntimeError("cluster unreachable")

    second = CachedFileTracker(
        kube_context="t", namespace="ns", dataset="ds", state_path=state_path,
        ttl_seconds=0.0, scanner=fail,
    )
    # Even with the cluster unreachable the loaded state exists; a successful listing
    # would still report the file as cached since the original time.
    assert second._state == {"/models/data/big.zip": body.files[0].cached_at_ms}


def test_scan_failure_is_an_error_payload_and_keeps_state(tmp_path):
    listings = [
        {"/models/data/big.zip": {"size_bytes": 10, "cache_pct": 100}},
        RuntimeError("boom"),
    ]
    tracker = _tracker(tmp_path, listings)

    assert len(tracker.snapshot().files) == 1
    error_body = tracker.snapshot()
    assert error_body.status == "error"
    assert "boom" in error_body.error
    assert error_body.files == []
    # The entry-time map survived the failed scan, so recovery is lossless.
    assert tracker._state == {"/models/data/big.zip": 1_700_000_000_000}


def test_ttl_serves_the_cached_response_without_rescanning(tmp_path):
    calls = {"n": 0}

    def scanner():
        calls["n"] += 1
        return {"/models/a.zip": {"size_bytes": 5, "cache_pct": 100}}

    time_now = [100.0]
    tracker = CachedFileTracker(
        kube_context="t", namespace="ns", dataset="ds",
        state_path=tmp_path / "state.json", ttl_seconds=30.0, scanner=scanner,
        clock=lambda: time_now[0],
    )
    tracker.snapshot()
    tracker.snapshot()
    assert calls["n"] == 1

    time_now[0] += 31.0
    tracker.snapshot()
    assert calls["n"] == 2


def test_corrupt_state_file_is_ignored(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{not json", encoding="utf-8")
    tracker = CachedFileTracker(
        kube_context="t", namespace="ns", dataset="ds", state_path=state_path,
        ttl_seconds=0.0, scanner=lambda: {},
    )
    assert tracker._state == {}


def _app(tracker):
    return TestClient(create_app(WebSettings(), tracker=tracker))


def test_endpoint_returns_cached_file_rows(tmp_path):
    listing = {"/models/data/big.zip": {"size_bytes": 1671222590, "cache_pct": 100}}
    tracker = _tracker(tmp_path, [listing])
    body = _app(tracker).get("/api/cached-files").json()

    assert body["status"] == "ok"
    assert body["scan_path"] == tracker._scan_path
    assert body["files"][0]["name"] == "big.zip"
    assert body["files"][0]["cache_pct"] == 100
    assert body["files"][0]["cached_at_ms"] == 1_700_000_000_000


def test_endpoint_reports_scan_failure(tmp_path):
    tracker = _tracker(tmp_path, [RuntimeError("kubeboom")])
    body = _app(tracker).get("/api/cached-files").json()

    assert body["status"] == "error"
    assert "kubeboom" in body["error"]
    assert body["files"] == []


def test_settings_read_cached_files_options_from_environment():
    settings = WebSettings.from_env({
        "WEB_CACHED_FILES_SCAN_PATH": "/data",
        "WEB_CACHED_FILES_TTL_SECONDS": "5",
        "KUBECTL": "/usr/local/bin/kubectl",
    })
    assert settings.cached_files_scan_path == "/data"
    assert settings.cached_files_ttl_seconds == 5.0
    assert settings.cached_files_kubectl == "/usr/local/bin/kubectl"


@pytest.mark.parametrize("exc", [FileNotFoundError("kubectl missing"),
                                 subprocess.TimeoutExpired("kubectl", 120)])
def test_exec_level_failures_become_error_payloads(exc, tmp_path):
    def fail():
        raise exc

    tracker = CachedFileTracker(
        kube_context="t", namespace="ns", dataset="ds",
        state_path=tmp_path / "state.json", scanner=fail,
    )
    assert tracker.snapshot().status == "error"
