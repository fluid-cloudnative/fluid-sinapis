---
name: fluid-cache-scale-up
description: Patch the Fluid Runtime's spec.replicas to add workers and expand cache capacity, with an optimistic lock and optional --wait for readiness.
---

Set the Runtime's spec.replicas to the target value (default +1) to add workers. The patch
carries a resourceVersion optimistic lock: concurrent modification returns 409, and the
caller should re-read and recompute. A successful patch does not mean the workers are
Ready (use `--wait` to poll readiness); a target below the current count is refused by
default — shrinking requires the explicit `--allow-shrink` flag (risk of data loss).
