---
name: evict-cache
description: When replicas are at the ceiling and used cache capacity is about 70%, so expansion is no longer possible, free cold cached data.
---

## When it applies

Both required:

- fluid_workers_desired has reached `context.policies.evict.max_workers` (default 2);
  fluid_workers_desired (spec.replicas) is the replica baseline, and fluid_workers is only
  the ready count, which still lags while replicas start up;
- usage_ratio = used_bytes / capacity_bytes is above `usage_ratio_above` (default 0.70).

Below the ceiling a full cache is answered by expand-cache, not by eviction; while the hot
set is still expanding across windows, expand instead of evicting.

## Rules

- Remove 3 files per cleanup pass; if fewer than 3 remain, remove them all.
- At most one evict per round: {"action":"evict","parameters":{"path":"/models","target_bytes":N}};

## Interaction

At most one evict per round: if shrink-cache already planned one, do not emit a second.
