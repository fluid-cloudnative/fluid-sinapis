---
name: shrink-cache
description: When the desired worker count (fluid_workers_desired) is not 1 and used cache capacity is below 35%, shrink the cache capacity.
---

## Rules

- Make the decision based solely on Fluid system metrics and Kubernetes cluster metrics.
- Remove at most 1 worker per round, never below min_workers:
  {"action":"shrink","parameters":{"desired_replicas":N}} with N below fluid_workers_desired,
  the known desired replica count; fluid_workers is only the ready count, so read the
  replica baseline from fluid_workers_desired;
- Never shrink in the same round as an expand: if expand-cache planned one, plan nothing
  here;
- Do not shrink while a replica adjustment is still in progress (fluid_workers below
  fluid_workers_desired, i.e. new replicas are not Ready yet);

## Interaction

At most one evict per round: if evict-cache already planned one, do not emit a second —
shrink alone when its own conditions hold, otherwise plan nothing. High usage at the
ceiling belongs to evict-cache; this strategy shrinks only once usage is genuinely low.
