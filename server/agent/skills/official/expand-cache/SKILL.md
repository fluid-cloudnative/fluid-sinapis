---
name: expand-cache
description: Expand when used cache capacity reaches about 70% or the remaining capacity can no longer hold the current hot working set, up to the worker ceiling.
---

## Rules

- Expand on either trigger: (a) used_ratio reaches about 70%, or (b) the remaining
  capacity cannot hold the current hot working set — the total size of the actively
  accessed files exceeds `capacity_bytes - used_bytes`. Judge on the working set, not on
  the ratio alone: a cache can sit below 70% and still be unable to admit the files in
  demand. Usage only moves when this agent acts, so a ratio stuck just under the
  threshold never crosses it by itself — waiting for 70% in that state never expands;

- Skip marginal expansions; prefer preload for occasional small files when space is ample;
- Read `context.policies.expand`: replica_increment defaults to 1; max_workers (default 2) is
  a hard ceiling on the replica count and the same value as
  `context.policies.evict.max_workers` — the two must stay equal;
- At most one expand per round: {"action":"expand","parameters":{"desired_replicas":N}} with
  N the target total (insufficient_data when unknown); expand comes before preload; no
  preloads depending on the new capacity in this round.
