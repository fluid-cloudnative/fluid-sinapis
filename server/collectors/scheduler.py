"""Periodic collection and push scheduler. Collection is decoupled from writing."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from server.prometheus.prometheus_client import (
    PrometheusClient, SnapshotStore,
)
from server.agent.schemas import CollectorSnapshot

logger = logging.getLogger(__name__)


class CollectorScheduler:
    """One round every interval_seconds: collect -> store snapshot -> write.

    A failed write neither blocks the next round nor back-fills zeros; it surfaces as a
    RuntimeError from PrometheusClient.
    """

    def __init__(self, client: PrometheusClient, store: SnapshotStore,
                 *, interval_seconds: float = 10) -> None:
        self._client = client
        self._store = store
        self._interval = interval_seconds
        self._jobs: list[tuple[str, Callable[[], CollectorSnapshot]]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, name: str,
                 collect_once: Callable[[], CollectorSnapshot]) -> "CollectorScheduler":
        if not name.strip():
            raise ValueError("collector name must not be empty")
        self._jobs.append((name, collect_once))
        return self

    def start(self, daemon: bool = True) -> None:
        if self._thread:
            raise RuntimeError("the scheduler is already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=daemon, name="collector-scheduler")
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._client.close()

    def _loop(self) -> None:
        # Collect immediately on start, then sleep between rounds: the first round must
        # not wait a full interval before any data exists.
        while not self._stop.is_set():
            for name, collect_once in self._jobs:
                try:
                    snap = collect_once()
                    self._store.update(snap)        # 1. cache first, so local reads are immediate
                    self._client.write(snap)        # 2. then push to Prometheus
                except Exception as exc:  # noqa: BLE001
                    # One failing collector is not a full stop; strategies see the previous
                    # round's snapshot.
                    logger.error("Collector %s failed, skipping this round", name, exc_info=exc)
            if self._stop.wait(timeout=self._interval):
                break