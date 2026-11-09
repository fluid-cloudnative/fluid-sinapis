#!/usr/bin/env python3
"""Generate the six big_model_*.zip fixtures, at the sizes the cache scenarios assume.

The sizes below are the real HDFS sizes of the files in the nju-model-hdfs dataset
(hdfs dfs -ls, 2026-10-31) and are the same numbers examples/user_system_simulator.py
pushes as size_bytes:

  big_model_104.zip  3152012255  (2.94GiB)
  big_model_105.zip  2643485774  (2.46GiB)
  big_model_106.zip  2830948396  (2.64GiB)
  big_model_107.zip  2841337723  (2.65GiB)
  big_model_108.zip  1985894795  (1.85GiB)
  big_model_109.zip  1671222590  (1.56GiB)

Every capacity calculation in this repository is calibrated against them: one worker
holds 5GiB, so 108+109 (3.41GiB) fit while adding 106+107 (8.69GiB) does not, which is
exactly what drives the preload and expand decisions. A fixture that is a few hundred
MiB off silently changes which plan the agent produces, so each file is size-checked
after writing and the run exits non-zero on a mismatch.

Run it and the six files appear in the current directory:

  python examples/data_generate.py

Two notes on what comes out:

  - Only the byte size matters: the agent reads sizes from the listing and never opens
    the archives. The content is filler (unwritten reads as zeros), these are not real
    model weights, and nothing ever unzips them.
  - The length is set with truncate, so the call returns at once and the filesystem
    decides whether the range is really allocated (ext4/xfs stay sparse, NTFS usually
    allocates). Fine for a local listing; anything that hashes or compresses the bytes
    still sees the full size.

Existing files are left untouched.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Real sizes from the dataset UFS (hdfs dfs -ls, 2026-10-31); keep in sync with
# BIG_FILES in examples/user_system_simulator.py.
FILES: dict[str, int] = {
    "big_model_104.zip": 3152012255,
    "big_model_105.zip": 2643485774,
    "big_model_106.zip": 2830948396,
    "big_model_107.zip": 2841337723,
    "big_model_108.zip": 1985894795,
    "big_model_109.zip": 1671222590,
}


def human(size: int) -> str:
    """Size in GiB, the unit the capacity policies are written in."""
    return f"{size / 1024**3:.2f}GiB"


def write_file(path: Path, size: int) -> float:
    """Set the file's length to size and return the elapsed seconds."""
    started = time.monotonic()
    with open(path, "wb") as handle:
        handle.truncate(size)
    return time.monotonic() - started


def main() -> int:
    directory = Path.cwd()
    print(f"dir={directory}, {len(FILES)} file(s), "
          f"{sum(FILES.values())} bytes ({human(sum(FILES.values()))})")

    failures: list[str] = []
    for index, (name, size) in enumerate(sorted(FILES.items()), start=1):
        path = directory / name
        if path.exists():
            print(f"[{index}/{len(FILES)}] {name}: exists, skipped")
            actual = path.stat().st_size
        else:
            try:
                elapsed = write_file(path, size)
            except OSError as exc:
                print(f"[{index}/{len(FILES)}] {name}: failed: {exc}", file=sys.stderr)
                failures.append(f"{name}: {exc}")
                continue
            actual = path.stat().st_size
            print(f"[{index}/{len(FILES)}] {name}: {size} bytes ({human(size)}) "
                  f"in {elapsed:.2f}s -> {'ok' if actual == size else 'MISMATCH'}")
        if actual != size:
            failures.append(f"{name}: {actual} bytes, expected {size}")

    print(f"done: {len(FILES) - len(failures)}/{len(FILES)} fixture(s) at the expected size")
    if failures:
        for failure in failures:
            print(f"  failed: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
