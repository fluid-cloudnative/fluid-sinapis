"""Collector contract: produce Metrics only, and never touch the network or the
prometheus package directly."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from server.agent.schemas import CollectorSnapshot, Metric


class Collector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self) -> CollectorSnapshot:
        """Read the data source and return this snapshot. Failures must raise
        RuntimeError; never fabricate a zero."""

    # ---------- two helpers for subclasses, so collectors need not repeat them ---------- #
    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def gauge(name: str, help_: str, value: float,
              labels: dict[str, str] | None = None,
              ts: int | None = None) -> Metric:
        return Metric(name=name, help=help_, type="gauge",
                      value=value, labels=labels or {}, timestamp_ms=ts)


# -- Shared pure function: Kubernetes quantity -> bytes ---------------------- #
def qty_to_bytes(qty) -> int:
    """Kubernetes quantity -> bytes: 100Gi / 512Mi / 2Ti / bare numbers (variants
    such as 8GiB are handled too).

    Gi/Mi/Ki/Ti/Pi are binary (2**n), G/M/K/T/P decimal; a parse failure raises
    ValueError.
    """
    if qty is None:
        return 0
    if isinstance(qty, (int, float)):
        return int(qty)
    s = str(qty).strip().lower()
    if not s:
        return 0
    if s.endswith("ib"):        # hand-written forms such as 1KiB / 8GiB; 1kib -> 1ki
        s = s[:-1]
    elif s.endswith("b"):      # plain-byte suffixes such as 0.00B / 512b
        s = s[:-1]
    units = {"k": 10**3, "ki": 2**10, "m": 10**6, "mi": 2**20,
             "g": 10**9, "gi": 2**30, "t": 10**12, "ti": 2**40,
             "p": 10**15, "pi": 2**50}
    for suf, mult in sorted(units.items(), key=lambda x: -len(x[0])):
        if s.endswith(suf):
            return int(float(s[:-len(suf)]) * mult)
    return int(float(s))