#!/usr/bin/env python3
"""Clear user-system metrics from the Pushgateway; Fluid and Kubernetes metrics are kept.

Every metric write in this system goes through the Pushgateway
(prometheus.pushgateway_url, default http://localhost:9091) under jobs named
"<job_prefix>:<collector>":

  fluid_agent:fluid                Fluid collector           -> kept
  fluid_agent:k8s                  Kubernetes collector      -> kept
  fluid_agent:user_system          directly pushed gauges    -> deleted
  fluid_agent:user_system_events   event-derived metrics     -> deleted

Jobs are discovered live from the gateway's /metrics exposition, so this works
whatever job_prefix the pushers use: a job is deleted when its collector part
(the suffix after the last ':') is user_system or user_system_events, and
everything else is left untouched.

Deletion happens per **grouping key**: the Pushgateway DELETE identifies one
group, and `DELETE /metrics/job/<job>` only removes the group whose labels are
exactly job + instance -- a job whose pushes carry extra grouping labels (the
user system groups by `source`) is NOT touched by it (the API still answers
202, because DELETE is idempotent). So the script first enumerates every group
of each user job, then deletes each group by its full label path
(/metrics/job/<job>/<label>/<value>/...).

The group list is parsed from the exposition's `push_time_seconds` /
`push_failure_time_seconds` lines, whose labels are exactly the group's
grouping key. Regular sample lines must NOT be used for this: they merge the
grouping labels with the metrics' own body labels (e.g. `file=...` inside the
user_system group), which would fabricate grouping keys that do not exist.

Dry-run by default; pass --yes to actually delete. Two notes on what "deleted"
means:
  - Prometheus scrapes the Pushgateway, so series scraped before the deletion
    disappear from query_url on their own within about one scrape interval
    (stale-marker mechanism); no Prometheus-side action is needed.
  - Deletion does not stop re-accumulation: while the backend and a user pusher
    keep running, the jobs refill on the next push.

Usage:
  python examples/clear_prometheus.py           # show what would be deleted
  python examples/clear_prometheus.py --yes     # delete
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# Collector parts that belong to the user system; everything else (fluid, k8s, ...)
# is never touched.
USER_COLLECTORS = ("user_system", "user_system_events")

_JOB_LABEL = re.compile(r'\bjob="([^"]*)"')
_LABEL_ITEM = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _unescape(value: str) -> str:
    """Undo the exposition format escaping of a quoted label value."""
    return (
        value.replace("\\\\", "\x00").replace('\\"', '"')
        .replace("\\n", "\n").replace("\x00", "\\")
    )


def fetch_groups(base: str, timeout: float) -> dict[str, set[tuple[tuple[str, str], ...]]]:
    """Map every job on the gateway to its grouping-label combinations.

    Only the per-group bookkeeping lines (push_time_seconds / push_failure_time_seconds)
    are parsed: the Pushgateway adds them to every group, and their labels are exactly
    the group's grouping key. Sample lines carry the metrics' own body labels next to
    the grouping ones, so they would fabricate grouping keys that do not exist.

    Each combination is a sorted tuple of (label, value) pairs covering every
    label that shapes a Pushgateway group: everything except job, plus instance
    when it is not the implicit empty string.
    """
    with urllib.request.urlopen(f"{base}/metrics", timeout=timeout) as response:
        body = response.read().decode("utf-8")
    jobs: dict[str, set[tuple[tuple[str, str], ...]]] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, brace, rest = line.partition("{")
        if name.strip() not in ("push_time_seconds", "push_failure_time_seconds"):
            continue
        end = rest.rfind("}")
        if end < 0:
            continue
        labels = {
            key: _unescape(value)
            for key, value in _LABEL_ITEM.findall(rest[:end])
        }
        job = labels.get("job")
        if job is None:
            continue
        combo = tuple(sorted(
            (key, value)
            for key, value in labels.items()
            if key not in ("job", "instance") and value != ""
        ))
        jobs.setdefault(job, set()).add(combo)
    return jobs


def delete_group(base: str, job: str, combo: tuple[tuple[str, str], ...],
                 timeout: float) -> None:
    """Delete one group: the job plus every non-empty grouping label in the path."""
    path = f"/metrics/job/{urllib.parse.quote(job, safe='')}"
    for key, value in combo:
        path += f"/{urllib.parse.quote(key, safe='')}/{urllib.parse.quote(value, safe='')}"
    request = urllib.request.Request(f"{base}{path}", method="DELETE")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete user-system metrics from the Pushgateway, keeping Fluid and k8s metrics"
    )
    parser.add_argument("--gateway", default="http://localhost:9091",
                        help="Pushgateway URL; must match prometheus.pushgateway_url "
                             "in server/config.yml (default http://localhost:9091)")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--yes", action="store_true",
                        help="actually delete; without it only show what would be deleted")
    args = parser.parse_args()
    base = args.gateway.rstrip("/")

    def is_user_job(job: str) -> bool:
        return job.rsplit(":", 1)[-1] in USER_COLLECTORS

    try:
        groups = fetch_groups(base, args.timeout)
    except (urllib.error.URLError, OSError) as exc:
        # URLError covers HTTPError and connection-level failures (refused, timeout).
        print(f"failed to read {base}/metrics: {exc}")
        return 1

    user_jobs = sorted(job for job in groups if is_user_job(job))
    kept_jobs = sorted(job for job in groups if not is_user_job(job))

    print(f"pushgateway: {base}")
    if not groups:
        print("nothing stored on this gateway; nothing to do")
        return 0

    print(f"\nto delete ({len(user_jobs)} user-system job"
          f"{'' if len(user_jobs) == 1 else 's'}):")
    for job in user_jobs:
        print(f"  {job}: {len(groups[job])} grouping key(s)")
        for combo in sorted(groups[job]):
            labels = ", ".join(f"{key}={value!r}" for key, value in combo) or "(none)"
            print(f"    - {labels}")
    print("\nkept (fluid / k8s / other):")
    for job in kept_jobs:
        print(f"  {job}  ({len(groups[job])} grouping key"
              f"{'' if len(groups[job]) == 1 else 's'})")

    total = sum(len(groups[job]) for job in user_jobs)
    if not total:
        print("\nno user-system metrics found; nothing to delete")
        return 0
    if not args.yes:
        print(f"\ndry run: {total} grouping key(s) would be deleted, "
              "rerun with --yes to delete")
        return 0

    print()
    failures = 0
    for job in user_jobs:
        for combo in sorted(groups[job]):
            labels = ",".join(f"{key}={value}" for key, value in combo) or "(no labels)"
            try:
                delete_group(base, job, combo, args.timeout)
                print(f"deleted: {job} [{labels}]")
            except (urllib.error.URLError, OSError) as exc:
                print(f"delete failed for {job} [{labels}]: {exc}")
                failures += 1

    leftover = {
        job: combo
        for job, combos in fetch_groups(base, args.timeout).items()
        if is_user_job(job) for combo in [combos]
    }
    if leftover:
        for job in leftover:
            print(f"still present after deletion: {job} "
                  f"({len(leftover[job])} grouping key(s))")
        failures += 1
    if failures:
        print(f"\ndone with {failures} failure(s)")
        return 1
    print("\ndone: all user-system metrics deleted; Fluid/k8s metrics untouched. "
          "query_url stops returning them within about one scrape interval.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
