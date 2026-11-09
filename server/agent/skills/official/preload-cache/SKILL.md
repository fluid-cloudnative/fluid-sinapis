---
name: preload-cache
description: Select the highest-benefit file set by access hotness and remaining capacity, and output a preload plan.
---

## Rules

- Read `context.policies.preload`: max_files defaults to 3 (hard cap), min_file_size_bytes
  defaults to 1024;
- Pick the highest-benefit combination by access frequency and file size; filling the quota
  is not required; the selected total must fit the budget;
- Consider expand-cache only when sustained demand exceeds capacity; never propose a preload
  that depends on capacity not yet added;
- At most one preload per round: {"action":"preload","parameters":{"paths":["/dataset/path"]}};
- Paths must come from real observations.
