---
name: fluid-cache-preload
description: Create a Fluid DataLoad to preload UFS paths into the distributed cache, with optional --wait for completion.
---

Create a DataLoad CRD to preload UFS paths into the cache (Alluxio etc.). This is an
asynchronous operation: a successfully created DataLoad does not mean the load has
completed; add `--wait` to confirm the result. The DataLoad name is derived from
`--operation-id`; re-running with the same id returns 409.
