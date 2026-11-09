---
name: validate-cache-plan
description: Check a decision item by item — structure, parameters, file provenance, counts and order, and cross-snapshot consistency; reject on any failed applicable check.
---

# Validate the Fluid cache plan

Review only; never rewrite, execute, or replan. Judge from request/context/decision and
the observations (two snapshots). When user custom strategies and builtin strategies
disagree on the same operation, verify that the decision followed the user strategy.

## Checklist (any failed applicable item → approved=false, name the specific gap)

1. status ∈ {action, noop, insufficient_data}, reason non-empty; action requires steps,
   the others require steps=[].
2. Each step's parameters follow the matching strategy's conventions:
   preload = deduplicated path list of at most min(max_files, 3) entries, all from real
   observations; expand = integer desired_replicas;
   evict = path plus exactly one of target_bytes/count;
   reject parameters that cannot be verified or contain undefined fields.
3. Every step has strategy support and observational evidence; "no strategy violated" is
   not the same as "supported by strategy". At most 1 expand and 1 preload per plan; when
   both are present, expand comes first.
4. noop/insufficient_data require evidence; when the two observation snapshots disagree,
   the fresh one wins and the divergence must be called out.
