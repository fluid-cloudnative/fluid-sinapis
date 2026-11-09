---
name: example-custom-strategy
description: Template example: demonstrates reading custom metrics and emitting a cache-operation verdict (copy this directory and adapt).
---

# <Your Strategy Name>

## Strategy semantics (read by the planner / validator)

- Trigger: state in one sentence "under which conditions which operation should run";
- Output operation: one of `expand` / `preload` / `shrink` / `evict`, with parameters
  following the executable action catalogue conventions
  (e.g. expand → `{"desired_replicas": N}`, preload → `{"paths": [...]}`);
- Conflict rule: when this strategy disagrees with a builtin strategy on the same
  operation, this strategy prevails;
- The default template outputs no operation (action=null) and only demonstrates how to
  read metrics.

## Script contract (the framework runs scripts/decide.py every round)

- stdin: `{"observations": [{"tool": "...", "result": {...}}], "context": {...}}`, where
  observations carry the latest snapshot of every metric source (including the custom
  metrics from the `user_system` section of config.yml);
- stdout: one JSON line `{"action": ..., "parameters": {...}, "reason": "..."}`.

For parameters, threshold examples, and the full description see [README.md](README.md)
in this directory (not sent to the model).
